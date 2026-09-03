# Changelog

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
