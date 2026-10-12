#!/usr/bin/env bash
# Stack control for Takeroom + ACE-Step.
#
#   ./stack.sh start     start both, in order, and wait for each
#   ./stack.sh stop      stop both, Takeroom first
#   ./stack.sh restart   stop then start
#   ./stack.sh status    what's up, what isn't
#
# Deliberately bash 3.2 compatible — macOS /bin/bash is from 2007.

set -uo pipefail

ACESTEP_DIR="${ACESTEP_DIR:-$HOME/ACE-Step-1.5}"
TAKEROOM_DIR="${TAKEROOM_DIR:-$(cd "$(dirname "$0")" && pwd)}"
ACESTEP_PORT="${ACESTEP_PORT:-8001}"
TAKEROOM_PORT="${TAKEROOM_PORT:-8800}"
LOG_DIR="$TAKEROOM_DIR/logs"

mkdir -p "$LOG_DIR"

if ! command -v lsof >/dev/null 2>&1; then
  echo "lsof not found — this script needs it to find processes by port." >&2
  exit 1
fi

pids_on() { lsof -ti ":$1" 2>/dev/null; }

up() { curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$1$2"; }

stop_port() {
  local port="$1" name="$2" pids
  pids=$(pids_on "$port")
  if [ -z "$pids" ]; then
    echo "  $name already stopped"
    return
  fi
  echo "  stopping $name (pid $pids)"
  echo "$pids" | xargs kill 2>/dev/null

  local waited=0
  while [ -n "$(pids_on "$port")" ] && [ "$waited" -lt 15 ]; do
    sleep 1
    waited=$((waited + 1))
  done

  if [ -n "$(pids_on "$port")" ]; then
    echo "  $name ignored SIGTERM, forcing"
    pids_on "$port" | xargs kill -9 2>/dev/null
    sleep 1
  fi
}

wait_for() {
  local port="$1" path="$2" name="$3" limit="$4" waited=0
  printf "  waiting for %s" "$name"
  while [ "$waited" -lt "$limit" ]; do
    if up "$port" "$path"; then
      printf " — ready (%ss)\n" "$waited"
      return 0
    fi
    printf "."
    sleep 2
    waited=$((waited + 2))
  done
  printf " — gave up after %ss\n" "$limit"
  return 1
}

cmd_stop() {
  echo "stopping:"
  stop_port "$TAKEROOM_PORT" "takeroom"
  stop_port "$ACESTEP_PORT" "acestep"
}

cmd_start() {
  echo "starting:"

  if up "$ACESTEP_PORT" "/openapi.json"; then
    echo "  acestep already up on $ACESTEP_PORT"
  else
    if [ ! -d "$ACESTEP_DIR" ]; then
      echo "  no ACE-Step at $ACESTEP_DIR — set ACESTEP_DIR" >&2
      return 1
    fi
    echo "  launching acestep (log: $LOG_DIR/acestep.log)"
    ( cd "$ACESTEP_DIR" && \
      ACESTEP_LM_BACKEND=mlx nohup uv run acestep-api \
        > "$LOG_DIR/acestep.log" 2>&1 & )
    # Cold start loads several GB over MLX; two minutes is not unusual.
    wait_for "$ACESTEP_PORT" "/openapi.json" "acestep" 180 || {
      echo "  last lines of acestep.log:" >&2
      tail -15 "$LOG_DIR/acestep.log" >&2
      return 1
    }
  fi

  if up "$TAKEROOM_PORT" "/api/status"; then
    echo "  takeroom already up on $TAKEROOM_PORT"
  else
    echo "  launching takeroom (log: $LOG_DIR/takeroom.log)"
    ( cd "$TAKEROOM_DIR" && nohup ./run.sh > "$LOG_DIR/takeroom.log" 2>&1 & )
    wait_for "$TAKEROOM_PORT" "/api/status" "takeroom" 90 || {
      tail -15 "$LOG_DIR/takeroom.log" >&2
      return 1
    }
  fi

  echo
  echo "  open http://127.0.0.1:$TAKEROOM_PORT   (http, not https)"
}

cmd_status() {
  if up "$ACESTEP_PORT" "/openapi.json"; then
    echo "acestep   up    :$ACESTEP_PORT"
  else
    echo "acestep   down  :$ACESTEP_PORT"
  fi

  if up "$TAKEROOM_PORT" "/api/status"; then
    echo "takeroom  up    :$TAKEROOM_PORT"
    curl -s "http://127.0.0.1:$TAKEROOM_PORT/api/status" \
      | python3 -c '
import json, sys
s = json.load(sys.stdin)
g = s["generator"]
print("  generator   %s  reachable=%s  endpoint_found=%s"
      % (g.get("endpoint", "?"), g.get("ok"), g.get("endpoint_present")))
if g.get("detail"):
    print("              %s" % g["detail"])
print("  writer      %s  available=%s" % (s["writer"]["model"], s["writer"]["available"]))
print("  stems       %s  available=%s" % (s["stems"]["model"], s["stems"]["available"]))
print("  queue       %s" % s["queue_depth"])
'
  else
    echo "takeroom  down  :$TAKEROOM_PORT"
  fi

  if curl -s -o /dev/null --max-time 2 http://127.0.0.1:11434/api/tags; then
    echo "ollama    up    :11434"
  else
    echo "ollama    down  :11434  (optional — writer will be skipped)"
  fi
}

case "${1:-status}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_stop; echo; sleep 2; cmd_start ;;
  status)  cmd_status ;;
  logs)    tail -f "$LOG_DIR/acestep.log" "$LOG_DIR/takeroom.log" ;;
  *)       echo "usage: $0 {start|stop|restart|status|logs}" >&2; exit 2 ;;
esac
