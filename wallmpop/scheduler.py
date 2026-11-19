"""Periodic watch loop: fetch → dedup → prefilter → Haiku in batch → notify."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import date
from typing import Protocol

from .db import DB
from .health import SourceHealth
from .judge import BATCH, judge_batch
from .llm import Claude, LLMError
from .models import AlertConfig, Listing
from .prefilter import prefilter
from .sources import Source, SourceError

log = logging.getLogger(__name__)
NOTIFY = ("match", "maybe")


class Notifier(Protocol):
    async def listing(self, alert_id: int, cfg: AlertConfig, it: Listing, verdict: str, score: float,
                      why: str, old_price: float | None = None) -> None: ...

    async def text(self, text: str) -> None: ...


class Scheduler:
    def __init__(self, db: DB, source: Source, claude: Claude, notifier: Notifier,
                 default_interval: int = 15, global_max_llm: int = 300, tick: int = 30):
        self.db, self.source, self.claude, self.out = db, source, claude, notifier
        self.default_interval, self.global_max_llm, self.tick = default_interval, global_max_llm, tick
        self.health = SourceHealth(source.name)
        self.next_run: dict[int, float] = {}
        self.cap_warned: set[tuple[str, int]] = set()

    def interval(self, cfg: AlertConfig) -> int:
        return max(5, cfg.interval_min or self.default_interval)

    def run_soon(self, alert_id: int) -> None:
        self.next_run[alert_id] = 0

    async def loop(self) -> None:
        while True:
            try:
                await self.tick_once()
            except Exception:  # never let the loop die
                log.exception("tick failed")
            await asyncio.sleep(self.tick)

    async def tick_once(self) -> None:
        now = time.time()
        ok_runs, total = 0, 0
        for row in self.db.alerts(include_paused=False):
            cfg = AlertConfig.from_dict(json.loads(row["config"]))
            due = self.next_run.get(row["id"], row["last_run"] + self.interval(cfg) * 60)
            if now < due:
                continue
            self.next_run[row["id"]] = now + self.interval(cfg) * 60 * random.uniform(0.9, 1.1)
            n = await self.run_alert(row["id"], cfg)
            if n is not None:
                ok_runs, total = ok_runs + 1, total + n
            await asyncio.sleep(random.uniform(2, 5))
        if ok_runs:
            msg = self.health.ok(total)
            if msg:
                await self.out.text(msg)

    async def run_alert(self, alert_id: int, cfg: AlertConfig) -> int | None:
        """Returns the number of listings fetched, or None if the source failed."""
        try:
            items = await self.source.search(cfg)
        except SourceError as e:
            log.warning("alert %s: %s", alert_id, e)
            self.db.mark_run(alert_id, str(e))
            msg = self.health.fail(e.kind, e.detail)
            if msg:
                await self.out.text(msg)
            return None
        try:
            await self.process(alert_id, cfg, items)
            self.db.mark_run(alert_id, None)
        except LLMError as e:
            log.warning("alert %s llm: %s", alert_id, e)
            self.db.mark_run(alert_id, f"llm: {e}")
        return len(items)

    async def process(self, alert_id: int, cfg: AlertConfig, items: list[Listing]) -> None:
        muted = self.db.muted_sellers()
        fresh: list[Listing] = []
        for it in items:
            row = self.db.listing(alert_id, it.id)
            if row:
                if it.price < (row["price"] or 0) - 0.01:
                    self.db.update_price(alert_id, it.id, it.price)
                    if row["verdict"] in NOTIFY and it.seller_id not in muted:
                        await self.out.listing(alert_id, cfg, it, row["verdict"], row["score"] or 0,
                                               row["why"] or "", old_price=row["price"])
                else:
                    self.db.touch(alert_id, it.id)
                continue
            rep = self.db.similar_seen(alert_id, it)
            if rep:  # reposted to bump it: inherit the verdict, warn only if cheaper
                self.db.save_listing(alert_id, it, rep["verdict"], rep["score"], rep["why"] or "")
                if rep["verdict"] in NOTIFY and it.price < (rep["price"] or 0) - 0.01 and it.seller_id not in muted:
                    await self.out.listing(alert_id, cfg, it, rep["verdict"], rep["score"] or 0,
                                           rep["why"] or "", old_price=rep["price"])
                continue
            reason = prefilter(cfg, it, muted)
            if reason:
                self.db.save_listing(alert_id, it, "pre", why=reason)
                continue
            fresh.append(it)
        if not fresh:
            return

        budget = min(cfg.max_llm_per_day - self.db.calls_today(alert_id),
                     self.global_max_llm - self.db.calls_today())
        if budget <= 0:
            key = (date.today().isoformat(), alert_id)
            if key not in self.cap_warned:
                self.cap_warned.add(key)
                await self.out.text(f"🛑 «{cfg.name}» ha llegado al tope de llamadas de hoy; "
                                    f"{len(fresh)} anuncios esperan a mañana (o sube max_llm_per_day con /editar).")
            return  # not saved: they'll be judged when there's budget, if still listed
        fresh = fresh[:budget * BATCH]
        verdicts = await judge_batch(self.claude, cfg, fresh, alert_id)
        hits = []
        for it in fresh:
            v = verdicts.get(it.id)
            if not v:
                continue  # model skipped it: retry next run
            self.db.save_listing(alert_id, it, v["verdict"], v["score"], v["why"])
            if v["verdict"] in NOTIFY:
                hits.append((it, v))
        hits.sort(key=lambda h: (h[1]["verdict"] != "match", -h[1]["score"]))
        for it, v in hits:
            await self.out.listing(alert_id, cfg, it, v["verdict"], v["score"], v["why"])

