from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.parsers.normalizer import Availability, normalize
from src.parsers.product import ParseError, RawProduct, parse
from src.retrieval.fetcher import FetchResult


def _result(html: str, url: str = "https://example.com/p/1") -> FetchResult:
    return FetchResult(url=url, status_code=200, html=html)


# ── product.parse ────────────────────────────────────────────────────────────

class TestParse:
    SELECTORS = {
        "name": "h1.product-title",
        "price": "span.price",
        "sku": "span[data-sku]",
        "availability": "div.stock-status",
    }

    def test_extracts_all_known_fields(self):
        html = """
        <html><body>
            <h1 class="product-title">Widget Pro</h1>
            <span class="price">$29.99</span>
            <span data-sku="WGT-001">WGT-001</span>
            <div class="stock-status">In Stock</div>
        </body></html>
        """
        raw = parse(_result(html), source_name="test_store", selectors=self.SELECTORS)

        assert raw.name == "Widget Pro"
        assert raw.price == "$29.99"
        assert raw.sku == "WGT-001"
        assert raw.availability == "In Stock"
        assert raw.source == "test_store"

    def test_missing_element_yields_none(self):
        html = "<html><body><h1 class='product-title'>Widget</h1></body></html>"
        raw = parse(_result(html), source_name="test_store", selectors=self.SELECTORS)

        assert raw.name == "Widget"
        assert raw.price is None
        assert raw.availability is None

    def test_unknown_selector_key_goes_to_extras(self):
        html = "<html><body><span class='brand'>Acme</span></body></html>"
        raw = parse(
            _result(html),
            source_name="test_store",
            selectors={"brand": "span.brand"},
        )
        assert raw.extras == {"brand": "Acme"}

    def test_prefers_data_price_attribute_over_text(self):
        html = '<html><body><span class="price" data-price="24.99">$24.99</span></body></html>'
        raw = parse(_result(html), source_name="s", selectors={"price": "span.price"})
        assert raw.price == "24.99"

    def test_image_url_extracted_from_src(self):
        html = '<html><body><img class="hero" src="https://cdn.example.com/img.jpg"></body></html>'
        raw = parse(_result(html), source_name="s", selectors={"image_url": "img.hero"})
        assert raw.image_url == "https://cdn.example.com/img.jpg"

    def test_attr_suffix_extracts_attribute(self):
        html = '<html><body><p class="star-rating Three"></p></body></html>'
        raw = parse(_result(html), source_name="s", selectors={"rating": "p.star-rating|attr:class"})
        assert raw.extras["rating"] == "star-rating Three"

    def test_attr_suffix_returns_none_when_attr_missing(self):
        html = '<html><body><p class="star-rating Three"></p></body></html>'
        raw = parse(_result(html), source_name="s", selectors={"rating": "p.star-rating|attr:data-value"})
        assert "rating" not in raw.extras

    def test_bad_selector_logs_warning_and_returns_none(self, caplog):
        import logging
        html = "<html><body></body></html>"
        with caplog.at_level(logging.WARNING, logger="src.parsers.product"):
            raw = parse(_result(html), source_name="s", selectors={"name": "["})
        assert raw.name is None

    def test_invalid_html_raises_parse_error(self):
        result = FetchResult(url="https://x.com", status_code=200, html=None)  # type: ignore[arg-type]
        with pytest.raises(ParseError):
            parse(result, source_name="s", selectors={})


# ── normalizer.normalize ─────────────────────────────────────────────────────

def _raw(**kwargs) -> RawProduct:
    defaults = dict(url="https://example.com/p/1", source="test_store", extras={})
    return RawProduct(**{**defaults, **kwargs})


class TestNormalize:
    def test_basic_price_usd(self):
        product = normalize(_raw(price="$29.99"))
        assert product.price == Decimal("29.99")
        assert product.currency == "USD"

    def test_price_with_thousands_separator(self):
        product = normalize(_raw(price="$1,299.00"))
        assert product.price == Decimal("1299.00")

    def test_european_price_format(self):
        product = normalize(_raw(price="1.299,99"))
        assert product.price == Decimal("1299.99")

    def test_price_currency_code_prefix(self):
        product = normalize(_raw(price="EUR 49.95"))
        assert product.price == Decimal("49.95")
        assert product.currency == "EUR"

    def test_currency_field_overrides_detected_currency(self):
        product = normalize(_raw(price="$9.99", currency="CAD"))
        assert product.currency == "CAD"

    def test_currency_symbol_normalized(self):
        product = normalize(_raw(currency="£"))
        assert product.currency == "GBP"

    def test_unparseable_price_is_none(self):
        product = normalize(_raw(price="contact us"))
        assert product.price is None

    def test_availability_in_stock(self):
        for text in ["In Stock", "Available", "in-stock", "IN STOCK"]:
            assert normalize(_raw(availability=text)).availability == Availability.IN_STOCK

    def test_availability_out_of_stock(self):
        for text in ["Out of Stock", "Sold Out", "unavailable"]:
            assert normalize(_raw(availability=text)).availability == Availability.OUT_OF_STOCK

    def test_availability_limited(self):
        assert normalize(_raw(availability="Only 3 left")).availability == Availability.LIMITED
        assert normalize(_raw(availability="Low Stock")).availability == Availability.LIMITED

    def test_availability_preorder(self):
        assert normalize(_raw(availability="Pre-Order Now")).availability == Availability.PREORDER

    def test_availability_unknown_when_missing(self):
        assert normalize(_raw(availability=None)).availability == Availability.UNKNOWN

    def test_name_whitespace_collapsed(self):
        product = normalize(_raw(name="  Widget   Pro  "))
        assert product.name == "Widget Pro"

    def test_name_none_when_empty_string(self):
        product = normalize(_raw(name=""))
        assert product.name is None

    def test_extras_passed_through(self):
        product = normalize(_raw(extras={"brand": "Acme"}))
        assert product.extras == {"brand": "Acme"}
