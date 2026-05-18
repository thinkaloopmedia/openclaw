import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.parsers.normalizer import Availability, Product
from src.storage.cache import (
    _dict_to_product,
    _product_to_dict,
    cache_product,
    get_cached_product,
    invalidate,
    is_recently_fetched,
    mark_fetched,
)
from src.storage.db import (
    Base,
    get_price_history,
    get_product,
    list_products,
    upsert_product,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


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


# ── db: upsert + get ──────────────────────────────────────────────────────────

class TestUpsertProduct:
    @pytest.mark.asyncio
    async def test_insert_new_product(self, session: AsyncSession):
        record = await upsert_product(session, _product())
        assert record.id is not None
        assert record.name == "Widget Pro"
        assert record.price == Decimal("29.99")

    @pytest.mark.asyncio
    async def test_insert_creates_initial_price_history(self, session: AsyncSession):
        record = await upsert_product(session, _product())
        history = await get_price_history(session, record.id)
        assert len(history) == 1
        assert history[0].price == Decimal("29.99")

    @pytest.mark.asyncio
    async def test_update_same_price_no_new_history_row(self, session: AsyncSession):
        record = await upsert_product(session, _product())
        await upsert_product(session, _product(name="Widget Pro v2"))
        history = await get_price_history(session, record.id)
        assert len(history) == 1

    @pytest.mark.asyncio
    async def test_update_changed_price_appends_history(self, session: AsyncSession):
        record = await upsert_product(session, _product())
        await upsert_product(session, _product(price=Decimal("24.99")))
        history = await get_price_history(session, record.id)
        assert len(history) == 2
        assert history[0].price == Decimal("29.99")
        assert history[1].price == Decimal("24.99")

    @pytest.mark.asyncio
    async def test_update_reflects_latest_fields(self, session: AsyncSession):
        await upsert_product(session, _product())
        await upsert_product(session, _product(name="Widget Pro Max", availability=Availability.LIMITED))
        fetched = await get_product(session, "https://example.com/p/1")
        assert fetched is not None
        assert fetched.name == "Widget Pro Max"
        assert fetched.availability == "limited"

    @pytest.mark.asyncio
    async def test_extras_stored_as_json(self, session: AsyncSession):
        record = await upsert_product(session, _product(extras={"brand": "Acme", "weight": "1kg"}))
        assert json.loads(record.extras) == {"brand": "Acme", "weight": "1kg"}


class TestGetProduct:
    @pytest.mark.asyncio
    async def test_returns_none_for_unknown_url(self, session: AsyncSession):
        result = await get_product(session, "https://example.com/missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_record_for_known_url(self, session: AsyncSession):
        await upsert_product(session, _product())
        result = await get_product(session, "https://example.com/p/1")
        assert result is not None
        assert result.sku == "WGT-001"


class TestListProducts:
    @pytest.mark.asyncio
    async def test_filter_by_source(self, session: AsyncSession):
        await upsert_product(session, _product(url="https://a.com/1", source="store_a"))
        await upsert_product(session, _product(url="https://b.com/1", source="store_b"))
        results = await list_products(session, source="store_a")
        assert len(results) == 1
        assert results[0].source == "store_a"

    @pytest.mark.asyncio
    async def test_filter_by_availability(self, session: AsyncSession):
        await upsert_product(session, _product(url="https://x.com/1", availability=Availability.IN_STOCK))
        await upsert_product(session, _product(url="https://x.com/2", availability=Availability.OUT_OF_STOCK))
        results = await list_products(session, availability="in_stock")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_limit_and_offset(self, session: AsyncSession):
        for i in range(5):
            await upsert_product(session, _product(url=f"https://example.com/p/{i}"))
        page1 = await list_products(session, limit=2, offset=0)
        page2 = await list_products(session, limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert {r.url for r in page1}.isdisjoint({r.url for r in page2})


# ── cache ─────────────────────────────────────────────────────────────────────

def _mock_redis() -> MagicMock:
    r = MagicMock()
    r.set = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.exists = AsyncMock(return_value=0)
    r.delete = AsyncMock()
    return r


class TestFetchDedup:
    @pytest.mark.asyncio
    async def test_mark_fetched_calls_set_with_ttl(self):
        r = _mock_redis()
        with patch("src.storage.cache.get_redis", return_value=r):
            await mark_fetched("https://example.com/p/1", ttl=600)
        r.set.assert_called_once_with("openclaw:fetched:https://example.com/p/1", "1", ex=600)

    @pytest.mark.asyncio
    async def test_is_recently_fetched_true_when_key_exists(self):
        r = _mock_redis()
        r.exists = AsyncMock(return_value=1)
        with patch("src.storage.cache.get_redis", return_value=r):
            result = await is_recently_fetched("https://example.com/p/1")
        assert result is True

    @pytest.mark.asyncio
    async def test_is_recently_fetched_false_when_key_missing(self):
        r = _mock_redis()
        with patch("src.storage.cache.get_redis", return_value=r):
            result = await is_recently_fetched("https://example.com/p/1")
        assert result is False


class TestProductCache:
    @pytest.mark.asyncio
    async def test_cache_product_stores_json(self):
        r = _mock_redis()
        p = _product()
        with patch("src.storage.cache.get_redis", return_value=r):
            await cache_product(p, ttl=300)
        call_args = r.set.call_args
        key, payload = call_args[0]
        assert key == "openclaw:product:https://example.com/p/1"
        stored = json.loads(payload)
        assert stored["price"] == "29.99"
        assert stored["currency"] == "USD"

    @pytest.mark.asyncio
    async def test_get_cached_product_returns_none_on_miss(self):
        r = _mock_redis()
        with patch("src.storage.cache.get_redis", return_value=r):
            result = await get_cached_product("https://example.com/p/missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_cached_product_roundtrip(self):
        p = _product()
        serialised = json.dumps(_product_to_dict(p))
        r = _mock_redis()
        r.get = AsyncMock(return_value=serialised)
        with patch("src.storage.cache.get_redis", return_value=r):
            result = await get_cached_product(p.url)
        assert result is not None
        assert result.price == Decimal("29.99")
        assert result.availability == Availability.IN_STOCK

    @pytest.mark.asyncio
    async def test_corrupt_cache_entry_discarded(self):
        r = _mock_redis()
        r.get = AsyncMock(return_value="not-valid-json{{")
        with patch("src.storage.cache.get_redis", return_value=r):
            result = await get_cached_product("https://example.com/p/1")
        assert result is None
        r.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalidate_removes_both_keys(self):
        r = _mock_redis()
        with patch("src.storage.cache.get_redis", return_value=r):
            await invalidate("https://example.com/p/1")
        r.delete.assert_called_once_with(
            "openclaw:fetched:https://example.com/p/1",
            "openclaw:product:https://example.com/p/1",
        )


# ── serialisation round-trip ──────────────────────────────────────────────────

class TestSerialisation:
    def test_product_roundtrip(self):
        p = _product(extras={"brand": "Acme"})
        assert _dict_to_product(_product_to_dict(p)) == p

    def test_none_price_roundtrip(self):
        p = _product(price=None, currency=None)
        result = _dict_to_product(_product_to_dict(p))
        assert result.price is None
        assert result.currency is None
