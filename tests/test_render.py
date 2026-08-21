import asyncio
import io
import zipfile
from contextlib import asynccontextmanager as _acm

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class _MockGate:
    @_acm
    async def slot(self, high=False):
        yield


# Branding is caller-supplied now: the package injects whatever snippet it is
# handed rather than owning a "Made with SheetRender" footer of its own. The
# tests below assert on this stand-in the way they used to assert on the
# hard-coded one.
WATERMARK_HTML = (
    '<div style="position:fixed;bottom:6mm;right:6mm;font:10px sans-serif;'
    'color:#888">Made with SheetRender</div>'
)


@pytest.mark.asyncio
async def test_render_pdf():
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        pytest.skip("playwright not installed")

    try:
        from sheetrender import render as render_service

        await render_service.start_browser()
        pdf_bytes = await render_service.render_pdf("<html><body><h1>hi</h1></body></html>")
        assert pdf_bytes[:4] == b"%PDF"

        import os
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            path1 = f.name
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            path2 = f.name

        merged = render_service.merge_pdfs([path1, path2])
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(merged))
        assert len(reader.pages) == 2

        os.unlink(path1)
        os.unlink(path2)
        await render_service.stop_browser()
    except Exception as e:
        if "Executable doesn't exist" in str(e) or "chromium" in str(e).lower():
            pytest.skip(f"chromium not installed: {e}")
        raise


@pytest.mark.asyncio
async def test_preview_stamp_repeats_on_every_page():
    """The whole mechanism is Blink repeating position:fixed on each printed page.

    Real Chromium (backend/scripts/test.sh --render); the string tests above
    would happily pass on a stamp that only ever rendered on page one.
    """
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        pytest.skip("playwright not installed")

    from pypdf import PdfReader

    from sheetrender import render as render_service

    try:
        await render_service.start_browser()
        try:
            pdf_bytes = await render_service.render_pdf(
                "<html><body><div style='height:1400px'>A</div>"
                "<div style='height:1400px'>B</div></body></html>",
                preview=True,
            )
        finally:
            await render_service.stop_browser()
    except Exception as e:
        if "Executable doesn't exist" in str(e) or "chromium" in str(e).lower():
            pytest.skip(f"chromium not installed: {e}")
        raise

    pages = PdfReader(io.BytesIO(pdf_bytes)).pages
    assert len(pages) >= 2
    # Chromium's rotated text matrix can introduce stray spacing between glyphs.
    for number, page in enumerate(pages, start=1):
        assert "PREVIEW" in "".join(page.extract_text().split()), f"page {number}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "css"),
    [
        # Both of these defeated an earlier CSS-injected mark. The first is a
        # deliberate attack; the second is an ordinary GPU-acceleration idiom
        # that makes body the containing block for fixed elements, which an
        # AI-built template can emit without meaning anything by it.
        ("higher specificity", "html body div.sp-preview-stamp{display:none!important}"),
        ("transformed body", "body{transform:translateZ(0)}"),
        ("everything hidden", "div{display:none!important}"),
        ("nothing positioned", "*{position:static!important}"),
        ("opaque cover", "body::after{content:'';position:fixed;inset:0;background:#fff;z-index:2147483647}"),
    ],
)
async def test_template_css_cannot_remove_the_preview_stamp(name, css):
    """Real Chromium: the template gets to fight, and must lose.

    The stamp is painted onto the finished PDF, so no rule in the document can
    reach it. Run with backend/scripts/test.sh --render.
    """
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        pytest.skip("playwright not installed")

    from pypdf import PdfReader

    from sheetrender import render as render_service

    html = (
        f"<html><head><style>{css}</style></head><body>"
        "<div style='height:1400px'>A</div><div style='height:1400px'>B</div>"
        "</body></html>"
    )
    try:
        await render_service.start_browser()
        try:
            pdf_bytes = await render_service.render_pdf(
                html, {"page_size": "letter"}, preview=True
            )
        finally:
            await render_service.stop_browser()
    except Exception as e:
        if "Executable doesn't exist" in str(e) or "chromium" in str(e).lower():
            pytest.skip(f"chromium not installed: {e}")
        raise

    pages = PdfReader(io.BytesIO(pdf_bytes)).pages
    for number, page in enumerate(pages, start=1):
        assert "PREVIEW" in "".join(page.extract_text().split()), f"{name}, page {number}"


@pytest.mark.asyncio
async def test_render_pdf_default_settings():
    """render_pdf with no page_settings uses A4/portrait/15mm defaults."""
    from sheetrender import render as render_service

    mock_browser = MagicMock()
    mock_gate = _MockGate()
    mock_context = AsyncMock()
    mock_page = AsyncMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_page.pdf = AsyncMock(return_value=b"%PDF-fake")

    with (
        patch.object(render_service, "_browser", mock_browser),
        patch.object(render_service, "_gate", mock_gate),
        patch.object(render_service, "_render_count", 0),
        patch.object(render_service, "_browser_launch_time", None),
    ):
        result = await render_service.render_pdf("<html/>")

    assert result == b"%PDF-fake"
    mock_page.pdf.assert_called_once()
    call_kwargs = mock_page.pdf.call_args.kwargs
    assert call_kwargs["format"] == "A4"
    assert call_kwargs["landscape"] is False
    assert call_kwargs["margin"]["top"] == "15mm"


@pytest.mark.asyncio
async def test_render_pdf_landscape_letter():
    """render_pdf with letter/landscape settings uses correct Playwright args."""
    from sheetrender import render as render_service

    mock_browser = MagicMock()
    mock_gate = _MockGate()
    mock_context = AsyncMock()
    mock_page = AsyncMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_page.pdf = AsyncMock(return_value=b"%PDF-fake")

    settings = {
        "page_size": "letter",
        "orientation": "landscape",
        "margins": {"top": 8, "right": 8, "bottom": 8, "left": 8},
    }
    with (
        patch.object(render_service, "_browser", mock_browser),
        patch.object(render_service, "_gate", mock_gate),
        patch.object(render_service, "_render_count", 0),
        patch.object(render_service, "_browser_launch_time", None),
    ):
        result = await render_service.render_pdf("<html/>", settings)

    assert result == b"%PDF-fake"
    call_kwargs = mock_page.pdf.call_args.kwargs
    assert call_kwargs["format"] == "Letter"
    assert call_kwargs["landscape"] is True
    assert call_kwargs["margin"]["top"] == "8mm"


@pytest.mark.asyncio
async def test_render_pdf_page_numbers_on():
    """render_pdf enables Playwright header/footer templates for page numbers."""
    from sheetrender import render as render_service

    mock_browser = MagicMock()
    mock_gate = _MockGate()
    mock_context = AsyncMock()
    mock_page = AsyncMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_page.pdf = AsyncMock(return_value=b"%PDF-fake")

    settings = {"page_numbers": True}
    with (
        patch.object(render_service, "_browser", mock_browser),
        patch.object(render_service, "_gate", mock_gate),
        patch.object(render_service, "_render_count", 0),
        patch.object(render_service, "_browser_launch_time", None),
    ):
        result = await render_service.render_pdf("<html/>", settings)

    assert result == b"%PDF-fake"
    call_kwargs = mock_page.pdf.call_args.kwargs
    assert call_kwargs["display_header_footer"] is True
    assert call_kwargs["header_template"] == "<span></span>"
    assert "pageNumber" in call_kwargs["footer_template"]
    assert "totalPages" in call_kwargs["footer_template"]


@pytest.mark.asyncio
async def test_render_pdf_page_numbers_off():
    """render_pdf omits Playwright header/footer options by default."""
    from sheetrender import render as render_service

    mock_browser = MagicMock()
    mock_gate = _MockGate()
    mock_context = AsyncMock()
    mock_page = AsyncMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_page.pdf = AsyncMock(return_value=b"%PDF-fake")

    settings = {}
    with (
        patch.object(render_service, "_browser", mock_browser),
        patch.object(render_service, "_gate", mock_gate),
        patch.object(render_service, "_render_count", 0),
        patch.object(render_service, "_browser_launch_time", None),
    ):
        result = await render_service.render_pdf("<html/>", settings)

    assert result == b"%PDF-fake"
    call_kwargs = mock_page.pdf.call_args.kwargs
    assert "display_header_footer" not in call_kwargs


def test_inject_watermark_before_body_close():
    from sheetrender.render import inject_watermark
    html = "<html><body><p>Hello</p></body></html>"
    result = inject_watermark(html, WATERMARK_HTML)
    assert "Made with SheetRender" in result
    assert result.index("Made with SheetRender") < result.lower().index("</body>")


def test_stamp_pdf_page_numbers_are_global():
    import pikepdf
    from pypdf import PdfReader
    from sheetrender.render import stamp_pdf_page_numbers

    source = pikepdf.Pdf.new()
    for _ in range(3):
        source.add_blank_page(page_size=(612, 792))
    raw = io.BytesIO()
    source.save(raw)
    source.close()

    numbered = stamp_pdf_page_numbers(raw.getvalue())
    pages = PdfReader(io.BytesIO(numbered)).pages
    assert [page.extract_text().strip() for page in pages] == [
        "Page 1 of 3",
        "Page 2 of 3",
        "Page 3 of 3",
    ]


def _write_pdf_with_image(path, image_bytes, *, min_version=None):
    import pikepdf
    import zlib

    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(72, 72))
    image = pdf.make_stream(
        zlib.compress(image_bytes),
        pikepdf.Dictionary(
            Type=pikepdf.Name.XObject,
            Subtype=pikepdf.Name.Image,
            Filter=pikepdf.Name.FlateDecode,
            Width=1,
            Height=1,
            ColorSpace=pikepdf.Name.DeviceGray,
            BitsPerComponent=8,
        ),
    )
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=image))
    page.Contents = pdf.make_stream(b"q 72 0 0 72 0 0 cm /Im0 Do Q")
    save_options = {"min_version": min_version} if min_version else {}
    pdf.save(path, **save_options)
    pdf.close()


def _write_pdf_with_skia_image(path, image_bytes):
    import pikepdf
    import zlib

    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(72, 72))
    icc = pdf.make_stream(
        zlib.compress(b"shared-icc-profile"),
        pikepdf.Dictionary(Filter=pikepdf.Name.FlateDecode, N=3),
    )
    mask = pdf.make_stream(
        zlib.compress(b"shared-alpha-mask"),
        pikepdf.Dictionary(
            Type=pikepdf.Name.XObject,
            Subtype=pikepdf.Name.Image,
            Filter=pikepdf.Name.FlateDecode,
            Width=1,
            Height=1,
            ColorSpace=pikepdf.Name.DeviceGray,
            BitsPerComponent=8,
        ),
    )
    image = pdf.make_stream(
        zlib.compress(image_bytes),
        pikepdf.Dictionary(
            Type=pikepdf.Name.XObject,
            Subtype=pikepdf.Name.Image,
            Filter=pikepdf.Name.FlateDecode,
            Width=1,
            Height=1,
            ColorSpace=pikepdf.Array([pikepdf.Name.ICCBased, icc]),
            BitsPerComponent=8,
            SMask=mask,
        ),
    )
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=image))
    page.Contents = pdf.make_stream(b"q 72 0 0 72 0 0 cm /Im0 Do Q")
    pdf.save(path)
    pdf.close()


def test_merge_pdfs_preserves_highest_source_version(tmp_path):
    import pikepdf

    from sheetrender.render import merge_pdfs

    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    _write_pdf_with_image(first, b"first")
    _write_pdf_with_image(second, b"second", min_version="1.5")

    with pikepdf.open(io.BytesIO(merge_pdfs([str(first), str(second)]))) as merged:
        assert tuple(map(int, merged.pdf_version.split("."))) >= (1, 5)


def test_merge_pdfs_falls_back_when_optimization_fails(tmp_path, caplog):
    import pikepdf

    from sheetrender import render as render_service

    source = tmp_path / "source.pdf"
    _write_pdf_with_image(source, b"image")

    with patch.object(render_service, "_pdf_version_key", side_effect=ValueError("bad version")):
        result = render_service.merge_pdfs([str(source)])

    with pikepdf.open(io.BytesIO(result)) as merged:
        assert len(merged.pages) == 1
    assert "PDF merge optimization failed; using plain save" in caplog.text


def test_merge_pdfs_deduplicates_identical_streams(tmp_path):
    import pikepdf
    import zlib

    from sheetrender.render import merge_pdfs

    image_bytes = b"same-image-stream"
    raw_image_bytes = zlib.compress(image_bytes)
    paths = []
    for index in range(3):
        path = tmp_path / f"source-{index}.pdf"
        _write_pdf_with_image(path, image_bytes)
        paths.append(str(path))

    with pikepdf.open(io.BytesIO(merge_pdfs(paths))) as merged:
        copies = [
            obj for obj in merged.objects
            if isinstance(obj, pikepdf.Stream) and obj.read_raw_bytes() == raw_image_bytes
        ]
        assert len(copies) == 1
        assert len(merged.pages) == 3
        images = [page.Resources.XObject.Im0 for page in merged.pages]
        assert len({image.objgen for image in images}) == 1
        assert all(image.read_bytes() == image_bytes for image in images)


def test_merge_pdfs_deduplicates_skia_images_with_indirect_children(tmp_path):
    import pikepdf
    import zlib

    from sheetrender.render import merge_pdfs

    image_bytes = b"large-shared-skia-image" * 100
    raw_image_bytes = zlib.compress(image_bytes)
    paths = []
    for index in range(3):
        path = tmp_path / f"source-{index}.pdf"
        _write_pdf_with_skia_image(path, image_bytes)
        paths.append(str(path))

    with pikepdf.open(io.BytesIO(merge_pdfs(paths))) as merged:
        copies = [
            obj for obj in merged.objects
            if isinstance(obj, pikepdf.Stream) and obj.read_raw_bytes() == raw_image_bytes
        ]
        assert len(copies) == 1


def test_merge_pdfs_keeps_distinct_streams(tmp_path):
    import pikepdf
    import zlib

    from sheetrender.render import merge_pdfs

    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    image_bytes = (b"first-image-stream", b"second-image-stream")
    _write_pdf_with_image(first, image_bytes[0])
    _write_pdf_with_image(second, image_bytes[1])

    with pikepdf.open(io.BytesIO(merge_pdfs([str(first), str(second)]))) as merged:
        raw_streams = {
            obj.read_raw_bytes() for obj in merged.objects
            if isinstance(obj, pikepdf.Stream)
        }
        assert {zlib.compress(data) for data in image_bytes} <= raw_streams


def test_merge_pdfs_keeps_same_bytes_with_different_stream_dictionaries(tmp_path):
    import pikepdf
    import zlib

    from sheetrender.render import merge_pdfs

    image_bytes = b"same-image-stream"
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    _write_pdf_with_image(first, image_bytes)
    _write_pdf_with_image(second, image_bytes)
    with pikepdf.open(second, allow_overwriting_input=True) as pdf:
        pdf.pages[0].Resources.XObject.Im0.BitsPerComponent = 4
        pdf.save(second)

    with pikepdf.open(io.BytesIO(merge_pdfs([str(first), str(second)]))) as merged:
        raw_image_bytes = zlib.compress(image_bytes)
        copies = [
            obj for obj in merged.objects
            if isinstance(obj, pikepdf.Stream) and obj.read_raw_bytes() == raw_image_bytes
        ]
        assert len(copies) == 2


def test_merge_pdfs_page_numbers_and_title_still_work(tmp_path):
    import pikepdf
    from pypdf import PdfReader

    from sheetrender.render import merge_pdfs

    paths = []
    for index in range(2):
        path = tmp_path / f"source-{index}.pdf"
        _write_pdf_with_image(path, f"image-{index}".encode(), min_version="1.5")
        paths.append(str(path))

    result = merge_pdfs(paths, page_numbers=True, title="X")
    reader = PdfReader(io.BytesIO(result))
    assert len(reader.pages) == 2
    assert reader.metadata.title == "X"
    with pikepdf.open(io.BytesIO(result)) as merged:
        assert tuple(map(int, merged.pdf_version.split("."))) >= (1, 5)


def test_zip_keeps_each_individual_pdf_unchanged(tmp_path):
    from sheetrender.render import zip_files

    first = tmp_path / "row_0.pdf"
    second = tmp_path / "row_1.pdf"
    first.write_bytes(b"first-numbered-pdf")
    second.write_bytes(b"second-numbered-pdf")

    archive = zipfile.ZipFile(
        io.BytesIO(zip_files([(str(first), "row_0.pdf"), (str(second), "row_1.pdf")]))
    )
    assert archive.read("row_0.pdf") == b"first-numbered-pdf"
    assert archive.read("row_1.pdf") == b"second-numbered-pdf"


def test_inject_watermark_no_body_tag():
    from sheetrender.render import inject_watermark
    html = "<p>Hello</p>"
    result = inject_watermark(html, WATERMARK_HTML)
    assert "Made with SheetRender" in result


def test_inject_preview_watermark_before_body_close():
    from sheetrender.render import inject_preview_watermark
    html = "<html><body><p>Hello</p></body></html>"
    result = inject_preview_watermark(html)
    assert "sp-preview-stamp" in result
    assert ">PREVIEW<" in result
    assert result.index("sp-preview-stamp") < result.lower().index("</body>")


def test_inject_preview_watermark_no_body_tag():
    from sheetrender.render import inject_preview_watermark
    html = "<p>Hello</p>"
    result = inject_preview_watermark(html)
    assert ">PREVIEW<" in result


def test_preview_watermark_has_no_letter_spacing():
    """Per-glyph offsets would surface as "P R E V I E W" in extracted text."""
    from sheetrender.render import _preview_watermark_html
    assert "letter-spacing" not in _preview_watermark_html()


def test_the_width_table_covers_every_letter_the_stamp_draws():
    """A missing glyph is not an error here — it is a silently wrong number.

    _helvetica_width defaults an unknown character to 556, so the table can be
    incomplete for a caller's string and still return a plausible answer. It
    was: built for "Page X of Y", it held P but not R/E/V/I/W, so "PREVIEW"
    measured 13% narrow and the stamp sized and centred itself against that.
    """
    from sheetrender.render import _HELVETICA_WIDTHS

    missing = sorted(set("PREVIEW") - set(_HELVETICA_WIDTHS))
    assert missing == [], f"stamp draws glyphs the width table lacks: {missing}"
    # The five that were absent, at their real AFM advances.
    assert [_HELVETICA_WIDTHS[c] for c in "REVIW"] == [722, 667, 667, 278, 944]


@pytest.mark.parametrize(
    ("name", "page_size"),
    [("a4 portrait", (595, 842)), ("letter", (612, 792)), ("a4 landscape", (842, 595))],
)
def test_the_preview_stamp_lands_inside_the_page(name, page_size):
    """Rasterise and look at the ink, because extracted text cannot see this.

    Both existing stamp tests read the word back with pypdf, which walks the
    content stream — it reports "PREVIEW" whether the glyphs land on the sheet
    or half a inch past its edge. Only the pixels show a clipped mark.
    """
    import pikepdf
    import pypdfium2 as pdfium

    from sheetrender.render import stamp_preview_watermark

    blank = pikepdf.new()
    blank.add_blank_page(page_size=page_size)
    buf = io.BytesIO()
    blank.save(buf)

    doc = pdfium.PdfDocument(stamp_preview_watermark(buf.getvalue()))
    try:
        image = doc[0].render(scale=1.0).to_pil().convert("L")
    finally:
        doc.close()

    width, height = image.size
    pixels = image.load()
    inked = [
        (x, y, pixels[x, y])
        for y in range(height)
        for x in range(width)
        if pixels[x, y] < 252
    ]
    assert inked, f"{name}: the stamp drew nothing"

    xs = [x for x, _, _ in inked]
    ys = [y for _, y, _ in inked]
    assert min(xs) > 0 and max(xs) < width - 1, f"{name}: clipped on the left/right edge"
    assert min(ys) > 0 and max(ys) < height - 1, f"{name}: clipped on the top/bottom edge"
    # A soft wash, not lettering: filled at low alpha, so every inked pixel sits
    # in the same narrow band near white. Outlined glyphs would put dark strokes
    # against white interiors and blow the spread wide open.
    darkest = min(value for _, _, value in inked)
    assert 200 < darkest, f"{name}: too heavy — darkest pixel is {darkest}"
    assert max(v for _, _, v in inked) - darkest < 25, f"{name}: not a flat fill"
    # Centred, within a tolerance that covers Helvetica's asymmetric side
    # bearings but not a mis-measured word.
    assert abs((min(xs) + max(xs)) / 2 - width / 2) < width * 0.03, f"{name}: off-centre"
    assert abs((min(ys) + max(ys)) / 2 - height / 2) < height * 0.03, f"{name}: off-centre"


def test_the_stamp_does_not_obscure_what_it_is_stamped_over():
    """The reason it is filled at low alpha rather than filled outright.

    An opaque fill of this size would white out a third of the document. The
    transparency is what makes a filled mark usable at all, and it lives in an
    ExtGState that the Canvas API does not build for us — so it is worth a test
    that would notice the alpha silently going missing.
    """
    import pikepdf
    import pypdfium2 as pdfium

    from sheetrender.render import stamp_preview_watermark

    page = pikepdf.new()
    page.add_blank_page(page_size=(300, 300))
    # A black square over the middle third, right under where the word crosses.
    page.pages[0].Contents = page.make_stream(b"0 0 0 rg 100 100 100 100 re f")
    buf = io.BytesIO()
    page.save(buf)

    doc = pdfium.PdfDocument(stamp_preview_watermark(buf.getvalue()))
    try:
        image = doc[0].render(scale=1.0).to_pil().convert("L")
    finally:
        doc.close()

    pixels = image.load()
    square = [pixels[x, y] for y in range(110, 190) for x in range(110, 190)]
    assert max(square) < 60, f"the stamp washed out the content beneath it: {max(square)}"


def test_preview_and_branding_watermarks_are_independent():
    """Two separate marks: plan-gated branding, unconditional preview stamp."""
    from sheetrender.render import inject_preview_watermark, inject_watermark

    html = "<html><body><p>Hello</p></body></html>"
    assert "sp-preview-stamp" not in inject_watermark(html, WATERMARK_HTML)
    assert "Made with SheetRender" not in inject_preview_watermark(html)

    both = inject_preview_watermark(inject_watermark(html, WATERMARK_HTML))
    assert "Made with SheetRender" in both
    assert "sp-preview-stamp" in both
    assert both.index("sp-preview-stamp") < both.lower().index("</body>")


def test_batch_renderer_cannot_stamp_previews():
    """Everything a batch renders is a deliverable — no preview flag to misuse."""
    import inspect

    from sheetrender.render import BatchRenderer

    assert "preview" not in inspect.signature(BatchRenderer.render_pdf).parameters


@pytest.mark.asyncio
@pytest.mark.parametrize("preview", [True, False])
async def test_render_pdf_stamps_the_preview_mark_after_rendering(preview):
    """The PDF path must stamp, never inject.

    Injected CSS cannot be relied on here: the template is author-supplied, and
    a rule of higher specificity than ours wins no matter that we inject last.
    Nothing PREVIEW-ish may reach the page HTML, and the finished bytes must go
    through the stamper exactly when preview is asked for.
    """
    from sheetrender import render as render_service

    mock_browser = MagicMock()
    mock_context = AsyncMock()
    mock_page = AsyncMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_page.pdf = AsyncMock(return_value=b"%PDF-fake")
    stamper = MagicMock(return_value=b"%PDF-stamped")

    with (
        patch.object(render_service, "_browser", mock_browser),
        patch.object(render_service, "_gate", _MockGate()),
        patch.object(render_service, "_render_count", 0),
        patch.object(render_service, "_browser_launch_time", None),
        patch.object(render_service, "stamp_preview_watermark", stamper),
    ):
        result = await render_service.render_pdf(
            "<html><body><p>Hi</p></body></html>", preview=preview
        )

    served = mock_page.set_content.call_args[0][0]
    assert "sp-preview-stamp" not in served
    assert "PREVIEW" not in served
    assert stamper.called is preview
    assert result == (b"%PDF-stamped" if preview else b"%PDF-fake")


@pytest.mark.asyncio
async def test_a_batch_render_is_never_stamped():
    """BatchRenderer output is a deliverable. Nothing may mark it."""
    from sheetrender import render as render_service

    mock_page = AsyncMock()
    mock_page.pdf = AsyncMock(return_value=b"%PDF-fake")
    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_browser = MagicMock(new_context=AsyncMock(return_value=mock_context))
    stamper = MagicMock(return_value=b"%PDF-stamped")

    with (
        patch.object(render_service, "_browser", mock_browser),
        patch.object(render_service, "_gate", _MockGate()),
        patch.object(render_service, "_render_count", 0),
        patch.object(render_service, "_browser_launch_time", None),
        patch.object(render_service, "stamp_preview_watermark", stamper),
    ):
        renderer = render_service.BatchRenderer()
        result = await renderer.render_pdf("<html><body><p>Hi</p></body></html>")

    assert stamper.called is False
    assert result == b"%PDF-fake"


@pytest.mark.asyncio
@pytest.mark.parametrize("renderer", ["module", "batch"])
async def test_watermark_html_is_injected_only_when_supplied(renderer):
    """Branding is a caller-supplied snippet, not a flag the package owns.

    It has to be injected *after* sanitization — the snippet is trusted, and
    nh3 would strip the fixed-position styling it needs.
    """
    from sheetrender import render as render_service

    mock_page = AsyncMock()
    mock_page.pdf = AsyncMock(return_value=b"%PDF-fake")
    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_browser = MagicMock(new_context=AsyncMock(return_value=mock_context))

    async def _render(**kwargs):
        with (
            patch.object(render_service, "_browser", mock_browser),
            patch.object(render_service, "_gate", _MockGate()),
            patch.object(render_service, "_render_count", 0),
            patch.object(render_service, "_browser_launch_time", None),
        ):
            if renderer == "module":
                await render_service.render_pdf("<html><body><p>Hi</p></body></html>", **kwargs)
            else:
                await render_service.BatchRenderer().render_pdf(
                    "<html><body><p>Hi</p></body></html>", **kwargs
                )
        return mock_page.set_content.call_args[0][0]

    plain = await _render()
    assert "Made with SheetRender" not in plain

    branded = await _render(watermark_html=WATERMARK_HTML)
    # Injected verbatim and last: sanitization runs first (and, working on a
    # fragment, has already dropped the </body> the standalone injector aims
    # for), so the snippet keeps the fixed-position styling nh3 would strip.
    assert branded.endswith(WATERMARK_HTML)
    assert branded.index("<p>Hi</p>") < branded.index("Made with SheetRender")


def test_stamp_preview_watermark_stamps_the_word_it_is_given():
    """The stamp's text is a parameter now; "PREVIEW" is only its default."""
    import pikepdf
    from pypdf import PdfReader

    from sheetrender.render import stamp_preview_watermark

    blank = pikepdf.new()
    blank.add_blank_page(page_size=(612, 792))
    buf = io.BytesIO()
    blank.save(buf)

    default = PdfReader(io.BytesIO(stamp_preview_watermark(buf.getvalue()))).pages[0]
    assert "PREVIEW" in "".join(default.extract_text().split())

    drafted = PdfReader(
        io.BytesIO(stamp_preview_watermark(buf.getvalue(), text="DRAFT"))
    ).pages[0]
    extracted = "".join(drafted.extract_text().split())
    assert "DRAFT" in extracted
    assert "PREVIEW" not in extracted


def test_inject_preview_watermark_stamps_the_word_it_is_given():
    from sheetrender.render import inject_preview_watermark

    result = inject_preview_watermark("<html><body><p>Hi</p></body></html>", text="DRAFT")
    assert ">DRAFT<" in result
    assert ">PREVIEW<" not in result


def test_strip_author_page_rules_removes_page_block():
    from sheetrender.render import strip_author_page_rules
    html = "<style>@page { size: A4; margin: 0 } body { color: red }</style><p>Hi</p>"
    result = strip_author_page_rules(html)
    assert "@page" not in result
    assert "margin: 0" not in result
    # Non-@page CSS and content are untouched.
    assert "body { color: red }" in result
    assert "<p>Hi</p>" in result


def test_strip_author_page_rules_handles_nested_margin_boxes():
    from sheetrender.render import strip_author_page_rules
    html = "<style>a{}@page{ margin: 1cm; @top-center { content: 'x' } }b{color:blue}</style>"
    result = strip_author_page_rules(html)
    assert "@page" not in result
    assert "@top-center" not in result
    assert "a{}" in result
    assert "b{color:blue}" in result


def test_strip_author_page_rules_leaves_body_text_alone():
    from sheetrender.render import strip_author_page_rules
    html = "<style>@page { margin: 0 }</style><p>Set the @page rule { like this }</p>"
    result = strip_author_page_rules(html)
    assert "@page { margin: 0 }" not in result
    assert "Set the @page rule { like this }" in result


def test_strip_author_page_rules_noop_without_page_rule():
    from sheetrender.render import strip_author_page_rules
    html = "<style>body { margin: 0 }</style>"
    assert strip_author_page_rules(html) == html


@pytest.mark.asyncio
async def test_priority_gate_high_before_low():
    """With capacity saturated, freed slot goes to high-priority waiter before low."""
    from sheetrender.render import PriorityGate

    gate = PriorityGate(1)
    await gate.acquire(high=False)
    assert gate._used == 1

    order = []
    low_acquired = asyncio.Event()
    high_acquired = asyncio.Event()
    low_release = asyncio.Event()
    high_release = asyncio.Event()

    async def low_waiter():
        await gate.acquire(high=False)
        order.append("low")
        low_acquired.set()
        await low_release.wait()
        gate.release()

    async def high_waiter():
        await gate.acquire(high=True)
        order.append("high")
        high_acquired.set()
        await high_release.wait()
        gate.release()

    low_task = asyncio.create_task(low_waiter())
    high_task = asyncio.create_task(high_waiter())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    gate.release()
    await asyncio.wait_for(high_acquired.wait(), timeout=1)

    assert order == ["high"]
    assert not low_acquired.is_set()

    high_release.set()
    await asyncio.wait_for(low_acquired.wait(), timeout=1)
    low_release.set()
    await asyncio.gather(low_task, high_task)
    assert order == ["high", "low"]


@pytest.mark.asyncio
async def test_priority_gate_basic_accounting():
    """acquire/release accounting and capacity enforcement."""
    from sheetrender.render import PriorityGate

    gate = PriorityGate(3)
    await gate.acquire()
    await gate.acquire()
    await gate.acquire()
    assert gate._used == 3

    acquired = asyncio.Event()

    async def try_acquire():
        await gate.acquire()
        acquired.set()
        gate.release()

    task = asyncio.create_task(try_acquire())
    await asyncio.sleep(0)
    assert not acquired.is_set()

    gate.release()
    await asyncio.wait_for(acquired.wait(), timeout=1)
    await task
    assert gate._used == 2
    gate.release()
    gate.release()
    assert gate._used == 0


@pytest.mark.asyncio
async def test_priority_gate_drain_excludes_new_acquires():
    """Drain (acquire all slots) blocks new acquires until released."""
    from sheetrender.render import PriorityGate

    capacity = 2
    gate = PriorityGate(capacity)

    for _ in range(capacity):
        await gate.acquire(high=True)
    assert gate._used == capacity

    acquired = asyncio.Event()

    async def try_low():
        await gate.acquire(high=False)
        acquired.set()
        gate.release()

    task = asyncio.create_task(try_low())
    await asyncio.sleep(0)
    assert not acquired.is_set()

    for _ in range(capacity):
        gate.release()

    await asyncio.gather(task)
    assert acquired.is_set()
    assert gate._used == 0


def test_should_recycle_check_by_count():
    from sheetrender.render import _should_recycle_check
    import time

    now = time.monotonic()
    assert _should_recycle_check(300, now, 300, 30) is True
    assert _should_recycle_check(299, now, 300, 30) is False


def test_should_recycle_check_by_age():
    from sheetrender.render import _should_recycle_check
    import time

    old_time = time.monotonic() - 31 * 60
    assert _should_recycle_check(0, old_time, 300, 30) is True
    recent = time.monotonic() - 29 * 60
    assert _should_recycle_check(0, recent, 300, 30) is False


def test_should_recycle_check_no_launch_time():
    from sheetrender.render import _should_recycle_check

    assert _should_recycle_check(0, None, 300, 30) is False
