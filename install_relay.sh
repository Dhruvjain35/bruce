#!/bin/bash
#
# install_relay.sh — one-time Mac installer for the Bruce iMessage relay + supervisor (Bite 1.5 A4).
#
# Run ONCE, under the dedicated `bruce-relay` GRAPHICAL account (a real login session — NOT ssh, NOT
# sudo/root), from a checkout of this repo:
#
#     ./install_relay.sh --commit <exact_sha> --api-base-url https://api.bruce... --device <name>
#
# The installer securely requests the temporary authorization itself: it reads the SHORT-LIVED,
# SINGLE-USE bootstrap token from a HIDDEN prompt (echo disabled), or from stdin with
# --bootstrap-token-stdin, and hands it to the bootstrap over stdin — never through argv or an env var.
# Mint the token first (operator, DB side): `python -m scripts.relay_bootstrap mint --device <name>`.
#
# After that: the supervisor + relay start automatically at login (LaunchAgent RunAtLoad), stay healthy,
# and stop/resume remotely via the control plane. Re-run with a different --commit to UPGRADE, or with a
# previously-installed sha to ROLL BACK — both preserve durable state.
#
# What it does NOT do (by design):
#   * NO `git pull` / no fetch of an arbitrary ref — it exports the EXACT approved commit only.
#   * NO wiping of durable state (checkpoint / outbound ledger / pending attachments survive every path).
#   * NO permanent credential in the plist, argv, env, disk, or logs — the installer registers over the
#     short-lived single-use bootstrap token and moves the returned credential straight into the login
#     Keychain via Security.framework (config.py reads it there). The operator never sees/pastes it.
#   * NO bootstrap token in argv or env either — it is read from a hidden prompt / stdin and unset after.
#
set -euo pipefail

COMMIT=""
API_BASE_URL="${BRUCE_API_BASE_URL:-}"
ACCOUNT="default"
DEVICE=""
DRY_RUN=0
BOOTSTRAP_STDIN=0
INSTALL_DIR="${BRUCE_RELAY_INSTALL_DIR:-$HOME/.bruce-relay-app}"    # versioned code checkouts + `current`
STATE_DIR="${BRUCE_RELAY_STATE_DIR:-$HOME/.bruce-relay}"            # durable runtime state (never wiped)
SERVICE="com.bruce.relay.device-secret"

while [ $# -gt 0 ]; do
  case "$1" in
    --commit) COMMIT="$2"; shift 2 ;;
    --api-base-url) API_BASE_URL="$2"; shift 2 ;;
    --account) ACCOUNT="$2"; shift 2 ;;
    --device) DEVICE="$2"; shift 2 ;;
    --bootstrap-token-stdin) BOOTSTRAP_STDIN=1; shift ;;
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
# Wrong-account guard: the intended login user may be pinned via BRUCE_RELAY_EXPECT_USER.
if [ -n "${BRUCE_RELAY_EXPECT_USER:-}" ] && [ "${BRUCE_RELAY_EXPECT_USER}" != "$(id -un)" ]; then
  echo "error: running as '$(id -un)', expected '${BRUCE_RELAY_EXPECT_USER}'" >&2; exit 64
fi
UID_NUM="$(id -u)"
# Active GUI login is required (LaunchAgent bootstrap needs a real Aqua session).
if ! launchctl print "gui/${UID_NUM}" >/dev/null 2>&1; then
  echo "error: no active GUI login session for uid ${UID_NUM} — log in on the desktop first" >&2; exit 64
fi
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
# EXACT commit SHA required (not a branch/ref): resolve, and require the input to equal the full SHA.
RESOLVED="$(git -C "$REPO_ROOT" rev-parse --verify "${COMMIT}^{commit}" 2>/dev/null || true)"
[ -n "$RESOLVED" ] || { echo "error: commit $COMMIT not found — fetch the approved build first" >&2; exit 64; }
[ "$RESOLVED" = "$COMMIT" ] || { echo "error: pass the EXACT 40-char commit SHA, not a ref ($COMMIT -> $RESOLVED)" >&2; exit 64; }
# Reject a dirty working tree (avoid installing from an ambiguous checkout).
if [ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)" ]; then
  echo "error: repository working tree is dirty — commit/clean before installing" >&2; exit 64
fi
# API readiness preflight (never prints a response body).
if ! curl -fsS --max-time 10 "${API_BASE_URL%/}/healthz" >/dev/null 2>&1 \
   && ! curl -fsS --max-time 10 "${API_BASE_URL%/}/" >/dev/null 2>&1; then
  echo "warning: API at $API_BASE_URL did not respond to a readiness probe (continuing)" >&2
fi

# Resolve the imsg binary to an ABSOLUTE, executable path BEFORE any mutation. The relay drives imsg from
# the LaunchAgent, where launchd's minimal PATH does NOT include Homebrew — so we pin the absolute path
# into the plist and never rely on the caller's PATH after installation. Fail here (no side effects yet).
IMSG_BIN="${BRUCE_IMSG_BIN:-$(command -v imsg 2>/dev/null || true)}"
case "$IMSG_BIN" in
  /*) : ;;
  "") echo "error: imsg not found — install it (e.g. 'brew install imsg') or set BRUCE_IMSG_BIN to its absolute path" >&2; exit 64 ;;
  *)  echo "error: imsg must resolve to an ABSOLUTE path, got '$IMSG_BIN'" >&2; exit 64 ;;
esac
[ -x "$IMSG_BIN" ] || { echo "error: imsg at '$IMSG_BIN' is not executable" >&2; exit 64; }
echo "  imsg: $IMSG_BIN"

PYTHON="${BRUCE_RELAY_PYTHON:-$REPO_ROOT/engine/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
ENGINE_DIR="$INSTALL_DIR/current/engine"

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

# ---- 2. secure device bootstrap: register + store the credential in the Keychain (never shown) ------
# The installer OWNS the credential bootstrap. The SHORT-LIVED, SINGLE-USE bootstrap token (minted by
# `python -m scripts.relay_bootstrap mint`) is read SECURELY — a hidden prompt (echo disabled) by
# default, or piped once via --bootstrap-token-stdin — and handed to the bootstrap over its STDIN. The
# token is NEVER placed in argv or an environment variable, never printed, and never persisted; the
# in-memory copy is unset right after. The bootstrap registers over TLS, moves the returned PERMANENT
# credential straight into the login Keychain via Security.framework, verifies it, and self-revokes on
# any failure. The permanent credential is never displayed, pasted, written to disk, in argv/env, or logged.
DEVICE="${DEVICE:-$ACCOUNT}"
if security find-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1; then
  echo "  keychain: device credential already present for account=$ACCOUNT (leaving as-is)"
elif [ "$DRY_RUN" = 1 ]; then
  echo "[dry-run] would securely read the bootstrap token (hidden prompt / stdin) and run relay.bootstrap"
else
  # read the token WITHOUT echo (or from stdin), into a shell variable only — never argv/env.
  if [ "$BOOTSTRAP_STDIN" = 1 ]; then
    IFS= read -r BOOTSTRAP_TOKEN || true
  else
    printf 'Paste the single-use bootstrap token (input hidden): ' >&2
    IFS= read -rs BOOTSTRAP_TOKEN || true
    printf '\n' >&2
  fi
  if [ -z "${BOOTSTRAP_TOKEN:-}" ]; then
    echo "error: no bootstrap token provided. Mint one:" >&2
    echo "  BRUCE_ENV=<env> ... python -m scripts.relay_bootstrap mint --device $DEVICE" >&2
    exit 64
  fi
  echo "  bootstrap: registering device=$DEVICE and storing the credential in the Keychain (never shown)"
  # hand the token to the bootstrap over STDIN (not argv, not env).
  printf '%s\n' "$BOOTSTRAP_TOKEN" | \
    env PYTHONPATH="$ENGINE_DIR:$REPO_ROOT/engine" "$PYTHON" -m relay.bootstrap \
        --base-url "$API_BASE_URL" --device "$DEVICE" --account "$ACCOUNT"
  unset BOOTSTRAP_TOKEN                                        # clear the in-memory value after registration
fi

# ---- 3. state dir + version symlink + plist + (re)load — all the testable file work in Python -------
PREP=(--install-dir "$INSTALL_DIR" --state-dir "$STATE_DIR" --commit "$COMMIT" \
      --python "$PYTHON" --api-base-url "$API_BASE_URL" --home "$HOME" --uid "$UID_NUM" \
      --imsg-bin "$IMSG_BIN")
[ "$DRY_RUN" = 1 ] && PREP+=(--dry-run)
run env PYTHONPATH="$ENGINE_DIR:$REPO_ROOT/engine" "$PYTHON" -m relay.installer prepare "${PREP[@]}"

echo "done. the relay starts at login and is controllable remotely (brucectl status)."
echo "to test: open Messages on this Mac and text Bruce — no manual relay-start needed."
