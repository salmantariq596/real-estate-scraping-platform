#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run_scraper.sh
# Run the detail scraper worker (loops until all pending URLs are processed).
# Usage: ./scripts/run_scraper.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

if [[ -f "$PROJECT_ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/.env"
    set +a
    echo "[run_scraper] Loaded .env"
fi

cd "$PROJECT_ROOT"

echo "[run_scraper] Starting detail scraper worker..."
python -m app.workers.detail_scraper_worker
