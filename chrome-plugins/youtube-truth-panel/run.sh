#!/usr/bin/env bash
# Launch the YouTube Truth Panel backend.
# Export your key first:  export YDC_API_KEY="your-you.com-api-key"
set -e
if [ -z "${YDC_API_KEY:-}" ]; then
  echo "YDC_API_KEY is not set — fact-checks will fail."
  echo "  export YDC_API_KEY='your-you.com-api-key'"
fi
export PORT="${PORT:-8765}"
echo "Starting YouTube Truth Panel backend on http://127.0.0.1:$PORT"
exec python3 "$(dirname "$0")/server.py"
