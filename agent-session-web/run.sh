#!/usr/bin/env bash
# Build the frontend (if needed) and start the agent-session-web server.
#
# Usage:
#   ./run.sh            Build frontend if missing, then start the server
#   ./run.sh --build    Force a fresh frontend build before starting
set -euo pipefail

cd "$(dirname "$0")"

force_build=false
if [[ "${1:-}" == "--build" ]]; then
  force_build=true
fi

# Build the SPA into frontend/dist unless it already exists.
if [[ "$force_build" == true || ! -f frontend/dist/index.html ]]; then
  echo "Building frontend..."
  if [[ ! -d frontend/node_modules ]]; then
    npm --prefix frontend install
  fi
  npm --prefix frontend run build
fi

# Bind on all interfaces so the server is reachable from the network.
# Override host/port with AGENT_SESSION_WEB_HOST / AGENT_SESSION_WEB_PORT.
export AGENT_SESSION_WEB_HOST="${AGENT_SESSION_WEB_HOST:-0.0.0.0}"

echo "Starting agent-session-web on ${AGENT_SESSION_WEB_HOST}:${AGENT_SESSION_WEB_PORT:-8770}..."
exec uv run agent-session-web
