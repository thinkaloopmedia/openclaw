import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.routes import _db, _orchestrator
from src.agents.orchestrator import Orchestrator
from src.pipeline.runner import SourceConfig
from src.parsers.normalizer import Availability, Product
from src.storage.db import Base, upsert_product


# ── in-memory DB + test app ───────────────────────────────────────────────────

@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _mock_orchestrator(sources: list[SourceConfig] | None = None, jobs: list | None = None) -> MagicMock:
    orch = MagicMock(spec=Orchestrator)
    srcs = {s.name: s for s in (sources or [])}
    orch.sources = srcs
    orch.jobs = jobs or []
    orch.url_registry = {s.name: list(s.urls) for s in (sources or [])}
    orch.submit = MagicMock(return_value=2)
    orch.trigger_now = AsyncMock(return_value=None)
    orch.pause = MagicMock()
    orch.resume = MagicMock()
    return orch


def _bare_app(session: AsyncSession, orch: MagicMock):
    from fastapi import FastAPI
    from src.api.routes import router

    # Build a proper async generator function that closes over `session`
    def _make_db_override(s: AsyncSession):
        async def _override():
            yield s
        return _override

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[_db] = _make_db_override(session)
    app.dependency_overrides[_orchestrator] = lambda: orch
    return app


@pytest.fixture
def source() -> SourceConfig:
    return SourceConfig(
        name="test_store",
        base_url="https://example.com",
        enabled=True,
        schedule="0 */6 * * *",
        selectors={},
        urls=["https://example.com/p/1", "https://example.com/p/2"],
    )


@pytest.fixture
async def client(db_session, source):
    orch = _mock_orchestrator(sources=[source])
    app = _bare_app(db_session, orch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, db_session, orch


def _product_obj(url: str = "https://example.com/p/1") -> Product:
    return Product(
        url=url,
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


# ── /health ───────────────────────────────────────────────────────────────────

class TestHealth:
    @pytest.mark.asyncio
    async def test_returns_ok(self, client):
        c, _, orch = client
        orch.sources = {"s": MagicMock()}
        orch.jobs = [{}]
        resp = await c.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["sources"] == 1
        assert data["jobs"] == 1


# ── /products ─────────────────────────────────────────────────────────────────

class TestListProducts:
    @pytest.mark.asyncio
    async def test_empty_returns_empty_list(self, client):
        c, _, _ = client
        resp = await c.get("/products")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["count"] == 0

    @pytest.mark.asyncio
    async def test_returns_inserted_products(self, client):
        c, session, _ = client
        await upsert_product(session, _product_obj())
        resp = await c.get("/products")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["name"] == "Widget Pro"
        assert items[0]["price"] == "29.99"

    @pytest.mark.asyncio
    async def test_filter_by_source(self, client):
        c, session, _ = client
        await upsert_product(session, _product_obj())
        resp = await c.get("/products", params={"source": "test_store"})
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

        resp2 = await c.get("/products", params={"source": "other_store"})
        assert resp2.json()["count"] == 0

    @pytest.mark.asyncio
    async def test_filter_by_availability(self, client):
        c, session, _ = client
        await upsert_product(session, _product_obj())
        resp = await c.get("/products", params={"availability": "in_stock"})
        assert resp.json()["count"] == 1

        resp2 = await c.get("/products", params={"availability": "out_of_stock"})
        assert resp2.json()["count"] == 0

    @pytest.mark.asyncio
    async def test_pagination(self, client):
        c, session, _ = client
        for i in range(5):
            await upsert_product(session, _product_obj(url=f"https://example.com/p/{i}"))
        resp = await c.get("/products", params={"limit": 2, "offset": 0})
        assert resp.json()["count"] == 2
        resp2 = await c.get("/products", params={"limit": 2, "offset": 2})
        assert resp2.json()["count"] == 2


class TestGetProduct:
    @pytest.mark.asyncio
    async def test_returns_product(self, client):
        c, session, _ = client
        record = await upsert_product(session, _product_obj())
        resp = await c.get(f"/products/{record.id}")
        assert resp.status_code == 200
        assert resp.json()["sku"] == "WGT-001"

    @pytest.mark.asyncio
    async def test_404_for_unknown_id(self, client):
        c, _, _ = client
        resp = await c.get("/products/99999")
        assert resp.status_code == 404


class TestPriceHistory:
    @pytest.mark.asyncio
    async def test_returns_history_entries(self, client):
        c, session, _ = client
        record = await upsert_product(session, _product_obj())
        # Trigger a price change
        changed = _product_obj()
        changed.price = Decimal("24.99")
        await upsert_product(session, changed)
        resp = await c.get(f"/products/{record.id}/price-history")
        assert resp.status_code == 200
        history = resp.json()
        assert len(history) == 2
        assert history[0]["price"] == "29.99"
        assert history[1]["price"] == "24.99"

    @pytest.mark.asyncio
    async def test_404_for_unknown_product(self, client):
        c, _, _ = client
        resp = await c.get("/products/99999/price-history")
        assert resp.status_code == 404


# ── /sources ──────────────────────────────────────────────────────────────────

class TestListSources:
    @pytest.mark.asyncio
    async def test_returns_source_list(self, client):
        c, _, _ = client
        resp = await c.get("/sources")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "test_store"
        assert data[0]["url_count"] == 2


class TestSubmitUrls:
    @pytest.mark.asyncio
    async def test_submit_calls_orchestrator(self, client):
        c, _, orch = client
        orch.submit.return_value = 3
        orch.url_registry = {"test_store": ["u1", "u2", "u3"]}
        resp = await c.post(
            "/sources/test_store/submit",
            json={"urls": ["https://example.com/p/3", "https://example.com/p/4", "https://example.com/p/5"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["added"] == 3
        assert data["source"] == "test_store"
        orch.submit.assert_called_once()

    @pytest.mark.asyncio
    async def test_submit_404_for_unknown_source(self, client):
        c, _, _ = client
        resp = await c.post("/sources/ghost/submit", json={"urls": ["https://x.com"]})
        assert resp.status_code == 404


class TestTriggerSource:
    @pytest.mark.asyncio
    async def test_trigger_returns_counts(self, client):
        c, _, orch = client
        from src.pipeline.runner import PipelineResult, BatchResult
        from src.retrieval.fetcher import FetchError

        ok = PipelineResult(url="u1", source="test_store", product=_product_obj())
        err = PipelineResult(url="u2", source="test_store", error=FetchError("u2", "x"))
        skip = PipelineResult(url="u3", source="test_store", skipped=True)
        batch = BatchResult(source="test_store", results=[ok, err, skip])
        orch.trigger_now = AsyncMock(return_value=batch)

        resp = await c.post("/sources/test_store/trigger")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] == 1
        assert data["failed"] == 1
        assert data["skipped"] == 1

    @pytest.mark.asyncio
    async def test_trigger_422_when_no_urls(self, client):
        c, _, orch = client
        orch.trigger_now = AsyncMock(return_value=None)
        resp = await c.post("/sources/test_store/trigger")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_trigger_404_for_unknown_source(self, client):
        c, _, _ = client
        resp = await c.post("/sources/ghost/trigger")
        assert resp.status_code == 404


class TestPauseResume:
    @pytest.mark.asyncio
    async def test_pause_204(self, client):
        c, _, orch = client
        resp = await c.post("/sources/test_store/pause")
        assert resp.status_code == 204
        orch.pause.assert_called_once_with("test_store")

    @pytest.mark.asyncio
    async def test_resume_204(self, client):
        c, _, orch = client
        resp = await c.post("/sources/test_store/resume")
        assert resp.status_code == 204
        orch.resume.assert_called_once_with("test_store")

    @pytest.mark.asyncio
    async def test_pause_404_for_unknown_source(self, client):
        c, _, _ = client
        resp = await c.post("/sources/ghost/pause")
        assert resp.status_code == 404


# ── /jobs ─────────────────────────────────────────────────────────────────────

class TestListJobs:
    @pytest.mark.asyncio
    async def test_returns_job_list(self, client):
        c, _, orch = client
        orch.jobs = [
            {"id": "test_store", "name": "openclaw:test_store", "next_run": None, "paused": False}
        ]
        resp = await c.get("/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "test_store"
        assert data[0]["paused"] is False
