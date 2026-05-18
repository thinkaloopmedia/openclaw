"""
Pipeline runner — wires fetch → parse → normalize → store → cache
for one URL or a whole source batch.

Public surface:
  load_sources(path)           read sources.yaml → list[SourceConfig]
  run_url(url, source, session) single URL, end-to-end
  run_source(source, urls)     concurrent batch for one source
  run_all(sources)             all enabled sources
"""

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.parsers.normalizer import Product, normalize
from src.parsers.product import ParseError, parse
from src.retrieval.fetcher import FetchError, fetch
from src.storage.cache import cache_product, get_cached_product, is_recently_fetched, mark_fetched
from src.storage.db import AsyncSessionFactory, get_session, upsert_product

logger = logging.getLogger(__name__)


# ── source configuration ──────────────────────────────────────────────────────

@dataclass
class SourceConfig:
    name: str
    base_url: str
    enabled: bool
    schedule: str
    selectors: dict[str, str]
    parser: str = "product"
    fetch_ttl: int = 3600       # seconds before the same URL may be re-fetched
    headers: dict[str, str] = field(default_factory=dict)
    urls: list[str] = field(default_factory=list)  # static seed URLs from config


def load_sources(path: str | Path = "config/sources.yaml") -> list[SourceConfig]:
    """Parse sources.yaml and return only structurally valid entries."""
    raw = Path(path).read_text()
    data = yaml.safe_load(raw) or {}
    sources = []
    for entry in data.get("sources", []):
        try:
            sources.append(SourceConfig(
                name=entry["name"],
                base_url=entry["base_url"],
                enabled=entry.get("enabled", True),
                schedule=entry.get("schedule", "0 */6 * * *"),
                selectors=entry.get("selectors", {}),
                parser=entry.get("parser", "product"),
                fetch_ttl=entry.get("fetch_ttl", 3600),
                headers=entry.get("headers", {}),
                urls=entry.get("urls", []),
            ))
        except KeyError as exc:
            logger.warning("Skipping malformed source entry (missing %s): %r", exc, entry)
    return sources


# ── result types ──────────────────────────────────────────────────────────────

@dataclass
class PipelineResult:
    url: str
    source: str
    product: Product | None = None
    error: Exception | None = None
    skipped: bool = False       # True = recently fetched, cache returned instead

    @property
    def ok(self) -> bool:
        return self.product is not None


@dataclass
class BatchResult:
    source: str
    results: list[PipelineResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[PipelineResult]:
        return [r for r in self.results if r.ok and not r.skipped]

    @property
    def failed(self) -> list[PipelineResult]:
        return [r for r in self.results if r.error is not None]

    @property
    def skipped(self) -> list[PipelineResult]:
        return [r for r in self.results if r.skipped]

    def summary(self) -> str:
        return (
            f"[{self.source}] "
            f"{len(self.succeeded)} ok / "
            f"{len(self.failed)} failed / "
            f"{len(self.skipped)} skipped"
        )


# ── single-URL pipeline ───────────────────────────────────────────────────────

async def run_url(url: str, source: SourceConfig) -> PipelineResult:
    """
    Full pipeline for one URL.
    Opens its own DB session; safe to call concurrently.
    """
    # 1. Fetch-dedup check
    if await is_recently_fetched(url):
        cached = await get_cached_product(url)
        logger.debug("Skipping recently fetched URL: %s", url)
        return PipelineResult(url=url, source=source.name, product=cached, skipped=True)

    # 2. Fetch
    try:
        fetch_result = await fetch(url, headers=source.headers or None)
    except FetchError as exc:
        logger.error("Fetch failed for %s: %s", url, exc)
        return PipelineResult(url=url, source=source.name, error=exc)

    # 3. Parse
    try:
        raw = parse(fetch_result, source_name=source.name, selectors=source.selectors)
    except ParseError as exc:
        logger.error("Parse failed for %s: %s", url, exc)
        return PipelineResult(url=url, source=source.name, error=exc)

    # 4. Normalize
    product = normalize(raw)

    # 5. Persist
    try:
        async with get_session() as session:
            await upsert_product(session, product)
    except Exception as exc:
        logger.error("DB write failed for %s: %s", url, exc)
        return PipelineResult(url=url, source=source.name, error=exc)

    # 6. Cache
    await cache_product(product)
    await mark_fetched(url, ttl=source.fetch_ttl)

    logger.info("Processed %s [%s] price=%s %s", url, product.availability, product.price, product.currency or "")
    return PipelineResult(url=url, source=source.name, product=product)


# ── batch pipeline ────────────────────────────────────────────────────────────

async def run_source(source: SourceConfig, urls: list[str]) -> BatchResult:
    """
    Run the pipeline for every URL in the list concurrently.
    Concurrency is already bounded inside the fetcher; no extra semaphore needed.
    """
    if not source.enabled:
        logger.info("Source %r is disabled — skipping", source.name)
        return BatchResult(source=source.name)

    tasks = [run_url(url, source) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    batch = BatchResult(source=source.name, results=list(results))
    logger.info(batch.summary())
    return batch


async def run_all(sources: list[SourceConfig], url_map: dict[str, list[str]]) -> list[BatchResult]:
    """
    Run all enabled sources.
    url_map: { source_name → [url, ...] }
    Sources not present in url_map are skipped.
    """
    tasks = [
        run_source(src, url_map[src.name])
        for src in sources
        if src.enabled and src.name in url_map
    ]
    return list(await asyncio.gather(*tasks))
