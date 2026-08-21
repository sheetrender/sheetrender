# Contributing

Thanks for looking under the hood.

## Running the tests

You don't need Python installed — everything runs in containers (Docker or
rootless Podman):

```sh
scripts/test.sh            # unit suite; Chromium-dependent tests self-skip
scripts/test.sh --render   # the real-Chromium tests, in Playwright's Python image
scripts/test.sh --all      # both — run this before opening a PR
scripts/lint.sh            # ruff
```

The first run downloads dependencies into named volumes
(`sheetrender-lib-uv-*`); later runs are fast. Any pytest arguments pass
through: `scripts/test.sh tests/test_templating.py -k money`.

If you prefer a local environment: `uv sync`, `uv run playwright install
chromium`, `uv run pytest`.

## Ground rules

- **A green plain run is not a passing suite.** The unit run skips the seven
  Chromium test cases; `--all` is the bar.
- New behavior needs a test. Bug fixes need a test that fails without the fix.
- This engine also powers [sheetrender.com](https://sheetrender.com), which
  pins exact versions — behavior changes to rendering output (pagination,
  stamping, sanitization) get extra scrutiny because a thousand production
  templates depend on them.
- Dependencies: floors only in `pyproject.toml`; `uv.lock` is committed. Bump
  deliberately, one PR per bump.

## Reporting bugs

A failing template is the perfect bug report: open an issue with the smallest
HTML + a few CSV rows that reproduce it. For anything security-relevant, see
[SECURITY.md](SECURITY.md) instead of a public issue.
