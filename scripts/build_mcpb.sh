#!/usr/bin/env bash
# Build an MCP Bundle (.mcpb) for ade-dedrm.
#
# The MCPB format is a zip archive containing manifest.json at the root
# plus everything the `uv` runtime needs to install and run the server.
# For type="uv" bundles we must ship pyproject.toml and the source tree,
# and we must NOT ship any pre-built virtualenv — the host runs uv at
# install time against our pyproject.
#
# Usage:
#   bash scripts/build_mcpb.sh            # writes dist/ade-dedrm-<version>.mcpb
#   bash scripts/build_mcpb.sh -o out.mcpb
#
# This script has no runtime dependencies beyond POSIX `zip`. It reads
# the version from manifest.json via a small Python one-liner (or jq if
# available), but falls back gracefully if neither is installed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

OUT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--output)
            OUT="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '2,18p' "$0"
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if [[ ! -f manifest.json ]]; then
    echo "error: manifest.json not found in $REPO_ROOT" >&2
    exit 1
fi

# Pull version out of manifest.json.
VERSION=""
if command -v python3 >/dev/null 2>&1; then
    VERSION="$(python3 -c 'import json,sys; print(json.load(open("manifest.json"))["version"])')"
elif command -v jq >/dev/null 2>&1; then
    VERSION="$(jq -r .version manifest.json)"
fi
if [[ -z "$VERSION" ]]; then
    VERSION="unknown"
fi

if [[ -z "$OUT" ]]; then
    mkdir -p dist
    OUT="dist/ade-dedrm-${VERSION}.mcpb"
fi

# Absolute path so we can `cd` into a staging dir without losing it.
case "$OUT" in
    /*) ABS_OUT="$OUT" ;;
    *)  ABS_OUT="$REPO_ROOT/$OUT" ;;
esac
mkdir -p "$(dirname "$ABS_OUT")"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "staging bundle in $STAGE"

# What goes into the bundle. Keep this list minimal: the uv runtime only
# needs the Python source + pyproject.toml + the manifest. README/LICENSE
# are included so users can read them from the installed bundle.
cp manifest.json    "$STAGE/"
cp pyproject.toml   "$STAGE/"
cp README.md        "$STAGE/"
[[ -f README.mcpb.md ]] && cp README.mcpb.md "$STAGE/"
[[ -f LICENSE ]]        && cp LICENSE        "$STAGE/"
[[ -f NOTICE ]]         && cp NOTICE         "$STAGE/"

mkdir -p "$STAGE/src"
cp -R src/ade_dedrm "$STAGE/src/ade_dedrm"

# Strip caches so the bundle is reproducible and small.
find "$STAGE" -type d -name "__pycache__" -exec rm -rf {} +
find "$STAGE" -type f -name "*.pyc"       -delete

# The MCPB format is just a zip. We deliberately do NOT include any .venv
# / server/lib / server/venv — the uv runtime installs everything.
rm -f "$ABS_OUT"
(
    cd "$STAGE"
    zip -qr "$ABS_OUT" . -x "*.DS_Store" "*.pyc" "__pycache__/*"
)

SIZE="$(du -h "$ABS_OUT" | cut -f1)"
echo "built $ABS_OUT ($SIZE)"
