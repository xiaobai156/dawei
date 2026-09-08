from __future__ import annotations

import unittest

from dawei.application.duplicate_runner import format_failure
from dawei.application.duplicate_service import SiteWindow, detect_latest_period
from dawei.domain.errors import ScrapeError
from dawei.domain.models import SiteConfig


def window(issue: int) -> SiteWindow:
    return SiteWindow("站点", "https://example.test", issue, 10, (), latest_issue=issue)


class DetectLatestPeriodTests(unittest.TestCase):
    def test_unique_mode_is_selected(self) -> None:
        self.assertEqual(225, detect_latest_period((window(225), window(225), window(224))))

    def test_tied_modes_fail_closed(self) -> None:
        with self.assertRaisesRegex(ScrapeError, "最新期数并列"):
            detect_latest_period((window(225), window(224)))

    def test_single_result_is_valid(self) -> None:
        self.assertEqual(225, detect_latest_period((window(225),)))


class DuplicateFailureFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = SiteConfig(
            name="测试站",
            url="https://example.test/topic",
            region="bottom",
            site_id="site_test",
            parser_id="generic_36",
        )

    def test_failure_contains_identity_direction_period_stage_and_reason(self) -> None:
        failure = format_failure(
            self.site,
            "network error: timeout",
            225,
            stage="抓取窗口",
        )

        self.assertTrue(failure.startswith("测试站 https://example.test/topic "))
        self.assertIn("方向:bottom", failure)
        self.assertIn("基准期:225期", failure)
        self.assertIn("失败阶段:抓取窗口", failure)
        self.assertIn("原因:网络请求失败", failure)

    def test_unknown_period_is_marked_auto_detection(self) -> None:
        failure = format_failure(self.site, "解析失败", 9999, stage="期数识别")

        self.assertIn("基准期:自动识别", failure)


if __name__ == "__main__":
    unittest.main()
