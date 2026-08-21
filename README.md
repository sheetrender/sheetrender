# SheetRender

**Spreadsheet + HTML template in, a stack of PDFs out.**

SheetRender turns a CSV or XLSX file and an HTML template into one
well-paginated PDF per row, or per group of rows for documents with line items
like invoices and statements. It's the same rendering engine that runs
[sheetrender.com](https://sheetrender.com), pulled out as a standalone MIT
library and CLI.

```sh
git clone https://github.com/sheetrender/sheetrender && cd sheetrender
uvx sheetrender batch examples/invoice/template.html examples/invoice/data.csv \
  -o out/ --group-by invoice_no --filename "{{ invoice_no }}.pdf" --zip invoices.zip
```

That renders one invoice per `invoice_no`, with the group's rows available to
the template as `items`, names each file from a template, and zips the stack.
(The clone is only there for the example files. `uvx sheetrender` itself needs
no install at all.)

## Why this exists

Every "generate PDFs from a spreadsheet" recipe I found glues a headless
browser to a for-loop and hopes. That holds up fine for fifty rows. Somewhere
past a few thousand it starts leaking memory, or it wedges, or a webfont request
stalls and takes a render slot down with it. I hit all of those running this in
production, and the fixes are what's in here:

- The browser is shared across renders and sits behind a priority-aware
  concurrency gate. It gets recycled after N renders or N minutes, so a
  thousand-row batch doesn't leak or wedge.
- Print CSS behaves. Margins, page sizes, backgrounds, and a bounded wait for
  webfonts, so a font fetch that never returns degrades to fallback fonts
  instead of hanging the render.
- Jinja2 runs sandboxed with `StrictUndefined` and autoescaping on. Misspell a
  column name and the render fails loudly instead of handing you 800 documents
  with a hole in them.
- Templates are sanitized with [nh3](https://github.com/messense/nh3), and the
  browser context blocks every network request except an allowlist (Google
  Fonts by default).
- Filenames come from templates, sanitized per-platform and de-duplicated. Rows
  group with auto-detection, PDFs merge with stamped page numbers, and there's
  zip output, thumbnails and PDF metadata.

## Install

```sh
uv add sheetrender          # or: pip install sheetrender
uv run playwright install chromium
```

Or run the CLI without installing anything: `uvx sheetrender --help`. You still
need Chromium once. Without a local playwright on PATH, that's

```sh
uvx --from playwright playwright install chromium
```

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

# Single document from a JSON object file (or --set key=value)
sheetrender render letter.html -o letter.pdf --data row.json

# What's in this file, and what will the template see?
sheetrender inspect data.xlsx

# PNG thumbnail of the first page
sheetrender thumbnail template.html -o thumb.png --data row.json
```

Page geometry: `--page-size A3|A4|A5|Letter|Legal|Tabloid --landscape
--margin 12mm`.

Watermarking works on `batch` only.
`--watermark-html '<div style="position:fixed;bottom:0">DRAFT</div>'` injects
your snippet before `</body>`. Use `position:fixed` if you want it on every
printed page; without it the snippet just sits at the end of the document.

Data files are `.csv` or `.xlsx`. The single-document `--data` flag takes a JSON
object file.

## Python API

```python
import asyncio
from pathlib import Path

import sheetrender as sr

async def main() -> None:
    out = Path("out")
    out.mkdir(exist_ok=True)
    await sr.start_browser()
    try:
        template = sr.compile_template(Path("template.html").read_text())
        parsed = sr.parse_csv("data.csv")
        async with sr.render_context() as renderer:
            for i, row in enumerate(sr.iter_rows("data.csv", parsed["columns"])):
                html = sr.render_compiled(template, row)
                pdf = await renderer.render_pdf(html)
                (out / f"row_{i:04}.pdf").write_bytes(pdf)
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

Templates are plain HTML and CSS rendered by Chromium's print pipeline, with
Jinja2 for data. Each row's columns become top-level variables, so a header of
`Invoice No.` is available as `{{ invoice_no }}` (`sheetrender inspect` shows
the exact mapping). The environment is sandboxed, autoescaped and strict:
referencing a column that doesn't exist is an error, not a silent blank.

The formatting filters are null-safe, so bad input renders as an empty string
rather than crashing halfway through a batch:

| Filter | Example output | Notes |
|---|---|---|
| `money` | `$1,234` | whole-dollar |
| `money2` | `$1,234.50` | cents |
| `money_k` | `$234K`, `$1.2M` | compact, negatives as `-$…` |
| `comma` / `comma2` | `1,234` / `1,234.50` | no currency symbol |
| `pct` | `89%` | rounds to whole percent |
| `bar_width` | `0`–`100` | clamped, for CSS bar charts |
| `sign_class` | `positive` / `negative` | takes the value to compare against: `{{ actual \| sign_class(target) }}` |
| `yesno_class` | `""` / `no` | empty string for truthy (default styling), `no` for falsy |
| `sumcol` | `{{ items \| sumcol('amount') \| money2 }}` | Decimal-exact column sum. Strips `$€£` and commas, treats `(123)` as negative |

Rendering is deterministic across machines. The browser context is pinned to
`en-US` and UTC, so dates and numbers format the same everywhere.

### Grouped documents

`--group-by customer_id` (or `grouped_render_units` in Python) renders one
document per group. The template sees the first row's fields at the top level
plus three reserved names: `items` (every row in the group), `item_count` and
`group_key`. See [`examples/invoice/`](examples/invoice/) for a complete
line-item invoice.

## Security model

This is built to render templates you didn't write yourself:

- Jinja2 runs in a `SandboxedEnvironment`, so there's no attribute traversal to
  dangerous internals, and autoescape is on.
- Template HTML goes through nh3 (allowlist-based) before it reaches the
  browser.
- The browser context intercepts every network request and blocks anything
  outside `RenderConfig.allowed_egress_hosts` (Google Fonts by default), so a
  malicious template can't exfiltrate row data through an `<img>` beacon.
- Chromium keeps its sandbox **on**, which is why you shouldn't run the engine
  as root.
- Author `@page` rules are stripped, so template CSS can't override the page
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

Call `configure()` once, and call it before `start_browser()` (or before your
first render, which starts the browser for you). `concurrency` sizes the gate
when the browser starts, so changing it after that does nothing.

## Development

You don't need a local Python. The test suite runs in containers:

```sh
scripts/test.sh            # unit suite (Chromium-dependent tests self-skip)
scripts/test.sh --render   # real-Chromium tests in the Playwright image
scripts/test.sh --all      # both
scripts/lint.sh            # ruff
```

## Hosted version

[sheetrender.com](https://sheetrender.com) is the hosted product built on this
engine: a template wizard with AI design generation, Google Sheets sync,
scheduled runs, and email/Drive delivery. If you'd rather not run Python, that's
the two-minute path.

## License

[MIT](LICENSE)
