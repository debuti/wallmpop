"""Deterministic filter: costs zero tokens. Returns a reject reason, or None if the listing passes."""
from __future__ import annotations

import math
import re

from .models import AlertConfig, Listing


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def distance_km(cfg: AlertConfig, it: Listing) -> float | None:
    if it.lat is None or it.lon is None:
        return None
    return haversine_km(cfg.lat, cfg.lon, it.lat, it.lon)


def prefilter(cfg: AlertConfig, it: Listing, muted: set[str] | None = None) -> str | None:
    if it.reserved:
        return "reservado"
    if muted and it.seller_id in muted:
        return "vendedor silenciado"
    if cfg.price_min is not None and it.price < cfg.price_min:
        return "precio bajo"
    if cfg.price_max is not None and it.price > cfg.price_max:
        return "precio alto"
    if cfg.radius_km:
        d = distance_km(cfg, it)
        # 10% slack: the API rounds and the seller's pin is approximate.
        if d is not None and d > cfg.radius_km * 1.1:
            return "lejos"
    text = f"{it.title}\n{it.description}"
    for rx in cfg.include_regex:
        if not re.search(rx, text, re.I):
            return f"no casa {rx}"
    for rx in cfg.exclude_regex:
        if re.search(rx, text, re.I):
            return f"excluido {rx}"
    return None
