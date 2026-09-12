"""Offline tests for the batch-scoped browser renderer pool (stage 2, revised).

Lifecycle proofs use real owner-thread renderers with a fake Playwright (not an
empty FakeRenderer counter), per review.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from dawei.application import batch_service
from dawei.application.batch_service import BatchOptions, SingleIssueBatchService
from dawei.application.scrape_service import ScrapeService
from dawei.cli import single_issue
from dawei.domain.errors import ScrapeError
from dawei.domain.models import ParsedRecord, SiteConfig
from dawei.infrastructure import browser_client

NUMBERS = tuple(f"{number:02d}" for number in range(1, 37))


class _StubRenderer:
    instances: ClassVar[list[_StubRenderer]] = []

    def __init__(self, _starter) -> None:
        self.closed = 0
        type(self).instances.append(self)

    def fetch(self, url: str, timeout: int = 1, timing=None) -> str:
        return f"text:{url}"

    def fetch_article(self, config: SiteConfig, timeout: int = 1, timing=None) -> str:
        return f"article:{config.url}"

    def close_all(self) -> None:
        self.closed += 1


def _reset() -> None:
    _StubRenderer.instances = []
    browser_client.close_browser_renderer()


def _site(name: str = "站点", url: str = "https://a.test/topic") -> SiteConfig:
    return SiteConfig(name=name, url=url, site_id="site_id", region="top")


def _options(root: Path, **overrides) -> BatchOptions:
    values: dict[str, object] = {
        "fixed_issue": 236,
        "output_path": root / "success.txt",
        "error_output_path": root / "failure.txt",
        "recent_cache_path": root / "recent_10_cache.json",
        "update_recent_cache": False,
        "workers": 1,
    }
    values.update(overrides)
    return BatchOptions(**values)


class PoolConstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset()

    def test_default_browser_workers_is_one(self) -> None:
        options = BatchOptions(
            fixed_issue=1,
            output_path=Path("success.txt"),
            error_output_path=Path("failure.txt"),
        )
        self.assertEqual(options.browser_workers, 1)

    def test_rejects_invalid_size(self) -> None:
        for size in (0, 3, -1, True):
            with self.subTest(size=size), self.assertRaises(ScrapeError):
                browser_client.BrowserRendererPool(size, lambda: object())

    def test_normalize_origin(self) -> None:
        cases = {
            "https://user:pass@Example.TEST:443/a": "https://example.test:443",
            "https://example.test/a": "https://example.test:443",
            "http://example.test:80/a": "http://example.test:80",
            "http://example.test/a": "http://example.test:80",
            "https://example.test:8443/a": "https://example.test:8443",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(browser_client.normalize_origin(url), expected)
        self.assertIsNone(browser_client.normalize_origin("not-a-url"))
        self.assertIsNone(browser_client.normalize_origin(None))

    def test_same_origin_affinity_and_explicit_grouping(self) -> None:
        with mock.patch.object(browser_client, "ReusableBrowserRenderer", _StubRenderer):
            pool = browser_client.BrowserRendererPool(2, lambda: object())
            pool.fetch("https://a.test/one", 1)
            pool.fetch("https://a.test/two", 1)
            self.assertEqual(len(_StubRenderer.instances), 1)
            pool.fetch("https://b.test/one", 1)
            self.assertEqual(len(_StubRenderer.instances), 2)
            pool.fetch("https://c.test/one", 1)
            self.assertEqual(len(_StubRenderer.instances), 2)

            grouped = browser_client.BrowserRendererPool(2, lambda: object())
            grouped.fetch("https://x.test/one", 1, affinity_key="group-1")
            grouped.fetch("https://y.test/one", 1, affinity_key="group-1")
            self.assertEqual(len(_StubRenderer.instances), 3)
            grouped.fetch("https://x.test/one", 1, affinity_key="group-2")
            self.assertEqual(len(_StubRenderer.instances), 4)


class LegacyGlobalIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset()

    def test_batch_pool_does_not_touch_legacy_global_renderer(self) -> None:
        with mock.patch.object(browser_client, "ReusableBrowserRenderer", _StubRenderer):
            legacy = browser_client._get_renderer(lambda: object())
            pool = browser_client.BrowserRendererPool(2, lambda: object())
            pool.fetch("https://a.test", 1)
            pool.fetch("https://b.test", 1)
            pool.close_all()
            self.assertIs(browser_client._get_renderer(lambda: object()), legacy)
            self.assertEqual(legacy.closed, 0)
            browser_client.close_browser_renderer()
            self.assertEqual(legacy.closed, 1)


class DefaultScraperInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset()

    def test_injects_both_browser_channels_when_pool_given(self) -> None:
        captured: dict[str, object] = {}

        class FakeService:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)

        pool = mock.Mock()
        pool.fetch.return_value = "text"
        pool.fetch_article.return_value = "article"
        with mock.patch.object(batch_service, "ScrapeService", FakeService):
            SingleIssueBatchService()._default_scraper(_options(Path(".")), pool)
            text_fetcher = captured["rendered_text_fetcher"]
            article_fetcher = captured["rendered_article_fetcher"]
            self.assertEqual(text_fetcher("https://x.test/a", 5), "text")
            article_fetcher(_site(), 5)
        pool.fetch.assert_called_once_with("https://x.test/a", 5)
        pool.fetch_article.assert_called_once()

    def test_no_browser_channels_without_pool(self) -> None:
        captured: dict[str, object] = {}

        class FakeService:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)

        with mock.patch.object(batch_service, "ScrapeService", FakeService):
            SingleIssueBatchService()._default_scraper(_options(Path(".")))
        self.assertNotIn("rendered_text_fetcher", captured)
        self.assertNotIn("rendered_article_fetcher", captured)

    def test_http_only_scrape_does_not_start_playwright(self) -> None:
        starters: list[int] = []

        def starter() -> object:
            starters.append(1)
            return object()

        document = "\n".join(
            [
                "253期 36码",
                ".".join(f"{n:02d}" for n in range(1, 13)),
                ".".join(f"{n:02d}" for n in range(13, 25)),
                ".".join(f"{n:02d}" for n in range(25, 37)),
            ]
        )
        config = SiteConfig(
            "HTTP站",
            "https://http.test/topic",
            keywords=("36码",),
            region="top",
        )
        pool = browser_client.BrowserRendererPool(2, starter)
        service = ScrapeService(
            text_fetcher=lambda url, timeout: document,
            rendered_text_fetcher=pool.fetch,
            rendered_article_fetcher=pool.fetch_article,
        )
        result = service.scrape(config, timeout=1, fixed_issue=253)
        self.assertEqual(result.issue, 253)
        self.assertEqual(starters, [])


class _FakePlaywrightRuntime:
    def __init__(self) -> None:
        self.events: list[tuple[str, int]] = []
        self.lock = threading.Lock()

    def record(self, name: str) -> None:
        with self.lock:
            self.events.append((name, threading.get_ident()))

    def playwright_class(self):
        runtime = self

        class FakeBody:
            def inner_text(self, timeout: int) -> str:
                return "<html>正文</html>"

        class FakePage:
            def goto(self, *_args, **_kwargs) -> None:
                runtime.record("page.goto")

            def locator(self, _selector: str) -> FakeBody:
                return FakeBody()

            def wait_for_load_state(self, *_args, **_kwargs) -> None:
                runtime.record("page.wait")

            def content(self) -> str:
                return "<html>正文</html>"

            def close(self) -> None:
                runtime.record("page.close")

        class FakeContext:
            def route(self, *_args, **_kwargs) -> None:
                return None

            def new_page(self) -> FakePage:
                return FakePage()

            def close(self) -> None:
                runtime.record("context.close")

        class FakeBrowser:
            def new_context(self, **_kwargs) -> FakeContext:
                return FakeContext()

            def close(self) -> None:
                runtime.record("browser.close")

        class FakeChromium:
            def launch(self, **_kwargs) -> FakeBrowser:
                return FakeBrowser()

        class FakePlaywright:
            chromium = FakeChromium()

            def stop(self) -> None:
                runtime.record("playwright.stop")

        return FakePlaywright


class TwoOwnerThreadTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset()

    def test_two_owner_threads_created_used_and_closed(self) -> None:
        runtime = _FakePlaywrightRuntime()
        starter_threads: list[int] = []

        def starter() -> object:
            starter_threads.append(threading.get_ident())
            return runtime.playwright_class()()

        pool = browser_client.BrowserRendererPool(2, starter)
        self.assertEqual(pool.fetch("https://a.test/x", 1), "<html>正文</html>")
        self.assertEqual(pool.fetch("https://b.test/y", 1), "<html>正文</html>")

        self.assertEqual(len(starter_threads), 2)
        owner_threads = {tid for name, tid in runtime.events if name == "page.goto"}
        self.assertEqual(len(owner_threads), 2)
        self.assertTrue(owner_threads.issubset(set(starter_threads)))

        pool.close_all()
        names = [name for name, _ in runtime.events]
        self.assertEqual(names.count("context.close"), 2)
        self.assertEqual(names.count("browser.close"), 2)
        self.assertEqual(names.count("playwright.stop"), 2)


class BatchPoolLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset()

    def test_run_does_not_close_pool_until_futures_complete(self) -> None:
        started = threading.Event()
        release = threading.Event()
        captured: dict[str, object] = {}

        class BlockingRenderer(_StubRenderer):
            def fetch(self, url: str, timeout: int = 1, timing=None) -> str:
                started.set()
                release.wait(5)
                return "正文"

        def fake_default(self, options, browser_pool=None):
            captured["pool"] = browser_pool

            def scrape(config, *, timeout, fixed_issue):
                if config.name == "慢站":
                    browser_pool.fetch(config.url, timeout)
                return ParsedRecord(config.name, config.url, 236, NUMBERS)

            return scrape

        sites = (_site("慢站", "https://slow.test"), _site("快站", "https://fast.test"))
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(browser_client, "ReusableBrowserRenderer", BlockingRenderer),
            mock.patch.object(SingleIssueBatchService, "_default_scraper", fake_default),
        ):
            root = Path(directory)
            holder: dict[str, object] = {}

            def run() -> None:
                holder["result"] = SingleIssueBatchService(
                    progress_sink=lambda _: None
                ).run(sites, _options(root, workers=2))

            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                self.assertFalse(captured["pool"]._closed)
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(captured["pool"]._closed)
            self.assertEqual(holder["result"].total_sites, 2)

    def test_concurrent_batches_do_not_close_each_other(self) -> None:
        started = {"A": threading.Event(), "B": threading.Event()}
        release = {"A": threading.Event(), "B": threading.Event()}
        pools: dict[int, object] = {}
        lock = threading.Lock()

        def fake_default(self, options, browser_pool=None):
            with lock:
                pools[threading.get_ident()] = browser_pool

            def scrape(config, *, timeout, fixed_issue):
                started[config.name].set()
                release[config.name].wait(5)
                return ParsedRecord(config.name, config.url, 236, NUMBERS)

            return scrape

        def run_batch(name: str) -> None:
            with tempfile.TemporaryDirectory() as directory:
                SingleIssueBatchService(progress_sink=lambda _: None).run(
                    (_site(name, f"https://{name.lower()}.test"),),
                    _options(Path(directory), workers=1),
                )

        with mock.patch.object(SingleIssueBatchService, "_default_scraper", fake_default):
            thread_a = threading.Thread(target=run_batch, args=("A",))
            thread_b = threading.Thread(target=run_batch, args=("B",))
            thread_a.start()
            thread_b.start()
            try:
                self.assertTrue(started["A"].wait(3))
                self.assertTrue(started["B"].wait(3))
                pool_a = pools[thread_a.ident]
                pool_b = pools[thread_b.ident]
                self.assertIsNot(pool_a, pool_b)
                self.assertFalse(pool_a._closed)
                self.assertFalse(pool_b._closed)
                release["A"].set()
                thread_a.join(5)
                self.assertTrue(pool_a._closed)
                self.assertFalse(pool_b._closed)
                release["B"].set()
                thread_b.join(5)
                self.assertTrue(pool_b._closed)
            finally:
                release["A"].set()
                release["B"].set()

    def test_pool_closed_when_default_scraper_construction_fails(self) -> None:
        captured: dict[str, object] = {}

        def fake_default(self, options, browser_pool=None):
            captured["pool"] = browser_pool
            raise RuntimeError("build failed")

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(SingleIssueBatchService, "_default_scraper", fake_default),
            self.assertRaises(RuntimeError),
        ):
            SingleIssueBatchService(progress_sink=lambda _: None).run(
                (_site(),), _options(Path(directory))
            )
        self.assertTrue(captured["pool"]._closed)

    def test_injected_scraper_does_not_build_pool(self) -> None:
        built: list[int] = []
        original_init = browser_client.BrowserRendererPool.__init__

        def spy_init(self, size, starter):
            built.append(1)
            original_init(self, size, starter)

        def scrape(config, *, timeout, fixed_issue):
            return ParsedRecord(config.name, config.url, 236, NUMBERS)

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(browser_client.BrowserRendererPool, "__init__", spy_init),
        ):
            SingleIssueBatchService(
                site_scraper=scrape,
                progress_sink=lambda _: None,
            ).run((_site(),), _options(Path(directory)))
        self.assertEqual(built, [])


class BrowserWorkersValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset()

    def _run_with(self, value: object) -> None:
        def scrape(config, *, timeout, fixed_issue):
            return ParsedRecord(config.name, config.url, 236, NUMBERS)

        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ScrapeError):
            SingleIssueBatchService(
                site_scraper=scrape,
                progress_sink=lambda _: None,
            ).run((_site(),), _options(Path(directory), browser_workers=value))

    def test_rejects_invalid(self) -> None:
        for value in (0, 3, -1, True):
            with self.subTest(value=value):
                self._run_with(value)

    def test_accepts_one_and_two(self) -> None:
        for value in (1, 2):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                result = SingleIssueBatchService(
                    site_scraper=lambda config, *, timeout, fixed_issue: ParsedRecord(
                        config.name, config.url, 236, NUMBERS
                    ),
                    progress_sink=lambda _: None,
                ).run((_site(),), _options(Path(directory), browser_workers=value))
                self.assertEqual(result.total_sites, 1)


class BrowserWorkersCliTests(unittest.TestCase):
    def test_default_is_one(self) -> None:
        args = single_issue.parse_args(["--fixed-issue", "236"])
        self.assertEqual(args.browser_workers, 1)

    def test_accepts_one_and_two(self) -> None:
        for value in ("1", "2"):
            with self.subTest(value=value):
                args = single_issue.parse_args(["--fixed-issue", "236", "--browser-workers", value])
                self.assertEqual(args.browser_workers, int(value))

    def test_rejects_three(self) -> None:
        with self.assertRaises(SystemExit):
            single_issue.parse_args(["--fixed-issue", "236", "--browser-workers", "3"])


if __name__ == "__main__":
    unittest.main()
