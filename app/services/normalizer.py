"""
app/services/normalizer.py -- Transforms raw PropertyRecord dicts into
normalized Listing dicts ready for DB upsert.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple


def _parse_number(text: Any) -> Optional[float]:
    if text is None:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", str(text))
    if m:
        try:
            return float(m.group().replace(",", ""))
        except ValueError:
            pass
    return None


def _map_mls_status(listing_status: Optional[str], status_message: Optional[str]) -> str:
    text = (status_message or listing_status or "").lower()
    if any(x in text for x in ["sold", "vendido"]):
        return "Sold"
    if any(x in text for x in ["pending", "reserved", "apartado"]):
        return "Pending"
    if any(x in text for x in ["cancelled", "unpublished", "no longer available", "off market"]):
        return "Cancelled"
    return "Active"


def _map_property_type(
    listing_type: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    location_subtitle: Optional[str] = None,
) -> str:
    parts = [listing_type, title, description, location_subtitle]
    combined = " ".join(str(p) for p in parts if p).lower()

    def _has(text: str, keywords: List[str]) -> bool:
        return any(k in text for k in keywords)

    # PRIORITY 0: Trust explicit listing_type if it is a clear property-type word.
    # The scraper extracts this from the first word of the page subtitle (h2.location)
    # or from structured data. It is the most reliable signal and should NOT be
    # overridden by incidental keywords in the description or title.
    # e.g. listing_type="Casa" → House (B), even if description says "condo amenities"
    if listing_type:
        lt = listing_type.strip().lower()
        if lt in ("house", "casa", "villa", "chalet", "townhouse", "bungalow",
                  "residencia", "ranch", "hacienda", "quinta", "cottage", "manor"):
            return "B"
        if lt in ("apartment", "condo", "departamento", "depto", "loft",
                  "penthouse", "ph", "studio", "estudio", "flat"):
            return "A"
        if lt in ("land", "terreno", "lot", "lote", "solar", "parcela"):
            return "E"
        if lt in ("commercial", "comercial", "office", "oficina", "retail",
                  "local", "bodega", "warehouse", "franchise", "business"):
            return "F"

    # PRIORITY 1: Broad keyword search across all fields (fallback when listing_type
    # is missing or ambiguous).
    if _has(combined, [
        "land", "terreno", "lot", "lote", "solar", "parcela",
        "development land", "subdivision",
    ]):
        return "E"

    if _has(combined, [
        "apartment", "condo", "departamento", "depto", "loft",
        "penthouse", "ph", "studio", "estudio", "flat",
    ]):
        return "A"

    if _has(combined, [
        "house", "casa", "villa", "chalet", "townhouse", "bungalow",
        "residencia", "single family", "ranch", "hacienda", "quinta",
        "cottage", "manor", "estate",
    ]):
        return "B"

    if _has(combined, [
        "commercial", "comercial", "office", "oficina", "retail",
        "shopping", "mall", "plaza", "local", "bodega",
        "warehouse", "nave industrial", "industrial warehouse",
        "storage", "self-storage", "mini bodega", "showroom",
        "franchise", "business", "mixed-use", "mixed use",
    ]):
        return "F"

    if _has(combined, [
        "multi-family", "duplex", "multifamily", "plurifamiliar",
        "triplex", "fourplex", "apartment building",
        "edificio de departamentos", "edificio de apartamentos",
    ]):
        return "I"

    if "fractional" in combined or "timeshare" in combined:
        return "H"

    return "B"


def _extract_city_location(
    location_subtitle: Optional[str],
    address: Optional[str],
    title: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Parse structured location fields from the listing's map/location address string.

    EasyBroker/Pincali address format (right-to-left hierarchy):
        <subdivision>, <community>, <city/municipality>, <state>

    Examples:
        "anillo vial III, Ciudad Maderas Residencial, El Marqués, Querétaro"
          → subdivision="anillo vial III", community="Ciudad Maderas Residencial",
            city="El Marqués", state="Querétaro"

        "El centro Vialidad, Villa California, Tlajomulco de Zúñiga, Jalisco"
          → subdivision="El centro Vialidad", community="Villa California",
            city="Tlajomulco de Zúñiga", state="Jalisco"

    Falls back to location_subtitle for city/state when address is missing or
    contains fewer than 2 meaningful parts.
    """
    city = area = community = subdivision = None

    # Primary source: full comma-separated address from the Location/Map section.
    # Parse RIGHT-TO-LEFT: last part = state, second-to-last = city/municipality,
    # third-to-last = community, fourth-to-last = subdivision.
    # Using negative indexing so longer addresses (5, 6, 7+ parts) with
    # street-level noise at the front don't contaminate the geo fields.
    # e.g. "Parcela 62 Z, Blvd. Santa Catarina, Infonavit Venta Prieta, 62,
    #        Venta Prieta Infonavit, Pachuca de Soto, Hidalgo"
    #   parts[-1] = "Hidalgo"  (state)
    #   parts[-2] = "Pachuca de Soto"  (city)
    #   parts[-3] = "Venta Prieta Infonavit"  (community)
    #   parts[-4] = "62"  (subdivision — street-level prefix discarded)
    if address:
        parts = [p.strip() for p in address.split(",") if p.strip()]
        if len(parts) >= 4:
            area        = parts[-1] or None     # state  (always rightmost)
            city        = parts[-2] or None
            community   = parts[-3] or None
            subdivision = parts[-4] or None
            # anything at parts[0 .. -5] is street-level detail; discard
        elif len(parts) == 3:
            area      = parts[-1] or None
            city      = parts[-2] or None
            community = parts[-3] or None
        elif len(parts) == 2:
            area = parts[-1] or None
            city = parts[-2] or None
        elif len(parts) == 1:
            city = parts[0] or None

    # Fallback: use location_subtitle for city/state only when address is absent
    # or too short to yield a city value.
    if not city and location_subtitle:
        subtitle_clean = location_subtitle.strip()
        title_clean = (title or "").strip().lower()
        subtitle_lower = subtitle_clean.lower()
        is_title_match = title_clean and subtitle_lower == title_clean
        is_too_long_no_comma = "," not in subtitle_clean and len(subtitle_clean) > 60

        if not is_title_match and not is_too_long_no_comma:
            sub_parts = [p.strip() for p in subtitle_clean.split(",") if p.strip()]
            if len(sub_parts) >= 2:
                # Subtitle is typically "House for Sale in <city>, <state>"
                # Strip any leading prose before the actual city name.
                raw_city = sub_parts[-2]
                raw_city = re.sub(r"(?i)^.+\b(?:in|en)\s+", "", raw_city).strip()
                city = raw_city or None
                area = sub_parts[-1] or None
            elif sub_parts:
                city = sub_parts[0] or None

    return city, area, community, subdivision


def _extract_bathroom_counts(
    features: Dict[str, Any]
) -> Tuple[Optional[int], Optional[int]]:
    baths_raw = features.get("bathrooms") or features.get("bathrooms_raw", "")
    if not baths_raw:
        return None, None
    num = _parse_number(baths_raw)
    if num is None:
        return None, None
    if isinstance(num, float) and num != int(num):
        full = int(num)
        half = 1 if (num - full) >= 0.4 else 0
        return full, half
    return int(num), 0


def _amenities_to_features(
    amenities_list: List[Dict],
) -> Tuple[List[str], List[str]]:
    interior_kw = [
        "interior", "inside", "indoors", "kitchen", "bath", "bed", "living",
        "dining", "floor", "ac", "heating", "appliance", "closet", "lighting",
        "security system", "fireplace",
    ]
    exterior_kw = [
        "exterior", "outside", "outdoor", "garden", "terrace", "bbq", "pool",
        "parking", "garage", "fence", "gate", "view", "roof", "balcony",
        "patio", "courtyard", "swimming",
    ]
    interior: set = set()
    exterior: set = set()
    for group in amenities_list:
        cat = group.get("category", "").lower()
        is_int = any(k in cat for k in interior_kw)
        is_ext = any(k in cat for k in exterior_kw)
        for item in group.get("items", []):
            item_low = item.lower()
            if is_int or (not is_ext and any(k in item_low for k in interior_kw)):
                interior.add(item)
            elif is_ext or any(k in item_low for k in exterior_kw):
                exterior.add(item)
            else:
                interior.add(item)
    return sorted(interior), sorted(exterior)


_LOGO_URL_PATTERNS = re.compile(
    r"/(logo|brand|agency|agent|office|watermark|favicon|icon|profile_images"
    r"|publisher|contact|headshot|avatar|thumb_agent)[^/]*\."
    r"|WhatsApp_Image"
    r"|[_\-](50x50|100x100|150x150|200x200|square)\.",
    re.I,
)


def _is_logo_url(url: str, agency_logo: str) -> bool:
    """Return True if the URL looks like an agency logo rather than a property photo."""
    if not url:
        return True
    if agency_logo and url.strip() == agency_logo.strip():
        return True
    if _LOGO_URL_PATTERNS.search(url):
        return True
    return False


def _sanitize_description(text: Optional[str]) -> Optional[str]:
    """
    Strip emojis and non-printable characters from description text, returning
    clean plain text. Returns None only if nothing meaningful remains.

    BUG FIX: the original implementation used hardcoded Unicode codepoint ranges
    which missed large classes of emoji/symbol characters (e.g. ⭐ U+2B50, ▶ U+25B6,
    ⬛ U+2B1B and many others in the U+2400–U+2BFF block that sit outside every
    hardcoded range). It also left a leading space on lines where an emoji appeared
    at the start (e.g. "🌊 Ocean view" → " Ocean view").

    Fix: use unicodedata.category(char) == "So" (Symbol, Other) as the universal
    emoji/symbol test. This covers all current and future Unicode emoji additions
    without needing to maintain codepoint ranges. Variation selectors (FE00–FE0F),
    zero-width chars (200B–200F), and the BOM (FEFF) are stripped separately since
    their Unicode category is not "So".
    """
    if not text:
        return None

    cleaned_chars = []
    for char in text:
        cp = ord(char)
        category = unicodedata.category(char)

        # "So" = Symbol, Other — covers all emoji, dingbats, and misc symbols
        if category == "So":
            continue

        # Control characters except tab, newline, carriage-return
        if category == "Cc" and char not in ("\t", "\n", "\r"):
            continue

        # Variation selectors: U+FE00–FE0F (emoji presentation modifiers)
        if 0xFE00 <= cp <= 0xFE0F:
            continue

        # Zero-width spaces and joiners: U+200B–200F
        if 0x200B <= cp <= 0x200F:
            continue

        # BOM / zero-width no-break space: U+FEFF
        if cp == 0xFEFF:
            continue

        cleaned_chars.append(char)

    result = "".join(cleaned_chars)

    # Normalise whitespace
    result = re.sub(r"[ \t]+", " ", result)           # collapse runs of spaces/tabs
    result = re.sub(r"^ +", "", result, flags=re.M)   # strip per-line leading spaces
    result = re.sub(r" +$", "", result, flags=re.M)   # strip per-line trailing spaces
    result = re.sub(r"\n{3,}", "\n\n", result)        # at most one blank line between paragraphs

    return result.strip() or None


def _normalize_currency(raw: Optional[str]) -> str:
    """
    Map any raw currency string the scraper may produce to a 3-letter ISO 4217
    code safe for the VARCHAR(16) original_currency column.

    Handles DOM text labels like "Pesos Mexicanos" / "Mexican Peso" that would
    previously overflow the old VARCHAR(8) column and crash the INSERT.

    Default is MXN — pincali.com is a Mexican property site and virtually all
    listings are priced in pesos. Only override to USD when there is an explicit
    USD/Dollar signal in the raw value.
    """
    if not raw:
        return "MXN"
    upper = raw.strip().upper()
    if upper in ("MXN", "USD", "EUR", "CAD"):
        return upper
    if any(k in upper for k in ("PESO", "MXN", "MEXICAN")):
        return "MXN"
    if any(k in upper for k in ("USD", "DOLLAR", "DOLAR", "DÓLAR")):
        return "USD"
    if upper == "$":
        return "MXN"
    # Unknown raw value on a Mexican site → default MXN
    return "MXN"


def normalize_property_record(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Convert a raw PropertyRecord dict (from the scraper) into a normalized
    dict ready for ListingRepository.upsert().
    Returns None if required fields are missing.
    """
    external_id = raw.get("property_id") or raw.get("external_id")
    origin_url = raw.get("product_url") or raw.get("origin_url")

    if not external_id or not origin_url:
        return None

    features = raw.get("features", {})
    if isinstance(features, dict):
        raw_features = features.get("_raw", features)
    else:
        raw_features = {}

    amenities = raw.get("amenities", [])
    title_h1 = raw.get("title_h1") or ""
    location_subtitle = raw.get("location_subtitle")
    description_raw = raw.get("description")

    city, area, community, subdivision = _extract_city_location(
        location_subtitle,
        raw.get("address"),
        title=title_h1,
    )

    beds_num = _parse_number(
        features.get("bedrooms") or raw_features.get("bedrooms")
    )
    full_baths, half_baths = _extract_bathroom_counts(features or raw_features)
    year_num = _parse_number(features.get("year_built") or raw_features.get("year_built"))
    lot_num = _parse_number(
        features.get("lot_size_m2")
        or raw_features.get("lot_size_m2")
        or features.get("total_space_m2")
    )
    total_num = _parse_number(
        features.get("total_space_m2") or raw_features.get("total_space_m2")
    )
    floors_num = _parse_number(features.get("floors") or raw_features.get("floors"))
    parking_num = _parse_number(features.get("parking") or raw_features.get("parking"))

    raw_price_str = raw.get("price_digits")
    raw_price = _parse_number(raw_price_str)
    currency = _normalize_currency(raw.get("currency"))

    has_pool = any("pool" in str(v).lower() for v in (features or {}).values()) or any(
        "pool" in item.lower()
        for group in amenities
        for item in group.get("items", [])
    )

    furnished = "unfurnished"
    if any("furnished" in str(v).lower() for v in (features or {}).values()) or any(
        "furnished" in item.lower()
        for group in amenities
        for item in group.get("items", [])
    ):
        furnished = "fully"

    view_kw = ["ocean", "mountain", "garden", "city", "pool", "panoramic"]
    view = None
    for vw in view_kw:
        if any(vw in str(val).lower() for val in (features or {}).values()) or any(
            vw in item.lower()
            for group in amenities
            for item in group.get("items", [])
        ):
            view = vw
            break

    interior, exterior = _amenities_to_features(amenities)

    listing_type_raw = raw.get("listing_type")
    property_type = _map_property_type(
        listing_type=listing_type_raw,
        title=title_h1,
        description=description_raw,
        location_subtitle=location_subtitle,
    )

    # Photos -- filter out logos and reindex
    agency_logo = raw.get("agency_logo_url") or ""
    clean_images = [
        u for u in raw.get("all_images", [])
        if not _is_logo_url(u, agency_logo)
    ]

    photos = []
    for idx, img_url in enumerate(clean_images):
        photos.append(
            {
                "url": img_url,
                "position": idx,
                "is_primary": idx == 0,
                "caption": None,
            }
        )

    # Videos
    videos = []
    video_iframe = raw.get("video_iframe")
    if video_iframe:
        url = video_iframe if video_iframe.startswith("http") else "https:" + video_iframe
        videos.append({"url": url, "title": "Video tour"})

    # Virtual tours
    tours = []
    vt_url = raw.get("virtual_tour_url")
    if vt_url:
        tours.append({"url": vt_url, "title": "Virtual tour"})

    # Extra JSON
    extra: Dict[str, Any] = {}
    if raw.get("operation_type"):
        extra["operation_type"] = raw["operation_type"]
    if raw.get("published_ago"):
        extra["published_ago"] = raw["published_ago"]
    if raw.get("agent_phone"):
        extra["agent_phone"] = raw["agent_phone"]
    if raw.get("agent_whatsapp"):
        extra["agent_whatsapp"] = raw["agent_whatsapp"]
    if raw.get("agency_logo_url"):
        extra["agency_logo_url"] = raw["agency_logo_url"]
    if raw.get("map_iframe"):
        extra["map_iframe"] = raw["map_iframe"]
    if listing_type_raw:
        extra["listing_type_raw"] = listing_type_raw
    if location_subtitle:
        extra["location_subtitle_raw"] = location_subtitle
    if raw.get("address"):
        extra["raw_address"] = raw["address"]
    learned = (raw.get("_extra") or {}).get("learned_features", {})
    if learned:
        extra["learned_features"] = learned

    return {
        "external_id": str(external_id),
        "source": "pincali",
        "origin_url": origin_url,
        "name": _sanitize_description(title_h1) or None,
        "description": _sanitize_description(description_raw),
        "supplement": _sanitize_description(raw.get("status_message")),
        "price_usd": None,
        "original_price": raw_price,
        "original_currency": currency,
        "mls_status": _map_mls_status(raw.get("listing_status"), raw.get("status_message")),
        "property_type": property_type,
        "city": city,
        "state": area,
        "country": "Mexico",
        "community": community,
        "subdivision": subdivision,
        "latitude": raw.get("latitude"),
        "longitude": raw.get("longitude"),
        "beds_total": int(beds_num) if beds_num is not None else None,
        "baths_full": full_baths,
        "baths_half": half_baths,
        "year_built": int(year_num) if year_num is not None else None,
        "lot_size_m2": float(lot_num) if lot_num is not None else None,
        "construction_m2": float(total_num) if total_num is not None else None,
        "living_area_m2": None,
        "stories": int(floors_num) if floors_num is not None else None,
        "parking_spaces": int(parking_num) if parking_num is not None else None,
        "pool": has_pool,
        "furnished": furnished,
        "view": view,
        "interior_features": interior or None,
        "exterior_features": exterior or None,
        "list_agent_name": raw.get("agent_name"),
        "list_office_name": raw.get("agency_name"),
        "extra": extra or None,
        "_photos": photos,
        "_videos": videos,
        "_tours": tours,
    }