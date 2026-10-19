"""
Tests for the FastAPI REST endpoints.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health(client: AsyncClient):
    """GET /health returns 200 with status ok."""
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_listings_empty(client: AsyncClient):
    """GET /api/v1/listings returns empty list when DB is empty."""
    response = await client.get("/api/v1/listings")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_listings_not_found(client: AsyncClient):
    """GET /api/v1/listings/99999 returns 404."""
    response = await client.get("/api/v1/listings/99999")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_listings_search_empty(client: AsyncClient):
    """GET /api/v1/listings/search with no results returns empty."""
    response = await client.get("/api/v1/listings/search?q=nonexistent")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_statistics(client: AsyncClient):
    """GET /api/v1/listings/statistics returns expected keys."""
    response = await client.get("/api/v1/listings/statistics")
    assert response.status_code == 200
    data = response.json()
    assert "total_listings" in data
    assert "active_listings" in data


@pytest.mark.asyncio
async def test_scraper_status(client: AsyncClient):
    """GET /api/v1/scraper/status returns expected structure."""
    response = await client.get("/api/v1/scraper/status")
    assert response.status_code == 200
    data = response.json()
    assert "pending_urls" in data
    assert "last_run" in data


@pytest.mark.asyncio
async def test_scraper_runs(client: AsyncClient):
    """GET /api/v1/scraper/runs returns a list."""
    response = await client.get("/api/v1/scraper/runs")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_scraper_errors(client: AsyncClient):
    """GET /api/v1/scraper/errors returns a list."""
    response = await client.get("/api/v1/scraper/errors")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_listings_pagination(client: AsyncClient):
    """GET /api/v1/listings pagination params are respected."""
    response = await client.get("/api/v1/listings?page=1&page_size=10")
    assert response.status_code == 200
    data = response.json()
    assert "page" in data
    assert "page_size" in data
    assert data["page"] == 1
    assert data["page_size"] == 10


@pytest.mark.asyncio
async def test_listings_filter_params(client: AsyncClient):
    """GET /api/v1/listings accepts filter query params without error."""
    response = await client.get(
        "/api/v1/listings?city=cabo&min_price=100000&max_price=500000&beds=3&property_type=B"
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_docs_available(client: AsyncClient):
    """GET /docs returns 200 (Swagger UI)."""
    response = await client.get("/docs")
    assert response.status_code == 200
