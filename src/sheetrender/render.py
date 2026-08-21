from __future__ import annotations

import asyncio
import io
import logging
import math
import time
from contextlib import asynccontextmanager
from typing import NamedTuple
from urllib.parse import urlparse

from sheetrender.config import get_config
from sheetrender.html_sanitize import sanitize_render_html

logger = logging.getLogger(__name__)

_playwright = None
_browser = None
_gate: PriorityGate | None = None
_semaphore = None
_restart_lock: asyncio.Lock | None = None
_recycle_lock: asyncio.Lock | None = None
_render_count: int = 0
_browser_launch_time: float | None = None


class PriorityGate:
    """Capacity gate that wakes high-priority waiters before low-priority ones."""

    def __init__(self, capacity: int):
        self._capacity = capacity
        self._used = 0
        self._high_waiters: list[asyncio.Future] = []
        self._low_waiters: list[asyncio.Future] = []

    def _try_wake_one(self) -> None:
        for lst in (self._high_waiters, self._low_waiters):
            while lst:
                fut = lst.pop(0)
                if not fut.done():
                    self._used += 1
                    fut.set_result(None)
                    return

    async def acquire(self, high: bool = False) -> None:
        if self._used < self._capacity:
            self._used += 1
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        lst = self._high_waiters if high else self._low_waiters
        lst.append(fut)
        try:
            await fut
        except asyncio.CancelledError:
            try:
                lst.remove(fut)
            except ValueError:
                pass
            if fut.done() and not fut.cancelled():
                self.release()
            raise

    def release(self) -> None:
        self._used -= 1
        self._try_wake_one()

    @asynccontextmanager
    async def slot(self, high: bool = False):
        await self.acquire(high)
        try:
            yield
        finally:
            self.release()

_PAGE_FORMATS = {
    "a3": "A3",
    "a4": "A4",
    "a5": "A5",
    "letter": "Letter",
    "legal": "Legal",
    "tabloid": "Tabloid",
}

# Page pixel dimensions at 96dpi (CSS pixels)
_PAGE_PX = {
    "a3": (1123, 1587),
    "a4": (794, 1123),
    "a5": (559, 794),
    "letter": (816, 1056),
    "legal": (816, 1344),
    "tabloid": (1056, 1632),
}

# Pin formatting-sensitive environment so Intl/date output in templates does
# not depend on the host the render happens to run on.
_CONTEXT_OPTS = {"locale": "en-US", "timezone_id": "UTC"}

# document.fonts.ready never rejects; a stalled webfont fetch would otherwise
# hold a render slot until Playwright's 30s default evaluate timeout. Degrade
# to fallback fonts instead of hanging.
_FONTS_READY_JS = (
    "Promise.race([document.fonts.ready, new Promise(r => setTimeout(r, 3000))])"
)

# How the diagonal PREVIEW mark is tinted, in both the CSS and the PDF paths.
# Kept as one pair of numbers because the two have to look like the same mark:
# the same document previewed as HTML and as PDF should not appear to change.
# A wash, not lettering — low enough that the design underneath reads normally
# through it.
_PREVIEW_STAMP_GRAY = (0.47, 0.47, 0.47)
_PREVIEW_STAMP_ALPHA = 0.13

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
        f'<div class="sp-preview-stamp" aria-hidden="true"><span>{text}</span></div>'
    )


def _inject_before_body(html: str, snippet: str) -> str:
    tag = "</body>"
    idx = html.lower().rfind(tag)
    if idx == -1:
        return html + snippet
    return html[:idx] + snippet + html[idx:]


def inject_watermark(html: str, watermark_html: str) -> str:
    """Inject a bottom-fixed watermark footer into HTML before </body>."""
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


async def _egress_guard(route) -> None:
    """Default-deny network egress for render pages.

    Backstop behind sanitize_render_html: CSS url()/@import fetches bypass
    markup sanitization but still route through here. Also blocks link-local
    targets like the cloud metadata endpoint (169.254.169.254).
    """
    host = (urlparse(route.request.url).hostname or "").lower()
    if host in get_config().allowed_egress_hosts:
        await route.continue_()
    else:
        await route.abort()


async def _lock_down_context(context) -> None:
    await context.route("**/*", _egress_guard)


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
    lower = html.lower()
    out: list[str] = []
    pos = 0
    while True:
        open_idx = lower.find("<style", pos)
        if open_idx == -1:
            out.append(html[pos:])
            break
        content_start = html.find(">", open_idx)
        close_idx = lower.find("</style", content_start if content_start != -1 else open_idx)
        if content_start == -1 or close_idx == -1:
            out.append(html[pos:])
            break
        out.append(html[pos : content_start + 1])
        out.append(_strip_page_rules_from_css(html[content_start + 1 : close_idx]))
        pos = close_idx
    return "".join(out)


def _strip_page_rules_from_css(css: str) -> str:
    lower = css.lower()
    out: list[str] = []
    i = 0
    n = len(css)
    while i < n:
        idx = lower.find("@page", i)
        if idx == -1:
            out.append(css[i:])
            break
        brace = css.find("{", idx)
        if brace == -1:
            out.append(css[i:])
            break
        out.append(css[i:idx])
        depth = 1
        k = brace + 1
        while k < n and depth:
            c = css[k]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            k += 1
        i = k
    return "".join(out)


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
    margins = {
        "top": f"{margins_raw.get('top', 15)}mm",
        "right": f"{margins_raw.get('right', 15)}mm",
        "bottom": f"{margins_raw.get('bottom', 15)}mm",
        "left": f"{margins_raw.get('left', 15)}mm",
    }
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


def _should_recycle_check(render_count: int, launch_time: float | None, max_renders: int, max_age_minutes: int) -> bool:
    if render_count >= max_renders:
        return True
    if launch_time is None:
        return False
    elapsed_minutes = (time.monotonic() - launch_time) / 60
    return elapsed_minutes >= max_age_minutes


def _active_gate():
    return _gate if _gate is not None else _semaphore


@asynccontextmanager
async def _gate_slot(gate, *, high: bool = False):
    if hasattr(gate, "slot"):
        async with gate.slot(high=high):
            yield
    else:
        async with gate:
            yield


async def start_browser():
    global _playwright, _browser, _gate, _semaphore, _restart_lock, _recycle_lock, _render_count, _browser_launch_time
    if _browser is not None:
        return
    from playwright.async_api import async_playwright

    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(headless=True)
    _gate = PriorityGate(get_config().concurrency)
    _semaphore = None
    _restart_lock = asyncio.Lock()
    _recycle_lock = asyncio.Lock()
    _render_count = 0
    _browser_launch_time = time.monotonic()


async def _get_browser():
    """Return a connected browser, relaunching Chromium if it has died."""
    global _playwright, _browser, _render_count, _browser_launch_time
    if _browser is not None and _browser.is_connected():
        return _browser
    if _restart_lock is None:
        raise RuntimeError("Playwright browser is not started")
    async with _restart_lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        from playwright.async_api import async_playwright

        try:
            if _browser is not None:
                await _browser.close()
        except Exception:
            pass
        try:
            if _playwright is not None:
                await _playwright.stop()
        except Exception:
            pass
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=True)
        _render_count = 0
        _browser_launch_time = time.monotonic()
        return _browser


async def _maybe_recycle() -> None:
    global _playwright, _browser, _render_count, _browser_launch_time
    gate = _active_gate()
    if gate is None:
        raise RuntimeError("Playwright browser is not started")
    if not _should_recycle_check(
        _render_count,
        _browser_launch_time,
        get_config().recycle_max_renders,
        get_config().recycle_max_age_minutes,
    ):
        return
    if _recycle_lock is None or _restart_lock is None:
        raise RuntimeError("Playwright browser is not started")
    if _recycle_lock.locked():
        return

    async with _recycle_lock:
        if not _should_recycle_check(
            _render_count,
            _browser_launch_time,
            get_config().recycle_max_renders,
            get_config().recycle_max_age_minutes,
        ):
            return

        # Drain before taking _restart_lock: a crashed in-flight render (still
        # holding a slot) needs _restart_lock inside _get_browser to finish, so
        # holding it while waiting on slots would deadlock.
        acquired = 0
        try:
            for _ in range(get_config().concurrency):
                await gate.acquire(high=True)
                acquired += 1

            async with _restart_lock:
                from playwright.async_api import async_playwright

                try:
                    if _browser is not None:
                        await _browser.close()
                except Exception:
                    pass
                try:
                    if _playwright is not None:
                        await _playwright.stop()
                except Exception:
                    pass
                _playwright = await async_playwright().start()
                _browser = await _playwright.chromium.launch(headless=True)
                _render_count = 0
                _browser_launch_time = time.monotonic()
        finally:
            for _ in range(acquired):
                gate.release()


async def stop_browser():
    global _playwright, _browser, _gate, _semaphore, _restart_lock, _recycle_lock, _render_count, _browser_launch_time
    if _browser:
        await _browser.close()
    if _playwright:
        await _playwright.stop()
    _playwright = None
    _browser = None
    _gate = None
    _semaphore = None
    _restart_lock = None
    _recycle_lock = None
    _render_count = 0
    _browser_launch_time = None


async def _retry_if_browser_died(render):
    """Run a render attempt, retrying once if Chromium died mid-flight."""
    try:
        return await render()
    except Exception as exc:
        if _browser is not None and _browser.is_connected():
            raise
        # Browser is gone — _get_browser() will relaunch it on the retry.
        _ = exc
        return await render()


class _ContextHolder:
    """Lazily create a locked-down context, recreating it if Chromium died."""

    def __init__(self):
        self._context = None
        self._browser = None
        self._lock = asyncio.Lock()

    def _alive(self) -> bool:
        try:
            return self._context is not None and self._browser is not None and self._browser.is_connected()
        except Exception:
            return False

    async def get(self):
        if self._alive():
            return self._context
        async with self._lock:
            if self._alive():
                return self._context
            browser = await _get_browser()
            try:
                if self._context is not None:
                    await self._context.close()
            except Exception:
                pass
            self._context = await browser.new_context(**_CONTEXT_OPTS)
            await _lock_down_context(self._context)
            self._browser = browser
            return self._context

    def invalidate(self) -> None:
        """Drop the context, closing the old one in the background.

        Closing matters even on the failure path: a context that is merely
        dereferenced survives inside a still-connected Chromium until browser
        recycling, so repeated page failures would pile up abandoned contexts.
        """
        context = self._context
        self._context = None
        self._browser = None
        if context is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # interpreter teardown; nothing left to close against
            loop.create_task(_close_context_quietly(context))

    async def close(self) -> None:
        context = self._context
        self._context = None
        self._browser = None
        if context is not None:
            await _close_context_quietly(context)


async def _close_context_quietly(context) -> None:
    try:
        await context.close()
    except Exception:
        pass


def _mark(timing: dict | None, key: str, since: float) -> float:
    """Record elapsed ms since `since` under `key`; return the current time."""
    now = time.monotonic()
    if timing is not None:
        timing[key] = (now - since) * 1000
    return now


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
    t = time.monotonic()
    html = sanitize_render_html(
        html, allowed_egress_hosts=get_config().allowed_egress_hosts
    )
    # Must follow sanitization — nh3 would strip it otherwise. The PREVIEW mark
    # is deliberately not injected here: it is stamped onto the finished PDF
    # below, where template CSS cannot reach it.
    if watermark_html:
        html = inject_watermark(html, watermark_html)
    _mark(timing, "sanitize", t)
    gate = _active_gate()
    if gate is None:
        raise RuntimeError("Playwright browser is not started")
    await _maybe_recycle()

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

    global _render_count
    gate_t = time.monotonic()
    async with _gate_slot(gate, high=priority):
        _mark(timing, "gate_wait", gate_t)
        try:
            pdf = await _retry_if_browser_died(_render)
        finally:
            _render_count += 1
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
        t = time.monotonic()
        html = sanitize_render_html(
            html, allowed_egress_hosts=get_config().allowed_egress_hosts
        )
        if watermark_html:
            html = inject_watermark(html, watermark_html)
        _mark(timing, "sanitize", t)
        gate = _active_gate()
        if gate is None:
            raise RuntimeError("Playwright browser is not started")
        await _maybe_recycle()

        effective_page_settings = self._page_settings_default if page_settings is None else page_settings
        opts = _pdf_options(effective_page_settings)

        async def _render() -> bytes:
            return await _render_page_pdf(self._holder, html, opts, timing=timing)

        global _render_count
        gate_t = time.monotonic()
        async with _gate_slot(gate, high=self._priority):
            _mark(timing, "gate_wait", gate_t)
            try:
                return await _retry_if_browser_died(_render)
            finally:
                _render_count += 1

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


def _pdf_first_page_png(pdf_bytes: bytes) -> bytes:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        # 96/72 matches the CSS-pixel density previews use, so thumbnails line
        # up with _PAGE_PX dimensions.
        bitmap = doc[0].render(scale=96 / 72)
        image = bitmap.to_pil()
        buf = io.BytesIO()
        image.save(buf, "PNG")
        return buf.getvalue()
    finally:
        doc.close()


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


# Chromium footer templates use a 10mm horizontal inset (see
# _page_number_footer); mirror it when stamping so labels line up.
_FOOTER_INSET_PT = 10 / 25.4 * 72
_STAMP_FONT_SIZE = 7

# Standard Helvetica AFM advance widths (per 1000 units); pikepdf's Helvetica
# does not ship metrics. Printable ASCII in full rather than just the glyphs one
# caller happens to need: this started as the alphabet of "Page X of Y", and
# when stamp_preview_watermark reused it for "PREVIEW" the five missing capitals
# silently fell through to the 556 default. That underestimated the word by 13%,
# which is enough to push the stamp off-centre and clip the W past the sheet
# edge on every page size — a wrong number here fails quietly, so keep it whole.
_HELVETICA_WIDTHS = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667,
    "'": 191, "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333,
    ".": 278, "/": 278,
    "0": 556, "1": 556, "2": 556, "3": 556, "4": 556, "5": 556, "6": 556,
    "7": 556, "8": 556, "9": 556,
    ":": 278, ";": 278, "<": 584, "=": 584, ">": 584, "?": 556, "@": 1015,
    "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778,
    "H": 722, "I": 278, "J": 500, "K": 667, "L": 556, "M": 833, "N": 722,
    "O": 778, "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722,
    "V": 667, "W": 944, "X": 667, "Y": 667, "Z": 611,
    "[": 278, "\\": 278, "]": 278, "^": 469, "_": 556, "`": 333,
    "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556,
    "h": 556, "i": 222, "j": 222, "k": 500, "l": 222, "m": 833, "n": 556,
    "o": 556, "p": 556, "q": 556, "r": 333, "s": 500, "t": 278, "u": 556,
    "v": 500, "w": 722, "x": 500, "y": 500, "z": 500,
    "{": 334, "|": 260, "}": 334, "~": 584,
}


def _helvetica_width(text: str, fontsize: float) -> float:
    return sum(_HELVETICA_WIDTHS.get(c, 556) for c in text) * fontsize / 1000


def _page_number_label(index: int, total: int, fmt: str) -> str:
    if fmt == "x_of_y":
        return f"{index} / {total}"
    return f"Page {index} of {total}"


def stamp_preview_watermark(pdf_bytes: bytes, *, text: str = "PREVIEW") -> bytes:
    """Draw a diagonal PREVIEW across every page of a finished PDF.

    Stamped after rendering rather than injected as CSS, because the template
    HTML is author-supplied and CSS cannot win reliably against it. Two probes
    settled that: `html body div.sp-preview-stamp{display:none!important}`
    removes an injected mark outright — the cascade compares specificity before
    source order, so being injected last does not help — and the far more
    ordinary `body{transform:...}` makes body the containing block for fixed
    elements, which silently stops the mark repeating per page. An AI-built
    template can emit the second by accident.

    Filled at low alpha, not stroked. Outlines were the first attempt, because
    pikepdf's Canvas exposes no ExtGState and an opaque fill would blot out the
    content underneath — but a hollow word reads as bordered lettering laid over
    the design rather than a tint washed across it. The alpha is attached to the
    overlay page directly instead; see _PREVIEW_STAMP_ALPHA.
    """
    import pikepdf
    from pikepdf import Dictionary, Matrix, Name, Rectangle
    from pikepdf.canvas import Canvas, Color, Helvetica, Text

    word = text.encode("ascii")
    diagonal = math.sqrt(2) / 2
    pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    overlays = []
    try:
        for page in pdf.pages:
            box = page.mediabox
            width = float(box[2]) - float(box[0])
            height = float(box[3]) - float(box[1])
            # Rotated 45°, the word's footprint on each axis is
            # (text_width + font_size) * cos45. Solve that against the smaller
            # axis so landscape and portrait both fit without clipping.
            unit_width = _helvetica_width(word.decode(), 1.0)
            font_size = (min(width, height) * 0.92 / diagonal) / (unit_width + 1.0)
            text_width = _helvetica_width(word.decode(), font_size)

            canvas = Canvas(page_size=(width, height))
            canvas.add_font(Name.F1, Helvetica())
            canvas.do.push()
            # Baseline start, so the word's midpoint lands on the page centre.
            # The perpendicular nudge accounts for cap height sitting above the
            # baseline; without it the word rides high of true centre.
            cap = font_size * 0.72
            start_x = width / 2 - (text_width / 2) * diagonal + (cap / 2) * diagonal
            start_y = height / 2 - (text_width / 2) * diagonal - (cap / 2) * diagonal
            canvas.do.cm(Matrix(diagonal, diagonal, -diagonal, diagonal, start_x, start_y))
            # `gs` is the one operator Canvas has no method for, and the builder
            # underneath it is private — ContentStreamBuilder.extend is public
            # and takes raw bytes, but canvas.do._cs is the only way to reach it.
            # Placed outside BT/ET so it governs the text that follows. If a
            # pikepdf upgrade renames this, the stamp loses its transparency
            # rather than failing quietly: see the test that stamps over a black
            # square and checks the square survives.
            canvas.do._cs.extend(b"/GsPreview gs")
            canvas.do.fill_color(Color(*_PREVIEW_STAMP_GRAY, 1))
            canvas.do.draw_text(
                Text()
                .font(Name.F1, font_size)
                .render_mode(0)  # filled, not outlined — see docstring
                .show(word)
            )
            canvas.do.pop()
            overlay = canvas.to_pdf()
            # Named by the `gs` above. Canvas builds no ExtGState of its own, so
            # it goes on after the fact — add_overlay turns this page into a form
            # XObject, which carries its own resources across to the target.
            overlay.pages[0].Resources.ExtGState = Dictionary(
                GsPreview=Dictionary(Type=Name.ExtGState, ca=_PREVIEW_STAMP_ALPHA)
            )
            overlays.append(overlay)
            # Placed against the same box the canvas was sized from. Left to
            # itself add_overlay targets the trim box, which pikepdf resolves
            # through the crop box — so a template that sets either one would
            # have the stamp scaled to a rectangle it was never measured for.
            page.add_overlay(
                overlay.pages[0],
                Rectangle(float(box[0]), float(box[1]), float(box[2]), float(box[3])),
            )
        buf = io.BytesIO()
        pdf.save(buf)
        return buf.getvalue()
    finally:
        for overlay in overlays:
            overlay.close()
        pdf.close()


def stamp_pdf_page_numbers(pdf_bytes: bytes, *, position: str = "center", fmt: str = "page_x_of_y") -> bytes:
    """Replace footer numbering with global Page X of N labels.

    Row PDFs are rendered independently, so Chromium correctly numbers each
    standalone document but every one-page row says 1 of 1. Cover that small
    footer band after merging and stamp numbering across the combined PDF.
    """
    import pikepdf
    from pikepdf import Name
    from pikepdf.canvas import WHITE, Canvas, Color, Helvetica, Text

    pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    overlays = []
    try:
        total = len(pdf.pages)
        gray = Color(0.53, 0.53, 0.53, 1)
        font = Helvetica()
        # The widest label that can appear governs the mask so numbering never
        # peeks out from behind it as the page count grows.
        widest = _helvetica_width(_page_number_label(total, total, fmt), _STAMP_FONT_SIZE)
        for index, page in enumerate(pdf.pages, start=1):
            box = page.mediabox
            width = float(box[2]) - float(box[0])
            height = float(box[3]) - float(box[1])
            label = _page_number_label(index, total, fmt)
            label_width = _helvetica_width(label, _STAMP_FONT_SIZE)
            if position == "left":
                x = _FOOTER_INSET_PT
            elif position == "right":
                x = max(0, width - _FOOTER_INSET_PT - label_width)
            else:
                x = max(0, (width - label_width) / 2)
            canvas = Canvas(page_size=(width, height))
            canvas.add_font(Name.F1, font)
            # Chromium's footer text sits about 13–25pt above the page edge.
            # Mask only the band the old numbering occupied (sized from real
            # text metrics, with a few points of slack) so narrow document
            # margins and the caller-supplied watermark remain untouched.
            mask_x = _FOOTER_INSET_PT if position == "left" else (
                max(0, width - _FOOTER_INSET_PT - widest - 6) if position == "right" else max(0, (width - widest) / 2 - 3)
            )
            canvas.do.fill_color(WHITE).rect(mask_x, 12, min(widest + 6, width), 16, fill=True)
            canvas.do.fill_color(gray)
            canvas.do.draw_text(
                Text().font(Name.F1, _STAMP_FONT_SIZE).move_cursor(x, 17).show(label.encode("ascii"))
            )
            overlay = canvas.to_pdf()
            overlays.append(overlay)
            page.add_overlay(overlay.pages[0])
        buf = io.BytesIO()
        pdf.save(buf)
        return buf.getvalue()
    finally:
        for overlay in overlays:
            overlay.close()
        pdf.close()


def apply_pdf_metadata(pdf_bytes: bytes, title: str | None = None) -> bytes:
    """Stamp document info so downloads are titled in PDF viewers.

    Chromium leaves Title empty and advertises itself as the producer; final
    outputs should carry the document's name and ours instead.
    """
    import pikepdf

    from sheetrender import __version__

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
            if title:
                meta["dc:title"] = title
            meta["pdf:Producer"] = get_config().pdf_producer or f"sheetrender/{__version__}"
        buf = io.BytesIO()
        pdf.save(buf)
        return buf.getvalue()


def _pdf_version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _coalesce_identical_streams(pdf) -> int:
    import hashlib

    import pikepdf

    canonical = {}
    replacements = {}
    for obj in pdf.objects:
        if not isinstance(obj, pikepdf.Stream) or obj.objgen == (0, 0):
            continue
        digest = hashlib.sha256()
        raw = obj.read_raw_bytes()
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
        for key in sorted(obj.keys()):
            # /Length is regenerated from the encoded bytes when the PDF is saved.
            if key == "/Length":
                continue
            key_bytes = key.encode("utf-8")
            value = obj[key]
            value_bytes = value.unparse() if hasattr(value, "unparse") else repr(value).encode("utf-8")
            digest.update(len(key_bytes).to_bytes(4, "big"))
            digest.update(key_bytes)
            digest.update(len(value_bytes).to_bytes(8, "big"))
            digest.update(value_bytes)
        fingerprint = digest.digest()
        if fingerprint in canonical:
            replacements[obj.objgen] = canonical[fingerprint]
        else:
            canonical[fingerprint] = obj

    visited = set()
    replacement_count = 0
    stack = list(pdf.objects)
    while stack:
        obj = stack.pop()
        if not isinstance(obj, (pikepdf.Array, pikepdf.Dictionary, pikepdf.Stream)):
            continue
        if obj.objgen != (0, 0):
            if obj.objgen in visited:
                continue
            visited.add(obj.objgen)
        if isinstance(obj, pikepdf.Array):
            items = enumerate(list(obj))
        else:
            items = list(obj.items())
        for key, value in items:
            replacement = replacements.get(value.objgen) if isinstance(value, pikepdf.Stream) else None
            if replacement is not None:
                obj[key] = replacement
                value = replacement
                replacement_count += 1
            stack.append(value)
    return replacement_count


def merge_pdfs(
    paths: list[str],
    *,
    page_numbers: bool = False,
    page_number_position: str = "center",
    page_number_format: str = "page_x_of_y",
    title: str | None = None,
) -> bytes:
    # pikepdf (C++ qpdf) rather than pypdf: merging a 100-row batch took 40+
    # seconds in pure Python and dominated the job's finalize phase.
    import pikepdf

    merged = pikepdf.Pdf.new()
    sources = []
    try:
        for path in paths:
            src = pikepdf.open(path)
            sources.append(src)
            merged.pages.extend(src.pages)
        save_options = {}
        try:
            min_version = max(
                [merged.pdf_version, *(src.pdf_version for src in sources)],
                key=_pdf_version_key,
            )
            for _ in range(10):
                if _coalesce_identical_streams(merged) == 0:
                    break
            else:
                logger.warning("PDF stream deduplication did not converge after 10 passes")
            save_options["min_version"] = min_version
        except Exception:
            logger.warning("PDF merge optimization failed; using plain save", exc_info=True)
        buf = io.BytesIO()
        merged.save(buf, **save_options)
        result = buf.getvalue()
        if page_numbers:
            result = stamp_pdf_page_numbers(result, position=page_number_position, fmt=page_number_format)
        return apply_pdf_metadata(result, title)
    finally:
        for src in sources:
            src.close()
        merged.close()


def zip_files(files: list[tuple[str, str]]) -> bytes:
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, arcname in files:
            zf.write(path, arcname)
    return buf.getvalue()
