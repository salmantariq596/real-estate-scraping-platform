"""
app/scraper/proxy_manager.py — Provider-agnostic rotating proxy layer.

Implements the proxy architecture agreed with Oscar:
  - ProxyProvider: abstract adapter interface. Switching vendors = new
    adapter class + .env change, no changes anywhere else in the scraper.
  - ProxyManager: the single object the rest of the codebase talks to.
    Owns health tracking, cooldowns, automatic rotation, and simple
    concurrency/rate limiting so we don't exceed the proxy plan's limits.

Active provider is selected via PROXY_PROVIDER=static|gateway|file.
Default is "static", which reproduces the exact behavior of the old
get_next_proxy() round robin — so this ships with zero behavior change
until Oscar's provider is configured and PROXY_PROVIDER is switched.

Once Oscar confirms a vendor, only GatewayProvider's connection string
building (or a new adapter) needs vendor-specific tweaks.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import quote, unquote, urlsplit

from app.core.config import get_settings
from app.core.logging import get_scraper_logger
from app.core.redis_client import get_redis

settings = get_settings()
logger = get_scraper_logger()

# -- Cross-process visibility (PATCH) -----------------------------------------
# ProxyManager is an in-memory singleton *per process*. The dashboard runs in
# the `api` container, which never scrapes -- so its own ProxyManager is
# always idle. Every mutating call below also mirrors a snapshot to Redis
# under a key unique to this process, so the dashboard can read real state
# from the worker containers. See app/core/redis_client.py.
PROXY_STATS_REDIS_PREFIX = "pincali:proxy_stats:"
PROXY_STATS_REDIS_TTL_SECONDS = 45  # a bit over the dashboard's 30s auto-refresh

# PATCH: the quota-exceeded alert must NOT share the 45s liveness TTL above.
# Once quota is confirmed exhausted, workers correctly STOP hammering the
# vendor (that's the whole point of the fail-fast fix) -- which means they
# also stop publishing fresh per-process snapshots. With only the 45s TTL,
# the very banner meant to explain "quota exceeded" would itself expire
# within seconds of the fix successfully doing its job, leaving the
# dashboard looking empty/broken instead of clearly explained. This key is
# separate and long-lived (24h) so the alert stays visible until a real
# success explicitly clears it (see report_success()).
PROXY_QUOTA_REDIS_KEY = "pincali:proxy_quota_exceeded"
PROXY_QUOTA_REDIS_TTL_SECONDS = 24 * 60 * 60


def _process_label() -> str:
    """Human-identifiable label for whichever process/container this is,
    e.g. 'detail_scraper_worker@a1b2c3d4'. Falls back to just the hostname
    (Docker container id) if PINCALI_PROCESS_NAME isn't set."""
    name = os.environ.get("PINCALI_PROCESS_NAME", "")
    host = socket.gethostname()
    return f"{name}@{host}" if name else host


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class ProxyEndpoint:
    """A single proxy 'identity' the manager can hand out.

    For list-style providers this maps 1:1 to a proxy URL.
    For gateway-style providers this maps to one rotating *session* against
    the vendor's gateway (same host:port, different session id -> different
    exit IP), which is how sticky-session rotation works for most
    residential proxy vendors (Bright Data, Oxylabs, Smartproxy, IPRoyal, ...).
    """

    proxy_id: str
    url: str                       # value to hand to httpx/Playwright as `proxy`
    provider_name: str
    session_id: Optional[str] = None
    failure_count: int = 0
    success_count: int = 0
    consecutive_failures: int = 0
    banned_until: float = 0.0
    last_used: float = 0.0
    requests_since_rotation: int = 0
    # -- Live-activity tracking (PATCH: dashboard "which proxy is live" view) --
    in_flight: int = 0              # requests currently in progress on this endpoint
    last_url: str = ""              # most recent URL this endpoint fetched/is fetching
    last_outcome: str = ""          # "success" | "failed" | "" (never used yet)
    last_latency_ms: Optional[int] = None

    def masked_host(self) -> str:
        """host:port only, no credentials -- safe to show on the dashboard."""
        if not self.url:
            return "—"
        parts = urlsplit(self.url)
        return f"{parts.hostname}:{parts.port}" if parts.port else (parts.hostname or "—")

    def masked_session(self) -> str:
        """Short, non-secret fingerprint of the session/identity in use --
        enough to tell endpoints apart on the dashboard without leaking the
        actual gateway password embedded in `url`."""
        if self.session_id:
            return self.session_id[:8]
        if not self.url:
            return "—"
        parts = urlsplit(self.url)
        if parts.username:
            return unquote(parts.username)[:16]
        return "—"

    def is_available(self) -> bool:
        return time.time() >= self.banned_until

    def as_playwright_proxy(self) -> Optional[Dict[str, str]]:
        """Playwright's browser.launch(proxy=...) wants credentials split out
        as separate "username"/"password" fields -- it does NOT parse
        user:pass@ out of an embedded "server" URL the way httpx does. Handing
        it a server string with embedded credentials means Chromium connects
        to the proxy with no auth at all, and the gateway comes back with
        407 Proxy Authentication Required on every request.
        """
        if not self.url:
            return None
        parts = urlsplit(self.url)
        server = f"{parts.scheme}://{parts.hostname}"
        if parts.port:
            server += f":{parts.port}"
        proxy: Dict[str, str] = {"server": server}
        if parts.username:
            proxy["username"] = unquote(parts.username)
        if parts.password:
            proxy["password"] = unquote(parts.password)
        return proxy


class ProxyPoolExhausted(Exception):
    """Raised when every known proxy endpoint is currently in cooldown."""


class ProxyQuotaExceededError(Exception):
    """Raised when the proxy VENDOR itself reports an account-level hard
    stop -- e.g. Webshare's 402 Payment Required / bandwidth limit reached.

    This is fundamentally different from a single blocked/failed request:
    it's account-wide, so rotating to a different proxy endpoint in the
    same pool will never help. The retry loop should fail fast on this
    instead of burning its retry budget hitting the same wall N times.
    """
    def __init__(self, message: str, reason: str = ""):
        super().__init__(message)
        self.reason = reason


# ── Provider interface ────────────────────────────────────────────────────────

class ProxyProvider:
    """Abstract adapter. Concrete providers only need to know how to produce
    ProxyEndpoint objects — all rotation/health/throttling logic lives in
    ProxyManager so it's shared across every vendor."""

    name = "base"

    def list_endpoints(self) -> List[ProxyEndpoint]:
        """Return the pool of endpoints this provider can hand out.
        Called once at startup and whenever the manager needs to refresh."""
        raise NotImplementedError

    def new_session_id(self) -> str:
        """Generate a fresh session id for providers that rotate IP by
        changing the session component of the proxy auth (gateway style)."""
        return uuid.uuid4().hex[:12]


class StaticListProvider(ProxyProvider):
    """Today's behavior: a flat comma-separated list of full proxy URLs
    (PINCALI_PROXY_LIST). Works for datacenter proxies, a vendor-supplied
    static list of residential IP:port endpoints, or local dev/no-proxy.
    Kept as the default so this ships with zero risk."""

    name = "static"

    def list_endpoints(self) -> List[ProxyEndpoint]:
        return [
            ProxyEndpoint(proxy_id=f"static-{i}", url=proxy_url, provider_name=self.name)
            for i, proxy_url in enumerate(settings.proxy_list)
        ]


class GatewayProvider(ProxyProvider):
    """Generic single-gateway rotating-residential adapter.

    Covers the common shape used by most residential proxy vendors
    (Bright Data, Oxylabs, Smartproxy, IPRoyal, and similar): one gateway
    host:port, and the exit IP rotates per *session* by embedding a session
    id in the proxy username, e.g.:

        http://{user}-session-{session_id}:{pass}@{host}:{port}

    If Oscar's chosen vendor uses a different username templating scheme,
    only PROXY_GATEWAY_USER_TEMPLATE needs to change — no code changes
    required elsewhere.
    """

    name = "gateway"

    def list_endpoints(self) -> List[ProxyEndpoint]:
        if not settings.proxy_gateway_host or not settings.proxy_gateway_port:
            return []
        pool_size = max(1, settings.proxy_gateway_pool_size)
        return [self._build_endpoint(f"gw-{i}") for i in range(pool_size)]

    def _build_endpoint(self, proxy_id: str) -> ProxyEndpoint:
        session_id = self.new_session_id()
        user = settings.proxy_gateway_user_template.format(
            user=settings.proxy_gateway_user, session_id=session_id
        )
        # PATCH: URL-encode credentials before embedding them in the URL.
        # Without this, a username/password containing a reserved URL
        # character (@, :, %, /) silently corrupts the connection string --
        # httpx and urlsplit() would then misparse where the credentials
        # end and the host begins. Harmless today (Webshare's given
        # username/password have no special characters) but a real trap
        # for the next vendor or a rotated password with symbols in it.
        user_enc = quote(user, safe="")
        pass_enc = quote(settings.proxy_gateway_pass, safe="")
        auth = f"{user_enc}:{pass_enc}@" if settings.proxy_gateway_user else ""
        url = f"http://{auth}{settings.proxy_gateway_host}:{settings.proxy_gateway_port}"
        return ProxyEndpoint(
            proxy_id=proxy_id,
            url=url,
            provider_name=self.name,
            session_id=session_id,
        )


class FileBackedProvider(ProxyProvider):
    """Loads a pool from a local file (one proxy URL per line). Useful when
    a vendor publishes a rotating pool as a downloadable/refreshable list
    rather than a single gateway. Path is PROXY_FILE_PATH."""

    name = "file"

    def list_endpoints(self) -> List[ProxyEndpoint]:
        path = settings.proxy_file_path
        if not path:
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        except FileNotFoundError:
            logger.warning(f"[ProxyManager] PROXY_FILE_PATH not found: {path}")
            return []
        return [
            ProxyEndpoint(proxy_id=f"file-{i}", url=url, provider_name=self.name)
            for i, url in enumerate(lines)
        ]


_PROVIDERS: Dict[str, type] = {
    "static": StaticListProvider,
    "gateway": GatewayProvider,
    "file": FileBackedProvider,
}


# ── Manager ──────────────────────────────────────────────────────────────────

class ProxyManager:
    """Single entry point the rest of the scraper talks to.

    Responsibilities:
      - Own the active ProxyProvider (chosen by PROXY_PROVIDER).
      - Hand out a healthy endpoint (get_proxy) skipping anything in cooldown.
      - Track success/failure per endpoint (report_success/report_failure).
      - Auto-rotate gateway sessions after N requests even without failures.
      - Cap concurrent requests per endpoint so we don't exceed vendor limits.
      - Enforce an optional global requests/sec ceiling across the whole pool.
      - Expose stats() for logging/monitoring/dashboard use.
    """

    def __init__(self, provider: Optional[ProxyProvider] = None):
        self.provider = provider or self._build_provider()
        self._endpoints: List[ProxyEndpoint] = self.provider.list_endpoints()
        self._by_id: Dict[str, ProxyEndpoint] = {e.proxy_id: e for e in self._endpoints}
        self._rotor = 0
        self._lock = asyncio.Lock()
        self._session_semaphores: Dict[str, asyncio.Semaphore] = {}
        self._rps_window: List[float] = []
        self._process_label = _process_label()
        # Account-level hard stop (e.g. vendor billing/bandwidth limit) --
        # separate from per-endpoint cooldown, see ProxyQuotaExceededError.
        self._quota_exceeded_at: Optional[float] = None
        self._quota_exceeded_reason: str = ""

    def is_quota_exceeded(self) -> bool:
        """Quick in-process check -- used to skip the Playwright fallback
        entirely once the HTTP path has already confirmed the proxy
        account itself is out of bandwidth/credit (no point burning a
        browser launch + navigation on the exact same wall)."""
        return self._quota_exceeded_at is not None

    async def mark_quota_exceeded(self, reason: str = "") -> None:
        self._quota_exceeded_at = time.time()
        self._quota_exceeded_reason = reason
        logger.error(
            f"[ProxyManager] Proxy account-level quota/billing limit hit "
            f"(reason={reason or 'unspecified'}). Rotating proxies will NOT "
            f"help -- this needs action on the vendor account itself."
        )
        await self._publish_stats_snapshot()
        await self._publish_quota_state(exceeded=True, reason=reason)

    async def clear_quota_exceeded(self) -> None:
        """Call this once the vendor account is confirmed working again
        (e.g. after topping up bandwidth) to clear the dashboard banner."""
        self._quota_exceeded_at = None
        self._quota_exceeded_reason = ""
        await self._publish_stats_snapshot()
        await self._publish_quota_state(exceeded=False, reason="")

    async def _publish_quota_state(self, exceeded: bool, reason: str) -> None:
        """PATCH: sticky, long-TTL Redis key -- see PROXY_QUOTA_REDIS_KEY
        comment above. Deliberately separate from _publish_stats_snapshot()
        (45s TTL) so the alert survives the very silence it causes (no more
        requests -> no more per-process liveness publishes)."""
        try:
            redis = get_redis()
            if exceeded:
                await redis.set(
                    PROXY_QUOTA_REDIS_KEY,
                    json.dumps({"reason": reason, "at": time.time()}),
                    ex=PROXY_QUOTA_REDIS_TTL_SECONDS,
                )
            else:
                await redis.delete(PROXY_QUOTA_REDIS_KEY)
        except Exception as exc:  # noqa: BLE001 - visibility only, must not raise
            logger.debug(f"[ProxyManager] quota-state publish to Redis skipped: {exc}")

    @staticmethod
    def _build_provider() -> ProxyProvider:
        provider_cls = _PROVIDERS.get(settings.proxy_provider, StaticListProvider)
        return provider_cls()

    def _semaphore_for(self, proxy_id: str) -> asyncio.Semaphore:
        if proxy_id not in self._session_semaphores:
            self._session_semaphores[proxy_id] = asyncio.Semaphore(
                max(1, settings.proxy_max_concurrent_per_session)
            )
        return self._session_semaphores[proxy_id]

    async def _respect_global_rps(self):
        limit = settings.proxy_global_rps_limit
        if not limit:
            return
        async with self._lock:
            now = time.time()
            self._rps_window[:] = [t for t in self._rps_window if now - t < 1.0]
            if len(self._rps_window) >= limit:
                wait = 1.0 - (now - self._rps_window[0])
                if wait > 0:
                    await asyncio.sleep(wait)
            self._rps_window.append(time.time())

    def _refresh_if_empty(self):
        if not self._endpoints:
            self._endpoints = self.provider.list_endpoints()
            self._by_id = {e.proxy_id: e for e in self._endpoints}

    async def get_proxy(self) -> Optional[ProxyEndpoint]:
        """Return a healthy proxy endpoint, or None if no proxy is configured
        at all (falls back to direct/no-proxy connections, same as before)."""
        self._refresh_if_empty()
        if not self._endpoints:
            return None

        await self._respect_global_rps()

        async with self._lock:
            n = len(self._endpoints)
            candidates = [self._endpoints[(self._rotor + i) % n] for i in range(n)]
            self._rotor += 1

            for ep in candidates:
                if not ep.is_available():
                    continue
                # Gateway providers: rotate to a fresh session periodically so
                # we get a genuinely new exit IP, not just re-use the same one.
                if (
                    ep.provider_name == "gateway"
                    and settings.proxy_rotate_every_n_requests > 0
                    and ep.requests_since_rotation >= settings.proxy_rotate_every_n_requests
                ):
                    ep = self._rotate_gateway_session(ep)

                ep.last_used = time.time()
                ep.requests_since_rotation += 1
                return ep

        # Every endpoint currently banned/in-cooldown.
        logger.warning(
            f"[ProxyManager] All {len(self._endpoints)} proxy endpoints are in "
            f"cooldown — no healthy proxy available right now."
        )
        raise ProxyPoolExhausted("All proxy endpoints are currently in cooldown")

    def get_proxy_sync(self) -> Optional[ProxyEndpoint]:
        """Non-async convenience for call sites that only run one at a time
        (Playwright browser launch/restart) and don't need the in-request
        retry/failover loop -- just the next healthy endpoint, same
        rotation + cooldown rules as get_proxy(), without awaiting the lock.
        Safe here because these call sites launch one browser at a time.
        """
        self._refresh_if_empty()
        if not self._endpoints:
            return None
        n = len(self._endpoints)
        for i in range(n):
            ep = self._endpoints[(self._rotor + i) % n]
            if ep.is_available():
                self._rotor += 1
                ep.last_used = time.time()
                ep.requests_since_rotation += 1
                return ep
        logger.warning(
            f"[ProxyManager] get_proxy_sync: all {n} endpoints in cooldown, "
            f"returning None (caller will run direct/no-proxy)"
        )
        return None

    def _rotate_gateway_session(self, old: ProxyEndpoint) -> ProxyEndpoint:
        new_ep = self.provider._build_endpoint(old.proxy_id)  # type: ignore[attr-defined]
        new_ep.success_count = old.success_count
        new_ep.failure_count = old.failure_count
        self._by_id[new_ep.proxy_id] = new_ep
        idx = self._endpoints.index(old) if old in self._endpoints else None
        if idx is not None:
            self._endpoints[idx] = new_ep
        return new_ep

    async def mark_request_start(self, endpoint: Optional[ProxyEndpoint], url: str) -> None:
        """Record that a request just started on this endpoint. Publishes
        immediately (before the outcome is known) so the dashboard can show
        which proxy is live *right now*, mid-request -- not just after the
        fact via success/failure counts."""
        if endpoint is None:
            return
        endpoint.in_flight += 1
        endpoint.last_url = url
        endpoint.last_used = time.time()
        await self._publish_stats_snapshot()

    async def mark_request_end(
        self,
        endpoint: Optional[ProxyEndpoint],
        outcome: str,
        latency_ms: Optional[int] = None,
    ) -> None:
        """Pair with mark_request_start -- always call even on failure/timeout
        so in_flight doesn't get stuck incremented."""
        if endpoint is None:
            return
        endpoint.in_flight = max(0, endpoint.in_flight - 1)
        endpoint.last_outcome = outcome
        if latency_ms is not None:
            endpoint.last_latency_ms = latency_ms
        await self._publish_stats_snapshot()

    async def report_success(self, endpoint: Optional[ProxyEndpoint]) -> None:
        if endpoint is None:
            return
        async with self._lock:
            endpoint.success_count += 1
            endpoint.consecutive_failures = 0
        if self._quota_exceeded_at is not None:
            # PATCH: a real success proves the account-level quota/billing
            # wall has cleared (bandwidth topped up, plan renewed, etc.) --
            # auto-clear so the Playwright short-circuit in scrape_url()
            # doesn't stay permanently stuck skipping the browser fallback.
            logger.info(
                "[ProxyManager] Fetch succeeded after a prior quota-exceeded "
                "state -- clearing it, proxy account appears healthy again."
            )
            self._quota_exceeded_at = None
            self._quota_exceeded_reason = ""
            await self._publish_quota_state(exceeded=False, reason="")
        await self._publish_stats_snapshot()

    async def report_failure(self, endpoint: Optional[ProxyEndpoint], reason: str = "") -> None:
        if endpoint is None:
            return
        async with self._lock:
            endpoint.failure_count += 1
            endpoint.consecutive_failures += 1
            if endpoint.consecutive_failures >= settings.proxy_failure_threshold:
                endpoint.banned_until = time.time() + settings.proxy_cooldown_seconds
                logger.warning(
                    f"[ProxyManager] Proxy {endpoint.proxy_id} ({endpoint.provider_name}) "
                    f"cooling down for {settings.proxy_cooldown_seconds}s after "
                    f"{endpoint.consecutive_failures} consecutive failures "
                    f"(reason: {reason or 'unspecified'})"
                )
            # Force a fresh session next time this endpoint is picked.
            if endpoint.provider_name == "gateway":
                endpoint.requests_since_rotation = settings.proxy_rotate_every_n_requests
        await self._publish_stats_snapshot()

    async def _publish_stats_snapshot(self) -> None:
        """PATCH: mirror this process's in-memory stats into Redis so the
        dashboard (running in a different container/process) can see real,
        live state instead of its own always-idle ProxyManager. Best-effort
        only -- never let a Redis hiccup break the actual scrape."""
        try:
            payload = self.stats()
            payload["process"] = self._process_label
            payload["updated_at"] = time.time()
            redis = get_redis()
            await redis.set(
                f"{PROXY_STATS_REDIS_PREFIX}{self._process_label}",
                json.dumps(payload),
                ex=PROXY_STATS_REDIS_TTL_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - visibility only, must not raise
            logger.debug(f"[ProxyManager] stats publish to Redis skipped: {exc}")

    def semaphore_for(self, endpoint: Optional[ProxyEndpoint]) -> Optional[asyncio.Semaphore]:
        if endpoint is None:
            return None
        return self._semaphore_for(endpoint.proxy_id)

    def stats(self) -> Dict[str, object]:
        """Snapshot for logging / monitoring endpoint / dashboard."""
        now = time.time()
        return {
            "provider": self.provider.name,
            "pool_size": len(self._endpoints),
            "quota_exceeded": self._quota_exceeded_at is not None,
            "quota_exceeded_reason": self._quota_exceeded_reason,
            "quota_exceeded_ago_s": (
                round(now - self._quota_exceeded_at) if self._quota_exceeded_at else None
            ),
            "endpoints": [
                {
                    "proxy_id": e.proxy_id,
                    "provider": e.provider_name,
                    "success_count": e.success_count,
                    "failure_count": e.failure_count,
                    "consecutive_failures": e.consecutive_failures,
                    "in_cooldown": e.banned_until > now,
                    "cooldown_remaining_s": max(0, round(e.banned_until - now)),
                    "requests_since_rotation": e.requests_since_rotation,
                    # -- Live-activity fields --
                    "in_flight": e.in_flight,
                    "last_url": e.last_url,
                    "last_outcome": e.last_outcome,
                    "last_latency_ms": e.last_latency_ms,
                    "last_used_ago_s": (round(now - e.last_used) if e.last_used else None),
                    "host": e.masked_host(),
                    "session": e.masked_session(),
                }
                for e in self._endpoints
            ],
        }


# ── Module-level singleton (mirrors the old get_next_proxy() convenience) ────

_manager: Optional[ProxyManager] = None


def get_proxy_manager() -> ProxyManager:
    global _manager
    if _manager is None:
        _manager = ProxyManager()
    return _manager


def reset_proxy_manager() -> None:
    """Rebuild the singleton — used by tests and by config reloads."""
    global _manager
    _manager = None


async def get_cluster_proxy_snapshots() -> List[Dict[str, object]]:
    """PATCH: read every worker process's published proxy stats snapshot
    from Redis (see _publish_stats_snapshot above). This is what the
    dashboard should read from -- NOT get_proxy_manager().stats(), which
    only reflects the dashboard's own (idle) in-process copy.

    Returns one snapshot dict per live process, most-recently-updated
    first. Empty list if Redis is unreachable or nothing has published yet
    (e.g. workers haven't made a request since startup, or PROXY_STATS_
    REDIS_TTL_SECONDS expired because scraping has been idle for a while).
    """
    try:
        redis = get_redis()
        keys = [k async for k in redis.scan_iter(match=f"{PROXY_STATS_REDIS_PREFIX}*")]
        if not keys:
            return []
        raw_values = await redis.mget(keys)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ProxyManager] could not read cluster proxy stats from Redis: {exc}")
        return []

    snapshots: List[Dict[str, object]] = []
    for raw in raw_values:
        if not raw:
            continue
        try:
            snapshots.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    snapshots.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
    return snapshots


async def _read_sticky_quota_state() -> Dict[str, object]:
    """Reads the long-TTL sticky quota-exceeded key (see
    PROXY_QUOTA_REDIS_KEY) -- this is the authoritative source, since it's
    specifically designed to survive the silence that a successful
    fail-fast fix causes (no more requests once quota is confirmed
    exhausted -> no more per-process liveness publishes)."""
    try:
        redis = get_redis()
        raw = await redis.get(PROXY_QUOTA_REDIS_KEY)
        if not raw:
            return {"quota_exceeded": False, "quota_exceeded_reason": "", "quota_exceeded_ago_s": None}
        payload = json.loads(raw)
        return {
            "quota_exceeded": True,
            "quota_exceeded_reason": payload.get("reason", ""),
            "quota_exceeded_ago_s": round(time.time() - payload.get("at", time.time())),
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[ProxyManager] could not read sticky quota state from Redis: {exc}")
        return {"quota_exceeded": False, "quota_exceeded_reason": "", "quota_exceeded_ago_s": None}


async def get_live_proxy_stats() -> Dict[str, object]:
    """Single source of truth for 'what's really happening with proxies
    right now', used by both the dashboard (/admin/proxies) and the API
    (/api/v1/scraper/proxy-stats). Aggregates per-process snapshots
    published to Redis by the actual worker processes -- NOT
    get_proxy_manager().stats() on whichever process calls this, which
    would only reflect that process's own (often idle) ProxyManager.

    Falls back to the local ProxyManager (flagged "stale": True) only if
    no worker has published a snapshot yet. The quota-exceeded alert is
    read from a SEPARATE sticky key (see _read_sticky_quota_state) in
    BOTH branches below, since it must stay visible even when scraping
    has correctly gone quiet and per-process snapshots have expired.
    """
    quota_state = await _read_sticky_quota_state()
    snapshots = await get_cluster_proxy_snapshots()

    if not snapshots:
        stats = get_proxy_manager().stats()
        stats["stale"] = True
        stats.update(quota_state)
        return stats

    provider = snapshots[0].get("provider", "static")
    pool_size = sum(snap.get("pool_size", 0) for snap in snapshots)
    endpoints: List[Dict[str, object]] = []
    for snap in snapshots:
        process_label = snap.get("process", "unknown")
        for ep in snap.get("endpoints", []):
            ep = dict(ep)
            # Different processes each build their own pool of e.g.
            # "gw-0".."gw-9" -- tag with the process so rows in the table
            # aren't silently merged/overwritten across workers.
            ep["proxy_id"] = f"{process_label} / {ep['proxy_id']}"
            endpoints.append(ep)

    endpoints.sort(
        key=lambda e: (
            e.get("in_flight", 0) == 0,
            e.get("last_used_ago_s") is None,
            e.get("last_used_ago_s") or float("inf"),
        )
    )

    return {
        "provider": provider,
        "pool_size": pool_size,
        "endpoints": endpoints,
        "stale": False,
        **quota_state,
    }