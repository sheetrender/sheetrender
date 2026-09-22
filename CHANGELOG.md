# Changelog

## 0.4.0

- New keyword `day_first` on `parse_date`, `date`, `get_env`,
  `compile_template`, `validate_and_render`/`render_row`, `render_text` and
  `render_filename` (and `--day-first` on the `render`, `batch` and `thumbnail`
  commands) reads ambiguous numeric dates such as `03/04/2026` day-first.
  The default is unchanged (month-first). Dates only one order can parse, ISO
  dates, Excel serials and date objects give the same result in both modes.

## 0.3.1

- Date inference: a column typed as a date from dotted or unpadded numeric
  strings becomes a string again when a later nonblank cell (within the first
  5,000 rows) is not a date, so version numbers such as `1.2.2024` and
  `2.0.1` in one column stay text.

## 0.3.0

- `date` formats date/datetime objects, ISO dates and timestamps, common numeric
  date strings, and Excel 1900-system serials 1..73415. Ambiguous day/month
  strings are month-first (US). Default: `%b %-d, %Y`; pass a strftime format
  for another layout. Blank or invalid values produce empty text.
- `money`, `money2`, and `money_k` accept a currency code, including the keyword
  `currency`. USD remains the default; EUR/GBP/JPY use symbols and other codes
  use a code plus a space. `money2` uses zero decimals for JPY and KRW.
- Date inference checks years 1900..2100 and requires a second nonblank date
  for dotted or unpadded numeric strings, within the first 5,000 rows.
  Rendering rows preserve native date/datetime values; only dataset preview
  `sample_rows` convert those values to ISO strings for JSON storage/responses.
- `qr` returns an SVG data URI for use in an image `src`. Optional `size` is
  pixels (default 128, clamped to 16..2048). Blank, unencodable, or over-1000-character
  input returns empty text. Segno generates the code locally.

## 0.2.0

Minor version because some output changes: CSV number inference, column keys
for `None`/`True`/`False`, group keys for whole floats, and the string padding
methods in templates. Details below.

Added:

- `browser_is_connected()` for readiness probes. The browser state that used to
  be module globals in `sheetrender.render` (`_browser`, `_gate`, ...) now lives
  on `sheetrender.browser._state`, so code or tests that read or patched those
  names need updating.

Fixes:

- A template syntax error is reported with its message and line number. It
  used to surface as `TypeError: __init__() missing ... 'lineno'`.
- `inject_watermark` and `strip_author_page_rules` no longer misplace their
  edits when the document contains `İ`. An unclosed `<style>` block also has
  its `@page` rules stripped now.
- CSV cells become numbers only when written as plain decimals. `02134`,
  `Nan`, `Infinity`, `1_000` and `1e3` stay text. Columns holding such values
  are now inferred as `string`.
- CSV and XLSX rows shorter than the header are padded with `None`, so the
  template no longer fails on an undefined variable for that row.
- Column keys are always unique: headers `a, a, a_2` give `a, a_2, a_2_2`.
  Headers `None`, `True` and `False` become `col_none`, `col_true`,
  `col_false`, because Jinja reads the bare words as constants.
- `dedupe_filenames` compares names case-insensitively, filename stems are
  capped at 200 bytes as well as 150 characters, and Windows device names are
  caught with an inner extension (`CON.txt`).
- The sandbox also refuses oversized `+`, `%` formatting, `str.format` widths,
  the `center`, `indent` and `format` filters, and the string padding methods
  (`ljust`, `rjust`, `center`, `zfill`, `expandtabs`), which are no longer
  callable from templates. Growth spread across a loop is still unbounded, so
  run hostile templates under a process memory limit.
- Group auto-detection matches the short name hints (`po`, `ref`, `bill`,
  `no`, `id`) as whole words only, so `postcode`, `paid` and `preferred_name`
  are no longer recommended. Group keys treat `1001` and `1001.0` as equal.
- `money_k(999999)` gives `$1M` instead of `$1000K`, the money and comma
  filters never print a negative zero, and `nan` or `inf` render as empty.
- A `None` page margin falls back to 15mm, and `stamp_preview_watermark`
  rejects non-ASCII text with a clear error.
- CLI: a `--filename` template that fails is reported instead of silently
  falling back to `row_N.pdf`, grouped batches without `--filename` are named
  after the group key, `--page-numbers` without `--merge` is an error,
  `--concurrency` keeps the rest of the configured `RenderConfig`, and
  `inspect` handles a file with no columns.

Internal:

- `render.py` is split into `browser.py` (Chromium lifecycle), `render.py`
  (HTML to PDF) and `pdf_post.py` (stamping, merging, zipping). Every name is
  still importable from `sheetrender.render`.
- `sheetrender.sanitize` moved to `sheetrender.column_keys`; the old module
  re-exports it.

## 0.1.2

- The template sandbox intercepts `*` and `**` and refuses repeats over one
  million characters or exponents over 10,000 before they allocate. The
  rendered-size check ran after evaluation, so `{{ 'x' * 10**9 }}` in a
  template or filename pattern could exhaust memory first.

## 0.1.1

- Contact address is now contact@sheetrender.com (metadata-only release).

## 0.1.0

- Initial extraction of the SheetRender render engine: Chromium PDF rendering
  with browser recycling, sandboxed Jinja templating, CSV/XLSX ingestion, row
  grouping, filename templates, merge/zip, and the `sheetrender` CLI.
