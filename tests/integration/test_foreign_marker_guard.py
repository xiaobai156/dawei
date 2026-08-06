import unittest

from tests.support import scrape_regression_api as scraper
from tests.support.evidence_factory import evidence


class ForeignMarkerGuardTests(unittest.TestCase):
    def test_foreign_36_marker_on_issue_line_is_rejected(self):
        config = scraper.SiteConfig(
            "host",
            "https://example.test",
            ("\u7b2c",),
            ("36\u7801\u56f4\u7279",),
            fixed_issue=138,
        )
        line = "138\u671f\u301036\u7801\u7206\u7279\u3011\u7279\u5f00:\u9a6c49\u51c6"

        self.assertTrue(scraper.candidate_foreign_strict_36_markers(line, config))

    def test_plain_issue_line_is_allowed_inside_strict_section(self):
        config = scraper.SiteConfig(
            "host",
            "https://example.test",
            ("\u7b2c",),
            ("36\u7801\u56f4\u7279",),
            fixed_issue=138,
        )
        line = "\u7b2c138\u671f"

        self.assertFalse(scraper.candidate_foreign_strict_36_markers(line, config))

    def test_number_collection_stops_before_foreign_section_marker(self):
        config = scraper.SiteConfig(
            "host",
            "https://example.test",
            ("\u7b2c",),
            ("36\u7801\u56f4\u7279",),
            fixed_issue=138,
        )
        lines = [
            "\u7b2c138\u671f",
            "138\u671f\u301036\u7801\u7206\u7279\u3011\u7279\u5f00:\u9a6c49\u51c6",
            " ".join(f"{number:02d}" for number in range(1, 37)),
        ]

        self.assertIsNone(scraper.collect_numbers_after_in_section(lines, 0, config))

    def test_region_bottom_chooses_last_matching_candidate(self):
        first = tuple(f"{number:02d}" for number in range(1, 37))
        second = tuple(f"{number:02d}" for number in range(14, 50))
        config = scraper.SiteConfig("host", "https://example.test", region="bottom")

        selected = scraper.select_candidate_for_position(
            [
                evidence("host", config.url, 144, first, page_index=10),
                evidence("host", config.url, 144, second, page_index=20),
            ],
            config,
        )

        self.assertEqual(selected.numbers, second)

    def test_region_top_chooses_first_matching_candidate(self):
        first = tuple(f"{number:02d}" for number in range(1, 37))
        second = tuple(f"{number:02d}" for number in range(14, 50))
        config = scraper.SiteConfig("host", "https://example.test", region="top")

        selected = scraper.select_candidate_for_position(
            [
                evidence("host", config.url, 144, first, page_index=10),
                evidence("host", config.url, 144, second, page_index=20),
            ],
            config,
        )

        self.assertEqual(selected.numbers, first)

    def test_merged_number_does_not_fill_from_recommendations(self):
        html = "\n".join(
            [
                "147\u671f: \u300e\u5e78\u798f\u5feb\u4e50\u300f \u4e09\u5341\u516d\u7801 \u5f00:00\u51c6",
                "\u301001.02.03.04.05.07.10.11.13.14.17.18\u3011",
                "\u301019.20.21.22.23.24.25.26.27.30.32.33\u3011",
                "\u301034.36.38.39.41.42.44.45.46.47.4849\u3011",
                "[ \u221a \u5df2\u89e3\u9501\u9605\u8bfb\u6743\u9650 ]",
                "\u5e7f\u544a",
                "\u76f8\u5173\u63a8\u8350 \u9ad8\u624b\u7814\u7a7605-27 \u5e78\u8fd0\u4e2d\u5956 05-27",
            ]
        )
        config = scraper.SiteConfig(
            "\u5e78\u798f\u5feb\u4e50",
            "https://example.test",
            ("\u4e09\u5341\u516d\u7801",),
            fixed_issue=147,
        )

        with self.assertRaises(scraper.ScrapeError) as context:
            scraper.extract_latest_36(html, config)

        message = str(context.exception)
        self.assertIn("\u7591\u4f3c\u8fde\u5199\u6570\u5b57: 4849", message)
        self.assertNotIn("\u6709\u6548\u53f7\u7801\u91cd\u590d", message)


if __name__ == "__main__":
    unittest.main()
