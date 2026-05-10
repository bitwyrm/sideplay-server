#!/usr/bin/env bash
set -euo pipefail

mkdir -p "${SIDEPLAY_DATA_ROOT:-/var/lib/sideplay}"

redis-server --save "" --appendonly no --daemonize yes

python media_service.py &
MEDIA_PID=$!

python core.py &
CORE_PID=$!

shutdown() {
  kill "$CORE_PID" "$MEDIA_PID" 2>/dev/null || true
  redis-cli shutdown nosave 2>/dev/null || true
}

trap shutdown SIGINT SIGTERM
wait -n "$CORE_PID" "$MEDIA_PID"
shutdown
