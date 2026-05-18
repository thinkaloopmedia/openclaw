"""
Manages a shared async HTTP client with sensible defaults.
Callers should use the module-level `get_client()` rather than
instantiating httpx.AsyncClient directly, so connection pools are reused.
"""

import httpx
from config.settings import settings

_client: httpx.AsyncClient | None = None


def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(settings.request_timeout),
        limits=httpx.Limits(max_connections=settings.max_concurrent_fetches),
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    )


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = _build_client()
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None
