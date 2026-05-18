"""
Extracts raw product fields from HTML using CSS selectors defined
per-source in sources.yaml. Returns unmodified strings — normalization
is a separate step handled by normalizer.py.
"""

import logging
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

from src.retrieval.fetcher import FetchResult

logger = logging.getLogger(__name__)

# Fields the parser knows how to extract.
# Each maps to a CSS selector key in sources.yaml → selectors.
KNOWN_FIELDS = frozenset({"name", "price", "sku", "availability", "description", "image_url", "currency"})


@dataclass
class RawProduct:
    url: str
    source: str
    name: str | None = None
    price: str | None = None
    currency: str | None = None
    sku: str | None = None
    availability: str | None = None
    description: str | None = None
    image_url: str | None = None
    extras: dict[str, str] = field(default_factory=dict)


class ParseError(Exception):
    def __init__(self, url: str, reason: str):
        self.url = url
        super().__init__(f"{url} — {reason}")


def parse(
    result: FetchResult,
    *,
    source_name: str,
    selectors: dict[str, str],
) -> RawProduct:
    """
    Extract product fields from a FetchResult using the provided CSS selectors.
    Unknown selector keys land in RawProduct.extras.
    Raises ParseError if the HTML cannot be parsed at all.
    """
    try:
        soup = BeautifulSoup(result.html, "lxml")
    except Exception as exc:
        raise ParseError(result.url, f"HTML parse failed: {exc}") from exc

    raw = RawProduct(url=result.url, source=source_name)

    for field_name, selector in selectors.items():
        value = _extract(soup, field_name, selector, result.url)
        if field_name in KNOWN_FIELDS:
            setattr(raw, field_name, value)
        else:
            if value is not None:
                raw.extras[field_name] = value

    return raw


def _extract(soup: BeautifulSoup, field_name: str, selector: str, url: str) -> str | None:
    """Select the first matching element and return its text or relevant attribute."""
    try:
        element = soup.select_one(selector)
    except Exception as exc:
        logger.warning("Bad selector %r for field %r on %s: %s", selector, field_name, url, exc)
        return None

    if element is None:
        logger.debug("No match for selector %r (field %r) on %s", selector, field_name, url)
        return None

    return _element_value(element, field_name)


def _element_value(element: Tag, field_name: str) -> str | None:
    """Pull the most meaningful value out of an element based on what field it is."""
    if field_name == "image_url":
        return element.get("src") or element.get("data-src") or element.get_text(strip=True) or None

    # For price/currency prefer data-price attribute when present (avoids formatted display strings)
    if field_name == "price":
        for attr in ("data-price", "data-amount", "content"):
            val = element.get(attr)
            if val:
                return str(val).strip()

    # Generic: prefer content/value attributes over visible text
    for attr in ("content", "value"):
        val = element.get(attr)
        if val:
            return str(val).strip()

    text = element.get_text(separator=" ", strip=True)
    return text or None
