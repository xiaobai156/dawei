from __future__ import annotations

import json
import unittest

from dawei.domain.errors import ScrapeError
from dawei.domain.models import SiteConfig
from dawei.infrastructure.browser_client import ReusableBrowserRenderer


def config() -> SiteConfig:
    return SiteConfig(
        name="目标作者",
        url="https://example.test/article/manager/target-id?url=x",
        keywords=("三十六码",),
        section_keywords=("目标作者",),
        region="bottom",
        site_id="target-site",
        source_type="dynamic_article",
        parser_id="dynamic_article",
        record_id="target-id",
        render_policy="fallback",
    )


def payload(record_id: str, marker: str) -> bytes:
    return json.dumps(
        {
            "data": [
                {
                    "id": record_id,
                    "authorNickname": "目标作者",
                    "title": "210期 三十六码",
                    "content": f"目标作者 三十六码 {marker}",
                }
            ]
        },
        ensure_ascii=False,
    ).encode()


class FakeResponse:
    headers = {"content-type": "application/json"}
    url = "https://example.test/api/articles"

    def __init__(self, body: bytes):
        self._body = body

    def body(self) -> bytes:
        return self._body


class FakeBody:
    def inner_text(self, timeout: int) -> str:
        return "页面正文"


class FakePage:
    def __init__(self, bodies: list[bytes]):
        self.bodies = bodies
        self.handlers = []

    def on(self, event: str, handler) -> None:
        if event == "response":
            self.handlers.append(handler)

    def goto(self, url: str, wait_until: str, timeout: int) -> None:
        for body in self.bodies:
            response = FakeResponse(body)
            for handler in self.handlers:
                handler(response)

    def locator(self, selector: str) -> FakeBody:
        return FakeBody()

    def wait_for_load_state(self, state: str, timeout: int) -> None:
        return None

    def wait_for_timeout(self, timeout: int) -> None:
        return None

    def close(self) -> None:
        return None


class FakeBrowser:
    def __init__(self, bodies: list[bytes], page_options: list[dict[str, object]]):
        self.bodies = bodies
        self.page_options = page_options

    def new_page(self, **kwargs) -> FakePage:
        self.page_options.append(kwargs)
        return FakePage(self.bodies)

    def close(self) -> None:
        return None


class FakePlaywright:
    def __init__(self, bodies: list[bytes], page_options: list[dict[str, object]]):
        self.bodies = bodies
        self.page_options = page_options
        self.chromium = self

    def launch(self, headless: bool) -> FakeBrowser:
        return FakeBrowser(self.bodies, self.page_options)

    def stop(self) -> None:
        return None


class BrowserClientTests(unittest.TestCase):
    def renderer(self, bodies: list[bytes], options: list[dict[str, object]]) -> ReusableBrowserRenderer:
        return ReusableBrowserRenderer(lambda: FakePlaywright(bodies, options))

    def test_browser_keeps_https_certificate_validation_enabled(self) -> None:
        options: list[dict[str, object]] = []
        renderer = self.renderer([payload("target-id", "TARGET")], options)
        try:
            result = renderer.fetch_article(config(), timeout=1)
        finally:
            renderer.close_all()
        self.assertEqual(result.record_id, "target-id")
        self.assertEqual(options, [{"ignore_https_errors": False}])

    def test_browser_ignores_decoy_record_and_rejects_target_conflict(self) -> None:
        renderer = self.renderer(
            [payload("decoy-id", "DECOY"), payload("target-id", "TARGET")],
            [],
        )
        try:
            result = renderer.fetch_article(config(), timeout=1)
        finally:
            renderer.close_all()
        self.assertIn("TARGET", result.document)
        self.assertNotIn("DECOY", result.document)

        conflicting = self.renderer(
            [payload("target-id", "ONE"), payload("target-id", "TWO")],
            [],
        )
        try:
            with self.assertRaisesRegex(ScrapeError, "同ID记录内容冲突"):
                conflicting.fetch_article(config(), timeout=1)
        finally:
            conflicting.close_all()


if __name__ == "__main__":
    unittest.main()
