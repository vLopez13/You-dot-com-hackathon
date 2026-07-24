#!/usr/bin/env bash
# Launch the LiveCheck podcast fact detector.
# Prefer a local .env for the API key (gitignored). Do not commit real keys.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$DIR/.env" ]]; then
  # shellcheck disable=SC1091
  set -a
  source "$DIR/.env"
  set +a
fi
if [[ -z "${YDC_API_KEY:-}" || "$YDC_API_KEY" == *"***"* ]]; then
  echo "Missing YDC_API_KEY. Put it in you.com/.env or: export YDC_API_KEY='your-key'"
  exit 1
fi
export YDC_API_KEY
export PORT="${PORT:-8000}"
echo "Starting LiveCheck on http://localhost:$PORT  (open in Chrome or Edge)"
python3 "$DIR/server.py"
