"""
Top-level coordinator.  Owns the scheduler, the URL registry, and
the process lifecycle (signal handling, graceful shutdown).

Typical usage (from scripts/run.py):
    orch = Orchestrator()
    await orch.start()          # blocks until SIGINT / SIGTERM

Runtime URL injection:
    orch.submit("example_store", ["https://example.com/products/new-item"])

Immediate trigger (bypass schedule):
    await orch.trigger_now("example_store")
"""

import asyncio
import logging
import signal
from typing import Any

from src.agents.scheduler import Scheduler
from src.pipeline.runner import BatchResult, SourceConfig, load_sources, run_source
from src.retrieval.session import close_client
from src.storage.cache import close_redis
from src.storage.db import create_tables

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, sources_path: str = "config/sources.yaml") -> None:
        self._sources_path = sources_path
        self._sources: dict[str, SourceConfig] = {}
        self._url_registry: dict[str, list[str]] = {}
        self._scheduler = Scheduler()
        self._stop_event: asyncio.Event | None = None

    # ── public lifecycle ──────────────────────────────────────────────────────

    async def setup(self) -> None:
        """
        Initialize without blocking: create tables, load sources, start scheduler.
        Called by the API lifespan; also called internally by start().
        """
        self._stop_event = asyncio.Event()

        await create_tables()

        sources = load_sources(self._sources_path)
        for source in sources:
            self._register_source(source)

        self._scheduler.start()
        self._install_signal_handlers()

        logger.info(
            "Orchestrator ready — %d source(s) registered",
            len(self._sources),
        )
        for job in self._scheduler.list_jobs():
            logger.info("  job=%-30s next=%s", job["id"], job["next_run"])

    async def start(self) -> None:
        """
        Full lifecycle: setup then block until SIGINT / SIGTERM.
        Use this as the CLI entrypoint (scripts/run.py).
        """
        await self.setup()
        await self._stop_event.wait()
        await self._shutdown()

    async def stop(self) -> None:
        """Signal the orchestrator to stop (safe to call from tests or API handlers)."""
        if self._stop_event:
            self._stop_event.set()
        else:
            await self._shutdown()

    # ── runtime URL management ────────────────────────────────────────────────

    def submit(self, source_name: str, urls: list[str]) -> int:
        """
        Add URLs to the crawl list for a source.
        Duplicates are ignored.  Returns the count of newly added URLs.
        """
        if source_name not in self._sources:
            logger.warning("submit() called for unknown source %r — ignored", source_name)
            return 0
        existing = set(self._url_registry.get(source_name, []))
        new_urls = [u for u in urls if u not in existing]
        self._url_registry[source_name] = list(existing) + new_urls
        logger.info("Submitted %d new URL(s) to source %r", len(new_urls), source_name)
        return len(new_urls)

    def remove_urls(self, source_name: str, urls: list[str]) -> None:
        """Remove specific URLs from the crawl list for a source."""
        drop = set(urls)
        self._url_registry[source_name] = [
            u for u in self._url_registry.get(source_name, []) if u not in drop
        ]

    # ── on-demand execution ───────────────────────────────────────────────────

    async def trigger_now(self, source_name: str) -> BatchResult | None:
        """Run a source immediately, outside its schedule."""
        source = self._sources.get(source_name)
        if source is None:
            logger.error("trigger_now: unknown source %r", source_name)
            return None
        return await self._run_source_job(source_name)

    # ── scheduler control pass-throughs ───────────────────────────────────────

    def pause(self, source_name: str) -> None:
        self._scheduler.pause(source_name)

    def resume(self, source_name: str) -> None:
        self._scheduler.resume(source_name)

    def reload_sources(self) -> None:
        """Re-read sources.yaml and register any new or changed sources."""
        for source in load_sources(self._sources_path):
            self._register_source(source)

    @property
    def jobs(self) -> list[dict[str, Any]]:
        return self._scheduler.list_jobs()

    @property
    def sources(self) -> dict[str, SourceConfig]:
        return dict(self._sources)

    @property
    def url_registry(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self._url_registry.items()}

    # ── internals ─────────────────────────────────────────────────────────────

    def _register_source(self, source: SourceConfig) -> None:
        self._sources[source.name] = source
        # Merge static URLs from config into the registry (preserve runtime additions)
        existing = set(self._url_registry.get(source.name, []))
        merged = list(existing | set(source.urls))
        self._url_registry[source.name] = merged

        if source.enabled:
            self._scheduler.register(source, self._make_job(source.name))
        else:
            logger.info("Source %r is disabled — job not scheduled", source.name)

    def _make_job(self, source_name: str):
        """Return a no-arg async callable suitable for APScheduler."""
        async def _job() -> None:
            await self._run_source_job(source_name)
        _job.__name__ = f"job_{source_name}"
        return _job

    async def _run_source_job(self, source_name: str) -> BatchResult | None:
        source = self._sources.get(source_name)
        if source is None:
            logger.error("_run_source_job: source %r not found", source_name)
            return None

        urls = self._url_registry.get(source_name, [])
        if not urls:
            logger.warning("Source %r has no URLs registered — skipping run", source_name)
            return None

        logger.info("Running source %r with %d URL(s)", source_name, len(urls))
        try:
            return await run_source(source, urls)
        except Exception as exc:
            logger.exception("Unhandled error running source %r: %s", source_name, exc)
            return None

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
            except (NotImplementedError, RuntimeError):
                # Windows doesn't support add_signal_handler
                pass

    def _on_signal(self, sig: signal.Signals) -> None:
        logger.info("Received %s — initiating graceful shutdown", sig.name)
        if self._stop_event:
            self._stop_event.set()

    async def _shutdown(self) -> None:
        logger.info("Shutting down orchestrator...")
        self._scheduler.shutdown(wait=False)
        await close_client()
        await close_redis()
        logger.info("Orchestrator stopped cleanly")
