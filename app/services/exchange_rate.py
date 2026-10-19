"""
app/services/exchange_rate.py — MXN → USD conversion service.
Supports static rate (from .env) or live fetch from openexchangerates.org.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx

from app.core.config import get_settings
from app.core.logging import setup_logging

logger = setup_logging("exchange_rate")
settings = get_settings()

_cached_rate: Optional[float] = None
_cache_ts: float = 0.0
_CACHE_TTL = 3600  # 1 hour


async def get_mxn_to_usd_rate() -> float:
    """Return the MXN → USD conversion rate."""
    global _cached_rate, _cache_ts

    if settings.exchange_rate_mode == "static":
        return settings.mxn_to_usd_rate

    now = time.time()
    if _cached_rate is not None and (now - _cache_ts) < _CACHE_TTL:
        return _cached_rate

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://openexchangerates.org/api/latest.json",
                params={
                    "app_id": settings.exchange_api_key,
                    "symbols": "MXN",
                    "base": "USD",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                mxn_per_usd = data["rates"]["MXN"]
                _cached_rate = mxn_per_usd
                _cache_ts = now
                logger.info(f"Live exchange rate fetched: 1 USD = {mxn_per_usd:.4f} MXN")
                return _cached_rate
    except Exception as e:
        logger.warning(f"Failed to fetch live rate: {e}. Using fallback.")

    return settings.mxn_to_usd_rate


def convert_to_usd(amount: Optional[float], currency: Optional[str], rate: float) -> Optional[float]:
    """Convert an amount to USD given the MXN/USD rate."""
    if amount is None:
        return None
    if not currency or currency.upper() == "USD":
        return amount
    if currency.upper() == "MXN":
        return round(amount / rate, 2)
    return amount  # Unknown currency — return as-is
