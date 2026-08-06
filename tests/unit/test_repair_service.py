from __future__ import annotations

import unittest

from dawei.application.repair_service import RepairService, ResolvedCase
from dawei.application.scrape_service import ScrapeService
from dawei.domain.models import ParsedRecord, SiteConfig
from tests.support.evidence_factory import record as evidence_record


NUMBERS = tuple(f"{value:02d}" for value in range(1, 37))


class RepairServiceTests(unittest.TestCase):
    def test_validation_uses_one_real_fetch_for_diagnostics_and_formal_result(self) -> None:
        calls: list[str] = []
        document = (
            "<h2>测试站</h2><div>210期 三十六码</div><div>"
            + ",".join(NUMBERS)
            + "</div>"
        )
        config = SiteConfig(
            "测试站",
            "https://example.test/topic/1",
            ("三十六码",),
            ("测试站",),
            region="bottom",
            site_id="site-test",
            parser_id="generic_36",
        )

        def fetcher(url: str, timeout: int) -> str:
            calls.append(url)
            return document

        def parser(source: str, site: SiteConfig) -> ParsedRecord:
            self.assertEqual(source, document)
            return evidence_record(site.name, site.url, 210, NUMBERS, page_index=1)

        report = RepairService(
            ScrapeService(text_fetcher=fetcher, parser=parser)
        ).validate(ResolvedCase(config, 210), timeout=2)

        self.assertEqual(calls, [config.url])
        self.assertTrue(report.passed)
        self.assertEqual(report.formal_result.numbers, report.diagnostics.numbers)
        self.assertEqual(report.raw_position, 1)


if __name__ == "__main__":
    unittest.main()
