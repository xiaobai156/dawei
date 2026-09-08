from __future__ import annotations

import json
import unittest
from pathlib import Path

from dawei.domain.errors import ScrapeError
from dawei.domain.models import (
    CandidateEvidence,
    CandidateOrigin,
    ParsedRecord,
    SiteConfig,
)
from dawei.parsers import DEFAULT_REGISTRY
from dawei.parsers.registry import ParserRegistry, extract_from_candidates
from dawei.parsers.specials.dedicated import extract_renjianrenai
from dawei.parsers.three_rows import extract_onboarded_manager_article

NUMBERS = tuple(f"{number:02d}" for number in range(1, 37))


def candidate(issue: int, page_index: int, numbers: tuple[str, ...] = NUMBERS) -> CandidateEvidence:
    origin = CandidateOrigin(
        raw_issue_line=f"{issue}期",
        raw_number_lines=(" ".join(numbers),),
        anchor_line="测试栏目",
        document_id="document-0",
        document_url="https://example.test",
        source_method="test",
        block_id=f"document-0:{page_index}-{page_index + 1}",
        block_start=page_index,
        block_end=page_index + 1,
        page_index=page_index,
        block_index=0,
        parser_id="test_parser",
    )
    return CandidateEvidence(
        issue=issue,
        numbers=numbers,
        raw_issue_line=origin.raw_issue_line,
        raw_number_lines=origin.raw_number_lines,
        anchor_line=origin.anchor_line,
        document_id=origin.document_id,
        document_url=origin.document_url,
        source_method=origin.source_method,
        block_id=origin.block_id,
        block_start=origin.block_start,
        block_end=origin.block_end,
        page_index=origin.page_index,
        block_index=origin.block_index,
        parser_id=origin.parser_id,
        origins=(origin,),
    )


def config(
    *,
    fixed_issue: int | None = None,
    region: str = "bottom",
    parser_id: str = "test_parser",
) -> SiteConfig:
    return SiteConfig(
        name="测试站",
        url="https://example.test",
        fixed_issue=fixed_issue,
        region=region,
        parser_id=parser_id,
    )


class SharedCandidateResultTests(unittest.TestCase):
    def test_selects_position_and_keeps_selected_evidence(self) -> None:
        values = [candidate(126, 3), candidate(126, 9)]

        result = extract_from_candidates(
            "ignored",
            config(fixed_issue=126),
            lambda _text, _config: (values, [], {126}),
            no_candidates_message="没有候选",
            issue_range_error="没有范围内候选",
        )

        self.assertEqual(result.numbers, NUMBERS)
        self.assertEqual(result.raw_position, 9)
        self.assertEqual(result.evidence.page_index, values[1].page_index)
        self.assertIn(values[1].origins[0], result.evidence.origins)

    def test_rejects_conflicting_same_issue_candidates(self) -> None:
        conflicting = tuple(reversed(NUMBERS))
        values = [candidate(126, 3), candidate(126, 9, conflicting)]

        with self.assertRaisesRegex(ScrapeError, "36码冲突"):
            extract_from_candidates(
                "ignored",
                config(fixed_issue=126),
                lambda _text, _config: (values, [], {126}),
                no_candidates_message="没有候选",
                issue_range_error="没有范围内候选",
            )

    def test_reports_fixed_issue_invalid_candidate_before_generic_failure(self) -> None:
        with self.assertRaisesRegex(ScrapeError, "126期数据无效: 数量不足"):
            extract_from_candidates(
                "ignored",
                config(fixed_issue=126),
                lambda _text, _config: ([], [(126, 4, "数量不足")], {126}),
                no_candidates_message="没有候选",
                issue_range_error="没有范围内候选",
            )

    def test_three_rows_parser_keeps_original_order_and_top_position(self) -> None:
        text = (
            "126期 测试栏 三十六码\n"
            "01 02 03 04 05 06 07 08 09 10 11 12\n"
            "13 14 15 16 17 18 19 20 21 22 23 24\n"
            "25 26 27 28 29 30 31 32 33 34 35 36"
        )
        site = SiteConfig(
            name="测试三行站",
            url="https://example.test/three-rows",
            keywords=("三十六码",),
            section_keywords=("测试栏",),
            fixed_issue=126,
            region="top",
            parser_id="three_rows",
        )

        result = extract_onboarded_manager_article(text, site)

        self.assertEqual(result.issue, 126)
        self.assertEqual(result.numbers, NUMBERS)
        self.assertEqual(result.raw_position, 0)

    def test_dedicated_parser_rejects_same_issue_conflict(self) -> None:
        text = "\n".join(
            [
                "126期 人见人爱 三十六码",
                " ".join(NUMBERS),
                "126期 人见人爱 三十六码",
                " ".join(reversed(NUMBERS)),
            ]
        )
        site = SiteConfig(
            name="测试专属站",
            url="https://example.test/dedicated",
            fixed_issue=126,
            region="bottom",
            parser_id="renjianrenai",
        )

        with self.assertRaisesRegex(ScrapeError, "36码冲突"):
            extract_renjianrenai(text, site)

    def test_every_active_parser_id_resolves(self) -> None:
        sites = json.loads(
            Path("sites_36.json").read_text(encoding="utf-8-sig")
        )
        active_ids = {site["parser_id"] for site in sites}

        self.assertTrue(active_ids <= set(DEFAULT_REGISTRY.parser_ids))


class ParserRegistryValidationTests(unittest.TestCase):
    def test_parse_does_not_recollect_candidates_after_wrapper(self) -> None:
        registry = ParserRegistry()
        calls: list[str] = []

        def collect(text: str, _site: SiteConfig):
            calls.append(text)
            evidence = candidate(126, 2)
            return [evidence], [], {evidence.issue}

        def parser(text: str, site: SiteConfig) -> ParsedRecord:
            evidence = collect(text, site)[0][0]
            return ParsedRecord(
                site.name,
                site.url,
                evidence.issue,
                evidence.numbers,
                raw_position=evidence.page_index,
                evidence=evidence,
            )

        registry.register("spy", parser, collect)

        result = registry.parse("document", config(parser_id="spy"))

        self.assertEqual(result.issue, 126)
        self.assertEqual(calls, ["document"])

    def test_parse_rejects_missing_candidate_evidence(self) -> None:
        registry = ParserRegistry()

        def parser(_text: str, site: SiteConfig) -> ParsedRecord:
            return ParsedRecord(site.name, site.url, 126, NUMBERS)

        registry.register("missing_evidence", parser)

        with self.assertRaisesRegex(ScrapeError, "missing_evidence.*候选证据缺失"):
            registry.parse(
                "document",
                config(parser_id="missing_evidence"),
            )

    def test_parse_rejects_missing_raw_position(self) -> None:
        registry = ParserRegistry()
        evidence = candidate(126, 2)

        def parser(_text: str, site: SiteConfig) -> ParsedRecord:
            return ParsedRecord(
                site.name,
                site.url,
                evidence.issue,
                evidence.numbers,
                evidence=evidence,
            )

        registry.register("missing_raw_position", parser)

        with self.assertRaisesRegex(ScrapeError, "missing_raw_position.*raw_position"):
            registry.parse(
                "document",
                config(parser_id="missing_raw_position"),
            )


if __name__ == "__main__":
    unittest.main()
