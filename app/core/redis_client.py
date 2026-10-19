"""
app/core/redis_client.py — Shared async Redis client.

Why this exists:
The API/dashboard process and the two worker processes
(url_discovery_worker, detail_scraper_worker) are separate OS processes
(separate Docker containers), each with its own Python memory space.
ProxyManager and DomainThrottler are in-memory singletons, so the
dashboard's own copy of them is always empty/idle -- it never scraped
anything, the workers did, in a different process.

Redis is already provisioned in docker-compose (REDIS_URL) but wasn't
being used anywhere. We reuse it here as a lightweight shared blackboard:
each worker process periodically writes a JSON snapshot of its own
in-memory stats to a per-process Redis key, and the dashboard reads all
of them back and merges them. This does NOT change any rotation/cooldown
*decision* logic -- that stays local/in-memory per process, which is
correct and fast. It only fixes *visibility*.
"""
from __future__ import annotations

import redis.asyncio as aioredis

from app.core.config import get_settings

settings = get_settings()

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.redis_url, decode_responses=True, socket_timeout=2.0
        )
    return _redis
