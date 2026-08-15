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
#   ./deploy.sh update      # git pull + reinstall + restart the service(s) (config/token untouched)
#   ./deploy.sh responder   # install + enable + start the optional auto-reply agent (responder.py)
#
# To tear it all down, see ./uninstall.sh (stops+removes the units and install prefix; --purge also
# removes config+token).
#
# The Discord bot token and guild/channel IDs are OUT-OF-BAND inputs only the operator supplies;
# this script never fabricates them. See README.md "Setup".

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

  install -m 0644 "$REPO_DIR/bridge.py"        "$PREFIX/bridge.py"
  install -m 0644 "$REPO_DIR/enforce.py"       "$PREFIX/enforce.py"
  install -m 0644 "$REPO_DIR/HOUSE_RULES.md"   "$PREFIX/HOUSE_RULES.md"   # the unit's Documentation= target
  # The optional responder agent + the files it needs (client lib + the enforced-channel contract).
  install -m 0644 "$REPO_DIR/responder.py"     "$PREFIX/responder.py"
  install -m 0644 "$REPO_DIR/client.py"        "$PREFIX/client.py"
  install -m 0644 "$REPO_DIR/harness_agent.py" "$PREFIX/harness_agent.py"
  install -m 0644 "$REPO_DIR/gate.py"          "$PREFIX/gate.py"
  install -m 0644 "$REPO_DIR/AGENTS.md"        "$PREFIX/AGENTS.md"
  install -m 0644 "$REPO_DIR/POSTING-SCHEMA.md" "$PREFIX/POSTING-SCHEMA.md"

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
        bridge.assert_airgap(cfg)     # fatal only on a non-loopback API bind; warns on suspicious env
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

cmd_responder() {
  # Install + start the optional autonomous responder (responder.py) as its own --user unit.
  # It needs the bridge running and a reachable OpenAI-compatible model (see [responder] in config).
  cmd_install
  local unit_dir="$HOME/.config/systemd/user"
  mkdir -p "$unit_dir"
  sed -e "s|@PY@|$PY|g" \
      -e "s|@PREFIX@|$PREFIX|g" \
      -e "s|@CONFIG_FILE@|$CONFIG_FILE|g" \
      "$REPO_DIR/systemd/agent-bridge-responder.service" > "$unit_dir/$SERVICE_NAME-responder.service"
  systemctl --user daemon-reload
  systemctl --user enable --now "$SERVICE_NAME-responder.service"
  log "responder started: systemctl --user status $SERVICE_NAME-responder"
  log "  (it replies via the model in [responder]; set enabled=false there to turn it off)"
}

cmd_update() {
  # One-command upgrade: pull the latest code, reinstall, restart the service if it is running.
  # Config and token live outside the repo (in $CONFIG_DIR) and are never touched.
  if [[ -d "$REPO_DIR/.git" ]]; then
    log "pulling latest in $REPO_DIR"
    git -C "$REPO_DIR" pull --ff-only 2>&1 | sed 's/^/  /' || \
      log "git pull did not fast-forward - resolve it manually, then re-run update"
  else
    log "$REPO_DIR is not a git checkout - skipping pull, reinstalling current files"
  fi
  cmd_install
  if systemctl --user is-active --quiet "$SERVICE_NAME.service" 2>/dev/null; then
    systemctl --user restart "$SERVICE_NAME.service"
    log "restarted $SERVICE_NAME on the updated code"
    curl -s --max-time 8 "http://127.0.0.1:8787/health" 2>/dev/null | sed 's/^/  health: /' || true
  else
    log "installed; service not running - start it with '$0 service'"
  fi
  # The responder is opt-in, but if it is already running, roll it onto the new code too.
  if systemctl --user is-active --quiet "$SERVICE_NAME-responder.service" 2>/dev/null; then
    systemctl --user restart "$SERVICE_NAME-responder.service"
    log "restarted $SERVICE_NAME-responder on the updated code"
  fi
}

case "${1:-install}" in
  install)   cmd_install ;;
  check)     cmd_check ;;
  service)   cmd_service ;;
  responder) cmd_responder ;;
  run)       cmd_run ;;
  update)    cmd_update ;;
  *) die "unknown subcommand '${1:-}'. Use: install | check | service | responder | run | update" ;;
esac
