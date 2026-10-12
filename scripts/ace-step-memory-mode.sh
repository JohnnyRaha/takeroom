#!/usr/bin/env bash
#
# Manage ACE-Step memory-oriented startup profiles for Takeroom.
#
# Usage:
#   ./ace-step-memory-mode.sh status
#   ./ace-step-memory-mode.sh safe
#   ./ace-step-memory-mode.sh turbo
#   ./ace-step-memory-mode.sh balanced
#   ./ace-step-memory-mode.sh xl
#   ./ace-step-memory-mode.sh restore
#
# macOS /bin/bash 3.2 compatible.

set -euo pipefail

TAKEROOM_DIR="${TAKEROOM_DIR:-$HOME/Documents/Takeroom/takeroom}"
ACESTEP_DIR="${ACESTEP_DIR:-$HOME/ACE-Step-1.5}"
STACK_SCRIPT="$TAKEROOM_DIR/stack.sh"
ENV_FILE="$ACESTEP_DIR/.env"
BACKUP_DIR="$ACESTEP_DIR/.env-backups"
LOG_DIR="$TAKEROOM_DIR/logs"

ACESTEP_PORT="${ACESTEP_PORT:-8001}"
TAKEROOM_PORT="${TAKEROOM_PORT:-8800}"

usage() {
  cat <<USAGE
Usage: $(basename "$0") {status|safe|turbo|balanced|xl|restore}

Profiles:
  safe       Turbo DiT, no LLM. Lowest practical memory use.
  turbo      Turbo DiT, 0.6B MLX LLM. Lightest LLM-enabled profile.
  balanced   Turbo DiT, 1.7B MLX LLM. More LLM capability, more memory.
  xl         Keep the existing DiT model selection, 1.7B MLX LLM.
  restore    Restore the newest .env backup, then restart.
  status     Show current settings and health status.

Every profile:
  - Uses ACESTEP_LM_BACKEND=mlx through your existing stack.sh.
  - Adds a conservative MPS high-watermark ratio of 1.0.
  - Creates a timestamped backup before changing .env.
  - Restarts through your existing stack.sh and checks both services.

This script never sets PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0.
USAGE
}

die() {
  echo "error: $*" >&2
  exit 1
}

require_layout() {
  [ -d "$ACESTEP_DIR" ] || die "ACE-Step directory not found: $ACESTEP_DIR"
  [ -x "$STACK_SCRIPT" ] || die "Takeroom stack script not executable: $STACK_SCRIPT"
  mkdir -p "$BACKUP_DIR" "$LOG_DIR"
  [ -e "$ENV_FILE" ] || : > "$ENV_FILE"
}

timestamp() {
  date '+%Y%m%d-%H%M%S'
}

backup_env() {
  local backup
  backup="$BACKUP_DIR/.env.$(timestamp).bak"
  cp "$ENV_FILE" "$backup"
  echo "$backup"
}

set_env_value() {
  local key="$1"
  local value="$2"
  local temp

  temp="$(mktemp "$ACESTEP_DIR/.env.tmp.XXXXXX")"

  if [ -f "$ENV_FILE" ]; then
    awk -v key="$key" -v value="$value" '
      BEGIN { changed=0 }
      $0 ~ "^[[:space:]]*" key "=" {
        if (!changed) {
          print key "=" value
          changed=1
        }
        next
      }
      { print }
      END {
        if (!changed) {
          print key "=" value
        }
      }
    ' "$ENV_FILE" > "$temp"
  else
    printf '%s=%s\n' "$key" "$value" > "$temp"
  fi

  mv "$temp" "$ENV_FILE"
}

unset_env_value() {
  local key="$1"
  local temp

  temp="$(mktemp "$ACESTEP_DIR/.env.tmp.XXXXXX")"

  if [ -f "$ENV_FILE" ]; then
    awk -v key="$key" '
      $0 !~ "^[[:space:]]*" key "=" { print }
    ' "$ENV_FILE" > "$temp"
  else
    : > "$temp"
  fi

  mv "$temp" "$ENV_FILE"
}

show_env() {
  echo "ACE-Step environment: $ENV_FILE"

  if [ ! -s "$ENV_FILE" ]; then
    echo "  (empty)"
    return
  fi

  grep -E '^(ACESTEP_(CONFIG_PATH|INIT_LLM|LM_MODEL_PATH|LM_BACKEND)|PYTORCH_MPS_(HIGH|LOW)_WATERMARK_RATIO)=' \
    "$ENV_FILE" 2>/dev/null \
    | sed 's/^/  /' \
    || echo "  (no managed settings found)"
}

service_ok() {
  local port="$1"
  local path="$2"
  curl -fsS --max-time 3 "http://127.0.0.1:$port$path" >/dev/null 2>&1
}

show_health() {
  echo
  if service_ok "$ACESTEP_PORT" "/openapi.json"; then
    echo "ACE-Step:  up on http://127.0.0.1:$ACESTEP_PORT"
  else
    echo "ACE-Step:  down or not healthy on :$ACESTEP_PORT"
  fi

  if service_ok "$TAKEROOM_PORT" "/api/status"; then
    echo "Takeroom:  up on http://127.0.0.1:$TAKEROOM_PORT"
    curl -fsS --max-time 3 "http://127.0.0.1:$TAKEROOM_PORT/api/status" \
      | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    generator = data.get("generator", {})
    print("  generator endpoint=%s reachable=%s endpoint_found=%s" % (
        generator.get("endpoint", "?"),
        generator.get("ok", "?"),
        generator.get("endpoint_present", "?"),
    ))
    if generator.get("detail"):
        print("  detail: %s" % generator["detail"])
except Exception as exc:
    print("  unable to parse Takeroom status: %s" % exc)
'
  else
    echo "Takeroom:  down or not healthy on :$TAKEROOM_PORT"
  fi
}

restart_stack() {
  echo
  echo "Restarting Takeroom and ACE-Step..."
  "$STACK_SCRIPT" restart
  show_health
}

apply_profile() {
  local profile="$1"
  local backup

  require_layout
  backup="$(backup_env)"

  case "$profile" in
    safe)
      set_env_value "ACESTEP_CONFIG_PATH" "acestep-v15-turbo"
      set_env_value "ACESTEP_INIT_LLM" "false"
      unset_env_value "ACESTEP_LM_MODEL_PATH"
      set_env_value "PYTORCH_MPS_HIGH_WATERMARK_RATIO" "1.0"
      set_env_value "PYTORCH_MPS_LOW_WATERMARK_RATIO" "0.9"
      ;;

    turbo)
      set_env_value "ACESTEP_CONFIG_PATH" "acestep-v15-turbo"
      set_env_value "ACESTEP_INIT_LLM" "true"
      set_env_value "ACESTEP_LM_MODEL_PATH" "acestep-5Hz-lm-0.6B"
      set_env_value "PYTORCH_MPS_HIGH_WATERMARK_RATIO" "1.0"
      set_env_value "PYTORCH_MPS_LOW_WATERMARK_RATIO" "0.9"
      ;;

    balanced)
      set_env_value "ACESTEP_CONFIG_PATH" "acestep-v15-turbo"
      set_env_value "ACESTEP_INIT_LLM" "true"
      set_env_value "ACESTEP_LM_MODEL_PATH" "acestep-5Hz-lm-1.7B"
      set_env_value "PYTORCH_MPS_HIGH_WATERMARK_RATIO" "1.0"
      set_env_value "PYTORCH_MPS_LOW_WATERMARK_RATIO" "0.9"
      ;;

    xl)
      unset_env_value "ACESTEP_CONFIG_PATH"
      set_env_value "ACESTEP_INIT_LLM" "true"
      set_env_value "ACESTEP_LM_MODEL_PATH" "acestep-5Hz-lm-1.7B"
      set_env_value "PYTORCH_MPS_HIGH_WATERMARK_RATIO" "1.0"
      set_env_value "PYTORCH_MPS_LOW_WATERMARK_RATIO" "0.9"
      ;;

    *)
      die "unknown profile: $profile"
      ;;
  esac

  echo "Applied '$profile' profile."
  echo "Backup created: $backup"
  echo
  show_env
  restart_stack
}

restore_latest() {
  local latest

  require_layout
  latest="$(ls -1t "$BACKUP_DIR"/.env.*.bak 2>/dev/null | head -n 1 || true)"

  [ -n "$latest" ] || die "no .env backup found in $BACKUP_DIR"

  cp "$ENV_FILE" "$BACKUP_DIR/.env.pre-restore.$(timestamp).bak"
  cp "$latest" "$ENV_FILE"

  echo "Restored: $latest"
  echo
  show_env
  restart_stack
}

status() {
  require_layout
  show_env
  show_health

  echo
  echo "Recent ACE-Step log warnings/errors:"
  if [ -f "$LOG_DIR/acestep.log" ]; then
    grep -E 'WARNING|ERROR|out of memory|failed' "$LOG_DIR/acestep.log" \
      | tail -n 12 \
      | sed 's/^/  /' \
      || echo "  (none found)"
  else
    echo "  Log not found: $LOG_DIR/acestep.log"
  fi
}

main() {
  case "${1:-status}" in
    status)
      status
      ;;
    safe|turbo|balanced|xl)
      apply_profile "$1"
      ;;
    restore)
      restore_latest
      ;;
    help|-h|--help)
      usage
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

main "$@"
