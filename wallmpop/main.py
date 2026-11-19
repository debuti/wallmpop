"""Entry point: `python -m wallmpop` runs the bot, `python -m wallmpop probe <query>` tests the source."""
from __future__ import annotations

import asyncio
import logging
import sys

from .config import Settings, load_dotenv
from .db import DB
from .llm import Claude
from .models import AlertConfig
from .prefilter import prefilter
from .scheduler import Scheduler
from .sources import SourceError
from .sources.wallapop import WallapopSource


async def probe(query: str, price_max: float | None) -> int:
    cfg = AlertConfig(name="probe", query=query, rubric="-", price_max=price_max)
    try:
        items = await WallapopSource().search(cfg)
    except SourceError as e:
        print(f"FALLO {e}")
        return 1
    for it in items:
        print(f"{it.price:>7g}€  {(prefilter(cfg, it) or 'pasa'):<14} {it.title[:60]}  ({it.city})")
    print(f"{len(items)} anuncios")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        load_dotenv()
        pmax = float(sys.argv[3]) if len(sys.argv) > 3 else None
        return asyncio.run(probe(sys.argv[2] if len(sys.argv) > 2 else "nvme 512", pmax))

    from .bot import Bot

    s = Settings.from_env()
    if not s.telegram_token:
        print("Falta TELEGRAM_BOT_TOKEN (en .env)", file=sys.stderr)
        return 2
    db = DB(s.db_path)
    claude = Claude(s.claude_bin, on_usage=lambda m, a, r: db.add_usage(m, a, r.input_tokens, r.output_tokens, r.cost))
    bot = Bot(s, db, claude)
    app = bot.build(lambda b: Scheduler(db, WallapopSource(), claude, b, s.default_interval_min,
                                        s.global_max_llm_per_day, s.tick_seconds))
    app.run_polling(allowed_updates=["message", "callback_query"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
