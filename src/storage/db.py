"""
SQLAlchemy async ORM — models, engine/session factory, and CRUD helpers.

Schema:
  products       — one row per unique URL, always reflects latest state
  price_history  — append-only log; a row is added whenever the price changes
"""

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config.settings import settings
from src.parsers.normalizer import Product

logger = logging.getLogger(__name__)


# ── engine + session factory ─────────────────────────────────────────────────

def _make_url(database_url: str) -> str:
    """Convert sync SQLite URL to its async equivalent."""
    if database_url.startswith("sqlite:///"):
        return database_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return database_url


engine = create_async_engine(_make_url(settings.database_url), echo=False)
AsyncSessionFactory = async_sessionmaker(engine, expire_on_commit=False)


def get_session() -> AsyncSession:
    return AsyncSessionFactory()


# ── ORM models ───────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class ProductRecord(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, unique=True, index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    currency: Mapped[str | None] = mapped_column(String(3))
    sku: Mapped[str | None] = mapped_column(String(128), index=True)
    availability: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    description: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    extras: Mapped[str] = mapped_column(Text, nullable=False, default="{}")  # JSON string
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    price_history: Mapped[list["PriceHistory"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", lazy="select"
    )


class PriceHistory(Base):
    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    currency: Mapped[str | None] = mapped_column(String(3))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    product: Mapped["ProductRecord"] = relationship(back_populates="price_history")


# ── schema management ─────────────────────────────────────────────────────────

async def create_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# ── CRUD ─────────────────────────────────────────────────────────────────────

async def upsert_product(session: AsyncSession, product: Product) -> ProductRecord:
    """
    Insert a new product or update an existing one (matched by URL).
    Appends to price_history whenever the price changes.
    Returns the persisted record.
    """
    now = datetime.now(timezone.utc)
    extras_json = json.dumps(product.extras)

    result = await session.execute(select(ProductRecord).where(ProductRecord.url == product.url))
    record = result.scalar_one_or_none()

    if record is None:
        record = ProductRecord(
            url=product.url,
            source=product.source,
            name=product.name,
            price=product.price,
            currency=product.currency,
            sku=product.sku,
            availability=product.availability,
            description=product.description,
            image_url=product.image_url,
            extras=extras_json,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(record)
        await session.flush()  # populate record.id

        session.add(PriceHistory(
            product_id=record.id,
            price=product.price,
            currency=product.currency,
            recorded_at=now,
        ))
        logger.debug("Inserted new product: %s", product.url)

    else:
        price_changed = record.price != product.price or record.currency != product.currency
        record.name = product.name
        record.price = product.price
        record.currency = product.currency
        record.sku = product.sku
        record.availability = product.availability
        record.description = product.description
        record.image_url = product.image_url
        record.extras = extras_json
        record.last_seen_at = now

        if price_changed:
            session.add(PriceHistory(
                product_id=record.id,
                price=product.price,
                currency=product.currency,
                recorded_at=now,
            ))
            logger.debug("Price changed for %s: %s → %s", product.url, record.price, product.price)

    await session.commit()
    return record


async def get_product(session: AsyncSession, url: str) -> ProductRecord | None:
    result = await session.execute(select(ProductRecord).where(ProductRecord.url == url))
    return result.scalar_one_or_none()


async def list_products(
    session: AsyncSession,
    *,
    source: str | None = None,
    availability: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[ProductRecord]:
    stmt = select(ProductRecord)
    if source:
        stmt = stmt.where(ProductRecord.source == source)
    if availability:
        stmt = stmt.where(ProductRecord.availability == availability)
    stmt = stmt.order_by(ProductRecord.last_seen_at.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_price_history(session: AsyncSession, product_id: int) -> list[PriceHistory]:
    result = await session.execute(
        select(PriceHistory)
        .where(PriceHistory.product_id == product_id)
        .order_by(PriceHistory.recorded_at.asc())
    )
    return list(result.scalars().all())
