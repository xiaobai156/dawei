import json
import tempfile
import unittest
from http.client import IncompleteRead
from pathlib import Path
from unittest import mock

from dawei.application.batch_service import BatchOptions, SingleIssueBatchService
from dawei.infrastructure.cache_repository import CacheRepository
from tests.support.evidence_factory import record as evidence_record
from tests.support import scrape_regression_api as scraper


class StrictExtractionTests(unittest.TestCase):
    def test_name_only_section_anchor_without_nearby_strict_keyword_is_ignored(self):
        config = scraper.SiteConfig(
            "树大招风",
            "https://example.test/topic.html",
            ("36码中特",),
            ("树大招风", "36码中特"),
        )
        lines = ["树大招风 发布"] + ["无关内容"] * 20 + ["130期：36码中特 开0000准"]

        ranges = scraper.section_ranges(lines, config)

        self.assertNotIn(0, [item.start for item in ranges])

    def test_name_section_anchor_with_nearby_strict_keyword_is_rejected(self):
        config = scraper.SiteConfig(
            "树大招风",
            "https://example.test/topic.html",
            ("36码中特",),
            ("树大招风", "36码中特"),
        )
        lines = ["树大招风 发布", "130期：36码中特 开0000准"]

        ranges = scraper.section_ranges(lines, config)

        self.assertNotIn(0, [item.start for item in ranges])

    def test_section_anchor_requires_all_section_keywords_on_same_line(self):
        config = scraper.SiteConfig(
            "树大招风",
            "https://example.test/topic.html",
            ("36码中特",),
            ("树大招风", "36码中特"),
        )
        lines = ["树大招风 36码中特", "130期：36码中特 开0000准"]

        ranges = scraper.section_ranges(lines, config)

        self.assertIn(0, [item.start for item in ranges])

    def test_candidate_context_includes_strict_keyword_below_issue(self):
        lines = [
            "内幕快报【内幕36码】933347a.com",
            "130期",
            "开: * **",
            "【特围36码】",
        ]

        context = scraper.candidate_context(lines, 1)

        self.assertIn("特围36码", context)

    def test_candidate_requires_all_configured_keywords(self):
        numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        html = "\n".join(
            [
                "测试站 三十六码",
                "166期 测试站 三十六码 开00准",
                numbers,
            ]
        )
        config = scraper.SiteConfig(
            "测试站",
            "https://example.test",
            ("三十六码", "精准36码"),
            ("测试站", "三十六码"),
            fixed_issue=166,
        )

        with self.assertRaisesRegex(scraper.ScrapeError, "没有找到符合关键词"):
            scraper.extract_latest_36(html, config)

    def test_windows_errno_2_during_https_open_is_tls_retryable(self):
        error = scraper.URLError(FileNotFoundError(2, "No such file or directory"))

        self.assertTrue(scraper.is_tls_error(error))

    def test_curl_schannel_exit_35_is_tls_retryable(self):
        error = RuntimeError(
            "curl exit 35: curl: (35) schannel: failed to receive handshake, SSL/TLS connection failed"
        )

        self.assertTrue(scraper.is_tls_error(error))
        self.assertEqual(scraper.classify_failure(error), "TLS失败")

    def test_dns_polluted_resolution_is_reported_as_line_failure(self):
        original_getaddrinfo = scraper.socket.getaddrinfo

        def fake_getaddrinfo(*args, **kwargs):
            return [(None, None, None, None, ("198.18.1.9", 443))]

        try:
            scraper.socket.getaddrinfo = fake_getaddrinfo
            message = scraper.resolution_diagnostic("https://33398999.com/read.php?tid=20160")
        finally:
            scraper.socket.getaddrinfo = original_getaddrinfo

        self.assertIn("198.18.1.9", message)
        self.assertEqual(scraper.classify_failure(scraper.ScrapeError(message)), "DNS/线路失败")

    def test_failure_classification_keeps_specific_reason(self):
        self.assertEqual(
            scraper.classify_failure(scraper.ScrapeError("未找到栏目关键词: 六彩宝箱")),
            "栏目/关键词解析失败",
        )
        self.assertEqual(
            scraper.classify_failure(scraper.ScrapeError("164期数据不是完整36码")),
            "36码数量失败",
        )
        self.assertEqual(
            scraper.classify_failure(RuntimeError("unexpected error: boom")),
            "程序异常",
        )

    def test_format_failure_never_outputs_bare_unknown_failure(self):
        site = scraper.SiteConfig("测试站", "https://example.test", ())

        line = scraper.format_failure(site, RuntimeError())

        self.assertNotIn("[未知失败]", line)
        self.assertIn("[未分类失败/RuntimeError]", line)
        self.assertIn("RuntimeError 无详细异常消息", line)

    def test_failure_txt_separates_each_site_with_one_blank_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "failures.txt"

            scraper.write_failures(("站点A 失败原因A", "站点B 失败原因B"), output_path)

            self.assertEqual(
                output_path.read_text(encoding="utf-8-sig"),
                "站点A 失败原因A\n\n站点B 失败原因B\n",
            )

    def test_bad_gateway_is_retryable_http(self):
        self.assertTrue(scraper.is_retryable_http_code(502))
        self.assertTrue(scraper.should_try_curl_fallback(scraper.ScrapeError("HTTP 502")))

    def test_document_writeln_chunks_are_expanded(self):
        html = 'document.writeln("<div>150期无错36码【36码中特】开？00准<br>08.09</div>");'

        expanded = scraper.decode_document_writeln_chunks(html)

        self.assertIn("150期无错36码", expanded)
        self.assertIn("08.09", expanded)

    def test_post_json_uses_curl_fallback_after_network_error(self):
        original_open = scraper.open_url_with_retries
        original_curl = scraper.post_json_with_curl
        original_attempts = scraper.DEFAULT_NETWORK_ATTEMPTS
        original_proxy_retries = scraper.PROXY_RETRIES

        def fail_open(*args, **kwargs):
            raise scraper.ScrapeError("network error: test")

        def fake_curl(url, data, timeout=20):
            self.assertEqual(url, "https://example.test/api")
            self.assertIn(b'"type": "am"', data)
            return b'{"ok": true}'

        try:
            scraper.open_url_with_retries = fail_open
            scraper.post_json_with_curl = fake_curl
            scraper.DEFAULT_NETWORK_ATTEMPTS = 1
            scraper.PROXY_RETRIES = 1

            result = scraper.post_json("https://example.test/api", {"type": "am"}, timeout=1)
        finally:
            scraper.open_url_with_retries = original_open
            scraper.post_json_with_curl = original_curl
            scraper.DEFAULT_NETWORK_ATTEMPTS = original_attempts
            scraper.PROXY_RETRIES = original_proxy_retries

        self.assertEqual(result, {"ok": True})

    def test_text_fetch_cache_reuses_same_url_response(self):
        calls = []

        def fake_fetch(url, timeout):
            calls.append((url, timeout))
            return "150期 测试 三十六码 开00准\n" + " ".join(f"{number:02d}" for number in range(1, 37))

        cache = scraper.TextFetchCache(fake_fetch)

        first = cache.fetch("https://example.test/a", 20)
        second = cache.fetch("https://example.test/a", 20)

        self.assertEqual(first, second)
        self.assertEqual(calls, [("https://example.test/a", 20)])

    def test_browser_renderer_reuses_browser_in_same_thread(self):
        launches = []

        class FakeLocator:
            def inner_text(self, timeout):
                return "rendered"

        class FakePage:
            def goto(self, *args, **kwargs):
                pass

            def wait_for_load_state(self, *args, **kwargs):
                pass

            def wait_for_timeout(self, *args, **kwargs):
                pass

            def locator(self, selector):
                return FakeLocator()

        class FakeBrowser:
            def new_page(self, **kwargs):
                return FakePage()

            def close(self):
                pass

        class FakeChromium:
            def launch(self, **kwargs):
                launches.append(kwargs)
                return FakeBrowser()

        class FakePlaywright:
            chromium = FakeChromium()

            def stop(self):
                pass

        renderer = scraper.ReusableBrowserRenderer(lambda: FakePlaywright())

        try:
            self.assertEqual(renderer.fetch("https://example.test/a", 20), "rendered")
            self.assertEqual(renderer.fetch("https://example.test/b", 20), "rendered")
        finally:
            renderer.close_all()

        self.assertEqual(len(launches), 1)

    def test_browser_renderer_returns_early_when_expected_data_is_visible(self):
        waits = []
        ready_text = "159期 测试站 三十六码 " + " ".join(f"{number:02d}" for number in range(1, 37))

        class FakeLocator:
            def inner_text(self, timeout):
                return ready_text

        class FakePage:
            def goto(self, *args, **kwargs):
                pass

            def wait_for_load_state(self, *args, **kwargs):
                pass

            def wait_for_timeout(self, timeout):
                waits.append(timeout)

            def locator(self, selector):
                return FakeLocator()

        class FakeBrowser:
            def new_page(self, **kwargs):
                return FakePage()

            def close(self):
                pass

        class FakePlaywright:
            class chromium:
                @staticmethod
                def launch(**kwargs):
                    return FakeBrowser()

            def stop(self):
                pass

        renderer = scraper.ReusableBrowserRenderer(lambda: FakePlaywright())
        try:
            text = renderer.fetch(
                "https://example.test/a",
                20,
                expected_issue=159,
                expected_keywords=("测试站", "三十六码"),
            )
        finally:
            renderer.close_all()

        self.assertIn("159期", text)
        self.assertEqual(waits, [])

    def test_browser_renderer_keeps_full_wait_when_expected_data_is_missing(self):
        waits = []

        class FakeLocator:
            def inner_text(self, timeout):
                return "页面还没加载出目标数据"

        class FakePage:
            def goto(self, *args, **kwargs):
                pass

            def wait_for_load_state(self, *args, **kwargs):
                pass

            def wait_for_timeout(self, timeout):
                waits.append(timeout)

            def locator(self, selector):
                return FakeLocator()

        class FakeBrowser:
            def new_page(self, **kwargs):
                return FakePage()

            def close(self):
                pass

        class FakePlaywright:
            class chromium:
                @staticmethod
                def launch(**kwargs):
                    return FakeBrowser()

            def stop(self):
                pass

        renderer = scraper.ReusableBrowserRenderer(lambda: FakePlaywright())
        try:
            renderer.fetch(
                "https://example.test/a",
                20,
                expected_issue=159,
                expected_keywords=("测试站", "三十六码"),
            )
        finally:
            renderer.close_all()

        self.assertEqual(waits, [3000])

    def test_incremental_backup_keeps_latest_ten_issues(self):
        def make_result(issue):
            return scraper.SiteResult(
                "测试站",
                "https://example.test",
                issue,
                tuple(f"{number:02d}" for number in range(1, 37)),
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / "backup.json"
            scraper.update_recent_duplicate_backup(
                [make_result(issue) for issue in range(149, 159)],
                (),
                backup_path,
                fixed_issue=158,
            )
            scraper.update_recent_duplicate_backup(
                [make_result(159)],
                (),
                backup_path,
                fixed_issue=159,
            )
            payload = json.loads(backup_path.read_text(encoding="utf-8-sig"))

        records = payload["sites"][0]["records"]
        self.assertEqual([record["issue"] for record in records], list(range(159, 149, -1)))
        self.assertFalse(any(record["issue"] == 149 for record in records))
        self.assertFalse(payload["incomplete"])

    def test_incremental_backup_records_failures(self):
        result = scraper.SiteResult(
            "测试站",
            "https://example.test",
            159,
            tuple(f"{number:02d}" for number in range(1, 37)),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / "backup.json"
            scraper.update_recent_duplicate_backup([result], ("坏站 失败",), backup_path, fixed_issue=159)
            payload = json.loads(backup_path.read_text(encoding="utf-8-sig"))

        self.assertTrue(payload["incomplete"])
        self.assertEqual(payload["failures"], ["坏站 失败"])

    def test_incremental_backup_refuses_to_roll_back_latest_period(self):
        def make_result(issue):
            return scraper.SiteResult(
                "测试站",
                "https://example.test",
                issue,
                tuple(f"{number:02d}" for number in range(1, 37)),
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / "backup.json"
            scraper.update_recent_duplicate_backup([make_result(190)], (), backup_path, fixed_issue=190)
            scraper.update_recent_duplicate_backup([make_result(170)], (), backup_path, fixed_issue=170)
            payload = json.loads(backup_path.read_text(encoding="utf-8-sig"))

        self.assertEqual(payload["period"], 190)
        self.assertEqual(payload["sites"][0]["records"][0]["issue"], 190)

    def test_duplicate_backup_is_written_via_atomic_replace(self):
        result = scraper.SiteResult(
            "测试站",
            "https://example.test",
            190,
            tuple(f"{number:02d}" for number in range(1, 37)),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / "backup.json"
            with mock.patch("os.replace") as replace:
                scraper.update_recent_duplicate_backup([result], (), backup_path, fixed_issue=190)

        replace.assert_called_once()
        self.assertEqual(Path(replace.call_args.args[1]), backup_path)

    def test_fetch_raw_rejects_incomplete_read_partial_body(self):
        class Headers:
            def get_content_charset(self):
                return "utf-8"

            def get(self, name, default=""):
                return default

        class PartialResponse:
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                raise IncompleteRead("partial 190期 ".encode("utf-8") + b"01 " * 36, 999)

        original_open = scraper.open_url_with_retries
        original_curl = scraper.fetch_raw_with_curl
        original_attempts = scraper.DEFAULT_NETWORK_ATTEMPTS

        try:
            scraper.DEFAULT_NETWORK_ATTEMPTS = 1
            scraper.open_url_with_retries = lambda request, timeout: PartialResponse()
            scraper.fetch_raw_with_curl = lambda *args, **kwargs: (_ for _ in ()).throw(
                scraper.ScrapeError("curl unavailable")
            )

            with self.assertRaisesRegex(scraper.ScrapeError, "incomplete read"):
                scraper.fetch_raw("https://example.test", timeout=1)
        finally:
            scraper.open_url_with_retries = original_open
            scraper.fetch_raw_with_curl = original_curl
            scraper.DEFAULT_NETWORK_ATTEMPTS = original_attempts

    def test_dynamic_api_200_empty_shell_uses_rendered_page_fallback(self):
        original_fetch_rendered = scraper.fetch_rendered_article_record
        original_extract = scraper.extract_latest_36
        rendered_calls = []
        config = scraper.SiteConfig(
            "测试站",
            "https://example.test/article/admin/abc?url=x",
            ("三十六码",),
            ("测试站",),
            api_url="https://example.test/api",
        )

        def fake_fetch_rendered(config, timeout=20, expected_issue=None, expected_keywords=(), force_full_wait=False):
            rendered_calls.append((config.url, expected_issue, tuple(expected_keywords), force_full_wait))
            body = "测试站 190期 三十六码\n" + " ".join(f"{number:02d}" for number in range(1, 37))
            return scraper.ArticleRecord("abc", "$.data", "190期：三十六码", "测试站", body, body)

        def fake_extract(text, extract_config):
            if "empty shell" in text:
                raise scraper.ScrapeError("未找到栏目关键词: 测试站")
            return evidence_record(
                "测试站",
                "https://example.test/article/admin/abc?url=x",
                190,
                tuple(f"{number:02d}" for number in range(1, 37)),
            )

        try:
            scraper.fetch_rendered_article_record = fake_fetch_rendered
            scraper.extract_latest_36 = fake_extract

            result = scraper.scrape_site(
                config,
                fixed_issue=190,
                text_fetcher=lambda url, timeout: json.dumps({"data": []}),
            )
        finally:
            scraper.fetch_rendered_article_record = original_fetch_rendered
            scraper.extract_latest_36 = original_extract

        self.assertEqual(result.issue, 190)
        self.assertEqual(len(rendered_calls), 1)

    def test_dynamic_api_content_that_fails_strict_parse_uses_rendered_page_fallback(self):
        numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        config = scraper.SiteConfig(
            "测试站",
            "https://example.test/article/admin/abc?url=x",
            ("三十六码",),
            ("测试站",),
            api_url="https://example.test/api",
        )

        def fetch_rendered(config, timeout=20, expected_issue=None, expected_keywords=(), force_full_wait=False):
            body = "\n".join(["测试站 三十六码", "190期 测试站 三十六码 开0000准", numbers])
            return scraper.ArticleRecord("abc", "$.data", "190期：三十六码", "测试站", body, body)

        api_payload = json.dumps({"data": [{"content": f"测试站 190期\n{numbers}"}]}, ensure_ascii=False)
        with mock.patch.object(scraper, "fetch_rendered_article_record", side_effect=fetch_rendered) as rendered:
            result = scraper.scrape_site(config, fixed_issue=190, text_fetcher=lambda url, timeout: api_payload)

        rendered.assert_called()
        self.assertEqual(result.issue, 190)
        self.assertEqual(result.numbers, tuple(f"{number:02d}" for number in range(1, 37)))

    def test_main_returns_failure_exit_code_when_any_site_fails(self):
        site = scraper.SiteConfig("测试站", "https://example.test", ("三十六码",), ("测试站",))
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = SingleIssueBatchService(
                site_scraper=lambda *args, **kwargs: (_ for _ in ()).throw(
                    scraper.ScrapeError("boom")
                ),
                progress_sink=lambda _line: None,
            ).run(
                (site,),
                BatchOptions(
                    fixed_issue=190,
                    output_path=root / "success.txt",
                    error_output_path=root / "fail.txt",
                    update_recent_cache=False,
                ),
            )
            failure_text = (root / "fail.txt").read_text(encoding="utf-8-sig")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("测试站 https://example.test", failure_text)

    def test_main_single_issue_never_requests_adjacent_issue(self):
        site = scraper.SiteConfig("测试站", "https://example.test", ("三十六码",), ("测试站",))
        requested_issues = []
        numbers = tuple(f"{number:02d}" for number in range(1, 37))

        def fake_scrape(config, *, timeout, fixed_issue):
            requested_issues.append(fixed_issue)
            return evidence_record(site.name, site.url, fixed_issue, numbers)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache_path = root / "recent.json"
            result = SingleIssueBatchService(
                site_scraper=fake_scrape,
                progress_sink=lambda _line: None,
            ).run(
                (site,),
                BatchOptions(
                    fixed_issue=197,
                    output_path=root / "success.txt",
                    error_output_path=root / "fail.txt",
                    recent_cache_path=cache_path,
                ),
            )
            snapshot = CacheRepository(cache_path).load()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(requested_issues, [197])
        self.assertEqual(snapshot.period, 197)

    def test_load_sites_missing_config_fails_instead_of_defaulting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_path = Path(tmpdir) / "missing-sites.json"

            with self.assertRaisesRegex(scraper.ScrapeError, "sites config not found"):
                scraper.load_sites(missing_path)

    def test_validate_result_rejects_wrong_fixed_issue(self):
        result = scraper.SiteResult(
            "测试站",
            "https://example.test",
            158,
            tuple(f"{number:02d}" for number in range(1, 37)),
        )

        errors = scraper.validate_result(result, fixed_issue=159)

        self.assertIn("测试站 抓错期数: 期望159期，实际158期", errors)

    def test_render_browser_retries_full_wait_after_fast_extract_failure(self):
        original_fetch = scraper.fetch_rendered_text
        original_extract = scraper.extract_latest_36
        fetch_modes = []

        config = scraper.SiteConfig(
            "测试站",
            "https://example.test/render",
            ("三十六码",),
            ("测试站",),
            render_browser=True,
        )

        def fake_fetch(url, timeout=20, expected_issue=None, expected_keywords=(), force_full_wait=False):
            fetch_modes.append(force_full_wait)
            return "full" if force_full_wait else "quick"

        def fake_extract(text, extract_config):
            if text == "quick":
                raise scraper.ScrapeError("quick text incomplete")
            return evidence_record(
                "测试站",
                "https://example.test/render",
                159,
                tuple(f"{number:02d}" for number in range(1, 37)),
            )

        try:
            scraper.fetch_rendered_text = fake_fetch
            scraper.extract_latest_36 = fake_extract

            result = scraper.scrape_site(config, fixed_issue=159)
        finally:
            scraper.fetch_rendered_text = original_fetch
            scraper.extract_latest_36 = original_extract

        self.assertEqual(result.issue, 159)
        self.assertEqual(fetch_modes, [False, True])

    def test_fixed_issue_conflicting_high_confidence_candidates_fail(self):
        first_numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        second_numbers = " ".join(f"{number:02d}" for number in range(14, 50))
        html = "\n".join(
            [
                "测试站 三十六码",
                "159期 测试站 三十六码 开00准",
                first_numbers,
                "159期 测试站 三十六码 开00准",
                second_numbers,
            ]
        )
        config = scraper.SiteConfig(
            "测试站",
            "https://example.test",
            ("三十六码",),
            ("测试站",),
            fixed_issue=159,
        )

        with self.assertRaisesRegex(scraper.ScrapeError, "多个高可信候选.*冲突"):
            scraper.extract_latest_36(html, config)

    def test_fixed_issue_duplicate_same_candidate_data_is_allowed(self):
        numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        html = "\n".join(
            [
                "测试站 三十六码",
                "159期 测试站 三十六码 开00准",
                numbers,
                "159期 测试站 三十六码 开00准",
                numbers,
            ]
        )
        config = scraper.SiteConfig(
            "测试站",
            "https://example.test",
            ("三十六码",),
            ("测试站",),
            fixed_issue=159,
        )

        result = scraper.extract_latest_36(html, config)

        self.assertEqual(result.issue, 159)

    def test_fenfatuqiang_ignores_domain_digits_in_title(self):
        numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        html = "\n".join(
            [
                "189期:奋发图强【36码中特】933341f.com",
                "无关内容 07 08 08 39 22",
                "189期:『【36码中特】』开：0000准",
                numbers,
                "上一篇：",
            ]
        )
        config = scraper.SiteConfig(
            "奋发图强",
            "https://ymkakun.dwgml-7jbcy-ohrbrq.xyz:16677/topic/547563.html",
            ("36码中特",),
            ("奋发图强", "36码中特"),
            fixed_issue=189,
            region="bottom",
            search_window=320,
        )

        result = scraper.extract_latest_36(html, config)

        self.assertEqual(result.issue, 189)
        self.assertEqual(result.numbers, tuple(f"{number:02d}" for number in range(1, 37)))

    def test_fenfatuqiang_dedicated_candidates_skip_title_line(self):
        numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        html = "\n".join(
            [
                "189期:奋发图强【36码中特】933341f.com",
                "无关内容 07 08 08 39 22",
                "189期:『【36码中特】』开：0000准",
                numbers,
                "上一篇：",
            ]
        )
        config = scraper.SiteConfig(
            "奋发图强",
            "https://ymkakun.dwgml-7jbcy-ohrbrq.xyz:16677/topic/547563.html",
            ("36码中特",),
            ("奋发图强", "36码中特"),
            fixed_issue=189,
            region="bottom",
            search_window=320,
        )

        candidates, invalid_candidates, seen_matching_issues = scraper.fenfatuqiang_candidates(html, config)

        self.assertEqual([candidate.page_index for candidate in candidates], [2])
        self.assertEqual(invalid_candidates, [])
        self.assertEqual(seen_matching_issues, {189})

    def test_fenfatuqiang_dedicated_parser_reaches_target_after_long_history(self):
        numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        lines = ["196期:奋发图强【36码中特】933341f.com"]
        for issue in range(132, 196):
            lines.extend(
                [
                    f"{issue}期:『【36码中特】』开：准",
                    numbers,
                    "====",
                    "历史说明",
                    "历史说明",
                ]
            )
        lines.extend(["196期:『【36码中特】』开：0000准", numbers, "上一篇："])
        config = scraper.SiteConfig(
            "奋发图强",
            "https://ymkakun.dwgml-7jbcy-ohrbrq.xyz:16677/topic/547563.html",
            ("36码中特",),
            ("奋发图强", "36码中特"),
            fixed_issue=196,
            region="bottom",
            search_window=320,
        )

        result = scraper.extract_latest_36("\n".join(lines), config)

        self.assertEqual(result.issue, 196)
        self.assertEqual(result.numbers, tuple(f"{number:02d}" for number in range(1, 37)))

    def test_fenfatuqiang_dedicated_parser_requires_article_end_boundary(self):
        numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        config = scraper.SiteConfig(
            "奋发图强",
            "https://ymkakun.dwgml-7jbcy-ohrbrq.xyz:16677/topic/547563.html",
            ("36码中特",),
            ("奋发图强", "36码中特"),
            fixed_issue=196,
            region="bottom",
        )
        html = "\n".join(["196期:奋发图强【36码中特】", "196期:『【36码中特】』开：准", numbers])

        with self.assertRaisesRegex(scraper.ScrapeError, "专属正文边界缺失"):
            scraper.extract_latest_36(html, config)

    def test_fenfatuqiang_dedicated_parser_ignores_candidate_after_article_end(self):
        first_numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        foreign_numbers = " ".join(f"{number:02d}" for number in range(14, 50))
        config = scraper.SiteConfig(
            "奋发图强",
            "https://ymkakun.dwgml-7jbcy-ohrbrq.xyz:16677/topic/547563.html",
            ("36码中特",),
            ("奋发图强", "36码中特"),
            fixed_issue=196,
            region="bottom",
        )
        html = "\n".join(
            [
                "196期:奋发图强【36码中特】",
                "196期:『【36码中特】』开：准",
                first_numbers,
                "上一篇：",
                "196期:『【36码中特】』开：准",
                foreign_numbers,
            ]
        )

        result = scraper.extract_latest_36(html, config)

        self.assertEqual(result.numbers, tuple(f"{number:02d}" for number in range(1, 37)))

    def test_fenfatuqiang_dedicated_parser_rejects_numbers_after_article_end(self):
        foreign_numbers = " ".join(f"{number:02d}" for number in range(14, 50))
        config = scraper.SiteConfig(
            "奋发图强",
            "https://ymkakun.dwgml-7jbcy-ohrbrq.xyz:16677/topic/547563.html",
            ("36码中特",),
            ("奋发图强", "36码中特"),
            fixed_issue=196,
            region="bottom",
        )
        html = "\n".join(
            [
                "196期:奋发图强【36码中特】",
                "196期:『【36码中特】』开：准",
                "上一篇：",
                foreign_numbers,
            ]
        )

        with self.assertRaisesRegex(scraper.ScrapeError, "196期数据无效|没有找到完整36码"):
            scraper.extract_latest_36(html, config)

    def test_xueqiu_dedicated_parser_accepts_spaced_36_marker(self):
        numbers = (
            "05", "37", "39", "27", "04", "02", "36", "15", "25", "43", "06", "07",
            "16", "31", "24", "32", "12", "03", "28", "08", "48", "44", "42", "29",
            "01", "38", "26", "30", "40", "19", "41", "17", "13", "18", "49", "14",
        )
        rows = [".".join(numbers[index:index + 12]) for index in range(0, 36, 12)]
        html = "\n".join(["（雪球36码）", "雪球模式正式启动", "196期【3 6码特围】开000准", *rows])
        config = scraper.SiteConfig(
            "雪球",
            "https://ocnrhq.du156-vb27w-tmhsed.xyz:16677/",
            ("36码特围",),
            ("雪球36码",),
            fixed_issue=196,
            region="top",
        )

        result = scraper.extract_latest_36(html, config)

        self.assertEqual(result.issue, 196)
        self.assertEqual(result.numbers, numbers)

    def test_xueqiu_dedicated_parser_rejects_spaced_marker_without_anchor(self):
        numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        config = scraper.SiteConfig(
            "雪球",
            "https://ocnrhq.du156-vb27w-tmhsed.xyz:16677/",
            ("36码特围",),
            ("雪球36码",),
            fixed_issue=196,
            region="top",
        )

        with self.assertRaisesRegex(scraper.ScrapeError, "未找到栏目关键词"):
            scraper.extract_latest_36(f"196期【3 6码特围】开000准\n{numbers}", config)

    def test_xueqiu_dedicated_parser_rejects_same_issue_conflict(self):
        first_numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        second_numbers = " ".join(f"{number:02d}" for number in range(14, 50))
        config = scraper.SiteConfig(
            "雪球",
            "https://ocnrhq.du156-vb27w-tmhsed.xyz:16677/",
            ("36码特围",),
            ("雪球36码",),
            fixed_issue=196,
            region="top",
        )
        html = "\n".join(
            [
                "雪球36码",
                "196期【3 6码特围】开000准",
                first_numbers,
                "196期【36码特围】开000准",
                second_numbers,
            ]
        )

        with self.assertRaisesRegex(scraper.ScrapeError, "多个高可信候选.*冲突"):
            scraper.extract_latest_36(html, config)

    def test_xueqiu_dedicated_parser_rejects_numbers_outside_section_window(self):
        foreign_numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        config = scraper.SiteConfig(
            "雪球",
            "https://ocnrhq.du156-vb27w-tmhsed.xyz:16677/",
            ("36码特围",),
            ("雪球36码",),
            fixed_issue=196,
            region="top",
            search_window=5,
        )
        html = "\n".join(
            [
                "雪球36码",
                "196期【3 6码特围】开000准",
                "正文说明",
                "正文说明",
                "正文说明",
                foreign_numbers,
            ]
        )

        with self.assertRaisesRegex(scraper.ScrapeError, "196期数据无效|没有找到完整36码"):
            scraper.extract_latest_36(html, config)

    def test_new_manager_articles_use_dedicated_three_by_twelve_parser(self):
        cases = (
            ("应接不暇", "6a081bc1e0d076537e1df8aa", "197期:『应接不暇』 🧸三十六码🧸开:00准"),
            ("山河千载", "6a09710b291caff3edcb8c25", "197期:『山河千载』 👩‍🌾三十六码👩‍🌾开:猪08准"),
            ("聊从兔颖", "6a5117245e6c7637a3f4eb7a", "197期:《聊从兔颖》三十六码开:猪08准"),
            ("貌美如花", "6a096aca291caff3edcb8b8c", "197期:〖貌美如花〗 👛三十六码👛开:猪08准"),
            ("伶牙俐齿", "6a58fc58f447e21b02db9c97", "197期:《伶牙俐齿》 😜三十六码😜开:猪08准"),
            ("腾讯专家", "6a5eef5ef447e21b02dd548a", "197期:《腾讯专家》三十六码开:猪08准"),
            ("金刚财子", "6a155508d9d9fc2cea524219", "197期:《金刚财子》三十六码开:猪08准"),
            ("九九归一", "6a156eeb8be59b17287c6dce", "197期:《九九归一》三十六码开:猪08准"),
        )
        numbers = tuple(f"{number:02d}" for number in range(1, 37))
        rows = ["【" + ".".join(numbers[index:index + 12]) + "】" for index in range(0, 36, 12)]

        for name, article_id, title in cases:
            with self.subTest(name=name):
                config = scraper.SiteConfig(
                    name,
                    f"https://example.test/article/manager/{article_id}?url=test",
                    ("三十六码",),
                    (name,),
                    fixed_issue=197,
                    region="bottom",
                )
                self.assertTrue(scraper.is_onboarded_manager_article_config(config))
                result = scraper.extract_latest_36("\n".join([title, *rows]), config)

                self.assertEqual(result.issue, 197)
                self.assertEqual(result.numbers, numbers)

    def test_new_manager_article_rejects_non_adjacent_number_rows(self):
        numbers = tuple(f"{number:02d}" for number in range(1, 37))
        rows = ["【" + ".".join(numbers[index:index + 12]) + "】" for index in range(0, 36, 12)]
        config = scraper.SiteConfig(
            "应接不暇",
            "https://example.test/article/manager/6a081bc1e0d076537e1df8aa?url=test",
            ("三十六码",),
            ("应接不暇",),
            fixed_issue=197,
            region="bottom",
        )
        html = "\n".join(["197期:『应接不暇』三十六码开:00准", rows[0], "无关内容", rows[1], rows[2]])

        with self.assertRaisesRegex(scraper.ScrapeError, "197期数据无效|没有找到完整36码"):
            scraper.extract_latest_36(html, config)

    def test_new_manager_article_rejects_same_issue_conflict(self):
        first = tuple(f"{number:02d}" for number in range(1, 37))
        second = tuple(f"{number:02d}" for number in range(14, 50))
        first_rows = ["【" + ".".join(first[index:index + 12]) + "】" for index in range(0, 36, 12)]
        second_rows = ["【" + ".".join(second[index:index + 12]) + "】" for index in range(0, 36, 12)]
        config = scraper.SiteConfig(
            "应接不暇",
            "https://example.test/article/manager/6a081bc1e0d076537e1df8aa?url=test",
            ("三十六码",),
            ("应接不暇",),
            fixed_issue=197,
            region="bottom",
        )
        html = "\n".join(
            [
                "197期:『应接不暇』三十六码开:00准",
                *first_rows,
                "197期:『应接不暇』三十六码开:00准",
                *second_rows,
            ]
        )

        with self.assertRaisesRegex(scraper.ScrapeError, "多个高可信候选.*冲突"):
            scraper.extract_latest_36(html, config)

    def test_new_manager_article_api_requires_matching_id_and_author(self):
        config = scraper.SiteConfig(
            "应接不暇",
            "https://example.test/article/manager/6a081bc1e0d076537e1df8aa?url=test",
            ("三十六码",),
            ("应接不暇",),
            region="bottom",
        )
        valid = {
            "id": "6a081bc1e0d076537e1df8aa",
            "authorNickname": "应接不暇",
            "title": "200期：三十六码",
            "html": "<div>197期:『应接不暇』三十六码开:00准</div>",
        }

        self.assertIn("应接不暇", scraper.onboarded_manager_api_payload_to_html(json.dumps(valid), config))
        with self.assertRaisesRegex(scraper.ScrapeError, "API ID不匹配"):
            scraper.onboarded_manager_api_payload_to_html(
                json.dumps({**valid, "id": "wrong"}), config
            )
        with self.assertRaisesRegex(scraper.ScrapeError, "API作者不匹配"):
            scraper.onboarded_manager_api_payload_to_html(
                json.dumps({**valid, "authorNickname": "其他作者"}), config
            )

    def test_dynamic_article_api_uses_exact_nested_id_and_rejects_decoy(self):
        target_numbers = tuple(f"{number:02d}" for number in range(1, 37))
        decoy_numbers = tuple(f"{number:02d}" for number in range(14, 50))
        config = scraper.SiteConfig(
            "目标站",
            "https://example.test/article/admin/target-id?url=test",
            ("三十六码",),
            ("目标站",),
            fixed_issue=190,
            api_url="https://example.test/api/target-id",
            region="top",
        )

        def record(record_id, numbers, name):
            return {
                "id": record_id,
                "authorNickname": name,
                "title": "190期：三十六码",
                "formSections": [{"name": name}],
                "html": f"{name} 190期:『{name}』三十六码开:00准\n{'.'.join(numbers)}",
            }

        payload = json.dumps(
            {"data": {"items": [record("decoy-id", decoy_numbers, "诱饵站"), record("target-id", target_numbers, "目标站")]}},
            ensure_ascii=False,
        )

        document = scraper.article_api_payload_to_html(payload, config)
        self.assertIn("目标站", document)
        self.assertNotIn("诱饵站", document)
        result = scraper.scrape_site(config, fixed_issue=190, text_fetcher=lambda url, timeout: payload)
        self.assertEqual(result.numbers, target_numbers)
        self.assertEqual(result.record_id, "target-id")
        self.assertEqual(
            result.record_path,
            "https://example.test/api/target-id::$.data.items[1]",
        )

    def test_dynamic_article_api_rejects_duplicate_target_id(self):
        config = scraper.SiteConfig(
            "目标站",
            "https://example.test/article/admin/target-id?url=test",
            ("三十六码",),
            ("目标站",),
        )
        record = {
            "id": "target-id",
            "authorNickname": "目标站",
            "title": "190期：三十六码",
            "formSections": [{"name": "目标站"}],
            "html": "目标站 190期 三十六码\n" + ".".join(f"{number:02d}" for number in range(1, 37)),
        }
        payload = json.dumps({"data": [record, {**record, "html": record["html"].replace("01", "49", 1)}]}, ensure_ascii=False)

        with self.assertRaisesRegex(scraper.ScrapeError, "多个同ID"):
            scraper.article_api_payload_to_html(payload, config)

    def test_dynamic_article_api_requires_title_and_section_identity(self):
        config = scraper.SiteConfig(
            "目标站",
            "https://example.test/article/admin/target-id?url=test",
            ("三十六码",),
            ("目标站",),
        )
        base = {
            "id": "target-id",
            "authorNickname": "目标站",
            "title": "190期：三十六码",
            "formSections": [{"name": "目标站"}],
            "html": "目标站 190期 三十六码\n" + ".".join(f"{number:02d}" for number in range(1, 37)),
        }
        with self.assertRaisesRegex(scraper.ScrapeError, "API标题缺失"):
            scraper.article_api_payload_to_html(json.dumps({**base, "title": ""}, ensure_ascii=False), config)
        with self.assertRaisesRegex(scraper.ScrapeError, "栏目关键词不匹配"):
            scraper.article_api_payload_to_html(
                json.dumps({**base, "html": base["html"].replace("目标站", "其他站")}, ensure_ascii=False), config
            )

    def test_kunnan_magazine_uses_structured_user_forum_records(self):
        config = scraper.SiteConfig(
            "困难杂志",
            "https://qvuuqqs.example/#/users/3792",
            ("三十六码特",),
            api_url="https://qvuuqqs.example/api/v1/users/3792/forums?per_page=20",
            region="top",
        )

        def record(record_id, issue, start):
            numbers = tuple(f"{number:02d}" for number in range(start, start + 36))
            rows = (numbers[:12], numbers[12:24], numbers[24:])
            content = "\n".join(
                [f"{issue}期【三十六码特】开00对"]
                + [" ".join(row) for row in rows]
            )
            return {
                "id": record_id,
                "status": "published",
                "user_id": 3792,
                "lottery": "macao",
                "draw": issue,
                "topic": "【三十六码特】",
                "content": content,
            }

        payload = json.dumps(
            [record(15650595, 201, 1), record(15650594, 200, 13)],
            ensure_ascii=False,
        )
        results = scraper.kunnan_magazine_records_from_payload(payload, config)
        self.assertEqual([result.issue for result in results], [201, 200])
        self.assertEqual(results[0].record_id, "15650595")
        self.assertEqual(results[0].record_path, config.api_url + "::$[0]")

        result = scraper.scrape_site(
            config,
            fixed_issue=200,
            text_fetcher=lambda _url, _timeout: payload,
        )
        self.assertEqual(result.issue, 200)
        self.assertEqual(result.record_id, "15650594")
        self.assertEqual(result.numbers, tuple(f"{number:02d}" for number in range(13, 49)))

    def test_kunnan_magazine_rejects_duplicate_target_issue(self):
        config = scraper.SiteConfig(
            "困难杂志",
            "https://qvuuqqs.example/#/users/3792",
            ("三十六码特",),
            api_url="https://qvuuqqs.example/api/v1/users/3792/forums?per_page=20",
        )
        numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        record = {
            "id": "one",
            "status": "published",
            "user_id": 3792,
            "lottery": "macao",
            "draw": 201,
            "topic": "【三十六码特】",
            "content": f"201期【三十六码特】开00对\n{numbers}",
        }
        with self.assertRaisesRegex(scraper.ScrapeError, "存在多个目标文章记录"):
            scraper.kunnan_magazine_records_from_payload(
                json.dumps([record, {**record, "id": "two"}], ensure_ascii=False),
                config,
            )

    def test_browser_article_accepts_only_expected_id_and_rejects_conflicting_target(self):
        config = scraper.SiteConfig(
            "目标站",
            "https://example.test/article/admin/target-id?url=test",
            ("三十六码",),
            ("目标站",),
        )

        def payload(record_id, marker="01"):
            numbers = ".".join(f"{number:02d}" for number in range(1, 37))
            return json.dumps(
                {
                    "id": record_id,
                    "authorNickname": "目标站",
                    "title": "190期：三十六码",
                    "html": f"目标站 190期 三十六码 {marker}\n{numbers}",
                },
                ensure_ascii=False,
            ).encode("utf-8")

        class FakeResponse:
            headers = {"content-type": "application/json"}

            def __init__(self, body):
                self.url = "https://example.test/api"
                self._body = body

            def body(self):
                return self._body

        class FakeBody:
            def inner_text(self, timeout=None):
                return ""

        class FakePage:
            def __init__(self, response_bodies):
                self.handlers = []
                self.response_bodies = response_bodies

            def on(self, event, handler):
                self.handlers.append(handler)

            def goto(self, url, wait_until=None, timeout=None):
                for body in self.response_bodies:
                    response = FakeResponse(body)
                    for handler in self.handlers:
                        handler(response)

            def locator(self, selector):
                return FakeBody()

            def wait_for_load_state(self, state, timeout=None):
                return None

            def wait_for_timeout(self, timeout):
                return None

            def close(self):
                return None

        class FakeBrowser:
            def __init__(self, response_bodies):
                self.response_bodies = response_bodies

            def new_page(self, ignore_https_errors=False):
                return FakePage(self.response_bodies)

        class FakePlaywright:
            def __init__(self, response_bodies):
                self.chromium = self
                self.response_bodies = response_bodies

            def launch(self, headless=True):
                return FakeBrowser(self.response_bodies)

        renderer = scraper.ReusableBrowserRenderer(
            lambda: FakePlaywright([payload("decoy-id"), payload("target-id")])
        )
        record = renderer.fetch_article(config, timeout=1)
        self.assertEqual(record.record_id, "target-id")

        conflicting = scraper.ReusableBrowserRenderer(
            lambda: FakePlaywright([payload("target-id", "01"), payload("target-id", "49")])
        )
        with self.assertRaisesRegex(scraper.ScrapeError, "同ID记录内容冲突"):
            conflicting.fetch_article(config, timeout=1)

    def test_backup_record_keeps_dynamic_identity_metadata(self):
        result = scraper.SiteResult(
            "目标站",
            "https://example.test/article/admin/target-id?url=test",
            190,
            tuple(f"{number:02d}" for number in range(1, 37)),
            record_id="target-id",
            record_path="$.data.items[1]",
            raw_position=7,
        )

        record = scraper.result_to_backup_record(result)
        self.assertEqual(record["article_id"], "target-id")
        self.assertEqual(record["source_path"], "$.data.items[1]")
        self.assertEqual(record["raw_position"], 7)
        restored = scraper.backup_record_to_result(result.name, result.url, record)
        self.assertEqual(restored.record_id, "target-id")
        self.assertEqual(restored.record_path, "$.data.items[1]")
        self.assertEqual(restored.raw_position, 7)

    def test_onboarded_bbs_topics_use_dedicated_three_row_parser(self):
        numbers = tuple(f"{number:02d}" for number in range(1, 37))
        rows = ["【" + ".".join(numbers[index:index + 12]) + "】" for index in range(0, 36, 12)]
        cases = (
            ("四海晏然", "198期:《四海晏然》 🥬三十六码🥬开:00准"),
            ("日益精进", "198期:《日益精进》 🥫三十六码🥫开:00准"),
            ("彩友大师", "198期:【彩友大师】 🚣三十六码🚣开:00准"),
            ("九宫彩神", "198期:『九宫彩神』 ♥️三十六码♥️开:00准"),
            ("大仙神算", "198期:【大仙神算】 三十六码开:00准"),
            ("天天送财", "198期:【天天送财】 三十六码开:00准"),
            ("财路家家", "198期:【财路家家】 三十六码开:00准"),
            ("钱彩两得", "198期:【钱彩两得】 三十六码开:00准"),
            ("三生桃花", "198期:【三生桃花】 三十六码开:00准"),
            ("金码豹子", "198期:【金码豹子】 三十六码开:00准"),
        )

        for name, title in cases:
            with self.subTest(name=name):
                config = scraper.SiteConfig(
                    name,
                    scraper.ONBOARDED_BBS_TOPIC_URLS[name],
                    ("三十六码",),
                    (name,),
                    fixed_issue=198,
                    region="bottom",
                )
                self.assertTrue(scraper.is_onboarded_bbs_topic_config(config))
                result = scraper.extract_latest_36("\n".join([title, *rows]), config)

                self.assertEqual(result.issue, 198)
                self.assertEqual(result.numbers, numbers)

    def test_output_line_omits_issue_when_fixed_issue_is_used(self):
        result = scraper.SiteResult(
            "测试站",
            "https://example.test",
            159,
            tuple(f"{number:02d}" for number in range(1, 37)),
        )

        line = scraper.output_line(result, include_issue=False)

        self.assertEqual(line, "01,02,03,04,05,06,07,08,09,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36 测试站")

    def test_onboarded_small_report_uses_its_exact_category_keyword(self):
        numbers = tuple(f"{number:02d}" for number in range(1, 37))
        rows = ["《" + ".".join(numbers[index:index + 12]) + "》" for index in range(0, 36, 12)]
        config = scraper.SiteConfig(
            "小报告",
            scraper.ONBOARDED_BBS_TOPIC_URLS["小报告"],
            ("三十六码中特",),
            ("小报告",),
            fixed_issue=198,
            region="bottom",
        )
        self.assertTrue(scraper.is_onboarded_bbs_topic_config(config))
        result = scraper.extract_latest_36(
            "\n".join(["198期:《小报告》三十六码中特 开:鼠07 准", *rows]),
            config,
        )
        self.assertEqual(result.issue, 198)
        self.assertEqual(result.numbers, numbers)

    def test_partial_incremental_backup_preserves_existing_failures(self):
        result = scraper.SiteResult(
            "测试站",
            "https://example.test",
            158,
            tuple(f"{number:02d}" for number in range(1, 37)),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / "backup.json"
            scraper.update_recent_duplicate_backup([result], ("旧失败",), backup_path, fixed_issue=158)
            scraper.update_recent_duplicate_backup(
                [result],
                (),
                backup_path,
                fixed_issue=158,
                preserve_existing_failures=True,
            )
            payload = json.loads(backup_path.read_text(encoding="utf-8-sig"))

        self.assertTrue(payload["incomplete"])
        self.assertEqual(payload["failures"], ["旧失败"])

    def test_top_position_rejects_conflicting_same_issue_blocks(self):
        top_numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        tail_numbers = " ".join(f"{number:02d}" for number in range(14, 50))
        html = "\n".join(
            [
                "126期 36码中特 开0000准",
                top_numbers,
                "126期 36码中特 开0000准",
                tail_numbers,
            ]
        )
        config = scraper.SiteConfig(
            "测试",
            "https://example.test",
            ("36码中特",),
            position="top",
        )

        with self.assertRaisesRegex(scraper.ScrapeError, "多个高可信候选.*冲突"):
            scraper.extract_latest_36(html, config)

    def test_tail_position_rejects_conflicting_same_issue_blocks(self):
        top_numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        tail_numbers = " ".join(f"{number:02d}" for number in range(14, 50))
        html = "\n".join(
            [
                "126期 36码中特 开0000准",
                top_numbers,
                "126期 36码中特 开0000准",
                tail_numbers,
            ]
        )
        config = scraper.SiteConfig(
            "测试",
            "https://example.test",
            ("36码中特",),
            position="tail",
        )

        with self.assertRaisesRegex(scraper.ScrapeError, "多个高可信候选.*冲突"):
            scraper.extract_latest_36(html, config)

    def test_heiguafu_site_is_configured_as_top(self):
        sites = [site for site in scraper.SITES if site.name == "黑寡妇"]

        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0].url, "https://wdhwvoux.ptev6-bnhp2-djxppz.xyz:16677/")
        self.assertEqual(scraper.candidate_region(sites[0]), "top")


    def test_zhuchiren_uses_only_36ma_weite_tab(self):
        baote_numbers = " ".join(f"{number:02d}" for number in range(14, 50))
        weite_numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        html = "\n".join(
            [
                "30码中特",
                "36码爆特",
                "36码围特",
                "九肖中特",
                "138期【36码爆特】特开:马49准",
                baote_numbers,
                "第147期",
                weite_numbers,
                "开:0000准",
            ]
        )
        config = scraper.SiteConfig(
            "主持人",
            "https://example.test",
            ("第",),
            ("36码围特",),
            fixed_issue=147,
            parser_id="zhuchiren_weite",
        )

        result = scraper.extract_latest_36(html, config)

        self.assertEqual(result.issue, 147)
        self.assertEqual(result.numbers, tuple(f"{number:02d}" for number in range(1, 37)))

    def test_zhuchiren_second_uses_only_36ma_baote_tab(self):
        baote_numbers = " ".join(f"{number:02d}" for number in range(14, 50))
        weite_numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        html = "\n".join(
            [
                "30码中特",
                "36码爆特",
                "36码围特",
                "九肖中特",
                "147期【36码爆特】特开:0000准",
                baote_numbers,
                "第147期",
                weite_numbers,
                "开:0000准",
            ]
        )
        config = scraper.SiteConfig(
            "主持人第二版",
            "https://example.test",
            ("36码爆特",),
            ("36码爆特",),
            fixed_issue=147,
            parser_id="zhuchiren_baote",
        )

        result = scraper.extract_latest_36(html, config)

        self.assertEqual(result.issue, 147)
        self.assertEqual(result.numbers, tuple(f"{number:02d}" for number in range(14, 50)))

    def test_baoma_xuanji_merges_three_12_number_parts(self):
        first = "25 41 39 16 01 33 04 02 29 47 32 24"
        second = "09 36 37 42 22 48 30 03 11 49 08 26"
        third = "18 07 34 20 21 17 46 44 15 23 28 35"
        html = "\n".join(
            [
                "无关内容 178期精选12码：【01 02 03 04 05 06 07 08 09 10 11 12】",
                "综合特码",
                f"178期必中12码肖:开:??准【第一份：{first}第二份：{second}第三份：{third}】",
            ]
        )
        config = scraper.SiteConfig(
            "宝马玄机",
            "https://example.test",
            ("必中12码肖",),
            ("综合特码",),
            fixed_issue=178,
            region="top",
        )

        result = scraper.extract_latest_36(html, config)

        self.assertEqual(result.issue, 178)
        self.assertEqual(result.numbers, tuple((first + " " + second + " " + third).split()))

    def test_xiongchumo_merges_four_lettered_rows(self):
        first = "49 26 44 43 12 01 38 10 35"
        second = "48 47 33 37 13 36 14 20 45"
        third = "34 46 15 23 25 32 39 03 24"
        fourth = "21 09 08 22 19 11 31 07 27"
        html = "\n".join(
            [
                "190期:熊出没〖内幕36码〗彩民推荐",
                "190期【69298g.com】开000中",
                f"[A] {first}",
                f"[B] {second}",
                f"[C] {third}",
                f"[D] {fourth}",
                "187期【69298g.com】开马01中",
                "[A] 12 30 38 24 37 18 40 03 44",
                "[B] 15 02 17 05 01 13 36 25 20",
                "[C] 31 14 08 48 19 27 28 16 26",
                "[D] 32 49 04 06 39 41 29 42 43",
            ]
        )
        config = scraper.SiteConfig(
            "熊出没",
            "https://example.test/topic/459501.html",
            ("内幕36码",),
            ("熊出没", "内幕36码"),
            fixed_issue=190,
            region="top",
        )

        result = scraper.extract_latest_36(html, config)

        self.assertEqual(result.issue, 190)
        self.assertEqual(result.numbers, tuple((first + " " + second + " " + third + " " + fourth).split()))

    def test_xiongchumo_historical_issue_keeps_section_context(self):
        rows_187 = (
            "12 30 38 24 37 18 40 03 44",
            "15 02 17 05 01 13 36 25 20",
            "31 14 08 48 19 27 28 16 26",
            "32 49 04 06 39 41 29 42 43",
        )
        html = "\n".join(
            [
                "190期:熊出没〖内幕36码〗彩民推荐",
                "190期【69298g.com】开000中",
                "[A] 49 26 44 43 12 01 38 10 35",
                "[B] 48 47 33 37 13 36 14 20 45",
                "[C] 34 46 15 23 25 32 39 03 24",
                "[D] 21 09 08 22 19 11 31 07 27",
                "189期【69298g.com】开龙15错",
                "[A] 13 43 47 46 32 33 01 09 29",
                "[B] 11 22 17 21 31 42 20 34 08",
                "[C] 36 35 06 07 23 45 05 19 37",
                "[D] 30 10 12 44 41 18 49 24 48",
                "187期【69298g.com】开马01中",
                f"[A] {rows_187[0]}",
                f"[B] {rows_187[1]}",
                f"[C] {rows_187[2]}",
                f"[D] {rows_187[3]}",
            ]
        )
        config = scraper.SiteConfig(
            "熊出没",
            "https://example.test/topic/459501.html",
            ("内幕36码",),
            ("熊出没", "内幕36码"),
            fixed_issue=187,
            region="top",
        )

        result = scraper.extract_latest_36(html, config)

        self.assertEqual(result.issue, 187)
        self.assertEqual(result.numbers, tuple(" ".join(rows_187).split()))

    def test_xiaoyuer_uses_result_anchor_grid_and_drops_zero(self):
        grid = [
            "00", "35", "29", "34", "39", "25", "06", "26", "31", "28", "12", "20",
            "45", "11", "00", "22", "00", "37", "30", "14", "00", "00", "24", "00",
            "33", "00", "17", "00", "27", "49", "18", "38", "07", "16", "00", "44",
            "21", "47", "41", "10", "15", "00", "00", "00", "19", "40", "36", "32",
        ]
        html = "\n".join(
            [
                "澳门精准36码",
                "147期:开奖结果:00-00-00-00-00-00特:0000",
                *grid,
                "香港小鱼儿官方网址155573f.com",
                "057期:开奖结果0000准",
            ]
        )
        config = scraper.SiteConfig(
            "小鱼儿",
            "https://example.test",
            ("开奖结果",),
            ("澳门精准36码",),
            fixed_issue=147,
            min_numbers_per_line=1,
            drop_zero_numbers=True,
        )

        result = scraper.extract_latest_36(html, config)

        self.assertEqual(result.issue, 147)
        self.assertEqual(result.numbers, tuple(number for number in grid if number != "00"))

    def test_xiaoyuer_accepts_row_grouped_grid_with_zero_result(self):
        rows = [
            "00 35 17 34 39 25 42 02 43 28 12 44",
            "09 47 00 22 00 37 30 14 00 00 48 00",
            "33 00 05 00 03 13 18 38 31 40 00 08",
            "21 11 29 46 15 00 00 00 19 16 36 32",
        ]
        html = "\n".join(
            [
                "澳门精准36码",
                "177期:开奖结果:00-00-00-00-00-00特:0000",
                "鼠 牛 虎 兔 龙 蛇 马 羊 猴 鸡 狗 猪",
                *rows,
                "香港小鱼儿官方网址155573f.com",
            ]
        )
        config = scraper.SiteConfig(
            "小鱼儿",
            "https://example.test",
            ("开奖结果",),
            ("澳门精准36码",),
            fixed_issue=177,
            min_numbers_per_line=1,
            drop_zero_numbers=True,
        )

        result = scraper.extract_latest_36(html, config)

        self.assertEqual(result.issue, 177)
        self.assertEqual(
            result.numbers,
            tuple(number for row in rows for number in row.split() if number != "00"),
        )

    def test_renjianrenai_uses_dedicated_issue_blocks(self):
        numbers = tuple(f"{number:02d}" for number in range(1, 37))
        html = "\n".join(
            [
                "作者:人见人爱",
                "156期: 人见人爱 ✂️三十六码✂️开:00准",
                "【01.02.03.04.05.06.07.08.09.10.11.12.13.14】",
                "【15.16.17.18.19.20.21.22.23.24.25.26】",
                "【27.28.29.30.31.32.33.34.35.36】",
            ]
        )
        config = scraper.SiteConfig(
            "人见人爱",
            "https://utklnwvn.xt8d7-kd9kc-csprqr.work:29488/article/admin/6a0445a74ea5c20141013e81?url=qdz",
            ("三十六码", "36码"),
            ("错误栏目",),
            fixed_issue=156,
            region="bottom",
            render_browser=True,
        )

        result = scraper.extract_latest_36(html, config)

        self.assertEqual(result.issue, 156)
        self.assertEqual(result.numbers, numbers)

    def test_many_candidates_rejects_fixed_issue_outside_bottom_window(self):
        numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        lines = []
        for issue in range(121, 152):
            lines.extend([f"{issue}\u671f 36\u7801\u4e2d\u7279 \u5f000000\u51c6", numbers])
        config = scraper.SiteConfig(
            "\u6d4b\u8bd5",
            "https://example.test",
            ("36\u7801\u4e2d\u7279",),
            fixed_issue=145,
            region="bottom",
        )

        with self.assertRaisesRegex(scraper.ScrapeError, "\u8d85\u51fa\u4e25\u683c\u5019\u9009\u8303\u56f4"):
            scraper.extract_latest_36("\n".join(lines), config)

    def test_duplicate_issue_uses_only_latest_three_candidates_for_bottom(self):
        def number_block(offset: int) -> tuple[str, ...]:
            return tuple(f"{((offset + index - 1) % 49) + 1:02d}" for index in range(36))

        lines = []
        blocks = [number_block(offset) for offset in range(1, 7)]
        for block in blocks:
            lines.extend(["151\u671f 36\u7801\u4e2d\u7279 \u5f000000\u51c6", " ".join(block)])
        config = scraper.SiteConfig(
            "\u6d4b\u8bd5",
            "https://example.test",
            ("36\u7801\u4e2d\u7279",),
            fixed_issue=151,
            region="bottom",
        )

        with self.assertRaisesRegex(scraper.ScrapeError, "多个高可信候选.*冲突"):
            scraper.extract_latest_36("\n".join(lines), config)

    def test_duplicate_issue_uses_only_latest_three_candidates_for_top(self):
        def number_block(offset: int) -> tuple[str, ...]:
            return tuple(f"{((offset + index - 1) % 49) + 1:02d}" for index in range(36))

        lines = []
        blocks = [number_block(offset) for offset in range(1, 7)]
        for block in blocks:
            lines.extend(["151\u671f 36\u7801\u4e2d\u7279 \u5f000000\u51c6", " ".join(block)])
        config = scraper.SiteConfig(
            "\u6d4b\u8bd5",
            "https://example.test",
            ("36\u7801\u4e2d\u7279",),
            fixed_issue=151,
            region="top",
        )

        with self.assertRaisesRegex(scraper.ScrapeError, "多个高可信候选.*冲突"):
            scraper.extract_latest_36("\n".join(lines), config)

    def test_yiyiba_uses_first_topic_content_and_ignores_decoded_duplicate(self):
        first = "\n".join(
            [
                "200期:『精准36码』开：0000准",
                "25.09.26.38.12.20.33.47.11.01.03.39",
                "15.21.44.37.02.35.22.36.04.14.24.34",
                "10.49.13.46.32.16.23.28.45.08.40.48",
            ]
        )
        second = "\n".join(
            [
                "200期:『精准36码』开：羊47准",
                "34.27.23.11.28.14.48.35.44.24.04.20",
                "02.13.49.38.08.15.03.45.36.40.33.26",
                "39.10.37.46.47.09.22.16.01.25.32.21",
            ]
        )
        html = f'<div class="topic-content"><p>{first.replace(chr(10), "<br>")}</p></div><div class="topic-content"><p>{second.replace(chr(10), "<br>")}</p></div>'
        config = scraper.SiteConfig(
            "以已把",
            "https://xosdbkls.9ca0p-tk7i0-yczwdd.work:16622/topic/238933.html",
            ("精准36码",),
            ("精准36码",),
            fixed_issue=200,
            region="top",
        )

        result = scraper.extract_latest_36(html, config)

        self.assertEqual(result.issue, 200)
        self.assertEqual(result.numbers, tuple((first.splitlines()[1:] and [token for line in first.splitlines()[1:] for token in line.split(".")])) )


if __name__ == "__main__":
    unittest.main()
