"""登录态（requires_login + 持久化 profile）支持的自测。

覆盖：配置解析与默认 profile、指纹向后兼容、配置校验、渲染器走持久化上下文、
抓取服务对登录站点强制浏览器渲染。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from dawei.application.scrape_service import ScrapeService, load_source
from dawei.domain.errors import ConfigurationError, ScrapeError, ValidationError
from dawei.domain.models import SiteConfig
from dawei.domain.validation import validate_site_config
from dawei.infrastructure import browser_client
from dawei.infrastructure.config_repository import (
    DEFAULT_LOGIN_PROFILE,
    ConfigRepository,
    config_fingerprint,
    migrate_site_mapping,
)

LOGIN_URL = "https://wwlhc.com/forum.php?mod=viewthread&tid=101698"


class _FakePage:
    def __init__(self) -> None:
        self.closed = 0

    def goto(self, url: str, wait_until: str = "", timeout: int = 0) -> None:
        self.url = url

    def locator(self, selector: str) -> _FakePage:
        return self

    def inner_text(self, timeout: int = 0) -> str:
        return "正文"

    def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        return None

    def content(self) -> str:
        return "<html>登录后正文</html>"

    def close(self) -> None:
        self.closed += 1


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []
        self.closed = 0

    def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page

    def route(self, pattern: str, handler: object) -> None:
        self.routed = pattern

    def close(self) -> None:
        self.closed += 1


class _FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[_FakeContext] = []
        self.closed = 0

    def new_context(self, **_kwargs: object) -> _FakeContext:
        context = _FakeContext()
        self.contexts.append(context)
        return context

    def close(self) -> None:
        self.closed += 1


class _FakeChromium:
    def __init__(self) -> None:
        self.launch_calls = 0
        self.persistent_dirs: list[str] = []
        self.persistent_contexts: list[_FakeContext] = []

    def launch(self, **_kwargs: object) -> _FakeBrowser:
        self.launch_calls += 1
        return _FakeBrowser()

    def launch_persistent_context(self, user_data_dir: str, **_kwargs: object) -> _FakeContext:
        self.persistent_dirs.append(user_data_dir)
        context = _FakeContext()
        context.browser = _FakeBrowser()
        self.persistent_contexts.append(context)
        return context


class _FakePlaywright:
    def __init__(self) -> None:
        self.chromium = _FakeChromium()
        self.stopped = 0

    def stop(self) -> None:
        self.stopped += 1


class ConfigParsingTests(unittest.TestCase):
    def test_requires_login_defaults_profile_to_project_dir(self) -> None:
        site = migrate_site_mapping(
            {"name": "洪福联盟", "url": LOGIN_URL, "requires_login": True},
            1,
        )
        self.assertTrue(site.requires_login)
        self.assertEqual(site.login_profile, str(DEFAULT_LOGIN_PROFILE))

    def test_explicit_profile_is_kept(self) -> None:
        site = migrate_site_mapping(
            {
                "name": "洪福联盟",
                "url": LOGIN_URL,
                "requires_login": True,
                "login_profile": r"C:\profile",
            },
            1,
        )
        self.assertEqual(site.login_profile, r"C:\profile")

    def test_non_login_site_keeps_null_profile(self) -> None:
        site = migrate_site_mapping({"name": "普通站", "url": "https://a.test/topic"}, 1)
        self.assertFalse(site.requires_login)
        self.assertIsNone(site.login_profile)

    def test_rejects_non_bool_requires_login(self) -> None:
        with self.assertRaises(ConfigurationError):
            migrate_site_mapping(
                {"name": "x", "url": LOGIN_URL, "requires_login": "yes"},
                1,
            )

    def test_default_login_fields_do_not_change_fingerprint(self) -> None:
        base = SiteConfig(name="普通站", url="https://a.test/topic", site_id="site_a", region="top")
        explicit = SiteConfig(
            name="普通站",
            url="https://a.test/topic",
            site_id="site_a",
            region="top",
            requires_login=False,
            login_profile=None,
        )
        self.assertEqual(config_fingerprint([base]), config_fingerprint([explicit]))

    def test_login_profile_participates_in_fingerprint(self) -> None:
        plain = SiteConfig(name="站点", url=LOGIN_URL, site_id="site_a", region="top")
        login = SiteConfig(
            name="站点",
            url=LOGIN_URL,
            site_id="site_a",
            region="top",
            requires_login=True,
            login_profile=r"C:\profile",
        )
        self.assertNotEqual(config_fingerprint([plain]), config_fingerprint([login]))

    def test_repository_loads_login_site(self) -> None:
        payload = [
            {
                "name": "洪福联盟",
                "url": LOGIN_URL,
                "site_id": "site_login",
                "region": "bottom",
                "render_policy": "always",
                "requires_login": True,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sites_36.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            sites = ConfigRepository(path).load()
        self.assertTrue(sites[0].requires_login)
        self.assertEqual(sites[0].login_profile, str(DEFAULT_LOGIN_PROFILE))


class ValidationTests(unittest.TestCase):
    def _site(self, **overrides: object) -> SiteConfig:
        values: dict[str, object] = {
            "name": "洪福联盟",
            "url": LOGIN_URL,
            "site_id": "site_login",
            "region": "bottom",
            "render_policy": "always",
            "requires_login": True,
            "login_profile": r"C:\profile",
        }
        values.update(overrides)
        return SiteConfig(**values)

    def test_valid_login_site(self) -> None:
        validate_site_config(self._site())

    def test_login_site_requires_profile(self) -> None:
        with self.assertRaises(ValidationError):
            validate_site_config(self._site(login_profile=None))

    def test_login_site_requires_browser_rendering(self) -> None:
        with self.assertRaises(ValidationError):
            validate_site_config(self._site(render_policy="never", render_browser=False))


class RendererLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.playwright = _FakePlaywright()
        self.renderer = browser_client.ReusableBrowserRenderer(lambda: self.playwright)
        self.addCleanup(self.renderer.close_all)

    def test_persistent_profile_is_used_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = self.renderer.fetch(LOGIN_URL, timeout=5, login_profile=tmp)
            second = self.renderer.fetch(LOGIN_URL, timeout=5, login_profile=tmp)
            expected = os.path.normcase(os.path.abspath(tmp))
        self.assertIn("登录后正文", first)
        self.assertEqual(first, second)
        self.assertEqual(self.playwright.chromium.launch_calls, 0)
        self.assertEqual(self.playwright.chromium.persistent_dirs, [expected])

    def test_anonymous_and_login_contexts_stay_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.renderer.fetch("https://a.test/topic", timeout=5)
            self.renderer.fetch(LOGIN_URL, timeout=5, login_profile=tmp)
        self.assertEqual(self.playwright.chromium.launch_calls, 1)
        self.assertEqual(len(self.playwright.chromium.persistent_dirs), 1)

    def test_missing_profile_dir_fails(self) -> None:
        with self.assertRaises(ScrapeError):
            self.renderer.fetch(LOGIN_URL, timeout=5, login_profile="Z:/definitely-missing-profile")

    def _single_start_renderer(self) -> tuple[browser_client.ReusableBrowserRenderer, dict]:
        """Starter that mimics Playwright: a second start in the same thread fails."""
        calls = {"count": 0}

        def starter() -> object:
            calls["count"] += 1
            if calls["count"] > 1:
                raise AssertionError(
                    "It looks like you are using Playwright Sync API inside the asyncio loop."
                )
            return self.playwright

        renderer = browser_client.ReusableBrowserRenderer(starter)
        self.addCleanup(renderer.close_all)
        return renderer, calls

    def test_login_fetch_after_anonymous_reuses_playwright(self) -> None:
        renderer, calls = self._single_start_renderer()
        with tempfile.TemporaryDirectory() as tmp:
            renderer.fetch("https://a.test/topic", timeout=5)
            text = renderer.fetch(LOGIN_URL, timeout=5, login_profile=tmp)
        self.assertIn("登录后正文", text)
        self.assertEqual(calls["count"], 1)

    def test_anonymous_fetch_after_login_reuses_playwright(self) -> None:
        renderer, calls = self._single_start_renderer()
        with tempfile.TemporaryDirectory() as tmp:
            renderer.fetch(LOGIN_URL, timeout=5, login_profile=tmp)
            text = renderer.fetch("https://a.test/topic", timeout=5)
        self.assertIn("正文", text)
        self.assertEqual(calls["count"], 1)


class ScrapeServiceLoginTests(unittest.TestCase):
    def test_login_site_forces_browser_with_config(self) -> None:
        calls: dict[str, object] = {}
        config = SiteConfig(
            name="洪福联盟",
            url=LOGIN_URL,
            site_id="site_login",
            region="bottom",
            render_policy="always",
            requires_login=True,
            login_profile=r"C:\profile",
        )

        def text_fetcher(url: str, timeout: int) -> str:
            raise AssertionError("登录站点不得走HTTP游客视图")

        def rendered_text_fetcher(url: str, timeout: int = 0, config=None) -> str:
            calls["url"] = url
            calls["config"] = config
            return "<html>正文</html>"

        source = load_source(
            config,
            5,
            text_fetcher=text_fetcher,
            rendered_text_fetcher=rendered_text_fetcher,
            rendered_article_fetcher=lambda _c, timeout=0: None,
        )
        self.assertTrue(source.rendered)
        self.assertEqual(calls["url"], LOGIN_URL)
        self.assertIs(calls["config"], config)

    def test_service_defaults_use_login_aware_fetchers(self) -> None:
        service = ScrapeService()
        self.assertIs(service.rendered_text_fetcher, browser_client.fetch_rendered_text)
        self.assertIs(
            service.rendered_article_fetcher,
            browser_client.fetch_rendered_article_record,
        )


if __name__ == "__main__":
    unittest.main()
