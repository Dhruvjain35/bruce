#!/usr/bin/env bash
# ONE-COMMAND deploy: Bruce engine -> Alibaba Cloud Function Compute (Singapore).
#
#   ./deploy/deploy_fc.sh --dry-run     # every local check, NEVER contacts Alibaba
#   ./deploy/deploy_fc.sh               # preflight -> build -> deploy -> verify live
#
# Designed so that after `Activate Function Compute` you run ONE command and either get a live URL
# or a diagnosis — never a half-deployed service and never a silent success.
#
# SECRETS: read from engine/.env (gitignored) or the environment. This script never prints, logs or
# packages a credential; the proof file it writes is non-secret by construction.
#
# EXIT CODES (so CI/a human can tell causes apart):
#   0 ok   10 missing tool   11 missing env   12 preflight/tests failed   13 build failed
#   14 package invalid   20 FC not activated  21 AccessDenied/RAM   22 risk control
#   23 deploy failed      30 no URL           31 health failed        32 auth not enforced

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="$ROOT/engine"
BUILD_DIR="$ROOT/build/fc"
ZIP="$ROOT/build/bruce-fc.zip"
PROOF="$ROOT/docs/deployment-proof.json"
REGION="ap-southeast-1"
FUNCTION="bruce-engine"
TEMPLATE="$ROOT/deploy/s-webfunction.yaml"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$*"; }
info() { printf "    %s\n" "$*"; }
die()  { bad "$2"; exit "$1"; }

# Alibaba error -> exit code + a human cause. Lives in deploy/diagnose.sh so it can be unit-tested
# against the REAL error strings this account has actually produced (tests/test_deploy_diagnose.py).
diagnose() {
  local out="$1" rc
  printf "%s" "$out" | "$ROOT/deploy/diagnose.sh" | while read -r line; do bad "$line"; done
  rc=${PIPESTATUS[1]}
  return "${rc:-23}"
}

# ---------------------------------------------------------------- 1. tools
bold "1. Tools"
for t in docker python3 zip git; do
  command -v "$t" >/dev/null || die 10 "$t is required but not installed"
  ok "$t"
done
if [[ $DRY_RUN -eq 0 ]]; then
  command -v s >/dev/null || die 10 "Serverless Devs (s) not installed. Run: npm install -g @serverless-devs/s"
  ok "s (Serverless Devs)"
  command -v aliyun >/dev/null || die 10 "aliyun CLI not installed. Run: brew install aliyun-cli"
  ok "aliyun CLI"
else
  command -v s >/dev/null && ok "s (Serverless Devs)" || info "s not installed (dry-run: not required yet)"
fi

# ---------------------------------------------------------------- 2. env
bold "2. Environment"
# shellcheck disable=SC1091
[[ -f "$ENGINE/.env" ]] && { set -a; . "$ENGINE/.env"; set +a; ok "loaded engine/.env (gitignored)"; }

REQUIRED=(
  ALIBABA_CLOUD_ACCESS_KEY_ID ALIBABA_CLOUD_ACCESS_KEY_SECRET
  BRUCE_JWT_SECRET BRUCE_APP_DATABASE_URL
  DASHSCOPE_API_KEY QWEN_BASE_URL QWEN_INTAKE_MODEL
)
OPTIONAL=(BRUCE_JWT_AUDIENCE BRUCE_DATABASE_URL GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET GOOGLE_REFRESH_TOKEN GOOGLE_CALENDAR_ID)
MISSING=()
for v in "${REQUIRED[@]}"; do [[ -n "${!v:-}" ]] && ok "$v is set" || { bad "$v is MISSING"; MISSING+=("$v"); }; done
for v in "${OPTIONAL[@]}"; do [[ -n "${!v:-}" ]] && ok "$v is set (optional)" || info "$v unset (optional — that feature will report unavailable)"; done
((${#MISSING[@]})) && die 11 "missing required env: ${MISSING[*]}"

if [[ "${BRUCE_JWT_SECRET:-}" == *"test"* || ${#BRUCE_JWT_SECRET} -lt 32 ]]; then
  bad "BRUCE_JWT_SECRET looks like a test value or is <32 bytes."
  info "This secret is the ONLY thing protecting student data on a public URL. Refusing."
  exit 11
fi
ok "BRUCE_JWT_SECRET length ok (${#BRUCE_JWT_SECRET} bytes)"

COMMIT="$(git -C "$ROOT" rev-parse --short HEAD)"
DIRTY=""; [[ -n "$(git -C "$ROOT" status --porcelain -- engine deploy)" ]] && DIRTY=" (DIRTY — deploying uncommitted changes)"
ok "commit $COMMIT$DIRTY"
[[ -n "$DIRTY" ]] && info "The live /health will report $COMMIT, which does NOT match the working tree."

# ---------------------------------------------------------------- 3. preflight
bold "3. Preflight (bounded — the security-critical suite only)"
( cd "$ENGINE" && .venv/bin/python -m pytest -q \
    tests/test_auth.py tests/test_provider_status.py tests/test_api.py 2>&1 | tail -1 ) || die 12 "preflight tests failed — refusing to deploy"
ok "auth + provider-status + api tests pass"

# ---------------------------------------------------------------- 4. build
bold "4. Build code package (linux/amd64)"
"$ROOT/deploy/build-package.sh" >/tmp/bruce-build.log 2>&1 || { tail -20 /tmp/bruce-build.log; die 13 "package build failed"; }
[[ -f "$ZIP" ]] || die 13 "package build produced no zip"
ok "built $(basename "$ZIP")"

# ---------------------------------------------------------------- 5. validate package
bold "5. Validate package"
ZIP_MB=$(python3 -c "import os;print(round(os.path.getsize('$ZIP')/1e6,1))")
B64_MB=$(python3 -c "import os;print(round(os.path.getsize('$ZIP')*4/3/1e6,1))")
SHA=$(python3 -c "
import hashlib,sys
h=hashlib.sha256()
with open('$ZIP','rb') as f:
    for c in iter(lambda:f.read(1<<20), b''): h.update(c)
print(h.hexdigest())")
info "zip ${ZIP_MB} MB   base64 ${B64_MB} MB   sha256 ${SHA:0:16}…"
python3 - "$ZIP" "$B64_MB" <<'PY' || exit 14
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1]); names = z.namelist()
b64 = float(sys.argv[2])
fail = []
boot = [n for n in names if n.rstrip('/').endswith('bootstrap')]
if not boot: fail.append("bootstrap missing from package")
else:
    mode = (z.getinfo(boot[0]).external_attr >> 16) & 0o777
    if not mode & 0o111: fail.append(f"bootstrap not executable (mode {oct(mode)}) — FC will not start it")
if any(n.endswith('.env') or '/.env' in n for n in names): fail.append("SECRET LEAK: .env is inside the package")
if b64 > 100: fail.append(f"base64 {b64}MB exceeds the 100MB create/update request-body limit — upload via OSS instead")
if not any('bruce_engine/api.py' in n for n in names): fail.append("bruce_engine/api.py missing from package")
for f in fail: print(f"  \033[31m✗\033[0m {f}")
sys.exit(1 if fail else 0)
PY
ok "bootstrap executable, no .env, api.py present, base64 ${B64_MB}MB < 100MB limit"

# ---------------------------------------------------------------- 6. dry-run stops here
if [[ $DRY_RUN -eq 1 ]]; then
  bold "DRY RUN — no Alibaba API was contacted."
  echo
  echo "  Everything that can be validated locally passed."
  echo "  To deploy for real: ./deploy/deploy_fc.sh"
  exit 0
fi

# ---------------------------------------------------------------- 7. deploy
bold "6. Deploy to Function Compute ($REGION)"
export BRUCE_COMMIT="$COMMIT"
DEPLOY_OUT="$(cd "$ROOT" && s deploy -t "$TEMPLATE" --assume-yes 2>&1)"
DEPLOY_RC=$?
if [[ $DEPLOY_RC -ne 0 ]]; then
  echo "$DEPLOY_OUT" | tail -25
  diagnose "$DEPLOY_OUT"; exit $?
fi
ok "s deploy completed"

# ---------------------------------------------------------------- 8. discover URL
bold "7. Discover URL"
URL="$(grep -oE 'https://[a-zA-Z0-9._-]+\.(fcapp\.run|aliyuncs\.com)[^ "]*' <<<"$DEPLOY_OUT" | head -1)"
if [[ -z "$URL" ]]; then
  URL="$(aliyun --profile bruce fc GET "/2023-03-30/functions/$FUNCTION/triggers" --region "$REGION" 2>/dev/null \
        | grep -oE 'https://[a-zA-Z0-9._-]+\.fcapp\.run[^"]*' | head -1)"
fi
[[ -z "$URL" ]] && { echo "$DEPLOY_OUT" | tail -15; die 30 "deployed but no HTTP trigger URL found"; }
URL="${URL%/}"
ok "URL: $URL"

# ---------------------------------------------------------------- 9. verify live
bold "8. Verify the LIVE service (this is what makes it 'deployed')"
HEALTH="$(curl -sS --max-time 90 "$URL/health" 2>&1)"
grep -q '"status":"ok"' <<<"$HEALTH" || { info "$HEALTH"; die 31 "/health did not return status=ok (cold start >15s? check FC logs)"; }
ok "/health 200"
LIVE_COMMIT="$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('commit',''))" "$HEALTH" 2>/dev/null)"
LIVE_REGION="$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('region',''))" "$HEALTH" 2>/dev/null)"
[[ "$LIVE_COMMIT" == "$COMMIT" ]] && ok "commit matches: $LIVE_COMMIT" || { bad "commit mismatch: live=$LIVE_COMMIT expected=$COMMIT"; exit 31; }
[[ "$LIVE_REGION" == "$REGION" ]] && ok "region matches: $LIVE_REGION" || { bad "region mismatch: live=$LIVE_REGION"; exit 31; }

# The HTTP trigger is anonymous at the gateway, so Bruce's JWT check is the ONLY thing between the
# public internet and student data. Verify it on the LIVE URL, not just locally.
for p in /v1/intake /v1/missions /v1/diagnostics; do
  CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 60 -X POST "$URL$p" -H 'Content-Type: application/json' -d '{"text":"x"}')"
  [[ "$CODE" == "401" || "$CODE" == "405" ]] || die 32 "SECURITY: $p returned $CODE without a token (expected 401). Service left deployed but is UNSAFE."
done
ok "protected endpoints reject unauthenticated requests (401)"

# ---------------------------------------------------------------- 10. proof
bold "9. Write deployment proof"
python3 - "$PROOF" "$URL" "$REGION" "$FUNCTION" "$COMMIT" "$SHA" "$ZIP_MB" "$HEALTH" <<'PY'
import json, sys, subprocess
proof, url, region, fn, commit, sha, zip_mb, health = sys.argv[1:9]
ts = subprocess.run(["date","-u","+%Y-%m-%dT%H:%M:%SZ"], capture_output=True, text=True).stdout.strip()
json.dump({
  "_comment": "Generated by deploy/deploy_fc.sh. Non-secret by construction — safe to commit.",
  "deployed_at_utc": ts, "service_url": url, "region": region, "function_name": fn,
  "runtime": "custom.debian12 (Python 3.11)", "commit": commit,
  "package_sha256": sha, "package_zip_mb": float(zip_mb),
  "health_response": json.loads(health),
  "verified": {
    "health_200": True, "commit_matches": True, "region_matches": True,
    "unauthenticated_requests_rejected_401": True,
  },
  "not_verified": {
    "qwen_live_inference": "blocked — see docs/deployment-verification.md",
    "google_calendar_live": "requires GOOGLE_* credentials",
  },
}, open(proof,"w"), indent=2)
print(f"    wrote {proof}")
PY
ok "proof written (non-secret)"

echo
bold "DEPLOYED AND LIVE-VERIFIED"
echo "  URL:      $URL"
echo "  Region:   $REGION      Function: $FUNCTION      Commit: $COMMIT"
echo
echo "  Next:"
echo "    export BRUCE_DEPLOY_URL=$URL"
echo "    cd engine && .venv/bin/python -m pytest tests/test_deployment_smoke.py -v"
echo "    # then paste URL/region/function/commit into docs/deployment-verification.md"
