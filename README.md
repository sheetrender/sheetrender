# SheetRender

**Spreadsheet + HTML template in, a stack of PDFs out.**

SheetRender turns a CSV or XLSX file and an HTML template into one
well-paginated PDF per row — or per group of rows, for documents with line
items like invoices and statements. It is the exact rendering engine behind
[sheetrender.com](https://sheetrender.com), extracted as a standalone MIT
library and CLI.

```sh
uvx sheetrender batch examples/invoice/template.html examples/invoice/data.csv \
  -o out/ --group-by invoice_no --filename "{{ invoice_no }}.pdf" --zip invoices.zip
```

That renders one invoice per `invoice_no`, with the group's rows available to
the template as `items`, names each file from a template, and zips the stack.

## Why this exists

Every "generate PDFs from a spreadsheet" recipe on the internet glues a
headless browser to a for-loop and hopes. This engine has rendered documents
in production for a long time, and the parts that took real debugging are the
parts you get for free:

- **Chromium rendering with lifecycle management** — a shared browser with a
  priority-aware concurrency gate, recycled after N renders or N minutes, so
  thousand-row batches don't leak memory or wedge.
- **Print CSS that behaves** — margins, page sizes, backgrounds, webfont
  readiness with a bounded wait (a stalled font fetch degrades to fallback
  fonts instead of hanging a render slot).
- **Safe templating over untrusted data** — sandboxed Jinja2 with
  `StrictUndefined` (typos fail loudly instead of rendering blanks) and
  autoescaping on.
- **HTML sanitization + network egress control** — templates are sanitized
  with [nh3](https://github.com/messense/nh3), and the browser context blocks
  all network requests except an allowlist (Google Fonts by default).
- **Batch ergonomics** — filename templates with cross-platform sanitization
  and de-duplication, row grouping with auto-detection, merged PDFs with
  stamped page numbers, zip output, thumbnails, PDF metadata.

## Install

```sh
uv add sheetrender          # or: pip install sheetrender
uv run playwright install chromium
```

Or run the CLI without installing anything: `uvx sheetrender --help`
(you still need `playwright install chromium` once).

## CLI

```sh
# One PDF per row
sheetrender batch template.html data.csv -o out/

# One PDF per invoice, line items available as {{ items }}
sheetrender batch template.html data.csv -o out/ --group-by invoice_no

# Name files from row data, merge everything, add page numbers
sheetrender batch template.html data.csv -o out/ \
  --filename "{{ customer }}-{{ invoice_no }}.pdf" \
  --merge all.pdf --page-numbers

# Single document from a JSON context (or --set key=value)
sheetrender render letter.html -o letter.pdf --data row.json

# What's in this file, and what will the template see?
sheetrender inspect data.xlsx

# PNG thumbnail of the first page
sheetrender thumbnail template.html -o thumb.png --data row.json
```

Page geometry: `--page-size A4|Letter|Legal --landscape --margin 12mm`.
Watermarking: `--watermark-html '<div class="foot">DRAFT</div>'` injects your
snippet on every page. Both `.csv` and `.xlsx` inputs work everywhere a data
file is accepted.

## Python API

```python
import asyncio
import sheetrender as sr

async def main() -> None:
    await sr.start_browser()
    try:
        template = sr.compile_template(open("template.html").read())
        parsed = sr.parse_csv("data.csv")
        async with sr.render_context() as renderer:
            for i, row in enumerate(sr.iter_rows("data.csv", parsed["columns"])):
                html = sr.render_compiled(template, row)
                pdf = await renderer.render_pdf(html)
                open(f"out/row_{i:04}.pdf", "wb").write(pdf)
    finally:
        await sr.stop_browser()

asyncio.run(main())
```

Renders inside one `render_context` share the browser and are gated to
`RenderConfig.concurrency` parallel pages. `merge_pdfs`, `zip_files`,
`render_thumbnail`, `apply_pdf_metadata`, and the grouping helpers
(`grouped_render_units`, `group_context`, `detect_group_candidates`) are all
exported from the package root.

## Templates

Templates are plain HTML + CSS rendered by Chromium's print pipeline, with
Jinja2 for data. Each row's columns become top-level variables — a header of
`Invoice No.` is available as `{{ invoice_no }}` (`sheetrender inspect` shows
the exact mapping). The environment is sandboxed, autoescaped, and strict:
referencing a column that doesn't exist is an error, not a silent blank.

Null-safe formatting filters (bad input renders as an empty string, never a
crash mid-batch):

| Filter | Example output | Notes |
|---|---|---|
| `money` | `$1,234` | whole-dollar |
| `money2` | `$1,234.50` | cents |
| `money_k` | `$234K`, `$1.2M` | compact, negatives as `-$…` |
| `comma` / `comma2` | `1,234` / `1,234.50` | no currency symbol |
| `pct` | `12.5%` | |
| `bar_width` | `0`–`100` | clamped, for CSS bar charts |
| `sign_class` | `positive` / `negative` | for conditional styling |
| `yesno_class` | `yes` / `no` | |
| `sumcol` | `{{ items \| sumcol('amount') \| money2 }}` | Decimal-exact column sum; strips `$€£` and commas, treats `(123)` as negative |

Rendering is deterministic across machines: the browser context is pinned to
`en-US` / UTC, so dates and numbers format the same everywhere.

### Grouped documents

`--group-by customer_id` (or `grouped_render_units` in Python) renders one
document per group. The template sees the first row's fields at the top level
plus three reserved names: `items` (every row in the group), `item_count`, and
`group_key`. See [`examples/invoice/`](examples/invoice/) for a complete
line-item invoice.

## Security model

Designed for rendering templates you didn't write:

- Jinja2 runs in `SandboxedEnvironment` — no attribute traversal to
  dangerous internals, autoescape on.
- Template HTML is sanitized with nh3 (allowlist-based) before it reaches the
  browser.
- The browser context intercepts all network requests and blocks everything
  outside `RenderConfig.allowed_egress_hosts` (default: Google Fonts) — a
  malicious template can't exfiltrate row data via an `<img>` beacon.
- Chromium runs with its sandbox left **on** (don't run the engine as root).
- Author `@page` rules are stripped so template CSS can't override the page
  geometry you asked for.

## Configuration

```python
from sheetrender import RenderConfig, configure

configure(RenderConfig(
    concurrency=4,                      # parallel Chromium pages
    recycle_max_renders=300,            # recycle the browser after N renders…
    recycle_max_age_minutes=30,         # …or N minutes, whichever comes first
    allowed_egress_hosts=frozenset({"fonts.googleapis.com", "fonts.gstatic.com"}),
    pdf_producer=None,                  # PDF metadata; default "sheetrender/<version>"
))
```

Call `configure()` once, before the first render.

## Development

No local Python needed — the test suite runs in containers:

```sh
scripts/test.sh            # unit suite (Chromium-dependent tests self-skip)
scripts/test.sh --render   # real-Chromium tests in the Playwright image
scripts/test.sh --all      # both
scripts/lint.sh            # ruff
```

## Hosted version

[sheetrender.com](https://sheetrender.com) is the hosted product built on this
engine: a template wizard with AI design generation, Google Sheets sync,
scheduled runs, and email/Drive delivery. If you'd rather not run Python,
that's the two-minute path.

## License

[MIT](LICENSE)
