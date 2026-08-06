from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dawei.application.batch_service import (
    BatchOptions,
    SingleIssueBatchService,
    cache_update_allowed,
    format_failure,
    write_multi_failure_summary,
)
from dawei.domain.errors import ScrapeError
from dawei.domain.models import ParsedRecord, SiteConfig
from tests.support.evidence_factory import record as evidence_record


NUMBERS = tuple(f"{value:02d}" for value in range(1, 37))


class BatchServiceTests(unittest.TestCase):
    def test_cache_update_threshold_is_strictly_above_85_percent(self) -> None:
        self.assertFalse(cache_update_allowed(0, 0))
        self.assertFalse(cache_update_allowed(17, 20))
        self.assertTrue(cache_update_allowed(18, 20))

    def test_batch_skips_cache_at_85_percent_and_preserves_failure_txt(self) -> None:
        sites = tuple(
            SiteConfig(
                f"站点{index}",
                f"https://example.test/{index}",
                site_id=f"site-{index}",
                parser_id="generic_36",
                region="bottom",
            )
            for index in range(20)
        )

        def scrape(config: SiteConfig, *, timeout: int, fixed_issue: int | None):
            if int(config.name[2:]) >= 17:
                raise ScrapeError("站点抓取失败")
            return evidence_record(config.name, config.url, fixed_issue or 0, NUMBERS)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_path = root / "recent.json"
            cache_path.write_text("unchanged", encoding="utf-8")
            output = root / "success.txt"
            errors = root / "failure.txt"
            result = SingleIssueBatchService(site_scraper=scrape).run(
                sites,
                BatchOptions(
                    fixed_issue=210,
                    output_path=output,
                    error_output_path=errors,
                    recent_cache_path=cache_path,
                ),
            )

            self.assertEqual(len(result.results), 17)
            self.assertEqual(len(result.failures), 3)
            self.assertFalse(result.cache_updated)
            self.assertEqual(cache_path.read_text(encoding="utf-8"), "unchanged")
            self.assertTrue(errors.exists())

    def test_batch_above_85_percent_updates_json_with_failure_marks(self) -> None:
        sites = tuple(
            SiteConfig(
                f"站点{index}",
                f"https://example.test/{index}",
                site_id=f"site-{index}",
                parser_id="generic_36",
                region="bottom",
            )
            for index in range(20)
        )

        def scrape(config: SiteConfig, *, timeout: int, fixed_issue: int | None):
            if int(config.name[2:]) >= 18:
                raise ScrapeError("站点抓取失败")
            return evidence_record(config.name, config.url, fixed_issue or 0, NUMBERS)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_path = root / "recent.json"
            result = SingleIssueBatchService(site_scraper=scrape).run(
                sites,
                BatchOptions(
                    fixed_issue=210,
                    output_path=root / "success.txt",
                    error_output_path=root / "failure.txt",
                    recent_cache_path=cache_path,
                ),
            )

            self.assertEqual(len(result.results), 18)
            self.assertEqual(len(result.failures), 2)
            self.assertTrue(result.cache_updated)
            payload = json.loads(cache_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(payload["failures"], list(result.failures))
            self.assertTrue(payload["incomplete"])

    def test_failure_detail_is_one_line_before_site_separator_is_added(self) -> None:
        site = SiteConfig("测试站", "https://example.test")

        failure = format_failure(site, ScrapeError("第一段\r\n\r\n第二段"))

        self.assertNotIn("\n", failure)
        self.assertIn("第一段 第二段", failure)

    def test_cache_failure_does_not_suppress_success_and_failure_txt(self) -> None:
        site = SiteConfig(
            "测试站",
            "https://example.test/topic/1",
            site_id="site-test",
            parser_id="generic_36",
            region="bottom",
        )

        def scrape(config: SiteConfig, *, timeout: int, fixed_issue: int | None):
            return evidence_record(config.name, config.url, fixed_issue or 0, NUMBERS)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "success.txt"
            errors = root / "failure.txt"
            service = SingleIssueBatchService(site_scraper=scrape)
            with mock.patch.object(
                service,
                "_update_cache",
                side_effect=ScrapeError("cache conflict"),
            ):
                with self.assertRaisesRegex(ScrapeError, "cache conflict"):
                    service.run(
                        (site,),
                        BatchOptions(
                            fixed_issue=210,
                            output_path=output,
                            error_output_path=errors,
                        ),
                    )

            self.assertEqual(output.read_text(encoding="utf-8-sig"), f"{','.join(NUMBERS)} 测试站\n")
            self.assertFalse(errors.exists())

    def test_multi_summary_lists_only_sites_failed_in_every_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "209期-大围-失败.txt").write_text(
                "甲 https://a.test 原因A\n\n乙 https://b.test 原因B\n",
                encoding="utf-8-sig",
            )
            (root / "210期-大围-失败.txt").write_text(
                "甲 https://a.test 原因C\n",
                encoding="utf-8-sig",
            )
            path = write_multi_failure_summary((209, 210), root)
            text = path.read_text(encoding="utf-8-sig")

        self.assertIn("甲 https://a.test", text)
        self.assertNotIn("乙 https://b.test", text)

    def test_single_issue_writes_compatible_txt_without_issue_suffix(self) -> None:
        site = SiteConfig(
            "测试站",
            "https://example.test/topic/1",
            site_id="site-test",
            parser_id="generic_36",
            region="bottom",
        )
        calls: list[int | None] = []

        def scrape(config: SiteConfig, *, timeout: int, fixed_issue: int | None):
            calls.append(fixed_issue)
            return evidence_record(config.name, config.url, fixed_issue or 0, NUMBERS)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "success.txt"
            errors = root / "failure.txt"
            result = SingleIssueBatchService(site_scraper=scrape).run(
                (site,),
                BatchOptions(
                    fixed_issue=210,
                    output_path=output,
                    error_output_path=errors,
                    update_recent_cache=False,
                ),
            )

            self.assertEqual(calls, [210])
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(output.read_text(encoding="utf-8-sig"), f"{','.join(NUMBERS)} 测试站\n")
            self.assertFalse(errors.exists())

    def test_output_preserves_webpage_number_order(self) -> None:
        numbers = tuple(f"{value:02d}" for value in (*range(36, 0, -1),))
        result = ParsedRecord("测试站", "https://example.test", 210, numbers)

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "success.txt"
            from dawei.application.batch_service import write_results

            write_results((result,), output)

            self.assertEqual(
                output.read_text(encoding="utf-8-sig"),
                f"{','.join(numbers)} 测试站\n",
            )

    def test_merge_with_without_current_source_evidence_is_rejected(self) -> None:
        site = SiteConfig(
            "测试站",
            "https://example.test/topic/1",
            site_id="site-test",
            parser_id="generic_36",
            region="bottom",
        )

        def scrape(config: SiteConfig, *, timeout: int, fixed_issue: int | None):
            raise ScrapeError("当前网页抓取失败")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            merge_file = root / "old.txt"
            merge_file.write_text(f"{','.join(NUMBERS)} 测试站\n", encoding="utf-8-sig")

            with self.assertRaisesRegex(ScrapeError, "merge-with.*禁止"):
                SingleIssueBatchService(site_scraper=scrape).run(
                    (site,),
                    BatchOptions(
                        fixed_issue=210,
                        output_path=root / "success.txt",
                        error_output_path=root / "failure.txt",
                        update_recent_cache=False,
                        merge_with=merge_file,
                    ),
                )


if __name__ == "__main__":
    unittest.main()
