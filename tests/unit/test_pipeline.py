import textwrap
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.parsers.normalizer import Availability, Product
from src.parsers.product import ParseError, RawProduct
from src.pipeline.runner import (
    BatchResult,
    PipelineResult,
    SourceConfig,
    load_sources,
    run_all,
    run_source,
    run_url,
)
from src.retrieval.fetcher import FetchError, FetchResult


# ── helpers ───────────────────────────────────────────────────────────────────

def _source(**kwargs) -> SourceConfig:
    defaults = dict(
        name="test_store",
        base_url="https://example.com",
        enabled=True,
        schedule="0 */6 * * *",
        selectors={"name": "h1", "price": "span.price"},
        fetch_ttl=3600,
    )
    return SourceConfig(**{**defaults, **kwargs})


def _product(**kwargs) -> Product:
    defaults = dict(
        url="https://example.com/p/1",
        source="test_store",
        name="Widget Pro",
        price=Decimal("29.99"),
        currency="USD",
        sku="WGT-001",
        availability=Availability.IN_STOCK,
        description=None,
        image_url=None,
        extras={},
    )
    return Product(**{**defaults, **kwargs})


def _fetch_result(url: str = "https://example.com/p/1") -> FetchResult:
    return FetchResult(url=url, status_code=200, html="<html><h1>Widget Pro</h1></html>")


def _mock_session_ctx(session: MagicMock) -> MagicMock:
    """Return a mock that works as `async with get_session() as session:`."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ── load_sources ──────────────────────────────────────────────────────────────

class TestLoadSources:
    def test_parses_valid_source(self, tmp_path: Path):
        yaml_content = textwrap.dedent("""\
            sources:
              - name: test_store
                base_url: https://example.com
                enabled: true
                schedule: "0 */6 * * *"
                selectors:
                  name: "h1.title"
                  price: "span.price"
        """)
        p = tmp_path / "sources.yaml"
        p.write_text(yaml_content)
        sources = load_sources(p)
        assert len(sources) == 1
        assert sources[0].name == "test_store"
        assert sources[0].selectors["name"] == "h1.title"

    def test_defaults_applied(self, tmp_path: Path):
        yaml_content = textwrap.dedent("""\
            sources:
              - name: minimal_store
                base_url: https://example.com
                selectors: {}
        """)
        p = tmp_path / "sources.yaml"
        p.write_text(yaml_content)
        sources = load_sources(p)
        assert sources[0].enabled is True
        assert sources[0].fetch_ttl == 3600
        assert sources[0].parser == "product"

    def test_skips_malformed_entry(self, tmp_path: Path, caplog):
        yaml_content = textwrap.dedent("""\
            sources:
              - base_url: https://example.com
                selectors: {}
        """)
        p = tmp_path / "sources.yaml"
        p.write_text(yaml_content)
        import logging
        with caplog.at_level(logging.WARNING, logger="src.pipeline.runner"):
            sources = load_sources(p)
        assert sources == []
        assert "malformed" in caplog.text

    def test_empty_file_returns_empty_list(self, tmp_path: Path):
        p = tmp_path / "sources.yaml"
        p.write_text("sources: []")
        assert load_sources(p) == []

    def test_multiple_sources(self, tmp_path: Path):
        yaml_content = textwrap.dedent("""\
            sources:
              - name: store_a
                base_url: https://a.com
                selectors: {}
              - name: store_b
                base_url: https://b.com
                selectors: {}
        """)
        p = tmp_path / "sources.yaml"
        p.write_text(yaml_content)
        sources = load_sources(p)
        assert [s.name for s in sources] == ["store_a", "store_b"]


# ── run_url ───────────────────────────────────────────────────────────────────

class TestRunUrl:
    @pytest.mark.asyncio
    async def test_skips_recently_fetched_url(self):
        cached = _product()
        with patch("src.pipeline.runner.is_recently_fetched", AsyncMock(return_value=True)), \
             patch("src.pipeline.runner.get_cached_product", AsyncMock(return_value=cached)):
            result = await run_url("https://example.com/p/1", _source())

        assert result.skipped is True
        assert result.product is cached

    @pytest.mark.asyncio
    async def test_returns_error_on_fetch_failure(self):
        exc = FetchError("https://example.com/p/1", "timeout")
        with patch("src.pipeline.runner.is_recently_fetched", AsyncMock(return_value=False)), \
             patch("src.pipeline.runner.fetch", AsyncMock(side_effect=exc)):
            result = await run_url("https://example.com/p/1", _source())

        assert result.ok is False
        assert isinstance(result.error, FetchError)

    @pytest.mark.asyncio
    async def test_returns_error_on_parse_failure(self):
        exc = ParseError("https://example.com/p/1", "bad html")
        with patch("src.pipeline.runner.is_recently_fetched", AsyncMock(return_value=False)), \
             patch("src.pipeline.runner.fetch", AsyncMock(return_value=_fetch_result())), \
             patch("src.pipeline.runner.parse", side_effect=exc):
            result = await run_url("https://example.com/p/1", _source())

        assert result.ok is False
        assert isinstance(result.error, ParseError)

    @pytest.mark.asyncio
    async def test_returns_error_on_db_failure(self):
        raw = RawProduct(url="https://example.com/p/1", source="test_store", name="Widget")
        session = AsyncMock()
        session_ctx = _mock_session_ctx(session)

        with patch("src.pipeline.runner.is_recently_fetched", AsyncMock(return_value=False)), \
             patch("src.pipeline.runner.fetch", AsyncMock(return_value=_fetch_result())), \
             patch("src.pipeline.runner.parse", return_value=raw), \
             patch("src.pipeline.runner.normalize", return_value=_product()), \
             patch("src.pipeline.runner.get_session", return_value=session_ctx), \
             patch("src.pipeline.runner.upsert_product", AsyncMock(side_effect=RuntimeError("DB down"))):
            result = await run_url("https://example.com/p/1", _source())

        assert result.ok is False
        assert isinstance(result.error, RuntimeError)

    @pytest.mark.asyncio
    async def test_happy_path_returns_product(self):
        raw = RawProduct(url="https://example.com/p/1", source="test_store", name="Widget")
        product = _product()
        session = AsyncMock()
        session_ctx = _mock_session_ctx(session)

        with patch("src.pipeline.runner.is_recently_fetched", AsyncMock(return_value=False)), \
             patch("src.pipeline.runner.fetch", AsyncMock(return_value=_fetch_result())), \
             patch("src.pipeline.runner.parse", return_value=raw), \
             patch("src.pipeline.runner.normalize", return_value=product), \
             patch("src.pipeline.runner.get_session", return_value=session_ctx), \
             patch("src.pipeline.runner.upsert_product", AsyncMock()), \
             patch("src.pipeline.runner.cache_product", AsyncMock()) as mock_cache, \
             patch("src.pipeline.runner.mark_fetched", AsyncMock()) as mock_mark:
            result = await run_url("https://example.com/p/1", _source())

        assert result.ok is True
        assert result.product is product
        assert result.skipped is False
        mock_cache.assert_called_once_with(product)
        mock_mark.assert_called_once_with("https://example.com/p/1", ttl=3600)

    @pytest.mark.asyncio
    async def test_fetch_ttl_from_source_config_is_used(self):
        raw = RawProduct(url="https://example.com/p/1", source="test_store")
        session_ctx = _mock_session_ctx(AsyncMock())

        with patch("src.pipeline.runner.is_recently_fetched", AsyncMock(return_value=False)), \
             patch("src.pipeline.runner.fetch", AsyncMock(return_value=_fetch_result())), \
             patch("src.pipeline.runner.parse", return_value=raw), \
             patch("src.pipeline.runner.normalize", return_value=_product()), \
             patch("src.pipeline.runner.get_session", return_value=session_ctx), \
             patch("src.pipeline.runner.upsert_product", AsyncMock()), \
             patch("src.pipeline.runner.cache_product", AsyncMock()), \
             patch("src.pipeline.runner.mark_fetched", AsyncMock()) as mock_mark:
            await run_url("https://example.com/p/1", _source(fetch_ttl=7200))

        mock_mark.assert_called_once_with("https://example.com/p/1", ttl=7200)


# ── run_source ────────────────────────────────────────────────────────────────

class TestRunSource:
    @pytest.mark.asyncio
    async def test_disabled_source_returns_empty_batch(self):
        source = _source(enabled=False)
        batch = await run_source(source, ["https://example.com/p/1"])
        assert batch.results == []
        assert batch.source == "test_store"

    @pytest.mark.asyncio
    async def test_aggregates_results(self):
        urls = ["https://example.com/p/1", "https://example.com/p/2"]
        ok_result = PipelineResult(url=urls[0], source="test_store", product=_product(url=urls[0]))
        err_result = PipelineResult(url=urls[1], source="test_store", error=FetchError(urls[1], "timeout"))

        async def _fake_run_url(url, source):
            return ok_result if url == urls[0] else err_result

        with patch("src.pipeline.runner.run_url", side_effect=_fake_run_url):
            batch = await run_source(_source(), urls)

        assert len(batch.succeeded) == 1
        assert len(batch.failed) == 1
        assert len(batch.skipped) == 0

    @pytest.mark.asyncio
    async def test_summary_string(self):
        results = [
            PipelineResult(url="u1", source="s", product=_product(url="u1")),
            PipelineResult(url="u2", source="s", error=FetchError("u2", "x")),
            PipelineResult(url="u3", source="s", product=_product(url="u3"), skipped=True),
        ]
        batch = BatchResult(source="test_store", results=results)
        summary = batch.summary()
        assert "1 ok" in summary
        assert "1 failed" in summary
        assert "1 skipped" in summary


# ── run_all ───────────────────────────────────────────────────────────────────

class TestRunAll:
    @pytest.mark.asyncio
    async def test_skips_disabled_sources(self):
        sources = [_source(name="active"), _source(name="inactive", enabled=False)]
        url_map = {"active": ["https://a.com/p/1"], "inactive": ["https://b.com/p/1"]}

        called = []

        async def _fake_run_source(source, urls):
            called.append(source.name)
            return BatchResult(source=source.name)

        with patch("src.pipeline.runner.run_source", side_effect=_fake_run_source):
            await run_all(sources, url_map)

        assert called == ["active"]

    @pytest.mark.asyncio
    async def test_skips_sources_not_in_url_map(self):
        sources = [_source(name="store_a"), _source(name="store_b")]
        url_map = {"store_a": ["https://a.com/p/1"]}

        called = []

        async def _fake_run_source(source, urls):
            called.append(source.name)
            return BatchResult(source=source.name)

        with patch("src.pipeline.runner.run_source", side_effect=_fake_run_source):
            await run_all(sources, url_map)

        assert called == ["store_a"]

    @pytest.mark.asyncio
    async def test_returns_one_batch_per_source(self):
        sources = [_source(name="store_a"), _source(name="store_b")]
        url_map = {"store_a": ["https://a.com/p/1"], "store_b": ["https://b.com/p/1"]}

        with patch("src.pipeline.runner.run_source", AsyncMock(return_value=BatchResult(source="x"))):
            batches = await run_all(sources, url_map)

        assert len(batches) == 2


# ── PipelineResult ────────────────────────────────────────────────────────────

class TestPipelineResult:
    def test_ok_true_when_product_present(self):
        r = PipelineResult(url="u", source="s", product=_product())
        assert r.ok is True

    def test_ok_false_when_no_product(self):
        r = PipelineResult(url="u", source="s", error=Exception("boom"))
        assert r.ok is False

    def test_ok_false_when_skipped_without_cache(self):
        r = PipelineResult(url="u", source="s", skipped=True, product=None)
        assert r.ok is False
