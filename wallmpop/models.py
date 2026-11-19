"""Alert config and listing models."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

FREQUENCIES = [5, 15, 30, 60]


@dataclass
class AlertConfig:
    name: str
    query: str
    rubric: str
    category_id: int | None = None
    price_min: float | None = None
    price_max: float | None = None
    lat: float = 40.4168
    lon: float = -3.7038
    radius_km: float | None = 30
    include_regex: list[str] = field(default_factory=list)
    exclude_regex: list[str] = field(default_factory=list)
    interval_min: int | None = None
    max_llm_per_day: int = 40

    @classmethod
    def from_dict(cls, d: dict[str, Any], default_lat: float = 40.4168, default_lon: float = -3.7038) -> "AlertConfig":
        if not isinstance(d, dict):
            raise ValueError("la config no es un objeto")
        for key in ("name", "query", "rubric"):
            if not str(d.get(key) or "").strip():
                raise ValueError(f"falta '{key}'")

        def num(key, cast=float):
            v = d.get(key)
            return None if v in (None, "", "null") else cast(v)

        cfg = cls(
            name=str(d["name"]).strip()[:60],
            query=str(d["query"]).strip(),
            rubric=str(d["rubric"]).strip(),
            category_id=num("category_id", int),
            price_min=num("price_min"),
            price_max=num("price_max"),
            lat=num("lat") if d.get("lat") is not None else default_lat,
            lon=num("lon") if d.get("lon") is not None else default_lon,
            radius_km=num("radius_km"),
            include_regex=[str(x) for x in d.get("include_regex") or []],
            exclude_regex=[str(x) for x in d.get("exclude_regex") or []],
            interval_min=num("interval_min", int),
            max_llm_per_day=int(d.get("max_llm_per_day") or 40),
        )
        for rx in cfg.include_regex + cfg.exclude_regex:
            try:
                re.compile(rx, re.I)
            except re.error as e:
                raise ValueError(f"regex inválida {rx!r}: {e}") from e
        if cfg.interval_min is not None and cfg.interval_min < 5:
            cfg.interval_min = 5
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Listing:
    id: str
    title: str
    description: str
    price: float
    seller_id: str
    url: str
    image: str | None = None
    city: str = ""
    lat: float | None = None
    lon: float | None = None
    shippable: bool = False
    reserved: bool = False
    created_at: int = 0
