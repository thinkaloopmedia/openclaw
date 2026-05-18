import json
import logging
from typing import Annotated, AsyncGenerator

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.orchestrator import Orchestrator
from src.api.schemas import (
    CriterionScoreResponse,
    HealthResponse,
    JobResponse,
    PriceHistoryEntry,
    ProductListResponse,
    ProductResponse,
    ProductScoreResponse,
    ScoreListResponse,
    SourceResponse,
    SubmitRequest,
    SubmitResponse,
    TriggerResponse,
)
from src.parsers.normalizer import Availability, Product
from src.scoring.engine import ScoringConfig, score_product, score_products
from src.storage.db import AsyncSessionFactory, get_price_history, get_product, list_products

logger = logging.getLogger(__name__)

router = APIRouter()


# ── dependencies ──────────────────────────────────────────────────────────────

async def _db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionFactory() as session:
        yield session


def _orchestrator(request: Request) -> Orchestrator:
    orch: Orchestrator | None = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialised")
    return orch


def _scoring_config(request: Request) -> ScoringConfig:
    cfg = getattr(request.app.state, "scoring_config", None)
    if cfg is None:
        raise HTTPException(status_code=503, detail="Scoring config not loaded")
    return cfg


DB = Annotated[AsyncSession, Depends(_db)]
Orch = Annotated[Orchestrator, Depends(_orchestrator)]
ScoringCfg = Annotated[ScoringConfig, Depends(_scoring_config)]


# ── health ────────────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health(orch: Orch) -> HealthResponse:
    return HealthResponse(
        status="ok",
        sources=len(orch.sources),
        jobs=len(orch.jobs),
    )


# ── products ──────────────────────────────────────────────────────────────────

@router.get("/products", response_model=ProductListResponse, tags=["products"])
async def list_products_endpoint(
    db: DB,
    source: str | None = Query(default=None, description="Filter by source name"),
    availability: str | None = Query(default=None, description="Filter by availability status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ProductListResponse:
    records = await list_products(db, source=source, availability=availability, limit=limit, offset=offset)
    items = [_to_product_response(r) for r in records]
    return ProductListResponse(items=items, count=len(items), limit=limit, offset=offset)


@router.get("/products/{product_id}", response_model=ProductResponse, tags=["products"])
async def get_product_endpoint(product_id: int, db: DB) -> ProductResponse:
    from sqlalchemy import select
    from src.storage.db import ProductRecord
    result = await db.execute(select(ProductRecord).where(ProductRecord.id == product_id))
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"Product {product_id} not found")
    return _to_product_response(record)


@router.get("/products/{product_id}/price-history", response_model=list[PriceHistoryEntry], tags=["products"])
async def price_history_endpoint(product_id: int, db: DB) -> list[PriceHistoryEntry]:
    from sqlalchemy import select
    from src.storage.db import ProductRecord
    result = await db.execute(select(ProductRecord).where(ProductRecord.id == product_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail=f"Product {product_id} not found")
    history = await get_price_history(db, product_id)
    return [PriceHistoryEntry.model_validate(h) for h in history]


# ── sources ───────────────────────────────────────────────────────────────────

@router.get("/sources", response_model=list[SourceResponse], tags=["sources"])
async def list_sources(orch: Orch) -> list[SourceResponse]:
    registry = orch.url_registry
    return [
        SourceResponse(
            name=src.name,
            base_url=src.base_url,
            enabled=src.enabled,
            schedule=src.schedule,
            url_count=len(registry.get(src.name, [])),
        )
        for src in orch.sources.values()
    ]


@router.post("/sources/{source_name}/submit", response_model=SubmitResponse, tags=["sources"])
async def submit_urls(source_name: str, body: SubmitRequest, orch: Orch) -> SubmitResponse:
    if source_name not in orch.sources:
        raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")
    added = orch.submit(source_name, body.urls)
    total = len(orch.url_registry.get(source_name, []))
    return SubmitResponse(source=source_name, added=added, total_registered=total)


@router.post("/sources/{source_name}/trigger", response_model=TriggerResponse, tags=["sources"])
async def trigger_source(source_name: str, orch: Orch) -> TriggerResponse:
    if source_name not in orch.sources:
        raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")
    batch = await orch.trigger_now(source_name)
    if batch is None:
        raise HTTPException(status_code=422, detail=f"Source '{source_name}' has no URLs registered")
    return TriggerResponse(
        source=source_name,
        ok=len(batch.succeeded),
        failed=len(batch.failed),
        skipped=len(batch.skipped),
    )


@router.post("/sources/{source_name}/pause", status_code=204, tags=["sources"])
async def pause_source(source_name: str, orch: Orch) -> None:
    if source_name not in orch.sources:
        raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")
    orch.pause(source_name)


@router.post("/sources/{source_name}/resume", status_code=204, tags=["sources"])
async def resume_source(source_name: str, orch: Orch) -> None:
    if source_name not in orch.sources:
        raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")
    orch.resume(source_name)


# ── scheduler jobs ────────────────────────────────────────────────────────────

@router.get("/jobs", response_model=list[JobResponse], tags=["system"])
async def list_jobs(orch: Orch) -> list[JobResponse]:
    return [JobResponse(**job) for job in orch.jobs]


# ── scores ────────────────────────────────────────────────────────────────────

@router.get("/products/{product_id}/score", response_model=ProductScoreResponse, tags=["scores"])
async def score_product_endpoint(product_id: int, db: DB, cfg: ScoringCfg) -> ProductScoreResponse:
    from sqlalchemy import select
    from src.storage.db import ProductRecord
    result = await db.execute(select(ProductRecord).where(ProductRecord.id == product_id))
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"Product {product_id} not found")
    scored = score_product(_record_to_product(record), cfg)
    return _to_score_response(product_id, record, scored)


@router.get("/scores", response_model=ScoreListResponse, tags=["scores"])
async def list_scores(
    db: DB,
    cfg: ScoringCfg,
    source: str | None = Query(default=None),
    min_score: float = Query(default=0.0, ge=0.0, le=10.0, description="Minimum total score"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ScoreListResponse:
    # Fetch up to 500 records, score them all, then page the results
    records = await list_products(db, source=source, limit=500, offset=0)
    record_by_url = {r.url: r for r in records}
    products = [_record_to_product(r) for r in records]
    all_scored = score_products(products, cfg)  # sorted best-first

    filtered = [s for s in all_scored if s.total_score >= min_score]
    page = filtered[offset: offset + limit]

    items = [
        _to_score_response(record_by_url[s.product.url].id, record_by_url[s.product.url], s)
        for s in page
    ]
    return ScoreListResponse(items=items, count=len(filtered), limit=limit, offset=offset)


# ── helpers ───────────────────────────────────────────────────────────────────

def _to_product_response(record) -> ProductResponse:
    extras = {}
    try:
        extras = json.loads(record.extras or "{}")
    except (ValueError, TypeError):
        pass
    return ProductResponse(
        id=record.id,
        url=record.url,
        source=record.source,
        name=record.name,
        price=record.price,
        currency=record.currency,
        sku=record.sku,
        availability=record.availability,
        description=record.description,
        image_url=record.image_url,
        extras=extras,
        first_seen_at=record.first_seen_at,
        last_seen_at=record.last_seen_at,
    )


def _record_to_product(record) -> Product:
    extras = {}
    try:
        extras = json.loads(record.extras or "{}")
    except (ValueError, TypeError):
        pass
    return Product(
        url=record.url,
        source=record.source,
        name=record.name,
        price=record.price,
        currency=record.currency,
        sku=record.sku,
        availability=Availability(record.availability),
        description=record.description,
        image_url=record.image_url,
        extras=extras,
    )


def _to_score_response(product_id: int, record, scored) -> ProductScoreResponse:
    return ProductScoreResponse(
        product_id=product_id,
        product=_to_product_response(record),
        total_score=scored.total_score,
        grade=scored.grade,
        scores=[
            CriterionScoreResponse(
                criterion_id=s.criterion_id,
                label=s.label,
                score=s.score,
                weight=s.weight,
                rationale=s.rationale,
            )
            for s in scored.scores
        ],
    )
