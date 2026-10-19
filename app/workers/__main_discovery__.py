"""
Entry point: python -m app.workers.url_discovery_worker
"""
import asyncio
from app.workers.url_discovery_worker import URLDiscoveryWorker


def main() -> None:
    worker = URLDiscoveryWorker()
    asyncio.run(worker.run_loop())


if __name__ == "__main__":
    main()
