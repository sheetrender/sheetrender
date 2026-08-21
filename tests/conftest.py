"""Fixtures shared by every test in this package.

The backend suite these tests were ported from had exactly one conftest
fixture and it reset an API rate limiter — nothing that survives extraction.
What the package needs in its place is config isolation: sheetrender.config
holds a single process-global RenderConfig, so any test that calls configure()
would otherwise leak its settings into every test that ran after it.
"""
from __future__ import annotations

import pytest

from sheetrender import config as render_config


@pytest.fixture(autouse=True)
def _reset_render_config():
    original = render_config.get_config()
    yield
    render_config.configure(original)
