#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run_discovery.sh
# Run the URL discovery worker once (single pass, then exit).
# Usage: ./scripts/run_discovery.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Load .env if present
if [[ -f "$PROJECT_ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/.env"
    set +a
    echo "[run_discovery] Loaded .env"
fi

cd "$PROJECT_ROOT"

echo "[run_discovery] Starting URL discovery worker (single run)..."
DISCOVERY_RUN_ONCE=true python -m app.workers.url_discovery_worker
