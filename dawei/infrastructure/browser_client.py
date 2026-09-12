"""Reusable Playwright rendering with strict dynamic-record validation."""

from __future__ import annotations

import atexit
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from dawei.domain.errors import ScrapeError
from dawei.domain.models import ArticleRecord, SiteConfig

from .source_adapters import (
    article_detail_id,
    article_record_from_payload,
    payload_contains_record_id,
)

try:
    from playwright.sync_api import Error as PlaywrightError
except ImportError:  # pragma: no cover - wrappers raise a clearer error later
    class PlaywrightError(Exception):
        """Fallback type used when Playwright is not installed."""

DEFAULT_TIMEOUT = 20
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})


@dataclass
class BrowserRenderState:
    playwright: object
    browser: object
    context: object


@dataclass
class _BrowserTask:
    callback: Callable[[], object]
    done: threading.Event
    result: object = None
    error: BaseException | None = None


def _close_page_best_effort(page: object) -> None:
    close_page = getattr(page, "close", None)
    if close_page:
        try:
            close_page()
        except PlaywrightError:
            pass


def _route_resource(route: object, request: object) -> None:
    """Keep document/code/data requests while dropping unused heavy assets."""
    if getattr(request, "resource_type", "") in _BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def _install_resource_filter(context: object) -> None:
    route = getattr(context, "route", None)
    if route is not None:
        route("**/*", _route_resource)


class ReusableBrowserRenderer:
    def __init__(self, playwright_starter: Callable[[], object]):
        self.playwright_starter = playwright_starter
        # ``local`` remains a compatibility hook for tests/callers that inject a
        # state; real Playwright state is created and used by the owner thread.
        self.local = threading.local()
        self.lock = threading.Lock()
        self.states: list[BrowserRenderState] = []

        self._lifecycle_lock = threading.Lock()
        self._tasks: queue.Queue[_BrowserTask] = queue.Queue()
        self._owner_thread: threading.Thread | None = None
        self._owner_thread_id: int | None = None
        self._accepting = True
        self._closing = False
        self._closed = False
        self._stop_owner = False
        self._close_done = threading.Event()

    def _start_owner_locked(self) -> None:
        if self._owner_thread is not None:
            return
        self._owner_thread = threading.Thread(
            target=self._owner_loop,
            name="dawei-playwright-owner",
            daemon=True,
        )
        self._owner_thread.start()

    def _owner_loop(self) -> None:
        self._owner_thread_id = threading.get_ident()
        while True:
            task = self._tasks.get()
            try:
                task.result = task.callback()
            except BaseException as exc:  # noqa: BLE001 - propagate task failures
                task.error = exc
            finally:
                task.done.set()
            if self._stop_owner:
                return

    def _run_on_owner(self, callback: Callable[[], object]) -> object:
        if threading.get_ident() == self._owner_thread_id:
            with self._lifecycle_lock:
                if not self._accepting:
                    raise RuntimeError("browser renderer is closed")
            return callback()

        task = _BrowserTask(callback, threading.Event())
        with self._lifecycle_lock:
            if not self._accepting:
                raise RuntimeError("browser renderer is closed")
            self._start_owner_locked()
            self._tasks.put(task)
        task.done.wait()
        if task.error is not None:
            raise task.error
        return task.result

    def _register_state(self, state: BrowserRenderState) -> None:
        with self.lock:
            if not any(existing is state for existing in self.states):
                self.states.append(state)

    def _existing_state(self) -> BrowserRenderState | None:
        with self.lock:
            return self.states[0] if self.states else None

    def _create_state_on_owner(self) -> BrowserRenderState:
        existing = self._existing_state()
        if existing is not None:
            return existing

        playwright = self.playwright_starter()
        browser = None
        try:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=False)
            _install_resource_filter(context)
        except Exception:
            if browser is not None:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001, S110
                    pass
            try:
                playwright.stop()
            except Exception:  # noqa: BLE001, S110
                pass
            raise
        state = BrowserRenderState(playwright, browser, context)
        self._register_state(state)
        return state

    def state(self) -> BrowserRenderState:
        state = getattr(self.local, "state", None)
        if state is None:
            state = self._run_on_owner(self._create_state_on_owner)
            self.local.state = state
        else:
            with self._lifecycle_lock:
                if not self._accepting:
                    raise RuntimeError("browser renderer is closed")
            self._register_state(state)
        return state

    def _state_for_call_on_owner(
        self,
        injected_state: BrowserRenderState | None,
    ) -> BrowserRenderState:
        if injected_state is not None:
            self._register_state(injected_state)
            return injected_state
        return self._create_state_on_owner()

    def _fetch_on_owner(
        self,
        url: str,
        timeout: int = DEFAULT_TIMEOUT,
        injected_state: BrowserRenderState | None = None,
    ) -> str:
        state = self._state_for_call_on_owner(injected_state)
        page = state.context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            body = page.locator("body")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightError:
                pass
            body.inner_text(timeout=timeout * 1000)
            return page.content()
        finally:
            _close_page_best_effort(page)

    def fetch(
        self,
        url: str,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> str:
        injected_state = getattr(self.local, "state", None)
        return self._run_on_owner(
            lambda: self._fetch_on_owner(url, timeout, injected_state)
        )

    def _fetch_article_on_owner(
        self,
        config: SiteConfig,
        timeout: int = DEFAULT_TIMEOUT,
        injected_state: BrowserRenderState | None = None,
    ) -> ArticleRecord:
        state = self._state_for_call_on_owner(injected_state)
        page = state.context.new_page()
        responses: list[object] = []

        def capture_response(response: object) -> None:
            headers = getattr(response, "headers", {})
            content_type = str(headers.get("content-type", "")).lower()
            response_url = str(getattr(response, "url", ""))
            if "json" in content_type or "/api/" in response_url.lower():
                responses.append(response)

        page.on("response", capture_response)
        try:
            page.goto(config.url, wait_until="domcontentloaded", timeout=timeout * 1000)
            body = page.locator("body")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightError:
                pass
            body.inner_text(timeout=timeout * 1000)

            expected_id = article_detail_id(config)
            found: list[ArticleRecord] = []
            for response in responses:
                try:
                    payload = response.body().decode("utf-8", errors="replace")
                except PlaywrightError:
                    continue
                if not payload_contains_record_id(payload, expected_id):
                    continue
                found.append(
                    article_record_from_payload(payload, config, source_path=str(response.url))
                )
            if not found:
                raise ScrapeError(f"浏览器兜底未找到URL记录ID: {expected_id}")
            first = found[0]
            first_identity = (
                first.record_id,
                first.title,
                first.author,
                first.body,
                first.document,
                first.section_names,
            )
            if any(
                (
                    record.record_id,
                    record.title,
                    record.author,
                    record.body,
                    record.document,
                    record.section_names,
                )
                != first_identity
                for record in found[1:]
            ):
                raise ScrapeError(f"浏览器兜底同ID记录内容冲突: {expected_id}")
            return first
        finally:
            _close_page_best_effort(page)

    def fetch_article(
        self,
        config: SiteConfig,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> ArticleRecord:
        injected_state = getattr(self.local, "state", None)
        return self._run_on_owner(
            lambda: self._fetch_article_on_owner(config, timeout, injected_state)
        )

    def _close_on_owner(self) -> None:
        with self.lock:
            states = self.states
            self.states = []
        for state in states:
            try:
                state.context.close()
            except Exception:  # noqa: BLE001, S110
                pass
            try:
                state.browser.close()
            except Exception:  # noqa: BLE001, S110
                pass
            try:
                state.playwright.stop()
            except Exception:  # noqa: BLE001, S110
                pass
        with self._lifecycle_lock:
            self._closed = True
            self._closing = False
            self._stop_owner = True
            self._close_done.set()

    def close_all(self) -> None:
        current_thread = threading.get_ident()
        wait_for_close = False
        close_on_owner = False
        owner: threading.Thread | None = None
        with self._lifecycle_lock:
            if self._closed:
                return
            if self._closing:
                wait_for_close = current_thread != self._owner_thread_id
                owner = self._owner_thread
            else:
                self._accepting = False
                self._closing = True
                close_on_owner = current_thread == self._owner_thread_id
                if not close_on_owner:
                    self._start_owner_locked()
                    owner = self._owner_thread
                    self._tasks.put(
                        _BrowserTask(self._close_on_owner, threading.Event())
                    )
                    wait_for_close = True

        if close_on_owner:
            self._close_on_owner()
            return
        if wait_for_close:
            self._close_done.wait()
            if owner is not None and owner is not threading.current_thread():
                owner.join()


def normalize_origin(url: str | None) -> str | None:
    """Normalize a URL to a ``scheme://hostname:effective-port`` affinity key.

    Userinfo (username/password) is ignored.  An omitted port is equivalent to
    the scheme default (https 443 / http 80), so explicit defaults and omitted
    defaults map to the same key.
    """
    if not url:
        return None
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if not scheme or not host:
        return None
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme)
    return f"{scheme}://{host}:{port}" if port is not None else f"{scheme}://{host}"


class BrowserRendererPool:
    """Independent batch-scoped pool of owner-thread renderers.

    The pool never touches the legacy global renderer singleton used by
    ``fetch_rendered_text`` / ``fetch_rendered_article_record``.  Same-origin
    affinity is derived from :func:`normalize_origin` unless an explicit
    ``affinity_key`` is supplied; no further grouping is guessed.
    """

    def __init__(self, size: int, playwright_starter: Callable[[], object]):
        if type(size) is not int or size not in (1, 2):
            raise ScrapeError("browser workers must be 1 or 2")
        self.size = size
        self.playwright_starter = playwright_starter
        self._lock = threading.Lock()
        self._renderers: list[ReusableBrowserRenderer] = []
        self._origin_index: dict[str, int] = {}
        self._closed = False

    def _affinity_key(self, url: str, affinity_key: str | None) -> str:
        if affinity_key is not None:
            return affinity_key
        return normalize_origin(url) or ""

    def _renderer_for(self, key: str) -> ReusableBrowserRenderer:
        with self._lock:
            if self._closed:
                raise ScrapeError("browser renderer pool is closed")
            index = self._origin_index.get(key)
            if index is None:
                index = len(self._origin_index) % self.size
                self._origin_index[key] = index
            while len(self._renderers) <= index:
                self._renderers.append(ReusableBrowserRenderer(self.playwright_starter))
            return self._renderers[index]

    def fetch(
        self,
        url: str,
        timeout: int = DEFAULT_TIMEOUT,
        affinity_key: str | None = None,
    ) -> str:
        renderer = self._renderer_for(self._affinity_key(url, affinity_key))
        return renderer.fetch(url, timeout=timeout)

    def fetch_article(
        self,
        config: SiteConfig,
        timeout: int = DEFAULT_TIMEOUT,
        affinity_key: str | None = None,
    ) -> ArticleRecord:
        renderer = self._renderer_for(self._affinity_key(config.url, affinity_key))
        return renderer.fetch_article(config, timeout=timeout)

    def close_all(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            renderers = list(self._renderers)
            self._renderers = []
            self._origin_index = {}
        errors: list[str] = []
        for renderer in renderers:
            try:
                renderer.close_all()
            except BaseException as exc:  # noqa: BLE001 - report every close failure
                errors.append(str(exc) or exc.__class__.__name__)
        if errors:
            raise ScrapeError("browser renderer pool close failed: " + "; ".join(errors))


_BROWSER_RENDERER: ReusableBrowserRenderer | None = None
_BROWSER_RENDERER_LOCK = threading.Lock()


def _get_renderer(playwright_starter: Callable[[], object]) -> ReusableBrowserRenderer:
    global _BROWSER_RENDERER
    with _BROWSER_RENDERER_LOCK:
        if _BROWSER_RENDERER is None:
            _BROWSER_RENDERER = ReusableBrowserRenderer(playwright_starter)
        return _BROWSER_RENDERER


def close_browser_renderer() -> None:
    global _BROWSER_RENDERER
    with _BROWSER_RENDERER_LOCK:
        renderer = _BROWSER_RENDERER
        _BROWSER_RENDERER = None
    if renderer is not None:
        renderer.close_all()


def fetch_rendered_text(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ScrapeError("browser rendering requires playwright") from exc
    try:
        renderer = _get_renderer(lambda: sync_playwright().start())
        return renderer.fetch(url, timeout=timeout)
    except Exception as exc:
        raise ScrapeError(f"browser rendering failed: {exc}") from exc


def fetch_rendered_article_record(
    config: SiteConfig,
    timeout: int = DEFAULT_TIMEOUT,
) -> ArticleRecord:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ScrapeError("browser rendering requires playwright") from exc
    try:
        renderer = _get_renderer(lambda: sync_playwright().start())
        return renderer.fetch_article(config, timeout=timeout)
    except ScrapeError:
        raise
    except Exception as exc:
        raise ScrapeError(f"browser article rendering failed: {exc}") from exc


atexit.register(close_browser_renderer)
