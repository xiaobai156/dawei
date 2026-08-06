"""Reusable Playwright rendering with strict dynamic-record validation."""

from __future__ import annotations

import atexit
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import re
import threading

from dawei.domain.errors import ScrapeError
from dawei.domain.models import ArticleRecord, SiteConfig

from .source_adapters import (
    article_detail_id,
    article_record_from_payload,
    payload_contains_record_id,
)


DEFAULT_TIMEOUT = 20
NUMBER_RE = re.compile(r"(?<!\d)(0[1-9]|[1-4]\d)(?!\d)")


def rendered_text_ready(
    text: str,
    expected_issue: int | None = None,
    expected_keywords: Iterable[str] = (),
) -> bool:
    if expected_issue is not None and f"{expected_issue:03d}期" not in text and f"{expected_issue}期" not in text:
        return False
    keywords = tuple(keyword for keyword in expected_keywords if keyword)
    if keywords and not any(keyword in text for keyword in keywords):
        return False
    return len(NUMBER_RE.findall(text)) >= 36


@dataclass
class BrowserRenderState:
    playwright: object
    browser: object


class ReusableBrowserRenderer:
    def __init__(self, playwright_starter: Callable[[], object]):
        self.playwright_starter = playwright_starter
        self.local = threading.local()
        self.lock = threading.Lock()
        self.states: list[BrowserRenderState] = []

    def state(self) -> BrowserRenderState:
        state = getattr(self.local, "state", None)
        if state is not None:
            return state
        playwright = self.playwright_starter()
        browser = playwright.chromium.launch(headless=True)
        state = BrowserRenderState(playwright, browser)
        self.local.state = state
        with self.lock:
            self.states.append(state)
        return state

    def fetch(
        self,
        url: str,
        timeout: int = DEFAULT_TIMEOUT,
        expected_issue: int | None = None,
        expected_keywords: Iterable[str] = (),
        force_full_wait: bool = False,
    ) -> str:
        page = self.state().browser.new_page(ignore_https_errors=False)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            body = page.locator("body")
            if not force_full_wait:
                text = body.inner_text(timeout=timeout * 1000)
                if rendered_text_ready(text, expected_issue, expected_keywords):
                    return text
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            if not force_full_wait:
                text = body.inner_text(timeout=timeout * 1000)
                if rendered_text_ready(text, expected_issue, expected_keywords):
                    return text
            page.wait_for_timeout(3000)
            return body.inner_text(timeout=timeout * 1000)
        finally:
            close_page = getattr(page, "close", None)
            if close_page:
                close_page()

    def fetch_article(
        self,
        config: SiteConfig,
        timeout: int = DEFAULT_TIMEOUT,
        expected_issue: int | None = None,
        expected_keywords: Iterable[str] = (),
        force_full_wait: bool = False,
    ) -> ArticleRecord:
        page = self.state().browser.new_page(ignore_https_errors=False)
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
            if not force_full_wait:
                body.inner_text(timeout=timeout * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            if not force_full_wait:
                body.inner_text(timeout=timeout * 1000)
            page.wait_for_timeout(3000)
            body.inner_text(timeout=timeout * 1000)

            expected_id = article_detail_id(config)
            found: list[ArticleRecord] = []
            for response in responses:
                try:
                    payload = response.body().decode("utf-8", errors="replace")
                except Exception:
                    continue
                if not payload_contains_record_id(payload, expected_id):
                    continue
                found.append(
                    article_record_from_payload(payload, config, source_path=str(response.url))
                )
            if not found:
                raise ScrapeError(f"浏览器兜底未找到URL记录ID: {expected_id}")
            first = found[0]
            if any(record.document != first.document for record in found[1:]):
                raise ScrapeError(f"浏览器兜底同ID记录内容冲突: {expected_id}")
            return first
        finally:
            close_page = getattr(page, "close", None)
            if close_page:
                close_page()

    def close_all(self) -> None:
        with self.lock:
            states = self.states
            self.states = []
        for state in states:
            try:
                state.browser.close()
            except Exception:
                pass
            try:
                state.playwright.stop()
            except Exception:
                pass


_BROWSER_RENDERER: ReusableBrowserRenderer | None = None


def close_browser_renderer() -> None:
    global _BROWSER_RENDERER
    if _BROWSER_RENDERER is not None:
        _BROWSER_RENDERER.close_all()
        _BROWSER_RENDERER = None


def fetch_rendered_text(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    expected_issue: int | None = None,
    expected_keywords: Iterable[str] = (),
    force_full_wait: bool = False,
) -> str:
    global _BROWSER_RENDERER
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ScrapeError("browser rendering requires playwright") from exc
    try:
        if _BROWSER_RENDERER is None:
            _BROWSER_RENDERER = ReusableBrowserRenderer(lambda: sync_playwright().start())
        return _BROWSER_RENDERER.fetch(
            url,
            timeout=timeout,
            expected_issue=expected_issue,
            expected_keywords=expected_keywords,
            force_full_wait=force_full_wait,
        )
    except Exception as exc:
        raise ScrapeError(f"browser rendering failed: {exc}") from exc


def fetch_rendered_article_record(
    config: SiteConfig,
    timeout: int = DEFAULT_TIMEOUT,
    expected_issue: int | None = None,
    expected_keywords: Iterable[str] = (),
    force_full_wait: bool = False,
) -> ArticleRecord:
    global _BROWSER_RENDERER
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ScrapeError("browser rendering requires playwright") from exc
    try:
        if _BROWSER_RENDERER is None:
            _BROWSER_RENDERER = ReusableBrowserRenderer(lambda: sync_playwright().start())
        return _BROWSER_RENDERER.fetch_article(
            config,
            timeout=timeout,
            expected_issue=expected_issue,
            expected_keywords=expected_keywords,
            force_full_wait=force_full_wait,
        )
    except ScrapeError:
        raise
    except Exception as exc:
        raise ScrapeError(f"browser article rendering failed: {exc}") from exc


atexit.register(close_browser_renderer)
