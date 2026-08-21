#!/usr/bin/env sh
# Containerized test runner — no local Python, uv, or browsers required.
#
#   scripts/test.sh                     # unit suite (Chromium tests self-skip)
#   scripts/test.sh tests/test_foo.py   # any pytest args pass through
#   scripts/test.sh --render            # real-Chromium tests in the Playwright image
#   scripts/test.sh --all               # unit suite + Chromium tests
#
# Dependencies live in named volumes (sheetrender-lib-uv-cache,
# sheetrender-lib-uv-venv), so only the first run downloads anything. The
# --render path uses Playwright's own Python image, whose tag is derived from
# the playwright version pinned in uv.lock so the bundled Chromium always
# matches the driver.
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="ghcr.io/astral-sh/uv:0.9.30-python3.12-bookworm-slim"

NEED_BROWSER=0
RUN_UNIT=1

while :; do
  case "${1:-}" in
    --render) NEED_BROWSER=1; RUN_UNIT=0; shift ;;
    --all) NEED_BROWSER=1; RUN_UNIT=1; shift ;;
    *) break ;;
  esac
done

if [ "$RUN_UNIT" = 1 ]; then
  docker run --rm \
    -v "$REPO_ROOT":/repo \
    -v sheetrender-lib-uv-cache:/cache/uv \
    -v sheetrender-lib-uv-venv:/cache/venv \
    -w /repo \
    -e UV_CACHE_DIR=/cache/uv \
    -e UV_PROJECT_ENVIRONMENT=/cache/venv \
    "$IMAGE" uv run pytest "$@"
fi

[ "$NEED_BROWSER" = 0 ] && exit 0

# --- real-Chromium path ------------------------------------------------------
#
# The uv image has the playwright package but no browser binary. Playwright's
# Python image ships Chromium plus its system libs at /ms-playwright; we sync
# our own locked environment inside it and point the driver at those browsers.
# render.py launches Chromium with the sandbox on, which refuses to run as
# root, so this runs as the image's unprivileged pwuser with its own volumes.
PW_VERSION="$(awk '/^name = "playwright"$/{getline; gsub(/[^0-9.]/, "", $0); print; exit}' "$REPO_ROOT/uv.lock")"
[ -n "$PW_VERSION" ] || { echo "could not read playwright version from uv.lock" >&2; exit 1; }
PW_IMAGE="mcr.microsoft.com/playwright/python:v${PW_VERSION}-noble"

# The Playwright image has no uv; seed the binary into the volume from the uv
# image (uv is a static binary, so it runs fine on noble).
docker run --rm -v sheetrender-lib-render-uv-cache:/cache/uv \
  "$IMAGE" sh -c '[ -x /cache/uv/bin/uv ] || { mkdir -p /cache/uv/bin && cp /usr/local/bin/uv /cache/uv/bin/uv; }'

PW_UID="$(docker run --rm --user root \
  -v sheetrender-lib-render-uv-cache:/cache/uv \
  -v sheetrender-lib-render-venv:/cache/venv \
  "$PW_IMAGE" sh -c 'chown -R pwuser:pwuser /cache/uv /cache/venv && id -u pwuser')"

if [ $# -eq 0 ]; then
  set -- tests/test_render.py -q
fi

# --frozen because pwuser must not rewrite uv.lock through the repo mount;
# -p no:cacheprovider keeps pytest from writing a cache dir as pwuser.
exec docker run --rm --user "$PW_UID" \
  -v "$REPO_ROOT":/repo \
  -v sheetrender-lib-render-uv-cache:/cache/uv \
  -v sheetrender-lib-render-venv:/cache/venv \
  -w /repo \
  -e HOME=/home/pwuser \
  -e PATH="/cache/uv/bin:/usr/local/bin:/usr/bin:/bin" \
  -e UV_CACHE_DIR=/cache/uv \
  -e UV_PROJECT_ENVIRONMENT=/cache/venv \
  -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
  "$PW_IMAGE" uv run --frozen pytest -p no:cacheprovider "$@"
