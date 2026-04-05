#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.env"
  set +a
fi

if [[ $# -gt 0 && -z "${REMOTE_BUILDER_BASE_URL:-}" ]]; then
  export REMOTE_BUILDER_BASE_URL="$1"
fi

if [[ -z "${REMOTE_BUILDER_BASE_URL:-}" ]]; then
  echo "Set REMOTE_BUILDER_BASE_URL in .env, export it, or pass it as the first argument."
  exit 1
fi

export PORT="${PORT:-8090}"

cd "$PROJECT_DIR"
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    echo "Python is required to run heroku-web."
    exit 1
  fi
fi

"$PYTHON_BIN" -m pip install -r requirements.txt
exec "$PYTHON_BIN" app.py
