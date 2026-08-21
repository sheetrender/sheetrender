"""Fixtures shared by every test in this package.

Config isolation is the one thing every test needs: sheetrender.config holds
a single process-global RenderConfig, so any test that calls configure()
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
