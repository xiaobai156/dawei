"""Offline tests for optional audit timing JSON (default off)."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from dawei.application.batch_service import BatchOptions, SingleIssueBatchService
from dawei.application.scrape_service import ScrapeService
from dawei.cli import single_issue
from dawei.domain.errors import ScrapeError
from dawei.domain.models import ParsedRecord, SiteConfig
from dawei.infrastructure import browser_client, http_client, image_client
from dawei.infrastructure.browser_client import (
    BrowserRenderState,
    ReusableBrowserRenderer,
)
from dawei.infrastructure.timing import AuditTiming, timing_scope

NUMBERS = tuple(f"{number:02d}" for number in range(1, 37))


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


class AuditTimingRecorderTests(unittest.TestCase):
    def test_snapshot_is_thread_safe_and_separates_kinds(self) -> None:
        recorder = AuditTiming()

        def worker() -> None:
            for _ in range(50):
                recorder.add("parse", 0.01)
                recorder.incr("http.attempts")

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        snapshot = recorder.snapshot()
        self.assertAlmostEqual(snapshot["durations"]["parse"], 2.0, places=6)
        self.assertEqual(snapshot["counters"]["http.attempts"], 200)


class HttpTimingInstrumentationTests(unittest.TestCase):
    def test_fetch_raw_records_attempts_and_bytes(self) -> None:
        recorder = AuditTiming()

        class Headers(dict):
            def get_content_charset(self):
                return "utf-8"

        class Response:
            def __init__(self) -> None:
                self.headers = Headers()

            def read(self) -> bytes:
                return b"0123456789"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with timing_scope(recorder):
            http_client.fetch_raw("https://timed.test", open_url=lambda *_a, **_k: Response())
        snapshot = recorder.snapshot()
        self.assertEqual(snapshot["counters"]["http.attempts"], 1)
        self.assertEqual(snapshot["counters"]["http.bytes"], 10)
        self.assertIn("http.attempt", snapshot["durations"])

    def test_fetch_raw_records_curl_fallback_bytes(self) -> None:
        recorder = AuditTiming()

        def curl(url, timeout, extra_headers=None):
            return b"curldata", "utf-8", ""

        with timing_scope(recorder):
            data, _, _ = http_client.fetch_raw(
                "https://curl.test",
                timeout=1,
                open_url=mock.Mock(side_effect=ScrapeError("network down")),
                curl_fetcher=curl,
                network_attempts=1,
            )
        self.assertEqual(data, b"curldata")
        snapshot = recorder.snapshot()
        self.assertEqual(snapshot["counters"]["http.bytes"], 8)
        self.assertIn("http.curl", snapshot["durations"])


class ParseTimingInstrumentationTests(unittest.TestCase):
    def test_execute_records_parse_stage(self) -> None:
        document = "\n".join(
            [
                "253期 36码",
                ".".join(f"{n:02d}" for n in range(1, 13)),
                ".".join(f"{n:02d}" for n in range(13, 25)),
                ".".join(f"{n:02d}" for n in range(25, 37)),
            ]
        )
        config = SiteConfig("解析站", "https://parse.test", keywords=("36码",), region="top")
        recorder = AuditTiming()
        result = ScrapeService(text_fetcher=lambda _url, _timeout: document).execute(
            config, timeout=1, fixed_issue=253, timing=recorder
        )
        self.assertEqual(result.result.issue, 253)
        snapshot = recorder.snapshot()
        self.assertIn("parse", snapshot["durations"])
        self.assertGreaterEqual(snapshot["durations"]["parse"], 0.0)


class BrowserTimingInstrumentationTests(unittest.TestCase):
    def test_renderer_records_queue_navigation_wait_and_read(self) -> None:
        recorder = AuditTiming()

        class FakeBody:
            def inner_text(self, timeout: int) -> str:
                return "正文"

        class FakePage:
            def goto(self, *_args, **_kwargs) -> None:
                return None

            def locator(self, _selector: str) -> FakeBody:
                return FakeBody()

            def wait_for_load_state(self, *_args, **_kwargs) -> None:
                return None

            def content(self) -> str:
                return "<html>正文</html>"

            def close(self) -> None:
                return None

        class FakeContext:
            def route(self, *_args, **_kwargs) -> None:
                return None

            def new_page(self) -> FakePage:
                return FakePage()

            def close(self) -> None:
                return None

        context = FakeContext()
        renderer = ReusableBrowserRenderer(lambda: object())
        renderer.local.state = BrowserRenderState(object(), object(), context)
        renderer.fetch("https://browser.test", timeout=1, timing=recorder)
        durations = recorder.snapshot()["durations"]
        for stage in ("browser.queue", "browser.nav", "browser.wait", "browser.read"):
            self.assertIn(stage, durations)


class OcrTimingInstrumentationTests(unittest.TestCase):
    def test_ocr_records_stage(self) -> None:
        class FakeEngine:
            def predict(self, _path):
                return [
                    {
                        "rec_texts": ["01"],
                        "rec_scores": [0.9],
                        "rec_boxes": [[0, 0, 1, 1]],
                    }
                ]

        recorder = AuditTiming()
        with (
            mock.patch.object(image_client, "_engine", return_value=FakeEngine()),
            timing_scope(recorder),
        ):
            image_client.ocr_image(b"image")
        self.assertIn("ocr", recorder.snapshot()["durations"])


class BatchTimingJsonTests(unittest.TestCase):
    def _failed_scraper(self, config, *, timeout, fixed_issue):
        return ParsedRecord(config.name, config.url, 236, NUMBERS)

    def test_default_off_writes_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            SingleIssueBatchService(
                site_scraper=self._failed_scraper,
                progress_sink=lambda _: None,
            ).run((_site(),), _options(root))
            self.assertFalse((root / "timing.json").exists())

    def test_report_written_with_keys_without_response_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timing_path = root / "timing.json"
            SingleIssueBatchService(
                site_scraper=self._failed_scraper,
                progress_sink=lambda _: None,
            ).run((_site(),), _options(root, timing_json_path=timing_path))
            self.assertTrue(timing_path.exists())
            payload = json.loads(timing_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(payload["issue"], 236)
            self.assertEqual(len(payload["sites"]), 1)
            self.assertIn("total_seconds", payload["batch"])
            self.assertIn("write_txt_seconds", payload["batch"])
            self.assertIn("cache_seconds", payload["batch"])
            self.assertIn("close_seconds", payload["batch"])
            for site in payload["sites"]:
                self.assertIn("queue_seconds", site)
                self.assertIn("total_seconds", site)
                self.assertIn("durations", site)
                self.assertIn("counters", site)
            self.assertNotIn("<html", timing_path.read_text(encoding="utf-8-sig"))

    def test_report_written_when_run_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timing_path = root / "timing.json"
            with (
                mock.patch(
                    "dawei.application.batch_service.write_results",
                    side_effect=RuntimeError("boom"),
                ),
                self.assertRaises(RuntimeError),
            ):
                SingleIssueBatchService(
                    site_scraper=self._failed_scraper,
                    progress_sink=lambda _: None,
                ).run((_site(),), _options(root, timing_json_path=timing_path))
            self.assertTrue(timing_path.exists())

    def test_report_written_when_pool_close_fails_and_error_propagates(self) -> None:
        def fake_default(self, options, browser_pool=None):
            def scrape(config, *, timeout, fixed_issue):
                return ParsedRecord(config.name, config.url, 236, NUMBERS)

            return scrape

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timing_path = root / "timing.json"
            with (
                mock.patch.object(SingleIssueBatchService, "_default_scraper", fake_default),
                mock.patch.object(
                    browser_client.BrowserRendererPool,
                    "close_all",
                    side_effect=ScrapeError("close boom"),
                ),
                self.assertRaisesRegex(ScrapeError, "close boom"),
            ):
                SingleIssueBatchService(progress_sink=lambda _: None).run(
                    (_site(),), _options(root, timing_json_path=timing_path)
                )
            self.assertTrue(timing_path.exists())


class TimingCliTests(unittest.TestCase):
    def test_timing_json_flag_parsed(self) -> None:
        args = single_issue.parse_args(["--fixed-issue", "236", "--timing-json", "audit/run.json"])
        self.assertEqual(args.timing_json, "audit/run.json")

    def test_timing_json_defaults_to_none(self) -> None:
        args = single_issue.parse_args(["--fixed-issue", "236"])
        self.assertIsNone(args.timing_json)


if __name__ == "__main__":
    unittest.main()
