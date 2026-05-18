"""
Individual criterion scorer functions.

Each scorer has the signature:
    scorer(product: Product, params: dict) -> tuple[float, str]
returning a score 1.0–10.0 and a one-line rationale.

Scorers that cannot be determined from available product data return 5.0
(neutral) with an explanation so the total score isn't artificially dragged
down by missing information.
"""

import re
from decimal import Decimal
from typing import Callable

from src.parsers.normalizer import Product

ScorerFn = Callable[[Product, dict], tuple[float, str]]


# ── registry ──────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, ScorerFn] = {}


def scorer(name: str):
    def _decorator(fn: ScorerFn) -> ScorerFn:
        _REGISTRY[name] = fn
        return fn
    return _decorator


def get_scorer(name: str) -> ScorerFn:
    if name not in _REGISTRY:
        raise KeyError(f"Unknown scorer: {name!r}. Available: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


# ── helpers ───────────────────────────────────────────────────────────────────

def _text(product: Product) -> str:
    """Concatenate all text fields for keyword searches."""
    parts = [product.name or "", product.description or ""]
    parts += list(product.extras.values())
    return " ".join(parts).lower()


def _keyword_score(text: str, positive: list[str], negative: list[str]) -> tuple[float, str]:
    """Return a 1–10 score based on keyword presence."""
    pos_hits = [kw for kw in positive if kw in text]
    neg_hits = [kw for kw in negative if kw in text]

    if neg_hits and not pos_hits:
        score = max(1.0, 4.0 - len(neg_hits))
        return score, f"negative signals: {neg_hits}"
    if pos_hits and not neg_hits:
        score = min(10.0, 7.0 + len(pos_hits))
        return score, f"positive signals: {pos_hits}"
    if pos_hits and neg_hits:
        return 5.0, f"mixed signals — positive: {pos_hits}, negative: {neg_hits}"
    return 5.0, "no clear signals in text"


def _clamp(value: float, lo: float = 1.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, value))


# ── scorers ───────────────────────────────────────────────────────────────────

@scorer("price_range")
def score_price_range(product: Product, params: dict) -> tuple[float, str]:
    """Full 10 inside the target window; tapers linearly outside it."""
    if product.price is None:
        return 5.0, "no price available"

    price = float(product.price)
    lo, hi = float(params.get("min", 50)), float(params.get("max", 75))
    mid = (lo + hi) / 2
    half_width = (hi - lo) / 2

    if lo <= price <= hi:
        # Peak score inside window: 10 at midpoint, 8 at edges
        proximity = 1.0 - abs(price - mid) / half_width
        score = 8.0 + 2.0 * proximity
        return round(score, 1), f"${price:.2f} is within ${lo}–${hi} target"

    if price < lo:
        gap = lo - price
        score = _clamp(8.0 - gap / 5.0)
        return round(score, 1), f"${price:.2f} is ${gap:.0f} below ${lo} floor"

    gap = price - hi
    score = _clamp(8.0 - gap / 5.0)
    return round(score, 1), f"${price:.2f} is ${gap:.0f} above ${hi} ceiling"


@scorer("rating_score")
def score_rating(product: Product, params: dict) -> tuple[float, str]:
    """
    Parse star rating from extras or a numeric field.
    Supports 'star-rating Four' style strings and plain floats/ints.
    """
    field = params.get("rating_field", "rating")
    raw = product.extras.get(field) or ""
    min_stars = float(params.get("min_stars", 4.0))

    # 'star-rating Three' style
    word_map = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    for word, val in word_map.items():
        if word in raw.lower():
            stars = float(val)
            score = _clamp((stars / 5.0) * 10.0)
            qualifier = "meets" if stars >= min_stars else "below"
            return round(score, 1), f"{stars:.0f}-star rating ({qualifier} {min_stars:.0f}★ threshold)"

    # Numeric float/int
    numeric = re.search(r"(\d+\.?\d*)", raw)
    if numeric:
        stars = float(numeric.group(1))
        # Normalise: assume 5-star scale unless value > 6 (then assume 10-scale)
        if stars > 6:
            stars = stars / 2.0
        score = _clamp((stars / 5.0) * 10.0)
        return round(score, 1), f"{stars:.1f}-star parsed from {raw!r}"

    return 5.0, "no rating data available — neutral score"


@scorer("size_estimate")
def score_size(product: Product, params: dict) -> tuple[float, str]:
    """Keyword inference for whether the product fits in a shoebox."""
    text = _text(product)
    positive = params.get("keywords_positive", [
        "small", "compact", "mini", "pocket", "portable", "lightweight",
        "travel", "wallet", "slim", "thin", "tiny", "hand-held", "handheld",
    ])
    negative = params.get("keywords_negative", [
        "large", "oversized", "bulky", "xl", "xxl", "king", "queen",
        "full-size", "heavy", "massive", "giant", "big",
    ])
    return _keyword_score(text, positive, negative)


@scorer("durability")
def score_durability(product: Product, params: dict) -> tuple[float, str]:
    """Keyword inference for material durability / breakability."""
    text = _text(product)
    positive = params.get("keywords_positive", [
        "metal", "aluminum", "aluminium", "steel", "stainless", "titanium",
        "silicone", "rubber", "plastic", "durable", "unbreakable", "rugged",
        "shockproof", "waterproof", "hard-wearing",
    ])
    negative = params.get("keywords_negative", [
        "glass", "ceramic", "porcelain", "crystal", "fragile", "delicate",
        "breakable", "china",
    ])
    return _keyword_score(text, positive, negative)


@scorer("premium_look")
def score_premium(product: Product, params: dict) -> tuple[float, str]:
    """Keyword inference for premium / high-perceived-value appearance."""
    text = _text(product)
    positive = params.get("keywords_positive", [
        "premium", "luxury", "professional", "sleek", "elegant", "high-end",
        "sophisticated", "designer", "artisan", "handcrafted", "quality",
        "exclusive", "polished",
    ])
    negative = params.get("keywords_negative", [
        "cheap", "basic", "budget", "economy", "bargain", "generic",
        "no-brand", "unbranded", "low-cost",
    ])
    return _keyword_score(text, positive, negative)


@scorer("weight_estimate")
def score_weight(product: Product, params: dict) -> tuple[float, str]:
    """
    Pattern-match explicit weight mentions in text.
    Target: under 2 lbs / ~900 g.
    """
    text = _text(product)
    max_lbs = float(params.get("max_lbs", 2.0))

    # Pounds pattern: "1.5 lb", "2lbs", "0.8 pound"
    lbs_match = re.search(r"(\d+\.?\d*)\s*(?:lb|lbs|pound|pounds)", text)
    if lbs_match:
        lbs = float(lbs_match.group(1))
        if lbs <= max_lbs:
            score = _clamp(10.0 - (lbs / max_lbs) * 2.0)
            return round(score, 1), f"{lbs} lbs — under {max_lbs} lb target"
        score = _clamp(8.0 - (lbs - max_lbs) * 2.0)
        return round(score, 1), f"{lbs} lbs — exceeds {max_lbs} lb target"

    # Grams/kilograms pattern: "500g", "1.2 kg"
    g_match = re.search(r"(\d+\.?\d*)\s*(?:kg|kilogram|kilograms)", text)
    if g_match:
        kg = float(g_match.group(1))
        lbs = kg * 2.205
        if lbs <= max_lbs:
            return round(_clamp(10.0 - (lbs / max_lbs) * 2.0), 1), f"{kg} kg ({lbs:.1f} lbs) — under target"
        return round(_clamp(8.0 - (lbs - max_lbs) * 2.0), 1), f"{kg} kg ({lbs:.1f} lbs) — over target"

    g_match2 = re.search(r"(\d+\.?\d*)\s*(?:g|gram|grams)\b", text)
    if g_match2:
        g = float(g_match2.group(1))
        lbs = g / 453.6
        if lbs <= max_lbs:
            return round(_clamp(10.0 - (lbs / max_lbs) * 2.0), 1), f"{g}g ({lbs:.2f} lbs) — under target"
        return round(_clamp(8.0 - (lbs - max_lbs) * 2.0), 1), f"{g}g ({lbs:.2f} lbs) — over target"

    return 5.0, "no weight information found — neutral score"


@scorer("single_sku")
def score_single_sku(product: Product, params: dict) -> tuple[float, str]:
    """Penalise products with detectable size/colour variant signals."""
    text = _text(product)
    variant_signals = params.get("variant_signals", [
        "size:", "sizes:", "color:", "colour:", "variant:", "option:",
        "small / medium / large", "s / m / l", "xs, s, m, l",
        "choose size", "select size", "available in",
    ])
    hits = [s for s in variant_signals if s in text]
    if not hits:
        if product.sku:
            return 9.0, f"single SKU present ({product.sku}), no variant signals"
        return 7.0, "no variant signals detected"
    score = _clamp(5.0 - len(hits) * 2.0)
    return round(score, 1), f"variant signals found: {hits}"


@scorer("markup_potential")
def score_markup(product: Product, params: dict) -> tuple[float, str]:
    """
    Score the product's ability to support a 4× markup.
    Without knowing the cost price, this is a heuristic based on the
    retail price — a higher retail price generally allows more margin room.
    If 'cost_field' is present in extras, use the actual ratio.
    """
    target = float(params.get("target_multiple", 4.0))
    cost_field = params.get("cost_field", "cost")

    # Use actual cost if available in extras
    raw_cost = product.extras.get(cost_field)
    if raw_cost:
        cost_match = re.search(r"(\d+\.?\d*)", raw_cost)
        if cost_match and product.price:
            cost = float(cost_match.group(1))
            if cost > 0:
                ratio = float(product.price) / cost
                score = _clamp((ratio / target) * 10.0)
                return round(score, 1), f"{ratio:.1f}× markup (cost={cost}, retail={product.price})"

    # Heuristic: products in the $50–$150 range tend to have healthy margin room
    if product.price is None:
        return 5.0, "no price data — neutral score"
    price = float(product.price)
    if 40 <= price <= 150:
        score = _clamp(7.0 + (price - 40) / 110 * 2.0)
        return round(score, 1), f"${price:.2f} retail — good margin range for {target:.0f}× target"
    if price < 40:
        return round(_clamp(3.0 + price / 40 * 4.0), 1), f"${price:.2f} retail — low price may limit {target:.0f}× margin"
    return round(_clamp(9.0 - (price - 150) / 50), 1), f"${price:.2f} retail — high price reduces addressable market"


@scorer("evergreen")
def score_evergreen(product: Product, params: dict) -> tuple[float, str]:
    """Detect seasonal or trend-driven products and penalise them."""
    text = _text(product)
    seasonal = params.get("seasonal_keywords", [
        "christmas", "xmas", "halloween", "valentine", "thanksgiving",
        "easter", "hanukkah", "diwali", "new year", "summer", "winter",
        "holiday", "seasonal", "limited edition", "limited time",
    ])
    trend = params.get("trend_keywords", [
        "trending", "viral", "tiktok", "instagram", "as seen on",
        "hot right now", "latest", "new release",
    ])
    evergreen = params.get("evergreen_keywords", [
        "everyday", "daily", "essential", "classic", "universal",
        "timeless", "all-year", "year-round", "always",
    ])

    seasonal_hits = [kw for kw in seasonal if kw in text]
    trend_hits = [kw for kw in trend if kw in text]
    evergreen_hits = [kw for kw in evergreen if kw in text]

    if seasonal_hits:
        return _clamp(3.0 - len(seasonal_hits)), f"seasonal keywords: {seasonal_hits}"
    if trend_hits and not evergreen_hits:
        return 4.0, f"trend-driven signals: {trend_hits}"
    if evergreen_hits:
        score = min(10.0, 7.0 + len(evergreen_hits))
        return score, f"evergreen signals: {evergreen_hits}"
    return 6.0, "no seasonal or trend signals — assumed evergreen"


@scorer("manual")
def score_manual(product: Product, params: dict) -> tuple[float, str]:
    """
    Placeholder for criteria that require human research
    (e.g. FB group engagement, Amazon reviews cross-check).
    Returns the configured default score until overridden.
    """
    default = float(params.get("default_score", 5.0))
    return default, "manual score — requires human research to update"
