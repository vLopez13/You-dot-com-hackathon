#!/usr/bin/env bash
# Launch the YouTube Truth Panel backend.
# Loads you.com/.env (or a local .env) when present.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
for envf in "$DIR/.env" "$DIR/../../you.com/.env"; do
  if [[ -f "$envf" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$envf"
    set +a
    break
  fi
done
if [[ -z "${YDC_API_KEY:-}" ]]; then
  echo "YDC_API_KEY is not set — fact-checks will fail."
  echo "  Put it in you.com/.env or: export YDC_API_KEY='your-you.com-api-key'"
fi
export PORT="${PORT:-8765}"
echo "Starting YouTube Truth Panel backend on http://127.0.0.1:$PORT"
exec python3 "$DIR/server.py"
