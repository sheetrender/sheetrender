"""RenderConfig plumbing.

The backend read these knobs off its own Settings object; the package has one
process-global RenderConfig instead. These tests follow each field from
configure() to the place it actually changes behavior — a config value that
nothing reads is the failure mode worth guarding against, and it is invisible
to every other test in this suite.

conftest.py restores the previous config after each test, since configure()
would otherwise leak across the whole session.
"""
from __future__ import annotations

import io
from contextlib import asynccontextmanager as _acm
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sheetrender import __version__
from sheetrender.config import DEFAULT_EGRESS_HOSTS, RenderConfig, configure, get_config
from sheetrender.html_sanitize import sanitize_render_html

CUSTOM_HOST = "cdn.example.com"


class _MockGate:
    @_acm
    async def slot(self, high=False):
        yield


class _FakeRoute:
    def __init__(self, url):
        self.request = MagicMock(url=url)
        self.action = None

    async def continue_(self):
        self.action = "continue"

    async def abort(self):
        self.action = "abort"


def _blank_pdf() -> bytes:
    import pikepdf

    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def _producer(pdf_bytes: bytes) -> str:
    import pikepdf

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf, pdf.open_metadata() as meta:
        return str(meta["pdf:Producer"])


def test_get_config_returns_defaults_when_nothing_was_configured():
    config = get_config()
    assert config.concurrency == 12
    assert config.recycle_max_renders == 300
    assert config.recycle_max_age_minutes == 30
    assert config.timing_logs is False
    assert config.allowed_egress_hosts == DEFAULT_EGRESS_HOSTS
    assert config.pdf_producer is None


def test_configure_replaces_the_process_global_config():
    configure(RenderConfig(concurrency=3, timing_logs=True))

    assert get_config().concurrency == 3
    assert get_config().timing_logs is True
    # Unset fields fall back to the dataclass defaults rather than to whatever
    # the previous configure() call left behind.
    assert get_config().recycle_max_renders == 300


def test_apply_pdf_metadata_producer_names_the_package_and_its_version():
    from sheetrender.render import apply_pdf_metadata

    producer = _producer(apply_pdf_metadata(_blank_pdf(), title="X"))

    assert producer.startswith("sheetrender/")
    assert producer == f"sheetrender/{__version__}"


def test_apply_pdf_metadata_honors_a_configured_producer():
    """Downstreams put their own name on the file they hand to a customer."""
    from sheetrender.render import apply_pdf_metadata

    configure(RenderConfig(pdf_producer="Acme Reports 2.0"))

    assert _producer(apply_pdf_metadata(_blank_pdf())) == "Acme Reports 2.0"


def test_apply_pdf_metadata_still_sets_the_title_with_a_custom_producer():
    import pikepdf

    from sheetrender.render import apply_pdf_metadata

    configure(RenderConfig(pdf_producer="Acme Reports 2.0"))
    result = apply_pdf_metadata(_blank_pdf(), title="Invoice INV-1")

    with pikepdf.open(io.BytesIO(result)) as pdf, pdf.open_metadata() as meta:
        assert str(meta["dc:title"]) == "Invoice INV-1"


def test_sanitize_render_html_accepts_a_custom_egress_allowlist():
    """The argument replaces the default list rather than extending it."""
    html = (
        f'<img src="https://{CUSTOM_HOST}/logo.png">'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">'
        '<img src="https://evil.example/track.gif">'
    )

    out = sanitize_render_html(html, allowed_egress_hosts=frozenset({CUSTOM_HOST}))

    assert CUSTOM_HOST in out
    assert "fonts.googleapis.com" not in out
    assert "evil.example" not in out


def test_a_custom_allowlist_can_keep_the_google_fonts_default():
    html = (
        f'<img src="https://{CUSTOM_HOST}/logo.png">'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">'
        '<img src="https://evil.example/track.gif">'
    )

    out = sanitize_render_html(
        html, allowed_egress_hosts=DEFAULT_EGRESS_HOSTS | {CUSTOM_HOST}
    )

    assert CUSTOM_HOST in out
    assert "fonts.googleapis.com" in out
    assert "evil.example" not in out


@pytest.mark.asyncio
async def test_configured_allowlist_reaches_the_sanitizer_through_render_pdf():
    """The knob is only worth having if the render path reads it.

    render_pdf sanitizes before serving, so what Chromium is handed is the
    proof: with the host allowed the tag survives, and with the default config
    the same markup loses it.
    """
    from sheetrender import browser
    from sheetrender import render as render_service

    html = (
        f'<img src="https://{CUSTOM_HOST}/logo.png">'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">'
        '<img src="https://evil.example/track.gif">'
    )

    async def _served() -> str:
        mock_page = AsyncMock()
        mock_page.pdf = AsyncMock(return_value=b"%PDF-fake")
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_browser = MagicMock(new_context=AsyncMock(return_value=mock_context))

        with (
            patch.object(browser._state, "browser", mock_browser),
            patch.object(browser._state, "gate", _MockGate()),
            patch.object(browser._state, "render_count", 0),
            patch.object(browser._state, "launch_time", None),
        ):
            await render_service.render_pdf(html)
        return mock_page.set_content.call_args[0][0]

    default = await _served()
    assert CUSTOM_HOST not in default
    assert "fonts.googleapis.com" in default

    configure(RenderConfig(allowed_egress_hosts=DEFAULT_EGRESS_HOSTS | {CUSTOM_HOST}))
    widened = await _served()
    assert CUSTOM_HOST in widened
    assert "fonts.googleapis.com" in widened
    assert "evil.example" not in widened


@pytest.mark.asyncio
async def test_configured_allowlist_reaches_the_egress_guard():
    """Sanitization is the first layer; the route guard is the one that holds.

    CSS url()/@import fetches never pass through nh3, so the two allowlists
    have to be the same allowlist.
    """
    from sheetrender.browser import _egress_guard

    allowed = _FakeRoute(f"https://{CUSTOM_HOST}/logo.png")
    await _egress_guard(allowed)
    assert allowed.action == "abort"

    configure(RenderConfig(allowed_egress_hosts=DEFAULT_EGRESS_HOSTS | {CUSTOM_HOST}))

    allowed = _FakeRoute(f"https://{CUSTOM_HOST}/logo.png")
    await _egress_guard(allowed)
    assert allowed.action == "continue"

    fonts = _FakeRoute("https://fonts.gstatic.com/s/inter/v1.woff2")
    await _egress_guard(fonts)
    assert fonts.action == "continue"

    blocked = _FakeRoute("https://evil.example/beacon")
    await _egress_guard(blocked)
    assert blocked.action == "abort"
