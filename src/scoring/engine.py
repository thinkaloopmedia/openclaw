"""
Scoring engine.

Usage:
    config = load_scoring_config()          # reads sources.yaml scoring block
    scored = score_product(product, config)
    print(scored.total_score, scored.grade)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.parsers.normalizer import Product
from src.scoring.criteria import get_scorer

logger = logging.getLogger(__name__)


# ── config types ──────────────────────────────────────────────────────────────

@dataclass
class CriterionConfig:
    id: str
    label: str
    scorer: str
    weight: float = 1.0
    params: dict = field(default_factory=dict)


@dataclass
class ScoringConfig:
    criteria: list[CriterionConfig]


# ── result types ──────────────────────────────────────────────────────────────

@dataclass
class CriterionScore:
    criterion_id: str
    label: str
    score: float        # 1.0 – 10.0
    weight: float
    rationale: str


@dataclass
class ScoredProduct:
    product: Product
    scores: list[CriterionScore]
    total_score: float  # weighted mean, 1.0 – 10.0
    grade: str          # A / B / C / D / F

    def __str__(self) -> str:
        lines = [
            f"{self.product.name or self.product.url}",
            f"  Total: {self.total_score:.1f}/10  Grade: {self.grade}",
        ]
        for s in self.scores:
            bar = "█" * int(s.score) + "░" * (10 - int(s.score))
            lines.append(f"  [{bar}] {s.score:4.1f}  {s.label}")
            lines.append(f"              → {s.rationale}")
        return "\n".join(lines)


# ── config loader ─────────────────────────────────────────────────────────────

def load_scoring_config(path: str | Path = "config/sources.yaml") -> ScoringConfig:
    data = yaml.safe_load(Path(path).read_text()) or {}
    raw_criteria = data.get("scoring", {}).get("criteria", [])
    criteria = []
    for entry in raw_criteria:
        try:
            criteria.append(CriterionConfig(
                id=entry["id"],
                label=entry["label"],
                scorer=entry["scorer"],
                weight=float(entry.get("weight", 1.0)),
                params=entry.get("params") or {},
            ))
        except KeyError as exc:
            logger.warning("Skipping malformed scoring criterion (missing %s): %r", exc, entry)
    return ScoringConfig(criteria=criteria)


# ── scoring ───────────────────────────────────────────────────────────────────

def score_product(product: Product, config: ScoringConfig) -> ScoredProduct:
    """Score a single product against all configured criteria."""
    criterion_scores: list[CriterionScore] = []

    for criterion in config.criteria:
        try:
            scorer_fn = get_scorer(criterion.scorer)
            raw_score, rationale = scorer_fn(product, criterion.params)
        except KeyError as exc:
            logger.warning("Criterion %r: %s", criterion.id, exc)
            raw_score, rationale = 5.0, f"scorer {criterion.scorer!r} not found"
        except Exception as exc:
            logger.warning("Criterion %r raised an error: %s", criterion.id, exc)
            raw_score, rationale = 5.0, f"scorer error: {exc}"

        criterion_scores.append(CriterionScore(
            criterion_id=criterion.id,
            label=criterion.label,
            score=raw_score,
            weight=criterion.weight,
            rationale=rationale,
        ))

    total = _weighted_mean(criterion_scores)
    return ScoredProduct(
        product=product,
        scores=criterion_scores,
        total_score=round(total, 2),
        grade=_grade(total),
    )


def score_products(products: list[Product], config: ScoringConfig) -> list[ScoredProduct]:
    """Score a list of products, sorted best-first."""
    scored = [score_product(p, config) for p in products]
    return sorted(scored, key=lambda s: s.total_score, reverse=True)


# ── helpers ───────────────────────────────────────────────────────────────────

def _weighted_mean(scores: list[CriterionScore]) -> float:
    total_weight = sum(s.weight for s in scores)
    if total_weight == 0:
        return 5.0
    return sum(s.score * s.weight for s in scores) / total_weight


def _grade(score: float) -> str:
    if score >= 8.0:
        return "A"
    if score >= 6.5:
        return "B"
    if score >= 5.0:
        return "C"
    if score >= 3.0:
        return "D"
    return "F"
