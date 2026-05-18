"""
Root conftest — shared fixtures available to all test modules.
"""

import pytest
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.storage.db import Base
from src.parsers.normalizer import Availability, Product


# ── shared async DB session ───────────────────────────────────────────────────

@pytest.fixture
async def async_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(async_engine):
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


# ── shared product factory ────────────────────────────────────────────────────

def make_product(**overrides) -> Product:
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
    return Product(**{**defaults, **overrides})
