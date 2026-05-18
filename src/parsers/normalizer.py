"""
Converts RawProduct strings into typed, standardized Product values.
All transformations are pure functions — no I/O, no side effects.
"""

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from src.parsers.product import RawProduct


class Availability(StrEnum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    LIMITED = "limited"
    PREORDER = "preorder"
    UNKNOWN = "unknown"


@dataclass
class Product:
    url: str
    source: str
    name: str | None
    price: Decimal | None
    currency: str | None
    sku: str | None
    availability: Availability
    description: str | None
    image_url: str | None
    extras: dict[str, str]


# ── availability keyword maps ────────────────────────────────────────────────

_AVAILABILITY_PATTERNS: list[tuple[re.Pattern[str], Availability]] = [
    (re.compile(r"\b(in[\s_-]?stock|available|in[\s_-]?store)\b", re.I), Availability.IN_STOCK),
    (re.compile(r"\b(out[\s_-]?of[\s_-]?stock|sold[\s_-]?out|unavailable|not[\s_-]?available)\b", re.I), Availability.OUT_OF_STOCK),
    (re.compile(r"\b(limited|few[\s_-]?left|low[\s_-]?stock|only\s+\d+)\b", re.I), Availability.LIMITED),
    (re.compile(r"\b(pre[\s_-]?order|coming[\s_-]?soon|pre[\s_-]?sale)\b", re.I), Availability.PREORDER),
]

# Approximate mid-market rates to USD — update periodically
FX_TO_USD: dict[str, Decimal] = {
    "GBP": Decimal("1.27"),
    "EUR": Decimal("1.08"),
    "CAD": Decimal("0.73"),
    "AUD": Decimal("0.65"),
    "CHF": Decimal("1.12"),
    "JPY": Decimal("0.0067"),
    "INR": Decimal("0.012"),
    "KRW": Decimal("0.00073"),
}

# Currency symbols → ISO 4217 code
_SYMBOL_TO_ISO: dict[str, str] = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₹": "INR",
    "₩": "KRW",
    "₿": "BTC",
    "A$": "AUD",
    "C$": "CAD",
    "CHF": "CHF",
}

# Matches an optional currency symbol/code, then a numeric amount
_PRICE_RE = re.compile(
    r"""
    (?P<symbol>[A-Z]{3}|[$€£¥₹₩₿]|A\$|C\$)?   # optional currency prefix
    \s*
    (?P<amount>[\d,._]+)                          # numeric part (commas or dots as separators)
    """,
    re.VERBOSE,
)


def normalize(raw: RawProduct) -> Product:
    price, detected_currency = _parse_price(raw.price)
    currency = _normalize_currency(raw.currency) or detected_currency

    if price is not None and currency is not None and currency != "USD":
        rate = FX_TO_USD.get(currency)
        if rate is not None:
            price = (price * rate).quantize(Decimal("0.01"))
            currency = "USD"

    return Product(
        url=raw.url,
        source=raw.source,
        name=_clean_text(raw.name),
        price=price,
        currency=currency,
        sku=_clean_text(raw.sku),
        availability=_parse_availability(raw.availability),
        description=_clean_text(raw.description),
        image_url=raw.image_url,
        extras=raw.extras,
    )


# ── private helpers ──────────────────────────────────────────────────────────

def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    # Normalize unicode (e.g. fancy apostrophes, non-breaking spaces)
    value = unicodedata.normalize("NFKC", value)
    # Collapse internal whitespace
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def _parse_price(raw: str | None) -> tuple[Decimal | None, str | None]:
    """Return (Decimal price, ISO currency code or None)."""
    if not raw:
        return None, None

    cleaned = unicodedata.normalize("NFKC", raw).strip()
    m = _PRICE_RE.search(cleaned)
    if not m:
        return None, None

    symbol = m.group("symbol")
    amount_str = m.group("amount")

    # Disambiguate European vs. US decimal notation:
    # "1.234,56" → 1234.56  |  "1,234.56" → 1234.56  |  "1.99" → 1.99
    amount_str = _disambiguate_decimal(amount_str)

    try:
        amount = Decimal(amount_str)
    except InvalidOperation:
        return None, None

    currency = _SYMBOL_TO_ISO.get(symbol or "", symbol) or None
    return amount, currency


def _disambiguate_decimal(s: str) -> str:
    """Normalise decimal/thousand separators to produce a plain decimal string."""
    # Both comma and dot present
    if "," in s and "." in s:
        # whichever comes last is the decimal separator
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # Comma only: decimal if exactly two digits follow, else thousands separator
        parts = s.split(",")
        if len(parts) == 2 and len(parts[-1]) == 2:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    # Dot only: keep as-is (standard decimal)
    return s


def _parse_availability(raw: str | None) -> Availability:
    if not raw:
        return Availability.UNKNOWN
    for pattern, status in _AVAILABILITY_PATTERNS:
        if pattern.search(raw):
            return status
    return Availability.UNKNOWN


def _normalize_currency(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = unicodedata.normalize("NFKC", raw).strip()
    return _SYMBOL_TO_ISO.get(cleaned, cleaned.upper()) or None
