#!/usr/bin/env sh
# Containerized ruff — no local Python or uv required.
#   scripts/lint.sh              # ruff check src tests
#   scripts/lint.sh --fix        # pass any ruff-check args through
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="ghcr.io/astral-sh/uv:0.9.30-python3.12-bookworm-slim"

exec docker run --rm \
  -v "$REPO_ROOT":/repo \
  -v sheetrender-lib-uv-cache:/cache/uv \
  -v sheetrender-lib-uv-venv:/cache/venv \
  -w /repo \
  -e UV_CACHE_DIR=/cache/uv \
  -e UV_PROJECT_ENVIRONMENT=/cache/venv \
  "$IMAGE" uv run ruff check src tests "$@"
