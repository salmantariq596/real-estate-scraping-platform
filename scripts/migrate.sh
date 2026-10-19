#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# migrate.sh
# Apply pending Alembic database migrations.
# Usage: ./scripts/migrate.sh [upgrade|downgrade|current|history]
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

if [[ -f "$PROJECT_ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/.env"
    set +a
fi

cd "$PROJECT_ROOT"

COMMAND="${1:-upgrade}"
TARGET="${2:-head}"

echo "[migrate] Running: alembic $COMMAND $TARGET"

case "$COMMAND" in
    upgrade)   alembic upgrade "$TARGET" ;;
    downgrade) alembic downgrade "$TARGET" ;;
    current)   alembic current ;;
    history)   alembic history ;;
    *)
        echo "Usage: $0 [upgrade|downgrade|current|history] [target]"
        exit 1
        ;;
esac

echo "[migrate] Done."
