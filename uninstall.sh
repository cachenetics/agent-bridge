#!/usr/bin/env bash
#
# uninstall.sh - tear down the agent-bridge deployment that deploy.sh set up.
#
# Reverses deploy.sh: stops + disables the systemd --user unit, removes it, and deletes the install
# prefix (venv + installed code). By DEFAULT it KEEPS your config and token - those hold out-of-band
# secrets (the Discord bot token) the operator supplied, and a teardown should not silently destroy
# a credential. Pass --purge to remove them too.
#
# Usage:
#   ./uninstall.sh            # stop+disable+remove the service and the install prefix; keep config/token
#   ./uninstall.sh --purge    # also delete the config dir (config.toml + token)
#   ./uninstall.sh --dry-run  # print what WOULD be removed, change nothing
#
# Honors the same overrides as deploy.sh: AGENT_BRIDGE_PREFIX, AGENT_BRIDGE_CONFIG_DIR.

set -euo pipefail

PREFIX="${AGENT_BRIDGE_PREFIX:-$HOME/.local/share/agent-bridge}"
CONFIG_DIR="${AGENT_BRIDGE_CONFIG_DIR:-$HOME/.config/agent-bridge}"
SERVICE_NAME="agent-bridge"
UNIT_FILE="$HOME/.config/systemd/user/$SERVICE_NAME.service"

PURGE=0
DRY=0
for arg in "$@"; do
  case "$arg" in
    --purge)   PURGE=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) printf '[uninstall] ERROR: unknown option %s (use --purge, --dry-run)\n' "$arg" >&2; exit 1 ;;
  esac
done

log() { printf '[uninstall] %s\n' "$*" >&2; }
run() {
  if [[ "$DRY" == 1 ]]; then printf '[uninstall] DRY: %s\n' "$*" >&2; else eval "$*"; fi
}

# 1) systemd --user unit: stop, disable, remove, reload. All best-effort - a missing unit is fine.
if command -v systemctl >/dev/null 2>&1; then
  if systemctl --user list-unit-files "$SERVICE_NAME.service" >/dev/null 2>&1 \
     && systemctl --user cat "$SERVICE_NAME.service" >/dev/null 2>&1; then
    log "stopping + disabling $SERVICE_NAME.service"
    run "systemctl --user disable --now '$SERVICE_NAME.service' 2>/dev/null || true"
  else
    log "no active $SERVICE_NAME.service unit"
  fi
  if [[ -f "$UNIT_FILE" ]]; then
    log "removing unit file $UNIT_FILE"
    run "rm -f '$UNIT_FILE'"
    run "systemctl --user daemon-reload 2>/dev/null || true"
    run "systemctl --user reset-failed '$SERVICE_NAME.service' 2>/dev/null || true"
  fi
else
  log "systemctl not found - skipping unit teardown"
fi

# 2) install prefix (venv + code + the unit's HOUSE_RULES copy).
if [[ -d "$PREFIX" ]]; then
  log "removing install prefix $PREFIX"
  run "rm -rf '$PREFIX'"
else
  log "install prefix $PREFIX already gone"
fi

# 3) config + token: kept unless --purge (they hold the bot token).
if [[ "$PURGE" == 1 ]]; then
  if [[ -d "$CONFIG_DIR" ]]; then
    log "PURGE: removing config dir $CONFIG_DIR (config.toml + token)"
    run "rm -rf '$CONFIG_DIR'"
  else
    log "config dir $CONFIG_DIR already gone"
  fi
else
  if [[ -d "$CONFIG_DIR" ]]; then
    log "kept config + token in $CONFIG_DIR (re-run with --purge to remove them)"
  fi
fi

log "done."
