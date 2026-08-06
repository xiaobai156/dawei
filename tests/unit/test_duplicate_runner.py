from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dawei.application.duplicate_runner import DuplicateOptions, DuplicateRunner
from dawei.application.duplicate_service import SiteWindow
from dawei.domain.models import ParsedRecord, SiteConfig
from dawei.infrastructure.config_repository import ConfigRepository


NUMBERS = tuple(f"{value:02d}" for value in range(1, 37))


class DuplicateRunnerTests(unittest.TestCase):
    def test_runner_compares_windows_by_issue_and_original_order(self) -> None:
        sites = (
            SiteConfig("甲", "https://a.test", site_id="a", parser_id="generic_36", region="bottom"),
            SiteConfig("乙", "https://b.test", site_id="b", parser_id="generic_36", region="bottom"),
        )

        def scrape(config, period, periods, min_records, timeout, text_fetcher):
            records = tuple(
                ParsedRecord(config.name, config.url, issue, NUMBERS)
                for issue in range(210, 204, -1)
            )
            return SiteWindow(config.name, config.url, period, periods, records, site_id=config.site_id)

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sites.json"
            ConfigRepository(config_path).save(sites)
            result = DuplicateRunner(window_scraper=scrape).run(
                DuplicateOptions(
                    sites_config=config_path,
                    backup_path=Path(temp_dir) / "recent.json",
                    period=210,
                    periods=6,
                    min_common=3,
                    duplicate_common=6,
                )
            )

        self.assertEqual(result.period, 210)
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].consecutive_count, 6)
        self.assertEqual(result.exit_code, 1)

    def test_runner_does_not_match_same_numbers_in_different_original_order(self) -> None:
        sites = (
            SiteConfig("甲", "https://a.test", site_id="a", parser_id="generic_36", region="bottom"),
            SiteConfig("乙", "https://b.test", site_id="b", parser_id="generic_36", region="bottom"),
        )

        def scrape(config, period, periods, min_records, timeout, text_fetcher):
            numbers = NUMBERS if config.name == "甲" else tuple(reversed(NUMBERS))
            records = tuple(
                ParsedRecord(config.name, config.url, issue, numbers)
                for issue in range(210, 204, -1)
            )
            return SiteWindow(config.name, config.url, period, periods, records, site_id=config.site_id)

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sites.json"
            ConfigRepository(config_path).save(sites)
            result = DuplicateRunner(window_scraper=scrape).run(
                DuplicateOptions(
                    sites_config=config_path,
                    backup_path=Path(temp_dir) / "recent.json",
                    period=210,
                    periods=6,
                    min_common=3,
                    duplicate_common=6,
                )
            )

        self.assertEqual(result.matches, ())
        self.assertEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
