"""
tests/test_asset_blocking.py — Regression tests for _should_block_request(),
the decision logic behind Playwright's request interception.

Context: PINCALI_BLOCK_ASSETS only blocked resource_type
image/stylesheet/font/media/other. Lazy-loaded photo galleries and
embedded maps (Google Maps/Mapbox -- both common on real estate listing
pages) fetch their assets via fetch()/XHR, which Playwright reports as
resource_type "fetch"/"xhr", not "image" -- so they were never blocked.
This is a likely major contributor to burning through a 1GB Webshare
bandwidth allowance in a few hours.
"""
from app.scraper.core import _should_block_request


def test_blocks_direct_image_resource_type():
    assert _should_block_request("image", "https://cdn.pincali.com/photo1.jpg") is True


def test_blocks_stylesheet_font_media_other():
    assert _should_block_request("stylesheet", "https://x.com/style.css") is True
    assert _should_block_request("font", "https://x.com/font.woff2") is True
    assert _should_block_request("media", "https://x.com/video.mp4") is True
    assert _should_block_request("other", "https://x.com/whatever") is True


def test_blocks_lazy_loaded_image_fetched_via_xhr():
    """The core bug: an image lazy-loaded via JS fetch()/XHR is reported
    as resource_type 'fetch', not 'image' -- must still be caught by URL."""
    assert _should_block_request("fetch", "https://cdn.pincali.com/gallery/photo-42.webp") is True
    assert _should_block_request("xhr", "https://images.pincali.com/large/img.jpeg?v=2") is True


def test_blocks_google_maps_and_mapbox_regardless_of_resource_type():
    assert _should_block_request("fetch", "https://maps.googleapis.com/maps/api/js") is True
    assert _should_block_request("script", "https://maps.gstatic.com/tile.png") is True
    assert _should_block_request("xhr", "https://api.mapbox.com/styles/v1/tiles/1/2/3") is True


def test_blocks_known_analytics_and_widget_domains():
    assert _should_block_request("script", "https://www.googletagmanager.com/gtm.js") is True
    assert _should_block_request("xhr", "https://www.google-analytics.com/collect") is True
    assert _should_block_request("script", "https://widget.intercom.io/widget/abc") is True
    assert _should_block_request("fetch", "https://api.hotjar.com/x") is True


def test_does_not_block_the_main_document():
    assert _should_block_request("document", "https://www.pincali.com/en/home/some-listing") is False


def test_does_not_block_scripts_needed_to_render_the_page():
    """Blocking all scripts would break JS-rendered content extraction --
    only specific known-heavy/third-party script domains are blocked."""
    assert _should_block_request("script", "https://www.pincali.com/_next/static/chunk.js") is False


def test_does_not_block_same_origin_xhr_used_for_property_data():
    """This is the whole reason we can't just blanket-block all fetch/xhr --
    the property details are very often hydrated via exactly this kind of
    call. Only block by URL pattern (file extension / known heavy domains),
    never resource_type=fetch/xhr in general."""
    assert _should_block_request("fetch", "https://www.pincali.com/api/listings/12345") is False
    assert _should_block_request("xhr", "https://www.pincali.com/_next/data/listing.json") is False
