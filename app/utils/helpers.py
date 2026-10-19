"""
Shared utility helpers used across the platform.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def slugify(text: str) -> str:
    """Convert a string to a URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    text = re.sub(r"^-+|-+$", "", text)
    return text


def truncate(text: Optional[str], length: int = 255) -> Optional[str]:
    """Truncate a string to at most `length` characters."""
    if text is None:
        return None
    return text[:length] if len(text) > length else text


def parse_numeric(value: Optional[str]) -> Optional[float]:
    """
    Parse a numeric value from a raw string that may contain commas,
    currency symbols, or unit suffixes (e.g. '1,200,000 MXN', '$450').
    Returns None when no numeric value can be extracted.
    """
    if not value:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", str(value))
    if m:
        try:
            return float(m.group().replace(",", ""))
        except ValueError:
            pass
    return None


def safe_int(value: Optional[str]) -> Optional[int]:
    """Parse to int, return None on failure."""
    num = parse_numeric(value)
    return int(num) if num is not None else None


def safe_float(value: Optional[str]) -> Optional[float]:
    """Parse to float, return None on failure."""
    return parse_numeric(value)
