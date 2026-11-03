"""澳彩家园专属解析：期号标签自 263 期起省略"期"字（"263:"）仍须可解析。

背景：该站原为 "262期: 〓澳彩家园〓 〓三十六码〓开:30准"，自 263 期起改写成
"263: …"。通用解析器要求标签带"期"，导致 263/264 期完全没有候选、并被误报为
"超出严格候选范围"。专属解析器允许省略"期"，其余候选契约不变。
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from dawei.domain.errors import ScrapeError
from dawei.domain.models import SiteConfig
from dawei.parsers import DEFAULT_REGISTRY
from dawei.parsers.registry import resolve_parser_id
from dawei.parsers.specials.dedicated import (
    aocaijiayuan_candidates,
    extract_aocaijiayuan,
)

POOL = [f"{number:02d}" for number in range(1, 50)]


def rows_for(seed: int) -> list[str]:
    rotated = POOL[seed % len(POOL):] + POOL[: seed % len(POOL)]
    picked = rotated[:36]
    return [".".join(picked[index:index + 12]) for index in (0, 12, 24)]


def entry(issue: int, seed: int, *, with_qi: bool) -> str:
    label = f"{issue}期:" if with_qi else f"{issue}:"
    rows = rows_for(seed)
    return (
        f"<div>{label} ⛩️『澳彩家园』 ⛩️三十六码⛩️开:30准</div>"
        + "".join(f"<div>【{row}】</div>" for row in rows)
    )


def page(entries: list[tuple[int, int, bool]]) -> str:
    return "\n".join(
        ["<div>264期：👑三十六码🐎上发财✔免费公开</div>"]
        + [entry(issue, seed, with_qi=with_qi) for issue, seed, with_qi in entries]
    )


def site_config(issue: int | None = None) -> SiteConfig:
    return SiteConfig(
        "澳彩家园",
        "https://sheuzjss.tgpcj-9w0vl-mwyhly.work:29422/article/admin/6a10cc5030a2246c4fe15acb?url=zfw",
        keywords=("三十六码",),
        section_keywords=("澳彩家园",),
        position="tail",
        region="bottom",
        source_type="dynamic_article",
        parser_id="aocaijiayuan",
        search_window=260,
        fixed_issue=issue,
    )


class AocaiJiayuanParserTests(unittest.TestCase):
    def test_registry_has_dedicated_parser(self) -> None:
        self.assertIn("aocaijiayuan", DEFAULT_REGISTRY.parser_ids)
        self.assertEqual(resolve_parser_id(site_config()), "aocaijiayuan")
        self.assertTrue(DEFAULT_REGISTRY.has_candidate_collector("aocaijiayuan"))

    def test_label_without_qi_is_parsed(self) -> None:
        html = page([(264, 0, False), (263, 5, False), (262, 9, True)])
        parsed = extract_aocaijiayuan(html, site_config(264))
        self.assertEqual(parsed.issue, 264)
        self.assertEqual(list(parsed.numbers), [n for row in rows_for(0) for n in row.split(".")])

    def test_old_label_with_qi_still_parsed(self) -> None:
        html = page([(262, 9, True)])
        parsed = extract_aocaijiayuan(html, site_config(262))
        self.assertEqual(parsed.issue, 262)
        self.assertEqual(len(parsed.numbers), 36)

    def test_generic_parser_would_miss_new_label(self) -> None:
        """对照：通用解析器对省略"期"的标签没有候选（证明专属解析器必要）。"""
        html = page([(264, 0, False)])
        with self.assertRaises(ScrapeError):
            DEFAULT_REGISTRY.parse(html, replace(site_config(264), parser_id="generic_36"))

    def test_missing_period_fails(self) -> None:
        html = page([(262, 9, True)])
        with self.assertRaises(ScrapeError):
            extract_aocaijiayuan(html, site_config(999))

    def test_wrong_section_keyword_yields_no_candidate(self) -> None:
        html = page([(264, 0, False)])
        wrong = replace(site_config(264), section_keywords=("别站",), keywords=("别站",))
        with self.assertRaises(ScrapeError):
            extract_aocaijiayuan(html, wrong)

    def test_same_period_conflict_fails(self) -> None:
        html = page([(264, 0, False), (264, 7, False)])
        with self.assertRaises(ScrapeError) as context:
            extract_aocaijiayuan(html, site_config(264))
        self.assertIn("冲突", str(context.exception))

    def test_invalid_numbers_are_reported(self) -> None:
        html = (
            "<div>264: ⛩️『澳彩家园』 ⛩️三十六码⛩️开:00准</div>\n"
            "<div>【01.02.03.04.05.06.07.08.09.10.11.12】</div>"
        )
        candidates, invalid, seen = aocaijiayuan_candidates(html, site_config())
        self.assertEqual(candidates, [])
        self.assertEqual(seen, {264})
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0][0], 264)

    def test_evidence_fields_are_complete(self) -> None:
        html = page([(264, 0, False)])
        parsed = extract_aocaijiayuan(html, site_config(264))
        evidence = parsed.evidence
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.parser_id, "aocaijiayuan")
        self.assertEqual(evidence.block_start <= evidence.page_index < evidence.block_end, True)
        self.assertIn("三十六码", evidence.anchor_line)
        self.assertEqual(parsed.raw_position, evidence.page_index)

    def test_bundled_target_period_wins_over_title_line(self) -> None:
        """标题行含 "264期：" 但无号码，不得被当成有效候选。"""
        html = page([(264, 0, False)])
        parsed = extract_aocaijiayuan(html, site_config(264))
        self.assertEqual(len(parsed.numbers), 36)
        self.assertNotIn("上发财", parsed.evidence.raw_number_lines[0])


if __name__ == "__main__":
    unittest.main()
