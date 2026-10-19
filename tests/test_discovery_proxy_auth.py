"""
tests/test_discovery_proxy_auth.py — Regression test for the discovery-worker
proxy auth bug.

Bug: DiscoveryService.__aenter__ used to call get_next_proxy(), which returns
the Playwright-shaped dict (credentials split into separate username/password
fields -- correct for browser.launch(), wrong for httpx). It then took only
the bare "server" value (no credentials) and handed that to
httpx.AsyncClient(proxy=...), which means every discovery request hit the
gateway with NO auth at all -- a guaranteed 407 Proxy Authentication Required
on every single fetch, even with fully correct vendor credentials configured.

Fix: pull the full credential-embedded URL directly from the ProxyEndpoint
(the same endpoint.url pattern app/scraper/core.py already uses correctly
for the detail-scraper's HTTP path).

This test doesn't hit the network -- it patches httpx.AsyncClient to capture
the constructor kwargs and asserts the proxy string actually contains the
configured username/password.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import get_settings
from app.scraper.discovery import DiscoveryService
from app.scraper.proxy_manager import (
    GatewayProvider,
    ProxyEndpoint,
    get_proxy_manager,
    reset_proxy_manager,
)


@pytest.fixture(autouse=True)
def _reset_manager_singleton():
    reset_proxy_manager()
    yield
    reset_proxy_manager()


@pytest.fixture
def _gateway_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "proxy_provider", "gateway", raising=False)
    monkeypatch.setattr(settings, "proxy_gateway_host", "p.webshare.io", raising=False)
    monkeypatch.setattr(settings, "proxy_gateway_port", 80, raising=False)
    monkeypatch.setattr(settings, "proxy_gateway_user", "testuser", raising=False)
    monkeypatch.setattr(settings, "proxy_gateway_pass", "supersecret", raising=False)
    monkeypatch.setattr(settings, "proxy_gateway_user_template", "{user}", raising=False)
    monkeypatch.setattr(settings, "proxy_gateway_pool_size", 3, raising=False)
    return settings


@pytest.mark.asyncio
async def test_discovery_httpx_client_receives_authenticated_proxy_url(_gateway_settings):
    """The proxy string handed to httpx.AsyncClient must embed credentials --
    a bare host:port (no user:pass@) is the exact shape that caused the 407s."""
    captured_kwargs = {}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)

        async def aclose(self):
            pass

    with patch("app.scraper.discovery.httpx.AsyncClient", _FakeAsyncClient):
        service = DiscoveryService()
        await service.__aenter__()
        await service.__aexit__(None, None, None)

    proxy_value = captured_kwargs.get("proxy")
    assert proxy_value is not None, "discovery client was launched with no proxy at all"
    assert "@" in proxy_value, (
        f"proxy URL has no embedded credentials (auth-less server URL): {proxy_value!r} -- "
        "this is the exact bug that caused 407 Proxy Authentication Required on every "
        "discovery request"
    )
    assert "testuser" in proxy_value
    assert "supersecret" in proxy_value
    assert proxy_value.startswith("http://")


@pytest.mark.asyncio
async def test_discovery_proxy_matches_manager_endpoint_url(_gateway_settings):
    """The exact string discovery hands to httpx should be the same
    credential-embedded URL the ProxyEndpoint itself carries -- i.e. no
    lossy re-derivation via the Playwright-shaped dict."""
    manager = get_proxy_manager()
    endpoint = manager.get_proxy_sync()
    assert endpoint is not None

    captured_kwargs = {}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)

        async def aclose(self):
            pass

    reset_proxy_manager()  # fresh pool so the next get_proxy_sync() inside discovery is deterministic-ish
    with patch("app.scraper.discovery.httpx.AsyncClient", _FakeAsyncClient):
        service = DiscoveryService()
        await service.__aenter__()
        await service.__aexit__(None, None, None)

    proxy_value = captured_kwargs.get("proxy")
    assert proxy_value is not None
    # Same shape as ProxyEndpoint.url (scheme://user:pass@host:port), not the
    # Playwright dict's bare "server" field.
    assert proxy_value.startswith("http://testuser:supersecret@p.webshare.io:80")
