"""
app/main.py — FastAPI application factory.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import api_router
from app.core.config import get_settings
from app.core.logging import get_api_logger
from app.dashboard.routes import router as dashboard_router
from app.db.session import dispose_engine

settings = get_settings()
logger = get_api_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting Pincali ETL Platform API (DB: {settings.database_type})")
    yield
    await dispose_engine()
    logger.info("API shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Pincali Real Estate ETL Platform",
        description=(
            "Production-grade real estate scraping + ETL API. "
            "Supports property listing discovery, detail scraping, "
            "normalization, deduplication, and REST access."
        ),
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        swagger_ui_parameters={"tryItOutEnabled": True},
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Health check
    @app.get("/health", tags=["health"])
    async def health():
        return {"status": "ok", "database": settings.database_type, "version": "1.0.0"}

    # API routes
    app.include_router(api_router)

    # Admin dashboard
    app.include_router(dashboard_router)

    return app


app = create_app()