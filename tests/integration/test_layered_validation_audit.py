from __future__ import annotations

import unittest

from dawei.application.scrape_service import ScrapeService
from dawei.domain.errors import ScrapeError
from dawei.domain.models import SiteConfig
from dawei.parsers import DEFAULT_REGISTRY
from dawei.parsers.generic_36 import extract_generic_36


NUMBERS = tuple(f"{value:02d}" for value in range(1, 37))
ALT_NUMBERS = tuple(f"{value:02d}" for value in range(2, 38))


def candidate_lines(issue: int, numbers: tuple[str, ...] = NUMBERS, keyword: str = "36码中特"):
    return [f"{issue}期 {keyword}", " ".join(numbers)]


class LayeredValidationAuditTests(unittest.TestCase):
    def test_bottom_rejects_issue_outside_last_three_candidates(self) -> None:
        lines = []
        for issue in range(206, 212):
            lines.extend(candidate_lines(issue))
        config = SiteConfig(
            "测试站",
            "https://example.test",
            keywords=("36码中特",),
            fixed_issue=208,
            region="bottom",
            parser_id="generic_36",
        )

        with self.assertRaisesRegex(ScrapeError, r"最近3组.*209, 210, 211"):
            extract_generic_36("\n".join(lines), config)

    def test_top_rejects_issue_outside_first_three_candidates(self) -> None:
        lines = []
        for issue in range(206, 212):
            lines.extend(candidate_lines(issue))
        config = SiteConfig(
            "测试站",
            "https://example.test",
            keywords=("36码中特",),
            fixed_issue=209,
            region="top",
            parser_id="generic_36",
        )

        with self.assertRaisesRegex(ScrapeError, r"最近3组.*206, 207, 208"):
            extract_generic_36("\n".join(lines), config)

    def test_keyword_cannot_be_borrowed_from_previous_issue(self) -> None:
        document = "\n".join(
            [
                "目标栏目",
                *candidate_lines(210),
                *candidate_lines(211, ALT_NUMBERS, keyword="无关内容"),
            ]
        )
        config = SiteConfig(
            "测试站",
            "https://example.test",
            keywords=("36码中特",),
            section_keywords=("目标栏目",),
            fixed_issue=211,
            region="bottom",
            parser_id="generic_36",
        )

        with self.assertRaisesRegex(
            ScrapeError,
            "未找到指定211期|没有找到符合关键词|指定211期超出严格候选范围",
        ):
            extract_generic_36(document, config)

    def test_target_section_stops_before_foreign_section(self) -> None:
        document = "\n".join(
            [
                "目标栏目",
                *candidate_lines(210),
                "其他栏目 36码中特",
                *candidate_lines(211),
            ]
        )
        config = SiteConfig(
            "测试站",
            "https://example.test",
            keywords=("36码中特",),
            section_keywords=("目标栏目",),
            fixed_issue=211,
            region="bottom",
            parser_id="generic_36",
        )

        with self.assertRaisesRegex(
            ScrapeError,
            "未找到指定211期|没有找到符合关键词|指定211期超出严格候选范围",
        ):
            extract_generic_36(document, config)

    def test_auto_latest_rejects_same_issue_conflict(self) -> None:
        document = "\n".join(
            [
                *candidate_lines(126, NUMBERS),
                *candidate_lines(126, ALT_NUMBERS),
            ]
        )
        config = SiteConfig(
            "测试站",
            "https://example.test",
            keywords=("36码中特",),
            region="bottom",
            parser_id="generic_36",
        )

        with self.assertRaisesRegex(ScrapeError, "多个高可信候选.*冲突"):
            extract_generic_36(document, config)

    def test_equal_duplicate_candidates_preserve_both_origins(self) -> None:
        document = "\n".join(
            [
                "目标栏目",
                *candidate_lines(211),
                *candidate_lines(211),
            ]
        )
        config = SiteConfig(
            "测试站",
            "https://example.test",
            keywords=("36码中特",),
            section_keywords=("目标栏目",),
            fixed_issue=211,
            region="bottom",
            parser_id="generic_36",
        )

        result = extract_generic_36(document, config)

        self.assertIsNotNone(result.evidence)
        assert result.evidence is not None
        self.assertEqual(len(result.evidence.origins), 2)
        self.assertEqual(result.evidence.anchor_line, "目标栏目")
        self.assertEqual(result.evidence.raw_issue_line, "211期 36码中特")
        self.assertEqual(result.evidence.parser_id, "generic_36")

    def test_dedicated_parser_uses_same_candidate_evidence_contract(self) -> None:
        rows = [
            " ".join(NUMBERS[0:12]),
            " ".join(NUMBERS[12:24]),
            " ".join(NUMBERS[24:36]),
        ]
        document = "\n".join(["211期 测试站 三十六码", *rows])
        config = SiteConfig(
            "测试站",
            "https://example.test",
            keywords=("三十六码",),
            section_keywords=("测试站",),
            fixed_issue=211,
            region="bottom",
            parser_id="three_rows",
        )

        result = DEFAULT_REGISTRY.parse(document, config)

        self.assertIsNotNone(result.evidence)
        assert result.evidence is not None
        self.assertEqual(result.evidence.raw_issue_line, "211期 测试站 三十六码")
        self.assertEqual(result.evidence.raw_number_lines, tuple(rows))
        self.assertEqual(result.evidence.parser_id, "three_rows")

    def test_scrape_service_records_actual_source_method(self) -> None:
        document = "\n".join(["目标栏目", *candidate_lines(211)])
        config = SiteConfig(
            "测试站",
            "https://example.test",
            keywords=("36码中特",),
            section_keywords=("目标栏目",),
            region="bottom",
            parser_id="generic_36",
        )

        result = ScrapeService(text_fetcher=lambda url, timeout: document).scrape(
            config,
            fixed_issue=211,
        )

        self.assertIsNotNone(result.evidence)
        assert result.evidence is not None
        self.assertEqual(result.evidence.source_method, "expanded_html")


if __name__ == "__main__":
    unittest.main()
