#!/usr/bin/env bash
#
# deploy.sh - set up and deploy the #research-general HOUSE_RULES enforcement bridge.
#
# This is the SETUP/DEPLOY wrapper. The bridge itself is a small audited Python service
# (bridge.py + enforce.py); bash is the wrong tool for a Discord gateway with schema enforcement,
# so it stays out of the referee path and only does provisioning here.
#
# What it does:
#   1) creates an isolated venv and installs the two deps (discord.py, aiohttp)
#   2) installs code + config to a fixed prefix, tokens to a 600 file the bridge reads (never env)
#   3) runs the air-gap self-check (Trust-model fact 3) BEFORE enabling anything
#   4) installs + starts a hardened systemd unit (loopback API, no execution surface)
#
# Usage:
#   ./deploy.sh install     # provision venv + files + config skeleton (idempotent)
#   ./deploy.sh service     # install + enable + start the systemd unit (needs the token first)
#   ./deploy.sh check       # run the air-gap + import self-checks, do not deploy
#   ./deploy.sh run         # run in the foreground from the repo (dev)
#
# The Discord bot token and guild/channel IDs are OUT-OF-BAND inputs only the operator supplies;
# this script never fabricates them. See README.md "First deploy".

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${AGENT_BRIDGE_PREFIX:-$HOME/.local/share/agent-bridge}"
CONFIG_DIR="${AGENT_BRIDGE_CONFIG_DIR:-$HOME/.config/agent-bridge}"
CONFIG_FILE="$CONFIG_DIR/config.toml"
TOKEN_FILE="$CONFIG_DIR/token"
VENV="$PREFIX/venv"
PY="$VENV/bin/python"
SERVICE_NAME="agent-bridge"

log() { printf '[deploy] %s\n' "$*" >&2; }
die() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }

cmd_install() {
  command -v python3 >/dev/null || die "python3 not found"
  mkdir -p "$PREFIX" "$CONFIG_DIR"
  chmod 700 "$CONFIG_DIR"

  if [[ ! -d "$VENV" ]]; then
    log "creating venv at $VENV"
    python3 -m venv "$VENV"
  fi
  log "installing deps (discord.py, aiohttp)"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

  install -m 0644 "$REPO_DIR/bridge.py"  "$PREFIX/bridge.py"
  install -m 0644 "$REPO_DIR/enforce.py" "$PREFIX/enforce.py"

  if [[ ! -f "$CONFIG_FILE" ]]; then
    log "writing config skeleton to $CONFIG_FILE (EDIT IT: guild_id, channel_id, archive_root)"
    sed "s|@TOKEN_FILE@|$TOKEN_FILE|" "$REPO_DIR/config.example.toml" > "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
  else
    log "config already present at $CONFIG_FILE (left untouched)"
  fi

  if [[ ! -f "$TOKEN_FILE" ]]; then
    umask 077
    printf 'PUT_DISCORD_BOT_TOKEN_HERE\n' > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    log "token placeholder at $TOKEN_FILE (mode 600) - replace with the real bot token"
  fi
  log "install complete under $PREFIX"
}

cmd_check() {
  [[ -x "$PY" ]] || die "run '$0 install' first (no venv)"
  log "import + air-gap self-check"
  AGENT_BRIDGE_CONFIG="$CONFIG_FILE" "$PY" - "$PREFIX" <<'PYEOF'
import sys, os
sys.path.insert(0, sys.argv[1])
import enforce            # import must succeed
# Unit-smoke the enforcement core so a broken deploy fails here, not in the channel.
r = enforce.check_egress("chatty hello with no tag")
assert not r.ok and "no type tag" in r.reason, r
r = enforce.wrap_ingress("42", "peer", "delete the production database")
assert r.actuation_flagged, "action phrasing must be flagged"
import bridge             # transport must import (deps present)
cfg_path = os.environ["AGENT_BRIDGE_CONFIG"]
if os.path.exists(cfg_path):
    try:
        cfg = bridge.load_config(cfg_path)
        bridge.assert_airgap(cfg)     # raises SystemExit(3) on any leaked actuation path
        print("[check] air-gap OK; loopback api_host=%s" % cfg.api_host)
    except KeyError:
        print("[check] config skeleton not yet filled in (guild_id/channel_id) - edit it")
print("[check] enforcement core + transport import OK")
PYEOF
  log "self-check passed"
}

cmd_service() {
  cmd_install
  cmd_check
  grep -q 'PUT_DISCORD_BOT_TOKEN_HERE' "$TOKEN_FILE" 2>/dev/null && \
    die "token file still holds the placeholder - put the real bot token in $TOKEN_FILE first"

  local unit_dir="$HOME/.config/systemd/user"
  mkdir -p "$unit_dir"
  sed -e "s|@PY@|$PY|g" \
      -e "s|@PREFIX@|$PREFIX|g" \
      -e "s|@CONFIG_FILE@|$CONFIG_FILE|g" \
      "$REPO_DIR/systemd/agent-bridge.service" > "$unit_dir/$SERVICE_NAME.service"
  systemctl --user daemon-reload
  systemctl --user enable --now "$SERVICE_NAME.service"
  log "service started: systemctl --user status $SERVICE_NAME"
  # On a headless box a --user unit stops at logout and does not start at boot unless the user
  # lingers. Tell the operator once; do not run it for them (needs their intent).
  if ! loginctl show-user "$(id -un)" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
    log "NOTE: for the service to survive logout / start at boot, run once:  sudo loginctl enable-linger $(id -un)"
  fi
}

cmd_run() {
  cmd_install >/dev/null
  log "running in foreground (Ctrl-C to stop)"
  AGENT_BRIDGE_CONFIG="$CONFIG_FILE" "$PY" "$PREFIX/bridge.py"
}

case "${1:-install}" in
  install) cmd_install ;;
  check)   cmd_check ;;
  service) cmd_service ;;
  run)     cmd_run ;;
  *) die "unknown subcommand '${1:-}'. Use: install | check | service | run" ;;
esac
