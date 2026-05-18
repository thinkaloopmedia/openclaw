"""
Thin wrapper around APScheduler's AsyncIOScheduler.
Translates SourceConfig cron strings into scheduler jobs and exposes
lifecycle + introspection methods the Orchestrator needs.
"""

import logging
from datetime import datetime, timezone
from typing import Callable, Coroutine, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.pipeline.runner import SourceConfig

logger = logging.getLogger(__name__)

AsyncCallback = Callable[[], Coroutine[Any, Any, None]]


class Scheduler:
    def __init__(self) -> None:
        self._aps = AsyncIOScheduler(timezone="UTC")

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._aps.start()
        logger.info("Scheduler started")

    def shutdown(self, wait: bool = False) -> None:
        if self._aps.running:
            self._aps.shutdown(wait=wait)
            logger.info("Scheduler stopped")

    # ── job management ────────────────────────────────────────────────────────

    def register(self, source: SourceConfig, callback: AsyncCallback) -> None:
        """Add or replace a cron job for a source."""
        try:
            trigger = CronTrigger.from_crontab(source.schedule, timezone="UTC")
        except ValueError as exc:
            logger.error("Invalid cron expression %r for source %r: %s", source.schedule, source.name, exc)
            return

        self._aps.add_job(
            callback,
            trigger=trigger,
            id=source.name,
            name=f"openclaw:{source.name}",
            replace_existing=True,
            misfire_grace_time=300,   # tolerate up to 5-minute late fire
        )
        next_run = self._aps.get_job(source.name)
        logger.info(
            "Registered job %r  schedule=%r  next=%s",
            source.name,
            source.schedule,
            next_run.next_run_time if next_run else "unknown",
        )

    def remove(self, source_name: str) -> None:
        try:
            self._aps.remove_job(source_name)
            logger.info("Removed job %r", source_name)
        except Exception:
            logger.warning("Job %r not found — nothing to remove", source_name)

    def pause(self, source_name: str) -> None:
        self._aps.pause_job(source_name)
        logger.info("Paused job %r", source_name)

    def resume(self, source_name: str) -> None:
        self._aps.resume_job(source_name)
        logger.info("Resumed job %r", source_name)

    def trigger_now(self, source_name: str) -> None:
        """Fire a job immediately without waiting for its next scheduled time."""
        self._aps.modify_job(source_name, next_run_time=datetime.now(tz=timezone.utc))
        logger.info("Triggered immediate run of job %r", source_name)

    # ── introspection ─────────────────────────────────────────────────────────

    def list_jobs(self) -> list[dict]:
        return [
            {
                "id": job.id,
                "name": job.name,
                "next_run": job.next_run_time,
                "paused": job.next_run_time is None,
            }
            for job in self._aps.get_jobs()
        ]

    @property
    def running(self) -> bool:
        return self._aps.running
