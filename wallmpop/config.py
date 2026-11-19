"""Settings read from the environment (and from .env if present)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Settings:
    telegram_token: str
    allowed_user_ids: set[int] = field(default_factory=set)
    db_path: str = "data/wallmpop.db"
    claude_bin: str = "claude"
    default_lat: float = 40.4168
    default_lon: float = -3.7038
    default_interval_min: int = 15
    global_max_llm_per_day: int = 300
    tick_seconds: int = 30
    max_alerts_per_user: int = 10

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        ids = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
        return cls(
            telegram_token=token,
            allowed_user_ids=ids,
            db_path=os.environ.get("DB_PATH", "data/wallmpop.db"),
            claude_bin=os.environ.get("CLAUDE_BIN", "claude"),
            default_lat=float(os.environ.get("DEFAULT_LAT", "40.4168")),
            default_lon=float(os.environ.get("DEFAULT_LON", "-3.7038")),
            default_interval_min=int(os.environ.get("DEFAULT_INTERVAL_MIN", "15")),
            global_max_llm_per_day=int(os.environ.get("GLOBAL_MAX_LLM_PER_DAY", "300")),
            max_alerts_per_user=int(os.environ.get("MAX_ALERTS_PER_USER", "10")),
        )
