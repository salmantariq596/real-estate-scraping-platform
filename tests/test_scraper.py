"""
Tests for the scraper core logic (HTTP extraction, helpers, normalizer).
These tests are pure-Python — no browser or network required.
"""
from __future__ import annotations

import pytest

from app.scraper.core import (
    HTTPExtractor,
    extract_property_id_from_url,
    extract_lat_lon_from_html,
    _strip_tags,
    _extract_meta_content,
)
from app.services.normalizer import normalize_property_record
from app.services.exchange_rate import convert_to_usd


# ── HTML helpers ──────────────────────────────────────────────────────────────

def test_strip_tags_basic():
    assert _strip_tags("<p>Hello <b>world</b></p>") == "Hello world"


def test_strip_tags_entities():
    assert "&amp;" not in _strip_tags("Tom &amp; Jerry")
    assert "&" in _strip_tags("Tom &amp; Jerry")


def test_extract_meta_content_name():
    html = '<meta name="description" content="A beautiful villa">'
    result = _extract_meta_content(html, "name", "description")
    assert result == "A beautiful villa"


def test_extract_meta_content_property():
    html = '<meta property="og:image" content="https://example.com/img.jpg">'
    result = _extract_meta_content(html, "property", "og:image")
    assert result == "https://example.com/img.jpg"


def test_extract_meta_content_missing():
    result = _extract_meta_content("<html></html>", "name", "keywords")
    assert result is None


# ── URL / lat-lon helpers ─────────────────────────────────────────────────────

def test_extract_property_id_numeric():
    url = "https://www.pincali.com/en/home/0012345-beautiful-villa"
    pid = extract_property_id_from_url(url)
    assert pid == "0012345"


def test_extract_property_id_slug_fallback():
    url = "https://www.pincali.com/en/home/some-property-without-numeric-prefix"
    pid = extract_property_id_from_url(url)
    assert len(pid) > 0
    assert "/" not in pid


def test_extract_property_id_empty_path():
    url = "https://www.pincali.com/"
    pid = extract_property_id_from_url(url)
    assert len(pid) > 0  # falls back to hash


def test_extract_lat_lon_from_clipboard():
    html = (
        '<a data-clipboard-text="https://www.google.com/maps?q=23.1234,-109.5678">'
        "Copy Location</a>"
    )
    lat, lon = extract_lat_lon_from_html(html)
    assert lat == pytest.approx(23.1234)
    assert lon == pytest.approx(-109.5678)


def test_extract_lat_lon_json_ld():
    html = """
    <script type="application/ld+json">
    {"@type": "Place", "geo": {"latitude": 20.9, "longitude": -86.7}}
    </script>
    """
    lat, lon = extract_lat_lon_from_html(html)
    assert lat == pytest.approx(20.9)
    assert lon == pytest.approx(-86.7)


def test_extract_lat_lon_missing():
    lat, lon = extract_lat_lon_from_html("<html><body>Nothing here</body></html>")
    assert lat is None
    assert lon is None


# ── HTTPExtractor ─────────────────────────────────────────────────────────────

SAMPLE_HTML = """
<html>
<head>
  <title>3 Bed Villa in Cabo</title>
  <meta name="description" content="Stunning ocean-view villa">
  <meta property="og:image" content="https://assets.easybroker.com/img/main.jpg">
  <script type="application/ld+json">{"@type":"SingleFamilyResidence","geo":{"latitude":22.9,"longitude":-109.9}}</script>
</head>
<body>
  <h1>Beautiful Villa in Cabo</h1>
  <h2 class="location">Cabo San Lucas, BCS</h2>
  <span class="listing-id"><span>ID: 0099999</span></span>
  <div class="price">
    <span class="digits">5,200,000</span>
    <span class="currency">MXN</span>
    <span class="operation-type">For Sale</span>
  </div>
  <div class="listing__description">
    <p class="text-description">
      <span data-translation="1">Amazing ocean view villa with pool and garden.</span>
    </p>
  </div>
  <div class="publisher-name"><a>John Doe</a></div>
  <span class="publisher-organization-name">Cabo Realty</span>
  <a href="tel:+52-624-000-0000">Call</a>
  <a href="https://wa.me/526240000000">WhatsApp</a>
  <a data-clipboard-text="https://www.google.com/maps?q=22.9,-109.9">Copy Location</a>
  <div class="feature-icon">3 Bedrooms</div>
  <div class="feature-icon">2 Bathrooms</div>
  <div class="feature-icon">2 Parking spaces</div>
</body>
</html>
"""


def test_http_extractor_basic():
    url = "https://www.pincali.com/en/home/0099999-villa-cabo"
    extractor = HTTPExtractor(SAMPLE_HTML, url)
    data = extractor.extract()
    assert data["property_id"] == "0099999"
    assert "Cabo" in data["title_h1"]
    assert data["price_digits"] == "5200000"
    assert data["currency"] == "MXN"
    assert data["agent_name"] == "John Doe"
    assert data["agency_name"] == "Cabo Realty"
    assert data["agent_phone"] == "+52-624-000-0000"
    assert data["latitude"] == pytest.approx(22.9)
    assert data["longitude"] == pytest.approx(-109.9)
    assert "Bedrooms" in data["features"].get("bedrooms", "")


def test_http_extractor_images():
    html = """
    <html><body>
    <img src="https://assets.easybroker.com/photo1.jpg" />
    <img data-src="https://assets.easybroker.com/photo2.jpg" />
    </body></html>
    """
    extractor = HTTPExtractor(html, "https://www.pincali.com/en/home/1")
    data = extractor.extract()
    assert len(data["all_images"]) == 2


def test_http_extractor_no_id_fallback():
    """When no listing-id element is found, ID is derived from URL."""
    html = "<html><body><div class='listing'><h1>Some Property</h1></div></body></html>"
    url = "https://www.pincali.com/en/home/0055555-test-slug"
    extractor = HTTPExtractor(html, url)
    data = extractor.extract()
    assert data["property_id"] == "0055555"


# ── Exchange rate service ─────────────────────────────────────────────────────

def test_exchange_rate_static():
    usd = convert_to_usd(1_000_000, "MXN", rate=20.0)
    assert usd == pytest.approx(50_000.0)


def test_exchange_rate_none_input():
    assert convert_to_usd(None, "MXN", rate=20.0) is None


# ── Normalizer ───────────────────────────────────────────────────────────────

def test_normalizer_basic():
    raw = {
        "property_id": "ABC123",
        "product_url": "https://www.pincali.com/en/home/ABC123",
        "title_h1": "Casa en venta en Tulum",
        "price_digits": "3,500,000",
        "currency": "MXN",
        "operation_type": "For Sale",
        "location_subtitle": "Tulum, Quintana Roo",
        "description": "Beautiful home with pool.",
        "features": {
            "bedrooms": "3 Bedrooms",
            "bathrooms": "2 Bathrooms",
            "parking": "1 Parking",
            "year_built": "Year built: 2018",
            "lot_size_m2": "Lot size: 250 m2",
        },
        "latitude": 20.21,
        "longitude": -87.46,
        "agent_name": "Maria Lopez",
        "agency_name": "Tulum Homes",
        "all_images": ["https://assets.easybroker.com/x.jpg"],
        "amenities": [],
    }
    result = normalize_property_record(raw)
    assert result["external_id"] == "ABC123"
    assert result["city"].lower() == "tulum"
    assert result["beds_total"] == 3
    assert result["baths_full"] == 2
    assert result["year_built"] == 2018
    assert result["original_price"] == pytest.approx(3_500_000.0)
    assert result["original_currency"] == "MXN"
    assert result["mls_status"] == "Active"


def test_normalizer_usd_passthrough():
    """USD prices should not be converted."""
    raw = {
        "property_id": "USD001",
        "product_url": "https://www.pincali.com/en/home/USD001",
        "title_h1": "Condo",
        "price_digits": "250,000",
        "currency": "USD",
        "features": {},
        "amenities": [],
        "all_images": [],
    }
    result = normalize_property_record(raw)
    # USD conversion happens in ListingRepository.upsert; the normalizer keeps the original amount
    assert result["original_price"] == pytest.approx(250_000.0)
    assert result["original_currency"] == "USD"
