from __future__ import annotations

import unittest

from dawei.application.repair_service import ResolvedCase, validate_case
from dawei.domain.models import SiteConfig


class RepairServiceIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = ResolvedCase(
            SiteConfig(
                name="测试站",
                url="https://example.test/topic",
                source_type="generic_html",
                parser_id="generic_36",
                region="top",
            ),
            210,
        )

    def test_source_fetcher_unexpected_exception_becomes_site_failure(self) -> None:
        def source_fetcher(*_args, **_kwargs):
            raise RuntimeError("injected network failure")

        report = validate_case(self.case, 1, source_fetcher, lambda *_args, **_kwargs: None)

        self.assertFalse(report.passed)
        self.assertIn("真实正文获取失败", report.failure_reason)
        self.assertIn("injected network failure", report.failure_reason)

    def test_site_scraper_unexpected_exception_becomes_site_failure(self) -> None:
        def site_scraper(*_args, **_kwargs):
            raise RuntimeError("injected scraper failure")

        report = validate_case(
            self.case,
            1,
            lambda *_args, **_kwargs: ("raw", "HTTP页面正文"),
            site_scraper,
        )

        self.assertFalse(report.passed)
        self.assertIn("正式抓取失败", report.failure_reason)
        self.assertIn("injected scraper failure", report.failure_reason)


if __name__ == "__main__":
    unittest.main()
