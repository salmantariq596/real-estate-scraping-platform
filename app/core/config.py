"""
app/core/config.py — Central configuration via pydantic-settings.
All settings are loaded from environment variables / .env file.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import List, Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ────────────────────────────────────────────
    database_type: str = "postgresql"          # postgresql | mysql
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "pincali"
    postgres_user: str = "pincali"
    postgres_password: str = "changeme"

    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_db: str = "pincali"
    mysql_user: str = "pincali"
    mysql_password: str = "changeme"

    # Full override (takes precedence)
    database_url: Optional[str] = None

    # ── Database connection pool ─────────────────────────────
    db_pool_size: int = 15
    db_pool_max_overflow: int = 30
    db_pool_timeout: int = 60
    db_pool_recycle: int = 1800

    # ── Redis ───────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── API ─────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_debug: bool = False
    secret_key: str = "change-this-secret-key"
    allowed_origins: str = "http://localhost:3000,http://localhost:8000"

    # ── Scraper ─────────────────────────────────────────────
    pincali_concurrency: int = 5
    pincali_headless: bool = True
    pincali_timeout: int = 50000
    pincali_max_retries_per_url: int = 2

    # How long a failed URL sits before it's automatically requeued for
    # retry. Without this delay, a URL that keeps failing gets retried on
    # every single worker pass (could be minutes apart), burning through
    # retry budget and proxy attempts with no time for a block to clear.
    pincali_failed_url_requeue_after_hours: int = 12
    pincali_browser_restart_every: int = 100
    pincali_rate_limit_min: float = 1.0
    pincali_rate_limit_max: float = 3.0
    pincali_http_first: bool = True
    pincali_block_assets: bool = True
    pincali_browser_max_memory_mb: int = 500
    pincali_enable_keyword_learning: bool = True
    pincali_keyword_save_every: int = 100
    pincali_hub_source_name: str = "pincali"
    pincali_proxy_list: str = ""

    # PATCH: Circuit breaker settings (was hardcoded to concurrency=3)
    pincali_circuit_failure_threshold: int = 15
    pincali_circuit_ban_seconds: int = 120

    # ── Proxy provider (rotating residential proxy support) ─────────────────
    # static  = comma-separated list in PINCALI_PROXY_LIST (default, current behavior)
    # gateway = single rotating-residential gateway (Bright Data / Oxylabs /
    #           Smartproxy / IPRoyal style — vendor confirmed later by Oscar)
    # file    = pool loaded from a local file (PROXY_FILE_PATH)
    proxy_provider: str = "static"

    # How many requests a single proxy/gateway session handles before the
    # manager rotates it to a fresh session (gateway provider only).
    proxy_rotate_every_n_requests: int = 1

    # How many consecutive failures a single proxy endpoint tolerates before
    # it's put in cooldown and skipped by rotation.
    proxy_failure_threshold: int = 3

    # How long (seconds) a failed proxy endpoint stays in cooldown.
    proxy_cooldown_seconds: int = 120

    # Max concurrent in-flight requests per proxy/session (respects vendor
    # concurrent-connection limits). 0 disables the cap (not recommended).
    proxy_max_concurrent_per_session: int = 5

    # Global requests/sec ceiling across the whole proxy pool. 0 = unlimited.
    proxy_global_rps_limit: int = 0

    # In-request retry loop (proxy-level failures only — see scrape_url()).
    proxy_max_retries_per_request: int = 3
    proxy_retry_backoff_base_seconds: float = 1.0

    # ── Gateway provider settings (used when proxy_provider=gateway) ────────
    proxy_gateway_host: str = ""
    proxy_gateway_port: Optional[int] = None
    proxy_gateway_user: str = ""
    proxy_gateway_pass: str = ""
    # {user} and {session_id} are substituted. Override if the eventual
    # vendor uses a different username templating scheme.
    proxy_gateway_user_template: str = "{user}-session-{session_id}"
    proxy_gateway_pool_size: int = 10

    # ── File-backed provider settings (used when proxy_provider=file) ───────
    proxy_file_path: str = ""

    # ── Exchange Rate ────────────────────────────────────────
    mxn_to_usd_rate: float = 20.5
    exchange_rate_mode: str = "static"   # static | live
    exchange_api_key: str = ""

    # ── Workers ─────────────────────────────────────────────
    discovery_interval_seconds: int = 3600
    discovery_sort_methods: str = "newest,oldest,expensive,cheapest"
    discovery_max_pages_per_filter: int = 101
    scraper_batch_size: int = 50
    scraper_poll_interval: int = 10
    scraper_rescrape_after_hours: int = 24

    # ── Logging ─────────────────────────────────────────────
    log_level: str = "INFO"
    log_dir: str = "logs"

    # ── Metrics ─────────────────────────────────────────────
    prometheus_port: int = 9090

    @field_validator("proxy_gateway_port", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """Env vars are always strings; treat an empty PROXY_GATEWAY_PORT
        (unset vendor) as None instead of a validation error."""
        if v == "" or v is None:
            return None
        return v

    # ── Computed properties ──────────────────────────────────
    @property
    def sync_database_url(self) -> str:
        """Synchronous DB URL for Alembic migrations."""
        if self.database_url:
            return self.database_url.replace("+asyncpg", "").replace("+aiomysql", "")
        if self.database_type == "mysql":
            return (
                f"mysql+pymysql://{self.mysql_user}:{self.mysql_password}"
                f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}"
            )
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def async_database_url(self) -> str:
        """Async DB URL for SQLAlchemy async engine."""
        if self.database_url:
            return self.database_url
        if self.database_type == "mysql":
            return (
                f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
                f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}"
            )
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def allowed_origins_list(self) -> List[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def proxy_list(self) -> List[str]:
        if not self.pincali_proxy_list:
            return []
        return [p.strip() for p in self.pincali_proxy_list.split(",") if p.strip()]

    @property
    def discovery_sort_methods_list(self) -> List[str]:
        return [s.strip() for s in self.discovery_sort_methods.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()