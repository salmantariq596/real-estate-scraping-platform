#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_dev.sh
# Bootstrap a local development environment.
# Usage: ./scripts/setup_dev.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "── Pincali ETL Platform — Dev Setup ────────────────────────────────────"

# 1. Copy .env if missing
if [[ ! -f .env ]]; then
    echo "[setup] Creating .env from .env.example"
    cp .env.example .env
    echo "[setup] ⚠  Edit .env before running workers!"
fi

# 2. Create virtualenv
if [[ ! -d .venv ]]; then
    echo "[setup] Creating Python virtual environment..."
    python3 -m venv .venv
fi

# 3. Install dependencies
echo "[setup] Installing Python dependencies..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q

# 4. Install Playwright browser
echo "[setup] Installing Playwright Chromium..."
.venv/bin/playwright install chromium

# 5. Run migrations (requires a running DB)
echo ""
echo "[setup] ── Skipping auto-migration (requires a running database) ──"
echo "[setup]    Run:  ./scripts/migrate.sh"
echo ""

echo "── Setup complete ──────────────────────────────────────────────────────"
echo ""
echo "  Activate venv:   source .venv/bin/activate"
echo "  Run API:         uvicorn app.main:app --reload"
echo "  Run discovery:   ./scripts/run_discovery.sh"
echo "  Run scraper:     ./scripts/run_scraper.sh"
echo "  Run tests:       pytest"
echo ""
