#!/usr/bin/env python3
"""跨期借值防护回归测试。

覆盖：归档表截断（期号回升 / 期号跳变 / 跨年回绕）、正常递减不误伤、
截断后目标期消失则保持原候选（只减不增）、校验器期号原文硬校验。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dawei.domain.errors import ValidationError  # noqa: E402
from dawei.domain.models import CandidateEvidence, CandidateOrigin  # noqa: E402
from dawei.parsers.generic_36 import truncate_archive_cycle_candidates  # noqa: E402

NUMBERS = tuple(f"{value:02d}" for value in range(1, 37))
RAW_NUMBER_LINES = (
    ".".join(NUMBERS[:12]),
    ".".join(NUMBERS[12:24]),
    ".".join(NUMBERS[24:]),
)


def candidate(issue: int, page_index: int, raw_issue_line: str | None = None) -> CandidateEvidence:
    line = raw_issue_line if raw_issue_line is not None else f"{issue}期:36码中特"
    return CandidateEvidence(
        issue=issue,
        numbers=NUMBERS,
        raw_issue_line=line,
        raw_number_lines=RAW_NUMBER_LINES,
        anchor_line="36码中特",
        document_id="doc",
        document_url="https://example.invalid/page",
        source_method="http_text",
        block_id="block",
        block_start=0,
        block_end=10,
        page_index=page_index,
        block_index=0,
        parser_id="generic_36",
        origins=(),
    )
    top = CandidateOrigin(
        line,
        RAW_NUMBER_LINES,
        "36码中特",
        "doc",
        "https://example.invalid/page",
        "http_text",
        "block",
        0,
        10,
        page_index,
        0,
        "generic_36",
    )
    return CandidateEvidence(
        issue=issue,
        numbers=NUMBERS,
        raw_issue_line=line,
        raw_number_lines=RAW_NUMBER_LINES,
        anchor_line="36码中特",
        document_id="doc",
        document_url="https://example.invalid/page",
        source_method="http_text",
        block_id="block",
        block_start=0,
        block_end=10,
        page_index=page_index,
        block_index=0,
        parser_id="generic_36",
        origins=(top,),
    )


class ArchiveCycleTruncationTests(unittest.TestCase):
    def test_issue_rollback_starts_archive(self) -> None:
        """期号回升（293、292 后出现 365）视为归档表，截断在其前。"""
        candidates = [candidate(293, 10), candidate(292, 20), candidate(365, 30)]
        kept = truncate_archive_cycle_candidates(candidates, 293)
        self.assertEqual([293, 292], [item.issue for item in kept])

    def test_issue_jump_starts_archive(self) -> None:
        """相邻期号间隔超过阈值（293→292→280）视为换表，截断在跳变前。"""
        candidates = [candidate(293, 10), candidate(292, 20), candidate(280, 30)]
        kept = truncate_archive_cycle_candidates(candidates, 293)
        self.assertEqual([293, 292], [item.issue for item in kept])

    def test_normal_descending_kept_intact(self) -> None:
        """正常连续递减的当期表不得被截断。"""
        candidates = [candidate(293, 10), candidate(292, 20), candidate(291, 30)]
        kept = truncate_archive_cycle_candidates(candidates, 291)
        self.assertEqual([293, 292, 291], [item.issue for item in kept])

    def test_year_wrap_is_not_archive(self) -> None:
        """跨年 365→001 属正常回绕，不得当成归档表。"""
        candidates = [candidate(1, 10), candidate(365, 20), candidate(364, 30)]
        kept = truncate_archive_cycle_candidates(candidates, 1)
        self.assertEqual([1], [item.issue for item in kept])

    def test_truncation_never_adds_target(self) -> None:
        """截断后目标期消失时，必须原样返回（只减不增，不改既有失败语义）。"""
        candidates = [candidate(293, 10), candidate(292, 20), candidate(365, 30)]
        kept = truncate_archive_cycle_candidates(candidates, 365)
        self.assertEqual(
            [293, 292, 365],
            [item.issue for item in kept],
            "目标期原本只在归档段内时，不得因截断改变可选性",
        )

    def test_empty_candidates_safe(self) -> None:
        self.assertEqual([], truncate_archive_cycle_candidates([], 298))

    def test_seamless_archive_table_rejected(self) -> None:
        """归档表期号无缝连续（当期293、292后接291、290）时必须拒绝取291。

        当期表只覆盖最近若干期；指定期落后页面最新期超过窗口时，说明它只可能
        来自归档区，必须拒绝，而不是把旧年同期号码当本期。
        """
        # 真实形态：当期表 293、292 之后接上一轮归档表 291、290…280
        candidates = [candidate(293, 10), candidate(292, 20)] + [
            candidate(issue, 30 + (291 - issue) * 5) for issue in range(291, 279, -1)
        ]
        from dawei.domain.errors import ScrapeError

        with self.assertRaises(ScrapeError) as ctx:
            truncate_archive_cycle_candidates(candidates, 291)
        self.assertIn("跨期借值防护", str(ctx.exception))

    def test_current_cycle_window_kept(self) -> None:
        """当期窗口内的目标期（落后最新期 <= 窗口）必须保留。"""
        candidates = [candidate(293, 10), candidate(292, 20), candidate(291, 30)]
        kept = truncate_archive_cycle_candidates(candidates, 293)
        self.assertIn(293, [item.issue for item in kept])


def _record(evidence, issue):
    from dawei.domain.models import ParsedRecord

    return ParsedRecord(
        name="测试站",
        url="https://example.invalid/page",
        issue=issue,
        numbers=NUMBERS,
        raw_position=evidence.page_index,
        evidence=evidence,
    )


class RawIssueLineValidationTests(unittest.TestCase):
    """只验证期号原文校验这一条规则（不依赖完整证据链）。"""

    @staticmethod
    def _check(issue: int, raw_issue_line: str, source_method: str = "http_text") -> None:
        from dawei.domain.validation import _validate_raw_issue_line

        _validate_raw_issue_line(issue, raw_issue_line, source_method)

    def test_mismatched_raw_issue_line_rejected(self) -> None:
        """期号字段 291、原文写 292 期 ⇒ 必须拒绝（跨期借值第二道防线）。"""
        with self.assertRaises(ValidationError) as ctx:
            self._check(291, "292期:36码中特")
        self.assertIn("原始期数行与期号不符", str(ctx.exception))

    def test_matched_raw_issue_line_accepted(self) -> None:
        self._check(291, "291期:36码中特")

    def test_empty_raw_issue_line_accepted(self) -> None:
        """原文无期数行时不做该判断（由其它校验负责）。"""
        self._check(291, "")

    def test_image_source_skipped(self) -> None:
        """图片/OCR 来源跳过该校验。"""
        self._check(291, "292期:36码中特", source_method="image_ocr")


if __name__ == "__main__":
    unittest.main()


class TitleLineShadowingTests(unittest.TestCase):
    """标题行先命中期号、正文在后面时，必须继续向后找到真实号码。

    站点版式：开头一行「299期：【站名】●【三十六码】标题」只是标题，
    后面紧跟作者/浏览量/旧期数据；真正的本期三行号码在页面末尾，
    期号行写作「299期:三十六码开:00准」。
    旧实现在标题行取号码失败后直接记无效并跳过，导致整站解析失败。
    """

    HTML = (
        "<div>"
        "<p>299期：【六合特供】●【三十六码】高手研究</p>"
        "<p>作者：六合特供 浏览量：40404</p>"
        "<p>265期:三十六码开:马49准</p>"
        "<p>【01.02.03.04.05.06.08.09.10.11.13.14】</p>"
        "<p>【15.16.17.19.20.21.22.24.26.27.28.29】</p>"
        "<p>【30.31.32.33.36.38.41.42.43.44.45.49】</p>"
        "<p>299期:三十六码开:00准</p>"
        "<p>【01.02.03.06.07.08.09.10.11.12.13.14】</p>"
        "<p>【15.16.17.20.21.22.23.24.26.27.28.29】</p>"
        "<p>【31.33.34.35.36.38.40.41.42.45.46.48】</p>"
        "</div>"
    )

    def _config(self):
        from dataclasses import replace

        from dawei.infrastructure.config_repository import ConfigRepository

        cfg = ConfigRepository(ROOT / "sites_36.json").load()
        tpl = next(site for site in cfg if site.name == "奇迹再现")
        return replace(
            tpl,
            name="六合特供",
            section_keywords=("三十六码",),
            keywords=("三十六码",),
            fixed_issue=299,
        )

    def test_numbers_found_after_title_line(self) -> None:
        from dawei.parsers.three_rows import onboarded_manager_article_candidates

        candidates, _invalid, _seen = onboarded_manager_article_candidates(
            self.HTML, self._config()
        )
        issues = [candidate.issue for candidate in candidates]
        self.assertIn(299, issues, "标题行不应挡住页面末尾的真实 299 期号码")

    def test_real_numbers_not_title_section(self) -> None:
        from dawei.parsers.three_rows import onboarded_manager_article_candidates

        candidates, _invalid, _seen = onboarded_manager_article_candidates(
            self.HTML, self._config()
        )
        target = next(c for c in candidates if c.issue == 299)
        self.assertEqual(36, len(target.numbers))
        self.assertEqual("01", target.numbers[0], "必须取页面末尾正文的号码，而不是标题附近")
        self.assertEqual("48", target.numbers[-1])
