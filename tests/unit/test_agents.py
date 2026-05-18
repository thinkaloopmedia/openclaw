import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.agents.orchestrator import Orchestrator
from src.agents.scheduler import Scheduler
from src.pipeline.runner import BatchResult, PipelineResult, SourceConfig


# ── helpers ───────────────────────────────────────────────────────────────────

def _source(name: str = "test_store", enabled: bool = True, urls: list[str] | None = None) -> SourceConfig:
    return SourceConfig(
        name=name,
        base_url="https://example.com",
        enabled=enabled,
        schedule="0 */6 * * *",
        selectors={},
        urls=urls or ["https://example.com/p/1"],
    )


def _mock_aps_scheduler():
    aps = MagicMock()
    aps.running = False
    aps.get_jobs.return_value = []
    aps.get_job.return_value = None
    return aps


# ── Scheduler ─────────────────────────────────────────────────────────────────

class TestScheduler:
    def test_register_adds_job_to_apscheduler(self):
        sched = Scheduler()
        sched._aps = _mock_aps_scheduler()
        sched._aps.get_job.return_value = MagicMock(next_run_time=None)

        async def _cb(): pass

        sched.register(_source(), _cb)
        sched._aps.add_job.assert_called_once()
        call_kwargs = sched._aps.add_job.call_args[1]
        assert call_kwargs["id"] == "test_store"
        assert call_kwargs["replace_existing"] is True

    def test_register_skips_invalid_cron(self, caplog):
        import logging
        sched = Scheduler()
        sched._aps = _mock_aps_scheduler()

        async def _cb(): pass

        bad_source = _source()
        bad_source.schedule = "not-a-cron"
        with caplog.at_level(logging.ERROR, logger="src.agents.scheduler"):
            sched.register(bad_source, _cb)
        sched._aps.add_job.assert_not_called()
        assert "Invalid cron" in caplog.text

    def test_start_delegates_to_apscheduler(self):
        sched = Scheduler()
        sched._aps = _mock_aps_scheduler()
        sched.start()
        sched._aps.start.assert_called_once()

    def test_shutdown_only_when_running(self):
        sched = Scheduler()
        sched._aps = _mock_aps_scheduler()
        sched._aps.running = False
        sched.shutdown()
        sched._aps.shutdown.assert_not_called()

        sched._aps.running = True
        sched.shutdown()
        sched._aps.shutdown.assert_called_once_with(wait=False)

    def test_pause_and_resume(self):
        sched = Scheduler()
        sched._aps = _mock_aps_scheduler()
        sched.pause("test_store")
        sched._aps.pause_job.assert_called_once_with("test_store")
        sched.resume("test_store")
        sched._aps.resume_job.assert_called_once_with("test_store")

    def test_remove_job(self):
        sched = Scheduler()
        sched._aps = _mock_aps_scheduler()
        sched.remove("test_store")
        sched._aps.remove_job.assert_called_once_with("test_store")

    def test_list_jobs(self):
        sched = Scheduler()
        sched._aps = _mock_aps_scheduler()
        job = MagicMock()
        job.id = "test_store"
        job.name = "openclaw:test_store"
        job.next_run_time = None
        sched._aps.get_jobs.return_value = [job]

        jobs = sched.list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["id"] == "test_store"
        assert jobs[0]["paused"] is True

    def test_trigger_now_modifies_next_run_time(self):
        sched = Scheduler()
        sched._aps = _mock_aps_scheduler()
        sched.trigger_now("test_store")
        sched._aps.modify_job.assert_called_once()
        assert sched._aps.modify_job.call_args[0][0] == "test_store"


# ── Orchestrator ──────────────────────────────────────────────────────────────

class TestOrchestrator:
    def _make_orch(self, sources=None) -> Orchestrator:
        orch = Orchestrator.__new__(Orchestrator)
        orch._sources_path = "config/sources.yaml"
        orch._sources = {}
        orch._url_registry = {}
        orch._stop_event = asyncio.Event()
        sched = MagicMock(spec=Scheduler)
        sched.list_jobs.return_value = []
        orch._scheduler = sched
        if sources:
            for s in sources:
                orch._sources[s.name] = s
                orch._url_registry[s.name] = list(s.urls)
        return orch

    # submit

    def test_submit_adds_new_urls(self):
        orch = self._make_orch(sources=[_source()])
        count = orch.submit("test_store", ["https://example.com/p/2", "https://example.com/p/3"])
        assert count == 2
        assert "https://example.com/p/2" in orch._url_registry["test_store"]

    def test_submit_ignores_duplicates(self):
        orch = self._make_orch(sources=[_source(urls=["https://example.com/p/1"])])
        count = orch.submit("test_store", ["https://example.com/p/1"])
        assert count == 0
        assert orch._url_registry["test_store"].count("https://example.com/p/1") == 1

    def test_submit_unknown_source_returns_zero(self, caplog):
        import logging
        orch = self._make_orch()
        with caplog.at_level(logging.WARNING, logger="src.agents.orchestrator"):
            count = orch.submit("ghost_store", ["https://x.com/p/1"])
        assert count == 0
        assert "unknown source" in caplog.text

    def test_remove_urls(self):
        orch = self._make_orch(sources=[_source(urls=["https://example.com/p/1", "https://example.com/p/2"])])
        orch.remove_urls("test_store", ["https://example.com/p/1"])
        assert orch._url_registry["test_store"] == ["https://example.com/p/2"]

    # _register_source

    def test_register_source_merges_static_urls(self):
        orch = self._make_orch()
        orch._url_registry["test_store"] = ["https://example.com/runtime/1"]
        source = _source(urls=["https://example.com/p/1"])
        orch._register_source(source)
        registry = set(orch._url_registry["test_store"])
        assert "https://example.com/runtime/1" in registry
        assert "https://example.com/p/1" in registry

    def test_register_source_disabled_skips_scheduler(self):
        orch = self._make_orch()
        orch._register_source(_source(enabled=False))
        orch._scheduler.register.assert_not_called()

    def test_register_source_enabled_calls_scheduler(self):
        orch = self._make_orch()
        orch._register_source(_source(enabled=True))
        orch._scheduler.register.assert_called_once()

    # _run_source_job

    @pytest.mark.asyncio
    async def test_run_source_job_unknown_source_returns_none(self):
        orch = self._make_orch()
        result = await orch._run_source_job("ghost")
        assert result is None

    @pytest.mark.asyncio
    async def test_run_source_job_no_urls_returns_none(self, caplog):
        import logging
        orch = self._make_orch(sources=[_source(urls=[])])
        orch._url_registry["test_store"] = []
        with caplog.at_level(logging.WARNING, logger="src.agents.orchestrator"):
            result = await orch._run_source_job("test_store")
        assert result is None
        assert "no URLs" in caplog.text

    @pytest.mark.asyncio
    async def test_run_source_job_calls_run_source(self):
        orch = self._make_orch(sources=[_source()])
        batch = BatchResult(source="test_store")

        with patch("src.agents.orchestrator.run_source", AsyncMock(return_value=batch)) as mock_run:
            result = await orch._run_source_job("test_store")

        assert result is batch
        mock_run.assert_called_once_with(orch._sources["test_store"], orch._url_registry["test_store"])

    @pytest.mark.asyncio
    async def test_run_source_job_catches_unexpected_exception(self, caplog):
        import logging
        orch = self._make_orch(sources=[_source()])

        with patch("src.agents.orchestrator.run_source", AsyncMock(side_effect=RuntimeError("boom"))), \
             caplog.at_level(logging.ERROR, logger="src.agents.orchestrator"):
            result = await orch._run_source_job("test_store")

        assert result is None
        assert "Unhandled error" in caplog.text

    # trigger_now

    @pytest.mark.asyncio
    async def test_trigger_now_unknown_source_returns_none(self):
        orch = self._make_orch()
        result = await orch.trigger_now("ghost")
        assert result is None

    @pytest.mark.asyncio
    async def test_trigger_now_delegates_to_run_source_job(self):
        orch = self._make_orch(sources=[_source()])
        batch = BatchResult(source="test_store")

        with patch.object(orch, "_run_source_job", AsyncMock(return_value=batch)) as mock_job:
            result = await orch.trigger_now("test_store")

        assert result is batch
        mock_job.assert_called_once_with("test_store")

    # stop

    @pytest.mark.asyncio
    async def test_stop_sets_stop_event(self):
        orch = self._make_orch()
        with patch.object(orch, "_shutdown", AsyncMock()):
            await orch.stop()
        assert orch._stop_event.is_set()

    # properties

    def test_sources_property_returns_copy(self):
        orch = self._make_orch(sources=[_source()])
        sources = orch.sources
        sources["mutated"] = _source(name="mutated")
        assert "mutated" not in orch._sources

    def test_url_registry_property_returns_copy(self):
        orch = self._make_orch(sources=[_source()])
        reg = orch.url_registry
        reg["test_store"].append("https://injected.com")
        assert "https://injected.com" not in orch._url_registry["test_store"]
