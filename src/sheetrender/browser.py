"""Chromium lifecycle and the concurrency gate the render paths share."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlparse

from sheetrender.config import get_config


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


@dataclass
class _BrowserState:
    """The running Chromium and everything scoped to that one launch."""

    playwright: object | None = None
    browser: object | None = None
    gate: PriorityGate | None = None
    restart_lock: asyncio.Lock | None = None
    recycle_lock: asyncio.Lock | None = None
    render_count: int = 0
    launch_time: float | None = None


_state = _BrowserState()


# Pin formatting-sensitive environment so Intl/date output in templates does
# not depend on the host the render happens to run on.
_CONTEXT_OPTS = {"locale": "en-US", "timezone_id": "UTC"}


def _mark(timing: dict | None, key: str, since: float) -> float:
    """Record elapsed ms since `since` under `key`; return the current time."""
    now = time.monotonic()
    if timing is not None:
        timing[key] = (now - since) * 1000
    return now


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


def _should_recycle_check(render_count: int, launch_time: float | None, max_renders: int, max_age_minutes: int) -> bool:
    if render_count >= max_renders:
        return True
    if launch_time is None:
        return False
    elapsed_minutes = (time.monotonic() - launch_time) / 60
    return elapsed_minutes >= max_age_minutes


async def start_browser():
    if _state.browser is not None:
        return
    from playwright.async_api import async_playwright

    _state.playwright = await async_playwright().start()
    _state.browser = await _state.playwright.chromium.launch(headless=True)
    _state.gate = PriorityGate(get_config().concurrency)
    _state.restart_lock = asyncio.Lock()
    _state.recycle_lock = asyncio.Lock()
    _state.render_count = 0
    _state.launch_time = time.monotonic()


async def _relaunch() -> None:
    """Drop the old Chromium and launch a fresh one, resetting the counters.

    The caller must hold `_state.restart_lock`.
    """
    from playwright.async_api import async_playwright

    try:
        if _state.browser is not None:
            await _state.browser.close()
    except Exception:
        pass
    try:
        if _state.playwright is not None:
            await _state.playwright.stop()
    except Exception:
        pass
    _state.playwright = await async_playwright().start()
    _state.browser = await _state.playwright.chromium.launch(headless=True)
    _state.render_count = 0
    _state.launch_time = time.monotonic()


async def _get_browser():
    """Return a connected browser, relaunching Chromium if it has died."""
    if _state.browser is not None and _state.browser.is_connected():
        return _state.browser
    if _state.restart_lock is None:
        raise RuntimeError("Playwright browser is not started")
    async with _state.restart_lock:
        if _state.browser is not None and _state.browser.is_connected():
            return _state.browser
        await _relaunch()
        return _state.browser


async def _maybe_recycle() -> None:
    gate = _state.gate
    if gate is None:
        raise RuntimeError("Playwright browser is not started")
    if not _should_recycle_check(
        _state.render_count,
        _state.launch_time,
        get_config().recycle_max_renders,
        get_config().recycle_max_age_minutes,
    ):
        return
    if _state.recycle_lock is None or _state.restart_lock is None:
        raise RuntimeError("Playwright browser is not started")
    if _state.recycle_lock.locked():
        return

    async with _state.recycle_lock:
        if not _should_recycle_check(
            _state.render_count,
            _state.launch_time,
            get_config().recycle_max_renders,
            get_config().recycle_max_age_minutes,
        ):
            return

        # Drain before taking restart_lock: a crashed in-flight render (still
        # holding a slot) needs restart_lock inside _get_browser to finish, so
        # holding it while waiting on slots would deadlock.
        acquired = 0
        try:
            for _ in range(get_config().concurrency):
                await gate.acquire(high=True)
                acquired += 1

            async with _state.restart_lock:
                await _relaunch()
        finally:
            for _ in range(acquired):
                gate.release()


def browser_is_connected() -> bool:
    """True while a launched Chromium is reachable. Meant for readiness probes."""
    browser = _state.browser
    return browser is not None and browser.is_connected()


async def stop_browser():
    if _state.browser:
        await _state.browser.close()
    if _state.playwright:
        await _state.playwright.stop()
    _state.playwright = None
    _state.browser = None
    _state.gate = None
    _state.restart_lock = None
    _state.recycle_lock = None
    _state.render_count = 0
    _state.launch_time = None


@asynccontextmanager
async def render_slot(*, high: bool = False, timing: dict | None = None):
    """Wait for a render slot, recycling the browser first if it is due."""
    gate = _state.gate
    if gate is None:
        raise RuntimeError("Playwright browser is not started")
    await _maybe_recycle()
    gate_t = time.monotonic()
    async with gate.slot(high=high):
        _mark(timing, "gate_wait", gate_t)
        try:
            yield
        finally:
            _state.render_count += 1


async def _retry_if_browser_died(render):
    """Run a render attempt, retrying once if Chromium died mid-flight."""
    try:
        return await render()
    except Exception:
        if _state.browser is not None and _state.browser.is_connected():
            raise
        # Browser is gone — _get_browser() will relaunch it on the retry.
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
