"""美人鱼专属解析：当期表与上一轮循环归档表的切分（archive-block exclusion）。

背景：该站在当期表（期号递减，例如 260→001）之后还附一段上一轮循环归档表
（期号从高位重新开始，例如 365→…）。归档表会以相同期号给出另一组号码。

修正前：归档表候选也进入同期冲突检查，导致指定期一律判"同期多候选冲突"。
修正后：专属解析只保留当期表（期号首次回升处切分），方向窗口与冲突检查都只在
当期表内进行；当期表内部若真有同期冲突，仍必须失败。
"""

from __future__ import annotations

import unittest

from dawei.domain.errors import ScrapeError
from dawei.domain.models import SiteConfig
from dawei.parsers import DEFAULT_REGISTRY
from dawei.parsers.generic_36 import generic_candidates
from dawei.parsers.specials.dedicated import (
    extract_meirenyu,
    meirenyu_candidates,
    meirenyu_current_cycle_split,
    meirenyu_effective_config,
)

POOL = [f"{number:02d}" for number in range(1, 50)]


def rows_for(seed: int) -> list[str]:
    """给出一组 36 个互不重复的两位数，固定排成 3 行 × 12 码。"""
    rotated = POOL[seed % len(POOL):] + POOL[: seed % len(POOL)]
    picked = rotated[:36]
    return [".".join(picked[index:index + 12]) for index in (0, 12, 24)]


def page(entries: list[tuple[int, list[str]]]) -> str:
    parts = ["<div>美人鱼-澳门</div>", "<div>36码</div>"]
    for issue, rows in entries:
        parts.append(f"<div>{issue}期围特【无错36码】00准</div>")
        parts.extend(f"<div>{row}</div>" for row in rows)
    return "\n".join(parts)


def site_config(issue: int | None = None) -> SiteConfig:
    return SiteConfig(
        "美人鱼",
        "https://meirenyu.test/topic/1.html",
        keywords=("无错36码",),
        section_keywords=("美人鱼",),
        region="top",
        position="top",
        source_type="topic_page",
        parser_id="meirenyu",
        fixed_issue=issue,
    )


def two_block_page(current: list[int], archive: list[int]) -> tuple[str, dict[int, str]]:
    """当期表（递减）+ 归档表（高位重新开始，且重复当前期号）。返回 HTML 与期望号码。"""
    entries: list[tuple[int, list[str]]] = []
    expected: dict[int, str] = {}
    for index, issue in enumerate(current):
        rows = rows_for(index)
        expected[issue] = ",".join(number for row in rows for number in row.split("."))
        entries.append((issue, rows))
    for index, issue in enumerate(archive):
        entries.append((issue, rows_for(100 + index)))
    return page(entries), expected


class CurrentCycleSplitTests(unittest.TestCase):
    def test_split_stops_at_first_issue_increase(self) -> None:
        html, _expected = two_block_page([260, 259, 258, 257], [365, 364, 260])
        effective = meirenyu_effective_config(html, site_config())
        candidates, invalid, _seen = generic_candidates(html, effective)
        self.assertEqual(len(candidates), 7)
        kept, kept_invalid, seen = meirenyu_current_cycle_split(candidates, invalid)
        self.assertEqual([candidate.issue for candidate in kept], [260, 259, 258, 257])
        self.assertEqual(kept_invalid, [])
        self.assertEqual(seen, {260, 259, 258, 257})

    def test_split_without_archive_keeps_everything(self) -> None:
        html, _expected = two_block_page([260, 259, 258], [])
        effective = meirenyu_effective_config(html, site_config())
        candidates, invalid, _seen = generic_candidates(html, effective)
        kept, _kept_invalid, _seen = meirenyu_current_cycle_split(candidates, invalid)
        self.assertEqual([candidate.issue for candidate in kept], [260, 259, 258])

    def test_repeated_issue_inside_current_block_is_not_a_boundary(self) -> None:
        """当期表内部重复展示同一期号，不得被当成归档起点切掉。"""
        html = page([(260, rows_for(0)), (259, rows_for(1)), (260, rows_for(2))])
        effective = meirenyu_effective_config(html, site_config())
        candidates, invalid, _seen = generic_candidates(html, effective)
        kept, _kept_invalid, _seen = meirenyu_current_cycle_split(candidates, invalid)
        self.assertEqual([candidate.issue for candidate in kept], [260, 259, 260])
        variants = {candidate.numbers for candidate in kept if candidate.issue == 260}
        self.assertEqual(len(variants), 2)


class ArchiveBlockExclusionTests(unittest.TestCase):
    def test_archive_duplicate_of_target_issue_is_excluded(self) -> None:
        """归档表里的同期另一组号码不得参与冲突检查。"""
        html, expected = two_block_page([260, 259, 258, 257], [365, 364, 260])
        effective = meirenyu_effective_config(html, site_config())

        raw_candidates, _invalid, _seen = generic_candidates(html, effective)
        raw_variants = {candidate.numbers for candidate in raw_candidates if candidate.issue == 260}
        self.assertEqual(len(raw_variants), 2, "前置条件：未切分时 260 期应有两组不同号码")

        kept, _invalid, _seen = meirenyu_candidates(html, site_config())
        kept_variants = {candidate.numbers for candidate in kept if candidate.issue == 260}
        self.assertEqual(len(kept_variants), 1)

        result = extract_meirenyu(html, site_config(260))
        self.assertEqual(result.issue, 260)
        self.assertEqual(",".join(result.numbers), expected[260])

    def test_registry_path_resolves_target_from_current_block(self) -> None:
        html, expected = two_block_page([260, 259, 258, 257], [365, 364, 260])
        result = DEFAULT_REGISTRY.parse(html, site_config(260))
        self.assertEqual(result.issue, 260)
        self.assertEqual(",".join(result.numbers), expected[260])
        self.assertIsNotNone(result.evidence)
        self.assertEqual(result.evidence.parser_id, "generic_36")

    def test_archive_only_issue_is_not_reachable(self) -> None:
        html, _expected = two_block_page([260, 259, 258, 257], [365, 364, 363])
        with self.assertRaises(ScrapeError) as context:
            extract_meirenyu(html, site_config(364))
        self.assertNotIn("多个高可信候选", str(context.exception))
        self.assertIn("364", str(context.exception))

    def test_current_block_keeps_original_order_and_numbers(self) -> None:
        html, expected = two_block_page([260, 259, 258], [365, 260])
        result = extract_meirenyu(html, site_config(259))
        self.assertEqual(",".join(result.numbers), expected[259])
        self.assertEqual(result.numbers, tuple(expected[259].split(",")))


class ConflictStillFailsTests(unittest.TestCase):
    def test_conflict_inside_current_block_still_fails(self) -> None:
        """当期表内部出现同期的两组不同号码时，仍必须失败。"""
        entries = [
            (260, rows_for(0)),
            (259, rows_for(1)),
            (260, rows_for(2)),
            (258, rows_for(3)),
        ]
        html = page(entries)
        with self.assertRaises(ScrapeError) as context:
            extract_meirenyu(html, site_config(260))
        self.assertIn("冲突", str(context.exception))

    def test_direction_window_still_rejects_old_issue(self) -> None:
        """切分不替代方向窗口：当前表内但超出最近 5 组的期数仍按契约失败。"""
        current = list(range(260, 260 - 35, -1))
        html, _expected = two_block_page(current, [])
        with self.assertRaises(ScrapeError) as context:
            extract_meirenyu(html, site_config(230))
        self.assertIn("超出严格候选范围", str(context.exception))

    def test_identity_and_boundary_are_still_enforced(self) -> None:
        html_without_identity = page([(260, rows_for(0))]).replace("美人鱼-澳门", "其他站")
        with self.assertRaises(ScrapeError) as context:
            meirenyu_candidates(html_without_identity, site_config())
        self.assertIn("身份缺失", str(context.exception))

        html_without_boundary = page([(260, rows_for(0))]).replace("36码", "36碼")
        with self.assertRaises(ScrapeError) as context:
            meirenyu_candidates(html_without_boundary, site_config())
        self.assertIn("区块边界缺失", str(context.exception))


if __name__ == "__main__":
    unittest.main()
