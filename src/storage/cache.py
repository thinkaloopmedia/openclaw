"""
Redis cache with two responsibilities:
  1. Fetch deduplication — skip re-fetching a URL that was retrieved recently.
  2. Product cache — short-lived store of the last normalized product for a URL,
     so the pipeline can serve a stale read while a fresh fetch is in flight.
"""

import json
import logging
from decimal import Decimal

import redis.asyncio as aioredis

from config.settings import settings
from src.parsers.normalizer import Availability, Product

logger = logging.getLogger(__name__)

_FETCH_KEY_PREFIX = "openclaw:fetched:"
_PRODUCT_KEY_PREFIX = "openclaw:product:"

_DEFAULT_FETCH_TTL = 3600        # 1 hour — don't re-fetch the same URL within this window
_DEFAULT_PRODUCT_TTL = 300       # 5 minutes — stale-while-revalidate window


_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client:
        await _client.aclose()
        _client = None


# ── fetch deduplication ───────────────────────────────────────────────────────

async def mark_fetched(url: str, ttl: int = _DEFAULT_FETCH_TTL) -> None:
    """Record that this URL was just fetched. Expires after `ttl` seconds."""
    await get_redis().set(_FETCH_KEY_PREFIX + url, "1", ex=ttl)


async def is_recently_fetched(url: str) -> bool:
    val = await get_redis().exists(_FETCH_KEY_PREFIX + url)
    return bool(val)


# ── product cache ─────────────────────────────────────────────────────────────

async def cache_product(product: Product, ttl: int = _DEFAULT_PRODUCT_TTL) -> None:
    payload = json.dumps(_product_to_dict(product))
    await get_redis().set(_PRODUCT_KEY_PREFIX + product.url, payload, ex=ttl)


async def get_cached_product(url: str) -> Product | None:
    raw = await get_redis().get(_PRODUCT_KEY_PREFIX + url)
    if raw is None:
        return None
    try:
        return _dict_to_product(json.loads(raw))
    except Exception:
        logger.warning("Corrupt cache entry for %s — discarding", url)
        await get_redis().delete(_PRODUCT_KEY_PREFIX + url)
        return None


async def invalidate(url: str) -> None:
    """Remove both fetch-dedup and product cache entries for a URL."""
    await get_redis().delete(_FETCH_KEY_PREFIX + url, _PRODUCT_KEY_PREFIX + url)


# ── serialisation helpers ─────────────────────────────────────────────────────

def _product_to_dict(p: Product) -> dict:
    return {
        "url": p.url,
        "source": p.source,
        "name": p.name,
        "price": str(p.price) if p.price is not None else None,
        "currency": p.currency,
        "sku": p.sku,
        "availability": p.availability,
        "description": p.description,
        "image_url": p.image_url,
        "extras": p.extras,
    }


def _dict_to_product(d: dict) -> Product:
    return Product(
        url=d["url"],
        source=d["source"],
        name=d.get("name"),
        price=Decimal(d["price"]) if d.get("price") is not None else None,
        currency=d.get("currency"),
        sku=d.get("sku"),
        availability=Availability(d.get("availability", "unknown")),
        description=d.get("description"),
        image_url=d.get("image_url"),
        extras=d.get("extras", {}),
    )
