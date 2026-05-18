from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, HttpUrl, field_serializer


# ── products ──────────────────────────────────────────────────────────────────

def _normalize_decimal(v: Decimal | None) -> str | None:
    if v is None:
        return None
    return str(v.normalize())


class PriceHistoryEntry(BaseModel):
    price: Decimal | None
    currency: str | None
    recorded_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("price")
    def serialize_price(self, v: Decimal | None) -> str | None:
        return _normalize_decimal(v)


class ProductResponse(BaseModel):
    id: int
    url: str
    source: str
    name: str | None
    price: Decimal | None
    currency: str | None
    sku: str | None
    availability: str
    description: str | None
    image_url: str | None
    extras: dict[str, str]
    first_seen_at: datetime
    last_seen_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("price")
    def serialize_price(self, v: Decimal | None) -> str | None:
        return _normalize_decimal(v)


class ProductListResponse(BaseModel):
    items: list[ProductResponse]
    count: int
    limit: int
    offset: int


# ── sources ───────────────────────────────────────────────────────────────────

class SourceResponse(BaseModel):
    name: str
    base_url: str
    enabled: bool
    schedule: str
    url_count: int


class SubmitRequest(BaseModel):
    urls: list[str]

    model_config = {"json_schema_extra": {"example": {"urls": ["https://example.com/products/widget"]}}}


class SubmitResponse(BaseModel):
    source: str
    added: int
    total_registered: int


class TriggerResponse(BaseModel):
    source: str
    ok: int
    failed: int
    skipped: int


# ── scheduler jobs ────────────────────────────────────────────────────────────

class JobResponse(BaseModel):
    id: str
    name: str
    next_run: datetime | None
    paused: bool


# ── scoring ───────────────────────────────────────────────────────────────────

class CriterionScoreResponse(BaseModel):
    criterion_id: str
    label: str
    score: float
    weight: float
    rationale: str


class ProductScoreResponse(BaseModel):
    product_id: int
    product: ProductResponse
    total_score: float
    grade: str
    scores: list[CriterionScoreResponse]


class ScoreListResponse(BaseModel):
    items: list[ProductScoreResponse]
    count: int
    limit: int
    offset: int


# ── health ────────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    sources: int
    jobs: int
