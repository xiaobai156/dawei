"""Offline equivalence tests for the generic_36 document boundary index."""

from __future__ import annotations

import unittest

from dawei.domain.models import SiteConfig
from dawei.parsers import common, generic_36


def _bounds(value: range) -> tuple[int, int]:
    return (value.start, value.stop)


def _document_lines() -> list[str]:
    return [
        "DAWEI_DOCUMENT_BOUNDARY",
        "专栏 36码",
        "111期 36码",
        "01.02.03.04.05.06",
        "DAWEI_DOCUMENT_BOUNDARY",
        "专栏 36码",
        "222期 36码",
        "07.08.09.10.11.12",
    ]


class DocumentBoundaryIndexTests(unittest.TestCase):
    def test_boundary_positions_collected(self) -> None:
        self.assertEqual(
            generic_36.document_boundary_positions(_document_lines()),
            (0, 4),
        )

    def test_range_helper_matches_linear_scan_for_every_index(self) -> None:
        lines = _document_lines()
        boundaries = generic_36.document_boundary_positions(lines)
        for index in range(len(lines)):
            self.assertEqual(
                list(generic_36.document_range_from_boundaries(lines, index, boundaries)),
                list(common.document_range_for_index(lines, index)),
                msg=f"index {index}",
            )

    def test_range_helper_treats_boundary_line_as_following(self) -> None:
        lines = _document_lines()
        boundaries = generic_36.document_boundary_positions(lines)
        self.assertEqual(
            _bounds(generic_36.document_range_from_boundaries(lines, 0, boundaries)),
            (0, 4),
        )
        self.assertEqual(
            _bounds(generic_36.document_range_from_boundaries(lines, 4, boundaries)),
            _bounds(common.document_range_for_index(lines, 4)),
        )
        self.assertEqual(
            _bounds(generic_36.document_range_from_boundaries(lines, 4, boundaries)),
            (1, 8),
        )

    def test_candidate_evidence_is_field_equivalent(self) -> None:
        lines = _document_lines()
        config = SiteConfig("测试站", "https://example.test", section_keywords=("36码",))
        boundaries = generic_36.document_boundary_positions(lines)
        cases = ((2, 111, range(1, 4)), (6, 222, range(5, 8)))
        for index, issue, line_range in cases:
            baseline = generic_36.candidate_evidence(
                lines, index, issue, ("01", "02"), config, line_range
            )
            indexed = generic_36.candidate_evidence(
                lines,
                index,
                issue,
                ("01", "02"),
                config,
                line_range,
                boundary_positions=boundaries,
            )
            self.assertEqual(baseline, indexed)
        self.assertNotEqual(
            generic_36.candidate_evidence(
                lines, 2, 111, ("01", "02"), config, range(1, 4)
            ).document_id,
            generic_36.candidate_evidence(
                lines, 6, 222, ("01", "02"), config, range(5, 8)
            ).document_id,
        )

    def test_generic_candidates_assigns_distinct_document_ids(self) -> None:
        numbers_doc_one = (
            "01.02.03.04.05.06.07.08.09.10.11.12",
            "13.14.15.16.17.18.19.20.21.22.23.24",
            "25.26.27.28.29.30.31.32.33.34.35.36",
        )
        numbers_doc_two = (
            "13.14.15.16.17.18.19.20.21.22.23.24",
            "25.26.27.28.29.30.31.32.33.34.35.36",
            "37.38.39.40.41.42.43.44.45.46.47.48",
        )
        text = "\n".join(
            [
                "DAWEI_DOCUMENT_BOUNDARY",
                "111期 36码",
                *numbers_doc_one,
                "DAWEI_DOCUMENT_BOUNDARY",
                "112期 36码",
                *numbers_doc_two,
            ]
        )
        config = SiteConfig("测试站", "https://example.test", keywords=("36码",), region="top")
        candidates, invalid, seen = generic_36.generic_candidates(text, config)
        self.assertEqual(invalid, [])
        self.assertEqual(seen, {111, 112})
        self.assertEqual(
            {candidate.document_id for candidate in candidates},
            {"document-1", "document-2"},
        )
        self.assertTrue(all(len(candidate.numbers) == 36 for candidate in candidates))


if __name__ == "__main__":
    unittest.main()
