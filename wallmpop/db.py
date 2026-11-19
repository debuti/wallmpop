"""SQLite storage."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Any

from .models import AlertConfig, Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  config TEXT NOT NULL,
  paused INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  last_run INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS listings (
  alert_id INTEGER NOT NULL,
  item_id TEXT NOT NULL,
  title TEXT, price REAL, seller_id TEXT, url TEXT, description TEXT,
  verdict TEXT, score REAL, why TEXT,
  price_history TEXT NOT NULL DEFAULT '[]',
  first_seen INTEGER, last_seen INTEGER,
  PRIMARY KEY (alert_id, item_id)
);
CREATE TABLE IF NOT EXISTS feedback (
  alert_id INTEGER, item_id TEXT, vote INTEGER, ts INTEGER,
  PRIMARY KEY (alert_id, item_id)
);
CREATE TABLE IF NOT EXISTS mutes (user_id INTEGER, seller_id TEXT, ts INTEGER, PRIMARY KEY (user_id, seller_id));
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY, name TEXT, username TEXT,
  status TEXT NOT NULL,  -- pending | allowed | denied
  ts INTEGER
);
CREATE TABLE IF NOT EXISTS usage (
  day TEXT, model TEXT, alert_id INTEGER, calls INTEGER, input_tokens INTEGER,
  output_tokens INTEGER, cost_usd REAL,
  PRIMARY KEY (day, model, alert_id)
);
"""


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)
        cols = {r[1] for r in self.c.execute("PRAGMA table_info(alerts)")}
        if "owner" not in cols:
            self.c.execute("ALTER TABLE alerts ADD COLUMN owner INTEGER")
        self.c.commit()

    def claim_unowned(self, owner: int) -> None:
        """Alerts from before multi-user belong to the first admin."""
        self.c.execute("UPDATE alerts SET owner=? WHERE owner IS NULL", (owner,))
        self.c.commit()

    # alerts
    def add_alert(self, cfg: AlertConfig, owner: int) -> int:
        cur = self.c.execute("INSERT INTO alerts(config, created_at, owner) VALUES (?, ?, ?)",
                             (json.dumps(cfg.to_dict(), ensure_ascii=False), int(time.time()), owner))
        self.c.commit()
        return cur.lastrowid

    def update_alert(self, alert_id: int, cfg: AlertConfig) -> None:
        self.c.execute("UPDATE alerts SET config=? WHERE id=?",
                       (json.dumps(cfg.to_dict(), ensure_ascii=False), alert_id))
        self.c.commit()

    def alerts(self, include_paused: bool = True, owner: int | None = None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM alerts WHERE 1=1", []
        if not include_paused:
            q += " AND paused=0"
        if owner is not None:
            q += " AND owner=?"; args.append(owner)
        return self.c.execute(q + " ORDER BY id", args).fetchall()

    def alert(self, alert_id: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()

    def find_alert(self, ref: str, owner: int | None = None) -> sqlite3.Row | None:
        ref = ref.strip()
        if ref.isdigit():
            row = self.alert(int(ref))
            return row if row and (owner is None or row["owner"] == owner) else None
        for row in self.alerts(owner=owner):
            if json.loads(row["config"])["name"].lower() == ref.lower():
                return row
        return None

    def set_paused(self, alert_id: int, paused: bool) -> None:
        self.c.execute("UPDATE alerts SET paused=? WHERE id=?", (int(paused), alert_id))
        self.c.commit()

    def delete_alert(self, alert_id: int) -> None:
        for t in ("alerts WHERE id", "listings WHERE alert_id", "feedback WHERE alert_id"):
            self.c.execute(f"DELETE FROM {t}=?", (alert_id,))
        self.c.commit()

    def mark_run(self, alert_id: int, error: str | None) -> None:
        self.c.execute("UPDATE alerts SET last_run=?, last_error=? WHERE id=?",
                       (int(time.time()), error, alert_id))
        self.c.commit()

    # listings
    def listing(self, alert_id: int, item_id: str) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM listings WHERE alert_id=? AND item_id=?",
                              (alert_id, item_id)).fetchone()

    def similar_seen(self, alert_id: int, it: Listing) -> sqlite3.Row | None:
        """Same seller, same title: a repost of something already judged."""
        return self.c.execute(
            "SELECT * FROM listings WHERE alert_id=? AND seller_id=? AND lower(title)=lower(?) AND item_id<>?",
            (alert_id, it.seller_id, it.title, it.id)).fetchone()

    def save_listing(self, alert_id: int, it: Listing, verdict: str, score: float | None = None,
                     why: str = "") -> None:
        now = int(time.time())
        self.c.execute(
            """INSERT INTO listings(alert_id,item_id,title,price,seller_id,url,description,verdict,score,why,
                                    price_history,first_seen,last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(alert_id,item_id) DO UPDATE SET verdict=excluded.verdict, score=excluded.score,
                 why=excluded.why, price=excluded.price, last_seen=excluded.last_seen""",
            (alert_id, it.id, it.title, it.price, it.seller_id, it.url, it.description[:2000], verdict, score,
             why, json.dumps([[now, it.price]]), now, now))
        self.c.commit()

    def update_price(self, alert_id: int, item_id: str, price: float) -> None:
        row = self.listing(alert_id, item_id)
        hist = json.loads(row["price_history"]) if row else []
        hist.append([int(time.time()), price])
        self.c.execute("UPDATE listings SET price=?, price_history=?, last_seen=? WHERE alert_id=? AND item_id=?",
                       (price, json.dumps(hist[-20:]), int(time.time()), alert_id, item_id))
        self.c.commit()

    def touch(self, alert_id: int, item_id: str) -> None:
        self.c.execute("UPDATE listings SET last_seen=? WHERE alert_id=? AND item_id=?",
                       (int(time.time()), alert_id, item_id))
        self.c.commit()

    # feedback / mutes
    def add_feedback(self, alert_id: int, item_id: str, vote: int) -> None:
        self.c.execute("INSERT OR REPLACE INTO feedback VALUES (?,?,?,?)", (alert_id, item_id, vote, int(time.time())))
        self.c.commit()

    def feedback_examples(self, alert_id: int, limit: int = 30) -> list[sqlite3.Row]:
        return self.c.execute(
            """SELECT f.vote, l.title, l.price, l.description, l.verdict, l.why FROM feedback f
               JOIN listings l ON l.alert_id=f.alert_id AND l.item_id=f.item_id
               WHERE f.alert_id=? ORDER BY f.ts DESC LIMIT ?""", (alert_id, limit)).fetchall()

    def mute_seller(self, user_id: int, seller_id: str) -> None:
        self.c.execute("INSERT OR IGNORE INTO mutes VALUES (?,?,?)", (user_id, seller_id, int(time.time())))
        self.c.commit()

    def muted_sellers(self, user_id: int | None) -> set[str]:
        return {r[0] for r in self.c.execute("SELECT seller_id FROM mutes WHERE user_id=?", (user_id,))}

    # users (admins come from the environment; everyone else asks for access)
    def user(self, user_id: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    def set_user(self, user_id: int, status: str, name: str | None = None, username: str | None = None) -> None:
        self.c.execute(
            """INSERT INTO users VALUES (?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET status=excluded.status,
               name=COALESCE(excluded.name, name), username=COALESCE(excluded.username, username), ts=excluded.ts""",
            (user_id, name, username, status, int(time.time())))
        self.c.commit()

    def users(self) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM users ORDER BY status, ts").fetchall()

    def pause_owner(self, owner: int) -> None:
        self.c.execute("UPDATE alerts SET paused=1 WHERE owner=?", (owner,))
        self.c.commit()

    # usage
    def add_usage(self, model: str, alert_id: int, input_tokens: int, output_tokens: int, cost: float) -> None:
        self.c.execute(
            """INSERT INTO usage VALUES (?,?,?,1,?,?,?)
               ON CONFLICT(day,model,alert_id) DO UPDATE SET calls=calls+1,
                 input_tokens=input_tokens+excluded.input_tokens,
                 output_tokens=output_tokens+excluded.output_tokens, cost_usd=cost_usd+excluded.cost_usd""",
            (date.today().isoformat(), model, alert_id, input_tokens, output_tokens, cost))
        self.c.commit()

    def calls_today_owner(self, owner: int) -> int:
        return self.c.execute(
            """SELECT COALESCE(SUM(u.calls),0) FROM usage u JOIN alerts a ON a.id=u.alert_id
               WHERE u.day=? AND a.owner=?""", (date.today().isoformat(), owner)).fetchone()[0]

    def calls_today(self, alert_id: int | None = None, model: str | None = None) -> int:
        q, args = "SELECT COALESCE(SUM(calls),0) FROM usage WHERE day=?", [date.today().isoformat()]
        if alert_id is not None:
            q += " AND alert_id=?"; args.append(alert_id)
        if model is not None:
            q += " AND model=?"; args.append(model)
        return self.c.execute(q, args).fetchone()[0]

    def usage_today(self) -> list[dict[str, Any]]:
        rows = self.c.execute(
            """SELECT model, SUM(calls) calls, SUM(input_tokens) i, SUM(output_tokens) o, SUM(cost_usd) cost
               FROM usage WHERE day=? GROUP BY model""", (date.today().isoformat(),)).fetchall()
        return [dict(r) for r in rows]

    def stats(self, alert_id: int) -> dict[str, int]:
        rows = self.c.execute("SELECT verdict, COUNT(*) n FROM listings WHERE alert_id=? GROUP BY verdict",
                              (alert_id,)).fetchall()
        return {r["verdict"]: r["n"] for r in rows}
