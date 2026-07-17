#!/usr/bin/env bash
# Build the Function Compute code package (custom.debian12 / Python 3.11, linux/amd64).
#
#   ./deploy/build-package.sh          -> build/bruce-fc.zip
#
# WHY DOCKER: FC runs linux/amd64 Debian 12. asyncpg, cryptography, pydantic-core and pillow are
# COMPILED extensions — wheels built on macOS/arm64 will not load there. So dependencies are
# installed inside a real python:3.11-slim amd64 container. Building this on the host would produce
# a package that imports fine locally and dies on FC with a cryptic ELF error.
#
# MEASURED (2026-07-17, linux/amd64):
#   full set : 138 MB unpacked -> 45.1 MB zip -> 60.1 MB base64   (limit: 100 MB request body)
#   Singapore code package limit is 500 MB; the binding constraint is the 100 MB create/update body.
#
# NO SECRETS ARE PACKAGED. Only source + dependencies go in the zip; every credential is injected
# by FC environment variables at deploy time (see s-webfunction.yaml).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build/fc"
ZIP="$ROOT/build/bruce-fc.zip"
LOCK="$ROOT/build/.build.lock"
COMMIT="$(git -C "$ROOT" rev-parse --short HEAD)"

command -v docker >/dev/null || { echo "docker required (amd64 wheels cannot be built on the host)"; exit 1; }

# SINGLE-WRITER LOCK. Two concurrent builds share $BUILD, so one creates files while the other is
# deleting them — `rm -rf` then fails with "Directory not empty" and the package is left corrupt.
# Observed for real during development. Fail fast and loudly rather than emit a half-built zip that
# only shows up as a broken deployment.
mkdir -p "$ROOT/build"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "ERROR: another build is already running (lock: $LOCK)."
  echo "       Wait for it, or remove the lock directory if that build is dead."
  exit 1
fi
cleanup_lock() { rmdir "$LOCK" 2>/dev/null || true; }
trap cleanup_lock EXIT

# Retry once: on a virtiofs mount a just-released file can briefly linger and defeat the first rm.
rm -rf "$BUILD" "$ZIP" 2>/dev/null || { sleep 1; rm -rf "$BUILD" "$ZIP"; }
mkdir -p "$BUILD/python"

echo "==> installing dependencies for linux/amd64 + python3.11"
docker run --rm --platform linux/amd64 \
  -v "$ROOT/deploy/requirements-fc.txt:/req.txt:ro" \
  -v "$BUILD/python:/out" \
  python:3.11-slim \
  sh -c '
    set -e
    pip install --quiet --no-cache-dir -r /req.txt -t /out
    # Strip what never needs to ship: ~47MB of bytecode/tests/stubs.
    find /out -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
    find /out -type d -name "tests" -prune -exec rm -rf {} + 2>/dev/null || true
    find /out \( -name "*.pyc" -o -name "*.pyi" \) -delete 2>/dev/null || true
    chmod -R a+rX /out
  '

echo "==> adding source + bootstrap"
cp -R "$ROOT/engine/bruce_engine" "$BUILD/bruce_engine"
cp -R "$ROOT/engine/migrations" "$BUILD/migrations"
cp "$ROOT/engine/alembic.ini" "$BUILD/alembic.ini"
cp "$ROOT/deploy/bootstrap" "$BUILD/bootstrap"
# FC requires bootstrap to be executable. python's zipfile drops the exec bit, so we use the zip
# CLI below and set the mode here.
chmod 755 "$BUILD/bootstrap"
find "$BUILD/bruce_engine" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true

# Belt and braces: a .env must never reach the package.
find "$BUILD" -name ".env" -o -name ".env.*" ! -name ".env.example" | grep -q . && {
  echo "REFUSING TO BUILD: a .env file reached the package staging dir"; exit 1; }

echo "==> zipping (zip CLI, to preserve bootstrap's exec bit)"
( cd "$BUILD" && zip -qr9 "$ZIP" . )

RAW=$(du -m "$ZIP" | cut -f1)
B64=$(python3 -c "import os;print(round(os.path.getsize('$ZIP')*4/3/1e6,1))")
echo
echo "    package : $ZIP"
echo "    commit  : $COMMIT"
echo "    zip     : ${RAW} MB"
echo "    base64  : ${B64} MB  (create/update request body limit: 100 MB)"
python3 - "$ZIP" <<'PY'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
names = z.namelist()
assert "./bootstrap" in names or "bootstrap" in names, "bootstrap missing from package"
info = z.getinfo([n for n in names if n.rstrip('/').endswith('bootstrap')][0])
mode = (info.external_attr >> 16) & 0o777
print(f"    bootstrap mode: {oct(mode)}  {'OK (executable)' if mode & 0o111 else 'BROKEN — FC will not start it'}")
assert not any(n.endswith('.env') for n in names), "SECRET LEAK: .env is in the package"
print(f"    files   : {len(names)}   .env present: no")
PY
