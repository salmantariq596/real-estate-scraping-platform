from app.workers.url_discovery_worker import URLDiscoveryWorker
from app.workers.detail_scraper_worker import DetailScraperWorker

# Lazy imports to avoid circular dependencies when running individual workers
__all__ = ["URLDiscoveryWorker", "DetailScraperWorker"]

def __getattr__(name):
    if name == "URLDiscoveryWorker":
        from app.workers.url_discovery_worker import URLDiscoveryWorker
        return URLDiscoveryWorker
    if name == "DetailScraperWorker":
        from app.workers.detail_scraper_worker import DetailScraperWorker
        return DetailScraperWorker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")