#!/usr/bin/env bash
set -euo pipefail

mkdir -p "${SIDEPLAY_DATA_ROOT:-/var/lib/sideplay}"

log() {
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*"
}

STOPPING=0

redis-server --save "" --appendonly no --daemonize yes
log "redis started"

python media_service.py &
MEDIA_PID=$!
log "media_service started pid=${MEDIA_PID}"

python core.py &
CORE_PID=$!
log "core started pid=${CORE_PID}"

shutdown() {
  STOPPING=1
  log "shutdown requested"
  kill "$CORE_PID" "$MEDIA_PID" 2>/dev/null || true
  wait "$CORE_PID" "$MEDIA_PID" 2>/dev/null || true
  redis-cli shutdown nosave 2>/dev/null || true
  log "redis stopped"
}

trap shutdown SIGINT SIGTERM

# If either child exits, terminate the other and exit non-zero so orchestrator restarts.
set +e
wait -n "$CORE_PID" "$MEDIA_PID"
EXIT_CODE=$?
set -e

if [[ "$STOPPING" -eq 0 ]]; then
  if kill -0 "$CORE_PID" 2>/dev/null; then
    DEAD="media_service"
  else
    DEAD="core"
  fi
  log "fatal: child process exited unexpectedly dead=${DEAD} exit_code=${EXIT_CODE}"
  shutdown
  exit 1
fi

shutdown
