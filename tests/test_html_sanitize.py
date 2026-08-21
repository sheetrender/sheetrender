from sheetrender.html_sanitize import ALLOWED_EGRESS_HOSTS, sanitize_render_html
from sheetrender.render import _egress_guard


class TestSanitizeRenderHtml:
    def test_strips_script_including_content(self):
        out = sanitize_render_html('<p>hi</p><script>fetch("https://evil.example/x")</script>')
        assert "script" not in out
        assert "evil.example" not in out
        assert "fetch" not in out

    def test_strips_iframe_and_object(self):
        out = sanitize_render_html('<iframe src="https://evil.example"></iframe><object data="x"></object>')
        assert "iframe" not in out
        assert "object" not in out

    def test_strips_event_handlers(self):
        out = sanitize_render_html('<div onclick="alert(1)" onmouseover="x()">hi</div>')
        assert "onclick" not in out
        assert "onmouseover" not in out
        assert ">hi</div>" in out

    def test_strips_javascript_href(self):
        out = sanitize_render_html('<a href="javascript:alert(1)">x</a>')
        assert "javascript" not in out

    def test_blocks_external_img_src(self):
        out = sanitize_render_html('<img src="https://evil.example/track.gif">')
        assert "evil.example" not in out

    def test_blocks_metadata_endpoint(self):
        out = sanitize_render_html('<img src="http://169.254.169.254/latest/meta-data/">')
        assert "169.254" not in out

    def test_keeps_data_uri_images(self):
        out = sanitize_render_html('<img src="data:image/png;base64,iVBOR=" alt="logo">')
        assert 'src="data:image/png;base64,iVBOR="' in out

    def test_keeps_google_fonts_link(self):
        out = sanitize_render_html('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">')
        assert "fonts.googleapis.com" in out

    def test_blocks_other_stylesheet_links(self):
        out = sanitize_render_html('<link rel="stylesheet" href="https://evil.example/style.css">')
        assert "evil.example" not in out

    def test_keeps_style_blocks_and_inline_styles(self):
        out = sanitize_render_html('<style>@page{size:A4} .x{color:red}</style><h1 style="color:teal">t</h1>')
        assert "@page{size:A4}" in out
        assert 'style="color:teal"' in out

    def test_keeps_tables_and_svg(self):
        out = sanitize_render_html(
            '<table><tr><td colspan="2">c</td></tr></table>'
            '<svg viewBox="0 0 10 10"><rect x="1" width="8" fill="teal"/></svg>'
        )
        assert 'colspan="2"' in out
        assert "<svg" in out
        assert 'fill="teal"' in out

    def test_prepends_doctype(self):
        assert sanitize_render_html("<p>x</p>").startswith("<!DOCTYPE html>")


class _FakeRequest:
    def __init__(self, url):
        self.url = url


class _FakeRoute:
    def __init__(self, url):
        self.request = _FakeRequest(url)
        self.action = None

    async def continue_(self):
        self.action = "continue"

    async def abort(self):
        self.action = "abort"


class TestEgressGuard:
    async def test_allows_google_fonts(self):
        for host in ALLOWED_EGRESS_HOSTS:
            route = _FakeRoute(f"https://{host}/some/font.woff2")
            await _egress_guard(route)
            assert route.action == "continue"

    async def test_blocks_everything_else(self):
        for url in [
            "https://evil.example.com/beacon",
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost:8000/api/auth/me",
            "http://10.0.0.5/internal",
            "https://fonts.googleapis.com.evil.example/x",
        ]:
            route = _FakeRoute(url)
            await _egress_guard(route)
            assert route.action == "abort", url
