#!/usr/bin/env bash
# Stops the MCP servers started by start-k8s-mcp.sh.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

for name in sandbox prod; do
  pidfile="$SENTINEL_RUN_DIR/mcp-$name.pid"
  port="${SANDBOX_MCP_PORT:-3001}"
  [ "$name" = "prod" ] && port="${PROD_MCP_PORT:-3002}"

  # A failed npx restart can replace the PID file while the old child still owns
  # the port. Discover listeners too, so the next start always uses the current token.
  listener_pids="$(lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  for listener_pid in $listener_pids; do
    pkill -P "$listener_pid" 2>/dev/null || true
    kill "$listener_pid" 2>/dev/null || true
  done

  if [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile")"
    # npx spawns a child node process; kill the whole group/children, then the parent.
    pkill -P "$pid" 2>/dev/null || true
    kill "$pid" 2>/dev/null || true
    rm -f "$pidfile"
    echo "stopped k8s-$name MCP server"
  fi
done
