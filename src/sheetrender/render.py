from __future__ import annotations

import asyncio
import html as _html
import io
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import NamedTuple

from sheetrender.browser import (
    PriorityGate,
    _ContextHolder,
    _mark,
    _retry_if_browser_died,
    browser_is_connected,
    render_slot,
    start_browser,
    stop_browser,
)
from sheetrender.config import get_config
from sheetrender.html_sanitize import sanitize_render_html
from sheetrender.pdf_post import (
    _PREVIEW_STAMP_ALPHA,
    _PREVIEW_STAMP_GRAY,
    _pdf_first_page_png,
    apply_pdf_metadata,
    merge_pdfs,
    stamp_pdf_page_numbers,
    stamp_preview_watermark,
    zip_files,
)

logger = logging.getLogger(__name__)

# Chromium lifecycle and finished-PDF work live in sheetrender.browser and
# sheetrender.pdf_post now; both are re-exported here because this module is
# the import path callers know.
__all__ = [
    "BatchRenderer",
    "PdfOptions",
    "PriorityGate",
    "apply_pdf_metadata",
    "browser_is_connected",
    "inject_preview_watermark",
    "inject_watermark",
    "merge_pdfs",
    "render_context",
    "render_pdf",
    "render_thumbnail",
    "rendered_page_count",
    "stamp_pdf_page_numbers",
    "stamp_preview_watermark",
    "start_browser",
    "stop_browser",
    "strip_author_page_rules",
    "zip_files",
]

_PAGE_FORMATS = {
    "a3": "A3",
    "a4": "A4",
    "a5": "A5",
    "letter": "Letter",
    "legal": "Legal",
    "tabloid": "Tabloid",
}

# document.fonts.ready never rejects; a stalled webfont fetch would otherwise
# hold a render slot until Playwright's 30s default evaluate timeout. Degrade
# to fallback fonts instead of hanging.
_FONTS_READY_JS = (
    "Promise.race([document.fonts.ready, new Promise(r => setTimeout(r, 3000))])"
)


# Diagonal PREVIEW stamp for non-deliverable renders. Separate from
# caller-supplied branding on purpose: this one marks non-deliverable renders.
#
# position:fixed is what makes Blink repeat it on every printed page — the same
# property the branding footer relies on. Being out of flow it cannot shift
# content or add pages, and inset:0 + overflow:hidden clips it to the page's
# content box so an oversized glyph run can't spill. Colour is text fill, not a
# background image: page.pdf can run with print_background=False and drops those.
# Sized in cqmin, not vw: that content box is inset by the template's margins
# while vw measures the whole sheet, so wide margins clipped the word mid-glyph.
# cqmin measures the box we actually paint into, and takes the
# smaller axis so landscape doesn't overflow vertically. 20cqmin is the fit
# bound — a rotated "PREVIEW" spans ~4.2x its font-size — with the px line as a
# fallback if container queries ever regress.
# No letter-spacing — it makes Chromium emit per-glyph offsets that pypdf then
# extracts as "P R E V I E W", which the render test asserts against.
# !important throughout because template CSS is author-supplied and may style
# bare divs and spans.
def _preview_watermark_html(text: str = "PREVIEW") -> str:
    return (
        '<style>.sp-preview-stamp{position:fixed!important;inset:0!important;'
        'z-index:2147483000!important;display:flex!important;'
        'align-items:center!important;justify-content:center!important;'
        'overflow:hidden!important;pointer-events:none!important;'
        'margin:0!important;padding:0!important;border:0!important;'
        'background:none!important;visibility:visible!important;opacity:1!important;'
        'container-type:size!important;}'
        '.sp-preview-stamp>span{font-family:Helvetica,Arial,sans-serif!important;'
        'font-weight:700!important;font-size:110px;font-size:20cqmin;'
        'line-height:1!important;white-space:nowrap!important;'
        f'color:rgba({int(_PREVIEW_STAMP_GRAY[0] * 255)},'
        f'{int(_PREVIEW_STAMP_GRAY[1] * 255)},'
        f'{int(_PREVIEW_STAMP_GRAY[2] * 255)},{_PREVIEW_STAMP_ALPHA})!important;'
        'transform:rotate(-45deg)!important;'
        '-webkit-user-select:none;user-select:none;'
        '-webkit-print-color-adjust:exact;print-color-adjust:exact;}'
        '</style>'
        f'<div class="sp-preview-stamp" aria-hidden="true"><span>{_html.escape(text)}</span></div>'
    )


# Tags and at-rules are matched with case-insensitive regexes on the original
# string. Searching html.lower() and slicing html with the result is wrong:
# "İ".lower() is two characters, so one Turkish name shifts every later index.
_BODY_CLOSE_RE = re.compile(r"</body>", re.IGNORECASE)
# A <style> that is never closed runs to the end of the document, as it does in
# the browser.
_STYLE_BLOCK_RE = re.compile(r"(<style\b[^>]*>)(.*?)(?=</style|\Z)", re.IGNORECASE | re.DOTALL)
_PAGE_RULE_RE = re.compile(r"@page", re.IGNORECASE)


def _inject_before_body(html: str, snippet: str) -> str:
    closers = list(_BODY_CLOSE_RE.finditer(html))
    if not closers:
        return html + snippet
    idx = closers[-1].start()
    return html[:idx] + snippet + html[idx:]


def inject_watermark(html: str, watermark_html: str) -> str:
    """Inject a bottom-fixed watermark footer into HTML before </body>.

    `watermark_html` is trusted: the render functions inject it after
    sanitization, so it reaches Chromium exactly as given. Never build it from
    template or spreadsheet content.
    """
    return _inject_before_body(html, watermark_html)


def inject_preview_watermark(html: str, *, text: str = "PREVIEW") -> str:
    """Mark HTML output with a diagonal overlay (default "PREVIEW").

    For output that stays HTML. PDFs are better served by
    stamp_preview_watermark, which stamps after rendering so template CSS
    cannot touch it; this injected version lives in the same document as the
    template's own styles and can be out-styled by them, so treat it as a
    label, not a guarantee.

    Independent of inject_watermark, which injects caller-supplied branding.
    """
    return _inject_before_body(html, _preview_watermark_html(text))


# Chromium renders header/footer templates in the page margin area with no
# document styles applied, so everything must be inline (and font-size set —
# the default is unreadably small).
_PAGE_NUMBER_MARKUP = {
    "page_x_of_y": 'Page <span class="pageNumber"></span> of <span class="totalPages"></span>',
    "x_of_y": '<span class="pageNumber"></span> / <span class="totalPages"></span>',
}


def _page_number_footer(position: str, fmt: str) -> str:
    markup = _PAGE_NUMBER_MARKUP.get(fmt, _PAGE_NUMBER_MARKUP["page_x_of_y"])
    align = position if position in ("left", "center", "right") else "center"
    # Horizontal padding keeps left/right numbering off the physical page edge.
    return (
        f'<div style="width:100%;font-size:9px;font-family:Helvetica,Arial,sans-serif;'
        f'color:#888;text-align:{align};padding:0 10mm;box-sizing:border-box;">{markup}</div>'
    )


# Suppress Chromium's default header (date + title) when the footer is enabled.
_EMPTY_HEADER = "<span></span>"


def strip_author_page_rules(html: str) -> str:
    """Remove the template's own ``@page`` rules before PDF rendering.

    Page size and margins are controlled by the API (they become Playwright
    ``page.pdf(format=..., margin=...)`` options). Chromium treats an author
    ``@page { margin: ... }`` as overriding those pdf() margins, so an
    template that emits ``@page { size: A4; margin: 0 }`` silently
    defeats the user's margin and page-size choices. Stripping the rule lets the
    pdf() options govern geometry. Brace-aware so nested margin-box at-rules
    (e.g. ``@page { @top-center { ... } }``) are removed whole. Only ``<style>``
    element contents are touched: a literal "@page" in visible text stays.
    """
    return _STYLE_BLOCK_RE.sub(
        lambda block: block.group(1) + _strip_page_rules_from_css(block.group(2)),
        html,
    )


def _strip_page_rules_from_css(css: str) -> str:
    out: list[str] = []
    pos = 0
    while True:
        rule = _PAGE_RULE_RE.search(css, pos)
        brace = css.find("{", rule.start()) if rule else -1
        if brace == -1:
            out.append(css[pos:])
            return "".join(out)
        out.append(css[pos : rule.start()])
        pos = _matching_brace_end(css, brace)


def _matching_brace_end(css: str, open_brace: int) -> int:
    """Index just past the "}" that closes the "{" at `open_brace`.

    Returns len(css) if the block is never closed.
    """
    depth = 0
    for index in range(open_brace, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return len(css)


_DEFAULT_MARGIN_MM = 15


class PdfOptions(NamedTuple):
    format: str
    landscape: bool
    margins: dict[str, str]
    page_numbers: bool
    page_number_position: str
    page_number_format: str
    scale: float
    print_background: bool


def _pdf_options(page_settings: dict | None = None) -> PdfOptions:
    ps = page_settings or {}
    page_size = ps.get("page_size", "a4")
    orientation = ps.get("orientation", "portrait")
    margins_raw = ps.get("margins") or {}
    margins = {}
    for side in ("top", "right", "bottom", "left"):
        value = margins_raw.get(side)
        margins[side] = f"{_DEFAULT_MARGIN_MM if value is None else value}mm"
    return PdfOptions(
        format=_PAGE_FORMATS.get(page_size, "A4"),
        landscape=orientation == "landscape",
        margins=margins,
        page_numbers=bool(ps.get("page_numbers", False)),
        page_number_position=ps.get("page_number_position", "center"),
        page_number_format=ps.get("page_number_format", "page_x_of_y"),
        scale=min(1.5, max(0.5, float(ps.get("scale", 1.0) or 1.0))),
        print_background=bool(ps.get("print_background", True)),
    )


def _prepare_html(html: str, watermark_html: str | None, timing: dict | None) -> str:
    """Sanitize the caller's HTML and inject their branding, timing the pass."""
    t = time.monotonic()
    html = sanitize_render_html(
        html, allowed_egress_hosts=get_config().allowed_egress_hosts
    )
    # Must follow sanitization — nh3 would strip it otherwise. The PREVIEW mark
    # is deliberately not injected here: it is stamped onto the finished PDF in
    # render_pdf, where template CSS cannot reach it.
    if watermark_html:
        html = inject_watermark(html, watermark_html)
    _mark(timing, "sanitize", t)
    return html


async def _render_page_pdf(
    holder: _ContextHolder,
    html: str,
    opts: PdfOptions,
    timing: dict | None = None,
) -> bytes:
    """Render one PDF on a fresh page in the holder's shared context."""
    footer_kwargs = (
        {
            "display_header_footer": True,
            "header_template": _EMPTY_HEADER,
            "footer_template": _page_number_footer(opts.page_number_position, opts.page_number_format),
        }
        if opts.page_numbers
        else {}
    )
    t = time.monotonic()
    context = await holder.get()
    t = _mark(timing, "context", t)
    page = await context.new_page()
    t = _mark(timing, "new_page", t)
    try:
        # Drop template @page rules so the API's page size/margins (passed to
        # page.pdf below) are not overridden by the design's own declarations.
        await page.set_content(strip_author_page_rules(html), wait_until="load")
        t = _mark(timing, "set_content", t)
        await page.evaluate(_FONTS_READY_JS)
        t = _mark(timing, "fonts", t)
        pdf = await page.pdf(
            format=opts.format,
            landscape=opts.landscape,
            margin=opts.margins,
            scale=opts.scale,
            print_background=opts.print_background,
            prefer_css_page_size=False,
            tagged=True,
            **footer_kwargs,
        )
        _mark(timing, "pdf", t)
        if timing is not None:
            logger.info(
                "render timing: %s total=%.0fms html=%.0fkB pdf=%.0fkB",
                " ".join(f"{k}={v:.0f}ms" for k, v in timing.items()),
                sum(timing.values()),
                len(html) / 1024,
                len(pdf) / 1024,
            )
        return pdf
    except Exception:
        holder.invalidate()
        raise
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def render_pdf(
    html: str,
    page_settings: dict | None = None,
    *,
    watermark_html: str | None = None,
    preview: bool = False,
    priority: bool = False,
) -> bytes:
    timing: dict | None = {} if get_config().timing_logs else None
    html = _prepare_html(html, watermark_html, timing)
    opts = _pdf_options(page_settings)

    async def _render() -> bytes:
        # One-off renders may come from unrelated callers, so each gets a
        # throwaway context: sharing one would share cookies, HTTP cache, and
        # other context state across documents that should not see each other.
        # Context reuse stays inside BatchRenderer, which is scoped to a batch.
        holder = _ContextHolder()
        try:
            return await _render_page_pdf(holder, html, opts, timing=timing)
        finally:
            await holder.close()

    async with render_slot(high=priority, timing=timing):
        pdf = await _retry_if_browser_died(_render)
    if preview:
        # Off the event loop: pikepdf reserialises the whole document, and a
        # preview of a long batch row would otherwise stall every other render.
        pdf = await asyncio.to_thread(stamp_preview_watermark, pdf)
    return pdf


class BatchRenderer:
    """Reuse one Playwright context for a batch while opening a fresh page per PDF."""

    def __init__(self, page_settings_default: dict | None = None, *, priority: bool = False):
        self._holder = _ContextHolder()
        self._page_settings_default = page_settings_default
        self._priority = priority

    async def render_pdf(self, html: str, page_settings: dict | None = None, *, watermark_html: str | None = None) -> bytes:
        """Render one PDF using the shared batch context.

        No `preview` counterpart to the module-level render_pdf on purpose —
        everything a batch produces is a deliverable and must never be stamped.
        """
        timing: dict | None = {} if get_config().timing_logs else None
        html = _prepare_html(html, watermark_html, timing)
        effective_page_settings = self._page_settings_default if page_settings is None else page_settings
        opts = _pdf_options(effective_page_settings)

        async def _render() -> bytes:
            return await _render_page_pdf(self._holder, html, opts, timing=timing)

        async with render_slot(high=self._priority, timing=timing):
            return await _retry_if_browser_died(_render)

    async def close(self):
        await self._holder.close()


@asynccontextmanager
async def render_context(*, priority: bool = False):
    """Yield a BatchRenderer for a render batch and close its context afterward."""
    renderer = BatchRenderer(priority=priority)
    try:
        yield renderer
    finally:
        await renderer.close()


async def render_thumbnail(html: str, page_settings: dict | None = None, *, priority: bool = False) -> bytes:
    """Return a PNG of the first page of the rendered PDF.

    Rasterized from the same PDF pipeline (print media, margins, watermark
    rules aside) rather than a screen-media screenshot, so thumbnails always
    match what actually prints.

    Thumbnails deliberately carry no watermark. _pdf_first_page_png rasterises
    at 96/72, so an A4 page comes back around 790x1120 and remains legible.
    """
    pdf = await render_pdf(html, page_settings, priority=priority)
    return await asyncio.to_thread(_pdf_first_page_png, pdf)


async def rendered_page_count(html: str, page_settings: dict | None = None) -> int:
    from pypdf import PdfReader

    pdf = await render_pdf(html, page_settings)
    return len(PdfReader(io.BytesIO(pdf)).pages)
