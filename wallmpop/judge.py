"""LLM steps: derive/edit/tune an alert (Sonnet), judge listings in batch (Haiku), deep look (Sonnet)."""
from __future__ import annotations

import json
from typing import Any

from .llm import HAIKU, SONNET, Claude, LLMError, extract_json, prompt
from .models import AlertConfig, Listing

BATCH = 15
DESC_CHARS = 400


async def derive_alert(claude: Claude, text: str, default_lat: float, default_lon: float) -> AlertConfig:
    res = await claude.run(SONNET, prompt("derive_alert"), text)
    return AlertConfig.from_dict(extract_json(res.text), default_lat, default_lon)


async def edit_alert(claude: Claude, cfg: AlertConfig, change: str, alert_id: int) -> AlertConfig:
    user = f"Configuración actual:\n{json.dumps(cfg.to_dict(), ensure_ascii=False)}\n\nCambio pedido:\n{change}"
    res = await claude.run(SONNET, prompt("edit_alert"), user, alert_id)
    return AlertConfig.from_dict(extract_json(res.text), cfg.lat, cfg.lon)


async def tune_alert(claude: Claude, cfg: AlertConfig, examples: list[dict[str, Any]], alert_id: int) -> AlertConfig:
    lines = [f"{'👍' if e['vote'] > 0 else '👎'} [{e['verdict']}: {e['why']}] {e['title']} — {e['price']}€ — "
             f"{(e['description'] or '')[:300]}" for e in examples]
    user = (f"Configuración actual:\n{json.dumps(cfg.to_dict(), ensure_ascii=False)}\n\nEjemplos:\n"
            + "\n".join(lines))
    res = await claude.run(SONNET, prompt("tune_alert"), user, alert_id)
    return AlertConfig.from_dict(extract_json(res.text), cfg.lat, cfg.lon)


def _compact(it: Listing) -> dict[str, Any]:
    desc = " ".join(it.description.split())[:DESC_CHARS]
    return {"id": it.id, "t": it.title, "p": it.price, "d": desc}


def parse_verdicts(data: Any, ids: set[str]) -> dict[str, dict[str, Any]]:
    if isinstance(data, dict):
        data = data.get("items") or data.get("results") or [data]
    out: dict[str, dict[str, Any]] = {}
    for x in data if isinstance(data, list) else []:
        if not isinstance(x, dict) or str(x.get("id")) not in ids:
            continue
        v = {"m": "match", "y": "maybe", "n": "reject"}.get(str(x.get("v", "n")).lower()[:1], "reject")
        try:
            s = float(x.get("s") or 0)
        except (TypeError, ValueError):
            s = 0.0
        out[str(x["id"])] = {"verdict": v, "score": s, "why": str(x.get("why") or "")[:120]}
    return out


async def judge_batch(claude: Claude, cfg: AlertConfig, items: list[Listing], alert_id: int) -> dict[str, dict]:
    """Returns {id: {verdict, score, why}}. Items missing from the answer are left out (judged again later)."""
    system = prompt("judge").replace("{rubric}", cfg.rubric)
    out: dict[str, dict] = {}
    for i in range(0, len(items), BATCH):
        chunk = items[i:i + BATCH]
        user = json.dumps([_compact(x) for x in chunk], ensure_ascii=False)
        res = await claude.run(HAIKU, system, user, alert_id)
        try:
            out.update(parse_verdicts(extract_json(res.text), {x.id for x in chunk}))
        except LLMError:
            continue
    return out


async def deep_look(claude: Claude, cfg: AlertConfig, it: dict[str, Any], alert_id: int) -> str:
    user = (f"Criterios:\n{cfg.rubric}\n\nAnuncio: {it['title']} — {it['price']}€\n{it['description']}\n\n"
            f"Historial de precio: {it['price_history']}")
    res = await claude.run(SONNET, prompt("deep"), user, alert_id)
    return res.text.strip()
