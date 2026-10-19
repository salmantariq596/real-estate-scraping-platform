"""
tests/test_proxy_quota_handling.py — Regression tests for detecting a
vendor-side account-level quota/billing hard stop (e.g. Webshare's
402 Payment Required / bandwidthlimit), reproduced live via curl against
the real account on 2026-07-11 and confirmed to be httpx.ProxyError('402
Payment Required').

Covers:
  - fetch_static_page() raises ProxyQuotaExceededError on a 402 CONNECT
    tunnel failure (via a local fake proxy, no real network needed).
  - _fetch_static_page_with_retry() aborts immediately instead of burning
    the full retry budget across every pool slot.
  - ProxyManager marks/clears the quota-exceeded state correctly, and it
    is visible through stats().
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.config import get_settings
from app.scraper import core as scraper_core
from app.scraper.proxy_manager import (
    ProxyManager,
    ProxyQuotaExceededError,
    StaticListProvider,
    reset_proxy_manager,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_proxy_manager()
    # PATCH: app.core.redis_client caches one module-level client for the
    # whole process lifetime, which is correct in production (one process,
    # one event loop, for the container's entire life) but not under
    # pytest-asyncio's per-test event loops -- reset it so each test binds
    # a fresh client to its own current loop instead of reusing a
    # connection pool tied to an already-closed one.
    import app.core.redis_client as redis_client_module
    redis_client_module._redis = None
    yield
    redis_client_module._redis = None
    reset_proxy_manager()


async def _start_fake_proxy(status_line: bytes, extra_headers: bytes = b""):
    """A tiny local CONNECT-tunnel proxy that always responds with the
    given status line -- lets us reproduce the exact 402 Webshare gave us
    without touching the real internet."""
    async def handle(reader, writer):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = await reader.read(4096)
            if not chunk:
                break
            data += chunk
        writer.write(status_line + extra_headers + b"\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    asyncio.create_task(server.serve_forever())
    return server, port


@pytest.mark.asyncio
async def test_fetch_static_page_raises_on_402_quota_exceeded():
    server, port = await _start_fake_proxy(
        b"HTTP/1.1 402 Payment Required",
        b"\r\nX-Webshare-Error: 402\r\nX-Webshare-Reason: bandwidthlimit",
    )
    try:
        proxy_url = f"http://user:pass@127.0.0.1:{port}"
        with pytest.raises(ProxyQuotaExceededError) as exc_info:
            await scraper_core.fetch_static_page("https://example.com", proxy=proxy_url)
        assert exc_info.value.reason == "402_payment_required"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_fetch_static_page_returns_none_on_ordinary_407_not_quota():
    """A plain auth failure (407) should NOT be misclassified as account
    quota exhaustion -- only a 402 gets the special fail-fast treatment."""
    server, port = await _start_fake_proxy(b"HTTP/1.1 407 Proxy Authentication Required")
    try:
        proxy_url = f"http://user:pass@127.0.0.1:{port}"
        result = await scraper_core.fetch_static_page("https://example.com", proxy=proxy_url)
        assert result is None
    finally:
        server.close()


@pytest.mark.asyncio
async def test_retry_loop_aborts_immediately_on_quota_exceeded(monkeypatch):
    """_fetch_static_page_with_retry() should NOT keep rotating through
    the pool and burning attempts once quota exhaustion is confirmed --
    it should mark it on the ProxyManager and return immediately."""
    settings = get_settings()
    settings.pincali_proxy_list = "http://p1:8080,http://p2:8080,http://p3:8080"
    settings.proxy_max_retries_per_request = 3
    manager = ProxyManager(provider=StaticListProvider())
    monkeypatch.setattr(scraper_core, "get_proxy_manager", lambda: manager)

    call_count = {"n": 0}

    async def fake_fetch(url, proxy=None):
        call_count["n"] += 1
        raise ProxyQuotaExceededError("402 Payment Required", reason="402_payment_required")

    monkeypatch.setattr(scraper_core, "fetch_static_page", fake_fetch)

    html, endpoint = await scraper_core._fetch_static_page_with_retry("https://example.com/x")

    assert html is None
    # Should have made exactly ONE attempt, not burned all 3 retries
    # rotating across p1/p2/p3.
    assert call_count["n"] == 1
    assert manager.is_quota_exceeded()


@pytest.mark.asyncio
async def test_quota_exceeded_state_visible_in_stats_and_clearable():
    manager = ProxyManager(provider=StaticListProvider())
    assert manager.is_quota_exceeded() is False

    await manager.mark_quota_exceeded(reason="402_payment_required")
    assert manager.is_quota_exceeded() is True
    stats = manager.stats()
    assert stats["quota_exceeded"] is True
    assert stats["quota_exceeded_reason"] == "402_payment_required"

    await manager.clear_quota_exceeded()
    assert manager.is_quota_exceeded() is False
    assert manager.stats()["quota_exceeded"] is False


@pytest.mark.asyncio
async def test_report_success_auto_clears_quota_exceeded():
    settings = get_settings()
    settings.pincali_proxy_list = "http://p1:8080"
    manager = ProxyManager(provider=StaticListProvider())

    await manager.mark_quota_exceeded(reason="402_payment_required")
    assert manager.is_quota_exceeded() is True

    endpoint = await manager.get_proxy()
    await manager.report_success(endpoint)

    assert manager.is_quota_exceeded() is False


@pytest.mark.asyncio
async def test_sticky_quota_alert_survives_ephemeral_snapshot_expiring():
    """The bug found while triaging the client's 'dashboard data looks
    wrong' report: once quota is exceeded, workers correctly STOP making
    requests (that's the fail-fast fix doing its job) -- which means they
    stop publishing fresh per-process snapshots (45s TTL). Without a
    separate sticky key, the very banner meant to explain the outage
    would itself disappear within seconds, leaving the dashboard looking
    empty/broken instead of clearly explained. This proves the sticky
    key (24h TTL) is independent of that liveness heartbeat.
    """
    from app.scraper.proxy_manager import (
        PROXY_QUOTA_REDIS_KEY,
        get_live_proxy_stats,
    )
    from app.core.redis_client import get_redis

    redis = get_redis()
    try:
        await redis.ping()
    except Exception:
        pytest.skip("needs a running Redis server (docker-compose up redis)")
    await redis.delete(PROXY_QUOTA_REDIS_KEY)

    settings = get_settings()
    settings.pincali_proxy_list = "http://p1:8080"
    manager = ProxyManager(provider=StaticListProvider())

    await manager.mark_quota_exceeded(reason="402_payment_required")

    # Simulate the ephemeral per-process snapshot having already expired
    # (as it will, 45s after the last request, with no new ones coming).
    await redis.delete(f"pincali:proxy_stats:{manager._process_label}")

    live = await get_live_proxy_stats()
    assert live["quota_exceeded"] is True
    assert live["quota_exceeded_reason"] == "402_payment_required"
    # Even in the "stale" (no live snapshots) fallback branch, the alert
    # must still come through.
    assert live.get("stale") in (True, False)  # either branch is fine here

    await redis.delete(PROXY_QUOTA_REDIS_KEY)
