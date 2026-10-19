"""
tests/test_proxy_manager.py — Unit tests for app/scraper/proxy_manager.py.

Pure logic tests, no network calls and no live proxy vendor calls in CI --
provider adapters are tested against config-driven pools only.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.config import get_settings
from app.scraper.proxy_manager import (
    FileBackedProvider,
    GatewayProvider,
    ProxyEndpoint,
    ProxyManager,
    ProxyPoolExhausted,
    StaticListProvider,
    get_proxy_manager,
    reset_proxy_manager,
)


@pytest.fixture(autouse=True)
def _reset_manager_singleton():
    reset_proxy_manager()
    yield
    reset_proxy_manager()


# ── StaticListProvider ───────────────────────────────────────────────────────

def test_static_provider_empty_by_default():
    settings = get_settings()
    settings.pincali_proxy_list = ""
    provider = StaticListProvider()
    assert provider.list_endpoints() == []


def test_static_provider_parses_comma_separated_list():
    settings = get_settings()
    settings.pincali_proxy_list = "http://p1:8080,http://p2:8080,http://p3:8080"
    provider = StaticListProvider()
    endpoints = provider.list_endpoints()
    assert len(endpoints) == 3
    assert endpoints[0].url == "http://p1:8080"
    assert endpoints[0].provider_name == "static"


# ── GatewayProvider ──────────────────────────────────────────────────────────

def test_gateway_provider_empty_without_host():
    settings = get_settings()
    settings.proxy_gateway_host = ""
    provider = GatewayProvider()
    assert provider.list_endpoints() == []


def test_gateway_provider_builds_pool_with_session_ids():
    settings = get_settings()
    settings.proxy_gateway_host = "gw.vendor.example"
    settings.proxy_gateway_port = 22225
    settings.proxy_gateway_user = "acct123"
    settings.proxy_gateway_pass = "secret"
    settings.proxy_gateway_pool_size = 4
    # PATCH: set explicitly rather than relying on the code-level default --
    # this test was silently coupled to whatever PROXY_GATEWAY_USER_TEMPLATE
    # happens to be in the real .env loaded by get_settings(), which broke
    # the moment .env was correctly overridden to "{user}" for Webshare.
    settings.proxy_gateway_user_template = "{user}-session-{session_id}"

    provider = GatewayProvider()
    endpoints = provider.list_endpoints()

    assert len(endpoints) == 4
    for ep in endpoints:
        assert ep.provider_name == "gateway"
        assert "gw.vendor.example:22225" in ep.url
        assert "acct123-session-" in ep.url
        assert ep.session_id and ep.session_id in ep.url

    # Every endpoint gets a distinct session id -> distinct exit IP
    session_ids = {ep.session_id for ep in endpoints}
    assert len(session_ids) == 4


def test_gateway_provider_custom_username_template():
    settings = get_settings()
    settings.proxy_gateway_host = "gw.vendor.example"
    settings.proxy_gateway_port = 22225
    settings.proxy_gateway_user = "acct123"
    settings.proxy_gateway_pass = "secret"
    settings.proxy_gateway_pool_size = 1
    settings.proxy_gateway_user_template = "{user}_rotate_{session_id}"

    provider = GatewayProvider()
    ep = provider.list_endpoints()[0]
    assert "acct123_rotate_" in ep.url


def test_gateway_provider_url_encodes_special_characters_in_credentials():
    """A username/password containing reserved URL characters (@, :, %, /)
    must not corrupt the connection string -- round-trip through
    as_playwright_proxy() should recover the exact original credentials."""
    settings = get_settings()
    settings.proxy_gateway_host = "p.webshare.io"
    settings.proxy_gateway_port = 80
    settings.proxy_gateway_user = "user@name:weird/chars%"
    settings.proxy_gateway_pass = "p@ss:word/with%percent"
    settings.proxy_gateway_pool_size = 1
    settings.proxy_gateway_user_template = "{user}"

    provider = GatewayProvider()
    ep = provider.list_endpoints()[0]

    playwright_proxy = ep.as_playwright_proxy()
    assert playwright_proxy["server"] == "http://p.webshare.io:80"
    assert playwright_proxy["username"] == "user@name:weird/chars%"
    assert playwright_proxy["password"] == "p@ss:word/with%percent"


# ── FileBackedProvider ───────────────────────────────────────────────────────

def test_file_backed_provider_missing_path_returns_empty():
    settings = get_settings()
    settings.proxy_file_path = ""
    provider = FileBackedProvider()
    assert provider.list_endpoints() == []


def test_file_backed_provider_reads_lines(tmp_path):
    settings = get_settings()
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text("http://a:1\n# comment\nhttp://b:2\n\nhttp://c:3\n")
    settings.proxy_file_path = str(proxy_file)

    provider = FileBackedProvider()
    endpoints = provider.list_endpoints()
    urls = [e.url for e in endpoints]
    assert urls == ["http://a:1", "http://b:2", "http://c:3"]


# ── ProxyManager: rotation ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_manager_returns_none_when_no_proxies_configured():
    settings = get_settings()
    settings.pincali_proxy_list = ""
    settings.proxy_provider = "static"
    manager = ProxyManager(provider=StaticListProvider())
    endpoint = await manager.get_proxy()
    assert endpoint is None


@pytest.mark.asyncio
async def test_manager_round_robins_across_pool():
    settings = get_settings()
    settings.pincali_proxy_list = "http://p1:8080,http://p2:8080"
    manager = ProxyManager(provider=StaticListProvider())

    seen = []
    for _ in range(4):
        ep = await manager.get_proxy()
        seen.append(ep.proxy_id)

    # Should alternate between the two endpoints
    assert seen[0] != seen[1]
    assert seen[0] == seen[2]
    assert seen[1] == seen[3]


def test_get_proxy_sync_matches_async_rotation_behavior():
    settings = get_settings()
    settings.pincali_proxy_list = "http://p1:8080,http://p2:8080"
    manager = ProxyManager(provider=StaticListProvider())

    first = manager.get_proxy_sync()
    second = manager.get_proxy_sync()
    assert first.proxy_id != second.proxy_id


# ── ProxyManager: health / cooldown ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_repeated_failures_put_endpoint_in_cooldown_and_skip_it():
    settings = get_settings()
    settings.pincali_proxy_list = "http://p1:8080,http://p2:8080"
    settings.proxy_failure_threshold = 2
    settings.proxy_cooldown_seconds = 60
    manager = ProxyManager(provider=StaticListProvider())

    ep1 = await manager.get_proxy()
    await manager.report_failure(ep1, reason="test")
    await manager.report_failure(ep1, reason="test")  # hits threshold -> cooldown

    # Next N picks should never return the cooled-down endpoint
    for _ in range(5):
        ep = await manager.get_proxy()
        assert ep.proxy_id != ep1.proxy_id


@pytest.mark.asyncio
async def test_success_resets_consecutive_failure_counter():
    settings = get_settings()
    settings.pincali_proxy_list = "http://p1:8080"
    settings.proxy_failure_threshold = 3
    manager = ProxyManager(provider=StaticListProvider())

    ep = await manager.get_proxy()
    await manager.report_failure(ep, reason="test")
    await manager.report_failure(ep, reason="test")
    await manager.report_success(ep)

    assert ep.consecutive_failures == 0
    # Should still be usable (not in cooldown) since success reset the streak
    ep2 = await manager.get_proxy()
    assert ep2.proxy_id == ep.proxy_id


@pytest.mark.asyncio
async def test_all_endpoints_in_cooldown_raises_pool_exhausted():
    settings = get_settings()
    settings.pincali_proxy_list = "http://p1:8080"
    settings.proxy_failure_threshold = 1
    settings.proxy_cooldown_seconds = 60
    manager = ProxyManager(provider=StaticListProvider())

    ep = await manager.get_proxy()
    await manager.report_failure(ep, reason="test")  # only endpoint -> cooldown

    with pytest.raises(ProxyPoolExhausted):
        await manager.get_proxy()


# ── ProxyManager: gateway session rotation ──────────────────────────────────

@pytest.mark.asyncio
async def test_gateway_session_rotates_after_n_requests():
    settings = get_settings()
    settings.proxy_gateway_host = "gw.vendor.example"
    settings.proxy_gateway_port = 22225
    settings.proxy_gateway_user = "acct123"
    settings.proxy_gateway_pass = "secret"
    settings.proxy_gateway_pool_size = 1
    settings.proxy_rotate_every_n_requests = 1

    manager = ProxyManager(provider=GatewayProvider())

    first = await manager.get_proxy()   # session A, 1st use
    second = await manager.get_proxy()  # A has now been used >= N times -> fresh session
    assert first.proxy_id == second.proxy_id
    assert first.session_id != second.session_id


# ── ProxyManager: stats ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stats_reports_pool_and_counters():
    settings = get_settings()
    settings.pincali_proxy_list = "http://p1:8080,http://p2:8080"
    manager = ProxyManager(provider=StaticListProvider())

    ep = await manager.get_proxy()
    await manager.report_success(ep)

    stats = manager.stats()
    assert stats["provider"] == "static"
    assert stats["pool_size"] == 2
    matching = [e for e in stats["endpoints"] if e["proxy_id"] == ep.proxy_id][0]
    assert matching["success_count"] == 1
    assert matching["in_cooldown"] is False


# ── Provider-agnostic swap ───────────────────────────────────────────────────

def test_switching_provider_requires_only_config_change():
    """The core acceptance criterion from the requirements doc: swapping
    providers is a PROXY_PROVIDER env change, not a code change."""
    settings = get_settings()
    settings.pincali_proxy_list = "http://p1:8080"
    settings.proxy_gateway_host = "gw.vendor.example"
    settings.proxy_gateway_port = 1234
    settings.proxy_gateway_user = "u"
    settings.proxy_gateway_pass = "p"
    settings.proxy_gateway_pool_size = 1

    settings.proxy_provider = "static"
    manager_static = ProxyManager()
    assert manager_static.provider.name == "static"

    settings.proxy_provider = "gateway"
    manager_gateway = ProxyManager()
    assert manager_gateway.provider.name == "gateway"


@pytest.mark.asyncio
async def test_singleton_accessor_returns_same_instance():
    m1 = get_proxy_manager()
    m2 = get_proxy_manager()
    assert m1 is m2
    reset_proxy_manager()
    m3 = get_proxy_manager()
    assert m3 is not m1
