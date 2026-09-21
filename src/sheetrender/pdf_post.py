"""Post-processing of finished PDF bytes: stamping, merging, packaging."""

from __future__ import annotations

import io
import logging
import math

from sheetrender.config import get_config

logger = logging.getLogger(__name__)

# How the diagonal PREVIEW mark is tinted, in both the CSS and the PDF paths.
# Kept as one pair of numbers because the two have to look like the same mark:
# the same document previewed as HTML and as PDF should not appear to change.
# A wash, not lettering — low enough that the design underneath reads normally
# through it.
_PREVIEW_STAMP_GRAY = (0.47, 0.47, 0.47)
_PREVIEW_STAMP_ALPHA = 0.13

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


def _page_size(page) -> tuple[float, float]:
    """Width and height of a page, in points, taken from its mediabox."""
    box = page.mediabox
    return float(box[2]) - float(box[0]), float(box[3]) - float(box[1])


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

    if not text.isascii():
        # pikepdf's built-in Helvetica has no encoding for anything else, and
        # _HELVETICA_WIDTHS could not measure it.
        raise ValueError(f"preview stamp text must be ASCII, got {text!r}")
    word = text.encode("ascii")
    diagonal = math.sqrt(2) / 2
    pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    overlays = []
    try:
        for page in pdf.pages:
            box = page.mediabox
            width, height = _page_size(page)
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
            width, height = _page_size(page)
            label = _page_number_label(index, total, fmt)
            label_width = _helvetica_width(label, _STAMP_FONT_SIZE)
            # Chromium's footer text sits about 13–25pt above the page edge.
            # Mask only the band the old numbering occupied (sized from real
            # text metrics, with a few points of slack) so narrow document
            # margins and the caller-supplied watermark remain untouched.
            if position == "left":
                x = _FOOTER_INSET_PT
                mask_x = _FOOTER_INSET_PT
            elif position == "right":
                x = max(0, width - _FOOTER_INSET_PT - label_width)
                mask_x = max(0, width - _FOOTER_INSET_PT - widest - 6)
            else:
                x = max(0, (width - label_width) / 2)
                mask_x = max(0, (width - widest) / 2 - 3)
            canvas = Canvas(page_size=(width, height))
            canvas.add_font(Name.F1, font)
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


def _pdf_first_page_png(pdf_bytes: bytes) -> bytes:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        # 96/72 matches the CSS-pixel density previews use, so thumbnails line
        # up with the page's CSS-pixel dimensions.
        bitmap = doc[0].render(scale=96 / 72)
        image = bitmap.to_pil()
        buf = io.BytesIO()
        image.save(buf, "PNG")
        return buf.getvalue()
    finally:
        doc.close()
