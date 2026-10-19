from fastapi import APIRouter
from app.api.v1.listings import router as listings_router
from app.api.v1.scraper import router as scraper_router
#from app.api.v1.hub import router as hub_router
from app.api.v1 import hub

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(listings_router)
api_router.include_router(scraper_router)
api_router.include_router(hub.router, tags=["hub"])

__all__ = ["api_router"]
