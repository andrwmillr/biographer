#!/usr/bin/env bash
# Dev backend — uvicorn without --reload, so editing api/*.py while a
# WebSocket session is live doesn't kill the connection. Restart this
# manually (Ctrl+C + re-run) when you've made changes you want loaded.
#
# Run from repo root:
#   _web/dev.sh
set -euo pipefail
cd "$(dirname "$0")"
exec uv run \
  --with 'fastapi[standard]' \
  --with anthropic \
  --with pyyaml \
  --with claude-agent-sdk \
  python -m uvicorn api.server:app --host 0.0.0.0 --port 8000
