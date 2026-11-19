"""Wallapop internal search API (api.wallapop.com/api/v3/search)."""
from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from ..models import AlertConfig, Listing
from . import SourceError

SEARCH_URL = "https://api.wallapop.com/api/v3/search"
ITEM_URL = "https://es.wallapop.com/item/{slug}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9",
    "X-DeviceOS": "0",
    "Origin": "https://es.wallapop.com",
    "Referer": "https://es.wallapop.com/",
}


def build_params(cfg: AlertConfig) -> dict[str, Any]:
    p: dict[str, Any] = {
        "keywords": cfg.query,
        "latitude": cfg.lat,
        "longitude": cfg.lon,
        "order_by": "newest",
        "source": "search_box",
    }
    if cfg.radius_km:
        p["distance_in_km"] = int(cfg.radius_km)
    if cfg.price_min is not None:
        p["min_sale_price"] = int(cfg.price_min)
    if cfg.price_max is not None:
        p["max_sale_price"] = int(cfg.price_max + 0.999)
    if cfg.category_id:
        p["category_id"] = cfg.category_id
    return p


def parse_item(x: dict[str, Any]) -> Listing:
    loc = x.get("location") or {}
    imgs = x.get("images") or []
    img = None
    if imgs:
        urls = imgs[0].get("urls") or {}
        img = urls.get("medium") or urls.get("big") or urls.get("small")
    return Listing(
        id=str(x["id"]),
        title=str(x.get("title") or ""),
        description=str(x.get("description") or ""),
        price=float(x["price"]["amount"]),
        seller_id=str(x.get("user_id") or ""),
        url=ITEM_URL.format(slug=x.get("web_slug") or x["id"]),
        image=img,
        city=str(loc.get("city") or ""),
        lat=loc.get("latitude"),
        lon=loc.get("longitude"),
        shippable=bool((x.get("shipping") or {}).get("user_allows_shipping")),
        reserved=bool((x.get("reserved") or {}).get("flag")),
        created_at=int(x.get("created_at") or 0),
    )


def parse_response(data: Any) -> list[Listing]:
    """Raise SourceError('schema') when the payload doesn't look like we expect."""
    try:
        items = data["data"]["section"]["payload"]["items"]
    except (KeyError, TypeError) as e:
        raise SourceError("schema", f"falta data.section.payload.items ({e!r})") from e
    if not isinstance(items, list):
        raise SourceError("schema", "items no es una lista")
    out, bad = [], 0
    for x in items:
        try:
            out.append(parse_item(x))
        except (KeyError, TypeError, ValueError):
            bad += 1
    if items and bad > len(items) // 2:
        raise SourceError("schema", f"{bad}/{len(items)} anuncios con campos inesperados (id/price)")
    return out


class WallapopSource:
    name = "wallapop"

    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True)

    async def search(self, cfg: AlertConfig) -> list[Listing]:
        params = build_params(cfg)
        last: SourceError | None = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(2 ** attempt + random.random() * 2)
            try:
                r = await self.client.get(SEARCH_URL, params=params)
            except httpx.HTTPError as e:
                last = SourceError("network", repr(e))
                continue
            if r.status_code in (401, 403, 429):
                last = SourceError("blocked", f"HTTP {r.status_code}")
                continue
            if r.status_code >= 500:
                last = SourceError("http", f"HTTP {r.status_code}")
                continue
            if r.status_code != 200:
                raise SourceError("http", f"HTTP {r.status_code}: {r.text[:200]}")
            try:
                data = r.json()
            except ValueError as e:
                raise SourceError("schema", f"respuesta no es JSON: {r.text[:120]!r}") from e
            return parse_response(data)
        assert last is not None
        raise last
