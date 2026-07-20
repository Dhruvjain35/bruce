#!/bin/bash
#
# install_relay.sh — one-time Mac installer for the Bruce iMessage relay + supervisor (Bite 1.5 A4).
#
# Run ONCE, under the dedicated `bruce-relay` GRAPHICAL account (a real login session — NOT ssh, NOT
# sudo/root), from a checkout of this repo:
#
#     ./install_relay.sh --commit <approved_sha> --api-base-url https://api.bruce... [--account default]
#
# After that: the supervisor + relay start automatically at login (LaunchAgent RunAtLoad), stay healthy,
# and stop/resume remotely via the control plane. Re-run with a different --commit to UPGRADE, or with a
# previously-installed sha to ROLL BACK — both preserve durable state.
#
# What it does NOT do (by design):
#   * NO `git pull` / no fetch of an arbitrary ref — it exports the EXACT approved commit only.
#   * NO wiping of durable state (checkpoint / outbound ledger / pending attachments survive every path).
#   * NO secret in the plist, argv, env, or logs — the device secret is prompted for and stored ONLY in
#     the login Keychain (config.py reads it there); `security` is invoked WITHOUT -w so it never hits argv.
#
set -euo pipefail

COMMIT=""
API_BASE_URL="${BRUCE_API_BASE_URL:-}"
ACCOUNT="default"
DRY_RUN=0
INSTALL_DIR="${BRUCE_RELAY_INSTALL_DIR:-$HOME/.bruce-relay-app}"    # versioned code checkouts + `current`
STATE_DIR="${BRUCE_RELAY_STATE_DIR:-$HOME/.bruce-relay}"            # durable runtime state (never wiped)
SERVICE="com.bruce.relay.device-secret"

while [ $# -gt 0 ]; do
  case "$1" in
    --commit) COMMIT="$2"; shift 2 ;;
    --api-base-url) API_BASE_URL="$2"; shift 2 ;;
    --account) ACCOUNT="$2"; shift 2 ;;
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    --state-dir) STATE_DIR="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done

run() { if [ "$DRY_RUN" = 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }

# ---- preconditions ---------------------------------------------------------------------------------
[ -n "$COMMIT" ] || { echo "error: --commit <approved_sha> is required" >&2; exit 64; }
[ -n "$API_BASE_URL" ] || { echo "error: --api-base-url is required (or set BRUCE_API_BASE_URL)" >&2; exit 64; }
[ "$(uname)" = "Darwin" ] || { echo "error: the relay installs on macOS only" >&2; exit 64; }
[ "$(id -u)" -ne 0 ] || { echo "error: run as the bruce-relay LOGIN user, never as root" >&2; exit 64; }
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
git -C "$REPO_ROOT" rev-parse --verify "${COMMIT}^{commit}" >/dev/null 2>&1 \
  || { echo "error: commit $COMMIT not found in this repo — fetch the approved build first" >&2; exit 64; }

PYTHON="${BRUCE_RELAY_PYTHON:-$REPO_ROOT/engine/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
ENGINE_DIR="$INSTALL_DIR/current/engine"
UID_NUM="$(id -u)"

echo "install: commit=$COMMIT account=$ACCOUNT"
echo "  install_dir=$INSTALL_DIR  state_dir=$STATE_DIR (durable, never wiped)"

# ---- 1. export the EXACT approved commit into versions/<sha> (no pull; idempotent) -----------------
VER_DIR="$INSTALL_DIR/versions/$COMMIT"
if [ ! -d "$VER_DIR" ]; then
  run mkdir -p "$VER_DIR"
  # git archive = a detached snapshot of exactly this commit — never a working-tree or a moving ref.
  if [ "$DRY_RUN" = 1 ]; then echo "[dry-run] git archive $COMMIT | tar -x -C $VER_DIR"; else
    git -C "$REPO_ROOT" archive "$COMMIT" | tar -x -C "$VER_DIR"
  fi
else
  echo "  version already present: $VER_DIR"
fi

# ---- 2. store the device secret in the Keychain (INTERACTIVE; never in argv) -----------------------
# security prompts for the secret (no -w), so it never appears in argv/env/logs. Paste the ONE-TIME value
# printed by `python -m scripts.register_relay_device` during device registration.
if security find-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1; then
  echo "  keychain: device secret already present for account=$ACCOUNT (leaving as-is)"
else
  echo "  keychain: paste the one-time device secret at the prompt (input hidden, not stored in argv)"
  run security add-generic-password -U -a "$ACCOUNT" -s "$SERVICE"
fi

# ---- 3. state dir + version symlink + plist + (re)load — all the testable file work in Python -------
PREP=(--install-dir "$INSTALL_DIR" --state-dir "$STATE_DIR" --commit "$COMMIT" \
      --python "$PYTHON" --api-base-url "$API_BASE_URL" --home "$HOME" --uid "$UID_NUM")
[ "$DRY_RUN" = 1 ] && PREP+=(--dry-run)
run env PYTHONPATH="$ENGINE_DIR:$REPO_ROOT/engine" "$PYTHON" -m relay.installer prepare "${PREP[@]}"

echo "done. the relay starts at login and is controllable remotely (brucectl status)."
echo "to test: open Messages on this Mac and text Bruce — no manual relay-start needed."
