#!/usr/bin/env bash
# Build the dependency layer.
#
# Lambda's Python runtime provides boto3 and nothing else — no requests, no
# oauthlib. Without this layer every function fails at import with
# "No module named 'requests'". Wheels must be for the Lambda platform
# (manylinux/aarch64), not the Mac this usually runs on.
set -euo pipefail
cd "$(dirname "$0")/.."

TARGET=layers/deps/python
rm -rf "$TARGET"
mkdir -p "$TARGET"

# uv-managed venvs have no pip, so prefer uv and fall back to pip.
if command -v uv >/dev/null 2>&1; then
  uv pip install \
    --requirement requirements-lambda.txt \
    --target "$TARGET" \
    --python-platform aarch64-manylinux2014 \
    --python-version 3.12 \
    --only-binary=:all: \
    --quiet
else
  python3 -m pip install \
    --requirement requirements-lambda.txt \
    --target "$TARGET" \
    --platform manylinux2014_aarch64 \
    --python-version 3.12 \
    --implementation cp \
    --only-binary=:all: \
    --upgrade \
    --quiet
fi

# Test and metadata directories are dead weight in a layer.
find "$TARGET" -type d \( -name '__pycache__' -o -name 'tests' \) -prune -exec rm -rf {} + 2>/dev/null || true

echo "built $TARGET ($(du -sh "$TARGET" | cut -f1))"
python3 - <<'PY'
import pathlib, sys
target = pathlib.Path("layers/deps/python")
for mod in ("requests", "requests_oauthlib", "oauthlib", "urllib3", "certifi"):
    if not (target / mod).exists():
        sys.exit(f"missing {mod} in the layer")
print("  verified: requests, requests_oauthlib, oauthlib, urllib3, certifi")
PY
