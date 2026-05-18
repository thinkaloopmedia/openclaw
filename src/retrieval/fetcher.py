import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from config.settings import settings
from src.retrieval.session import get_client

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    url: str
    status_code: int
    html: str
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_ms: float = 0.0


class FetchError(Exception):
    def __init__(self, url: str, reason: str, status_code: int | None = None):
        self.url = url
        self.reason = reason
        self.status_code = status_code
        super().__init__(f"[{status_code}] {url} — {reason}")


async def fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> FetchResult:
    """Fetch a single URL with automatic retry on transient errors."""
    client = get_client()
    last_exc: Exception | None = None

    for attempt in range(settings.retry_attempts):
        try:
            response = await client.get(url, headers=headers, params=params)

            if response.status_code in settings.retry_status_codes:
                delay = _retry_delay(attempt, response)
                logger.warning(
                    "Retryable status %d for %s (attempt %d/%d), waiting %.1fs",
                    response.status_code,
                    url,
                    attempt + 1,
                    settings.retry_attempts,
                    delay,
                )
                await asyncio.sleep(delay)
                last_exc = FetchError(url, "retryable status", response.status_code)
                continue

            response.raise_for_status()

            return FetchResult(
                url=url,
                status_code=response.status_code,
                html=response.text,
                headers=dict(response.headers),
                elapsed_ms=response.elapsed.total_seconds() * 1000,
            )

        except httpx.TimeoutException as exc:
            delay = settings.retry_backoff_base ** attempt
            logger.warning("Timeout on %s (attempt %d/%d), waiting %.1fs", url, attempt + 1, settings.retry_attempts, delay)
            await asyncio.sleep(delay)
            last_exc = FetchError(url, "timeout", None)

        except httpx.HTTPStatusError as exc:
            raise FetchError(url, "non-retryable HTTP error", exc.response.status_code) from exc

        except httpx.RequestError as exc:
            raise FetchError(url, str(exc)) from exc

    raise FetchError(url, "max retries exceeded") from last_exc


async def fetch_many(
    urls: list[str],
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> list[FetchResult | FetchError]:
    """
    Fetch multiple URLs concurrently, bounded by max_concurrent_fetches.
    Returns results in the same order as the input list.
    Errors are returned as FetchError instances rather than raised.
    """
    semaphore = asyncio.Semaphore(settings.max_concurrent_fetches)

    async def _guarded(url: str) -> FetchResult | FetchError:
        async with semaphore:
            try:
                return await fetch(url, headers=headers, params=params)
            except FetchError as exc:
                logger.error("Failed to fetch %s: %s", url, exc)
                return exc

    return list(await asyncio.gather(*[_guarded(u) for u in urls]))


def _retry_delay(attempt: int, response: httpx.Response) -> float:
    """Honour Retry-After header when present, otherwise exponential backoff."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return settings.retry_backoff_base ** attempt
