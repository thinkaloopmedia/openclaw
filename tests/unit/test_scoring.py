import textwrap
from decimal import Decimal
from pathlib import Path

import pytest

from src.parsers.normalizer import Availability, Product
from src.scoring.criteria import (
    score_durability,
    score_evergreen,
    score_manual,
    score_markup,
    score_premium,
    score_price_range,
    score_rating,
    score_single_sku,
    score_size,
    score_weight,
)
from src.scoring.engine import (
    ScoringConfig,
    CriterionConfig,
    load_scoring_config,
    score_product,
    score_products,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _product(**overrides) -> Product:
    defaults = dict(
        url="https://example.com/p/1",
        source="test",
        name="Premium Compact Widget",
        price=Decimal("62.00"),
        currency="USD",
        sku="WGT-001",
        availability=Availability.IN_STOCK,
        description="A small, lightweight everyday essential.",
        image_url=None,
        extras={},
    )
    return Product(**{**defaults, **overrides})


def _config(*ids) -> ScoringConfig:
    """Build a minimal ScoringConfig with the given criterion IDs."""
    scorer_map = {
        "price_range": ("price_range", {"min": 50, "max": 75}),
        "high_ratings": ("rating_score", {"min_stars": 4.0, "rating_field": "rating"}),
        "fits_in_shoebox": ("size_estimate", {}),
        "non_breakable": ("durability", {}),
        "looks_premium": ("premium_look", {}),
        "under_2lbs": ("weight_estimate", {}),
        "single_sku": ("single_sku", {}),
        "markup_4x": ("markup_potential", {"target_multiple": 4.0}),
        "evergreen": ("evergreen", {}),
        "community_interest": ("manual", {"default_score": 5.0}),
    }
    criteria = []
    for cid in ids:
        scorer_name, params = scorer_map[cid]
        criteria.append(CriterionConfig(id=cid, label=cid, scorer=scorer_name, params=params))
    return ScoringConfig(criteria=criteria)


# ── price_range ───────────────────────────────────────────────────────────────

class TestPriceRange:
    def test_midpoint_scores_10(self):
        score, _ = score_price_range(_product(price=Decimal("62.50")), {"min": 50, "max": 75})
        assert score == 10.0

    def test_inside_range_scores_high(self):
        score, _ = score_price_range(_product(price=Decimal("55")), {"min": 50, "max": 75})
        assert score >= 8.0

    def test_below_range_loses_points(self):
        score, rationale = score_price_range(_product(price=Decimal("30")), {"min": 50, "max": 75})
        assert score < 8.0
        assert "below" in rationale

    def test_above_range_loses_points(self):
        score, rationale = score_price_range(_product(price=Decimal("100")), {"min": 50, "max": 75})
        assert score < 8.0
        assert "above" in rationale

    def test_no_price_returns_neutral(self):
        score, _ = score_price_range(_product(price=None), {"min": 50, "max": 75})
        assert score == 5.0


# ── rating_score ──────────────────────────────────────────────────────────────

class TestRatingScore:
    def test_five_stars_scores_10(self):
        p = _product(extras={"rating": "star-rating Five"})
        score, rationale = score_rating(p, {"rating_field": "rating", "min_stars": 4.0})
        assert score == 10.0
        assert "5" in rationale

    def test_four_stars_scores_8(self):
        p = _product(extras={"rating": "star-rating Four"})
        score, _ = score_rating(p, {"rating_field": "rating", "min_stars": 4.0})
        assert score == 8.0

    def test_three_stars_below_threshold(self):
        p = _product(extras={"rating": "star-rating Three"})
        score, rationale = score_rating(p, {"rating_field": "rating", "min_stars": 4.0})
        assert score == 6.0
        assert "below" in rationale

    def test_numeric_rating_parsed(self):
        p = _product(extras={"rating": "4.5"})
        score, _ = score_rating(p, {"rating_field": "rating", "min_stars": 4.0})
        assert score == 9.0

    def test_missing_rating_returns_neutral(self):
        score, _ = score_rating(_product(), {"rating_field": "rating"})
        assert score == 5.0


# ── size_estimate ─────────────────────────────────────────────────────────────

class TestSizeEstimate:
    def test_compact_keyword_scores_high(self):
        p = _product(name="Compact Pocket Organiser")
        score, _ = score_size(p, {})
        assert score >= 8.0

    def test_large_keyword_scores_low(self):
        p = _product(name="Large Oversized Storage Box", description="A product.")
        score, _ = score_size(p, {})
        assert score <= 4.0

    def test_no_keywords_neutral(self):
        p = _product(name="Widget Pro", description="A product.")
        score, _ = score_size(p, {})
        assert score == 5.0


# ── durability ────────────────────────────────────────────────────────────────

class TestDurability:
    def test_metal_scores_high(self):
        p = _product(name="Stainless Steel Water Bottle")
        score, _ = score_durability(p, {})
        assert score >= 8.0

    def test_glass_scores_low(self):
        p = _product(name="Handmade Glass Vase")
        score, _ = score_durability(p, {})
        assert score <= 4.0

    def test_no_material_keywords_neutral(self):
        score, _ = score_durability(_product(name="Widget", description=""), {})
        assert score == 5.0


# ── premium_look ──────────────────────────────────────────────────────────────

class TestPremiumLook:
    def test_premium_keywords_score_high(self):
        p = _product(name="Premium Luxury Professional Organiser")
        score, _ = score_premium(p, {})
        assert score >= 9.0

    def test_budget_keywords_score_low(self):
        p = _product(name="Budget Economy Basic Case")
        score, _ = score_premium(p, {})
        assert score <= 4.0


# ── weight_estimate ───────────────────────────────────────────────────────────

class TestWeightEstimate:
    def test_under_2lbs_scores_high(self):
        p = _product(description="Weighs only 0.8 lbs.")
        score, rationale = score_weight(p, {})
        assert score >= 8.0
        assert "under" in rationale

    def test_over_2lbs_scores_low(self):
        p = _product(description="Weight: 5 lbs")
        score, rationale = score_weight(p, {})
        assert score < 6.0
        assert "exceeds" in rationale

    def test_grams_converted(self):
        p = _product(description="Only 400 grams")
        score, _ = score_weight(p, {})
        assert score >= 8.0

    def test_kg_over_limit(self):
        p = _product(description="2.5 kg unit")
        score, _ = score_weight(p, {})
        assert score < 6.0

    def test_no_weight_neutral(self):
        score, _ = score_weight(_product(), {})
        assert score == 5.0


# ── single_sku ────────────────────────────────────────────────────────────────

class TestSingleSku:
    def test_sku_no_variants_scores_high(self):
        score, _ = score_single_sku(_product(), {})
        assert score >= 7.0

    def test_variant_signals_penalise(self):
        p = _product(description="Available in sizes: S / M / L")
        score, rationale = score_single_sku(p, {})
        assert score < 7.0
        assert "variant" in rationale

    def test_no_sku_but_no_variants_is_ok(self):
        p = _product(sku=None, description="Single item.")
        score, _ = score_single_sku(p, {})
        assert score >= 5.0


# ── markup_potential ──────────────────────────────────────────────────────────

class TestMarkupPotential:
    def test_good_price_range_scores_well(self):
        score, _ = score_markup(_product(price=Decimal("65")), {"target_multiple": 4.0})
        assert score >= 7.0

    def test_actual_cost_used_when_available(self):
        p = _product(price=Decimal("60"), extras={"cost": "12.00"})
        score, rationale = score_markup(p, {"target_multiple": 4.0, "cost_field": "cost"})
        assert score >= 9.0      # 60/12 = 5× > 4× target
        assert "5.0×" in rationale

    def test_very_low_price_scores_low(self):
        score, _ = score_markup(_product(price=Decimal("8")), {"target_multiple": 4.0})
        assert score < 5.0

    def test_no_price_neutral(self):
        score, _ = score_markup(_product(price=None), {})
        assert score == 5.0


# ── evergreen ─────────────────────────────────────────────────────────────────

class TestEvergreen:
    def test_seasonal_keyword_penalised(self):
        p = _product(name="Christmas Holiday Special Edition")
        score, rationale = score_evergreen(p, {})
        assert score <= 3.0
        assert "seasonal" in rationale

    def test_evergreen_keyword_rewarded(self):
        p = _product(name="Everyday Essential Classic Organiser")
        score, _ = score_evergreen(p, {})
        assert score >= 8.0

    def test_trend_keyword_penalised(self):
        p = _product(name="Trending Viral TikTok Gadget", description="A product.")
        score, rationale = score_evergreen(p, {})
        assert score <= 5.0

    def test_neutral_product_assumed_evergreen(self):
        score, _ = score_evergreen(_product(name="Widget"), {})
        assert score >= 5.0


# ── manual ────────────────────────────────────────────────────────────────────

class TestManual:
    def test_returns_default_score(self):
        score, rationale = score_manual(_product(), {"default_score": 7.0})
        assert score == 7.0
        assert "manual" in rationale.lower()

    def test_default_is_5(self):
        score, _ = score_manual(_product(), {})
        assert score == 5.0


# ── engine ────────────────────────────────────────────────────────────────────

class TestScoringEngine:
    def test_score_product_returns_all_criteria(self):
        config = _config("price_range", "high_ratings", "evergreen")
        result = score_product(_product(), config)
        assert len(result.scores) == 3

    def test_total_is_weighted_mean(self):
        config = ScoringConfig(criteria=[
            CriterionConfig(id="a", label="A", scorer="price_range", weight=2.0, params={"min": 50, "max": 75}),
            CriterionConfig(id="b", label="B", scorer="manual", weight=1.0, params={"default_score": 5.0}),
        ])
        result = score_product(_product(price=Decimal("62.50")), config)
        # price_range at $62.50 (midpoint) → 10.0, manual → 5.0
        # weighted mean = (10*2 + 5*1) / 3 = 25/3 ≈ 8.33
        assert abs(result.total_score - 8.33) < 0.1

    def test_grade_boundaries(self):
        config = _config("community_interest")  # manual → 5.0

        for default, expected_grade in [(9.0, "A"), (7.0, "B"), (5.5, "C"), (3.5, "D"), (1.5, "F")]:
            cfg = ScoringConfig(criteria=[
                CriterionConfig(id="m", label="m", scorer="manual", weight=1.0, params={"default_score": default})
            ])
            result = score_product(_product(), cfg)
            assert result.grade == expected_grade, f"score={default} should be {expected_grade}"

    def test_score_products_sorted_best_first(self):
        cheap = _product(url="https://example.com/cheap", price=Decimal("5"))
        good = _product(url="https://example.com/good", price=Decimal("62"))
        config = _config("price_range")
        results = score_products([cheap, good], config)
        assert results[0].product.url == "https://example.com/good"

    def test_unknown_scorer_falls_back_to_neutral(self):
        config = ScoringConfig(criteria=[
            CriterionConfig(id="x", label="X", scorer="nonexistent_scorer", weight=1.0)
        ])
        result = score_product(_product(), config)
        assert result.scores[0].score == 5.0

    def test_load_scoring_config_from_yaml(self, tmp_path: Path):
        yaml_content = textwrap.dedent("""\
            scoring:
              criteria:
                - id: price_range
                  label: "Sells for $50-$75"
                  weight: 1.0
                  scorer: price_range
                  params:
                    min: 50
                    max: 75
            sources: []
        """)
        p = tmp_path / "sources.yaml"
        p.write_text(yaml_content)
        config = load_scoring_config(p)
        assert len(config.criteria) == 1
        assert config.criteria[0].id == "price_range"
        assert config.criteria[0].params == {"min": 50, "max": 75}

    def test_str_output(self):
        config = _config("price_range")
        result = score_product(_product(price=Decimal("62.50")), config)
        output = str(result)
        assert "10.0" in output
        assert "Grade" in output
