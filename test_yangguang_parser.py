import unittest

from dawei.domain.errors import ScrapeError
from dawei.domain.models import SiteConfig
from dawei.parsers.specials.dedicated import extract_yangguang_mingmei

CONFIG = SiteConfig(
    name="阳光明媚", url="https://b7sz3gaw48.552149.shop/bbs/topic.php?id=1278",
    keywords=("三十六码",), section_keywords=("阳光明媚",), region="bottom",
    site_id="candidate", source_type="bbs_topic", parser_id="yangguang_mingmei", fixed_issue=255,
)
HTML = """
255期:<span>《阳光明媚》</span> 三十六码 开:00准<br>
【03.04.05.06.09.11.12.13.14.15.17.18】<br>
【20.23.24.25.26.27.29.30.31.32.33.34】<br>
【35.38.39.40.41.43.44.45.46.47.48.49】
"""


class YangguangParserTests(unittest.TestCase):
    def test_parses_three_dot_rows_and_ignores_result_marker(self):
        result = extract_yangguang_mingmei(HTML, CONFIG)
        self.assertEqual(result.issue, 255)
        self.assertEqual(len(result.numbers), 36)
        self.assertEqual(result.numbers[:4], ("03", "04", "05", "06"))
        self.assertEqual(result.numbers[-2:], ("48", "49"))
        self.assertEqual(result.evidence.raw_number_lines, (
            "【03.04.05.06.09.11.12.13.14.15.17.18】",
            "【20.23.24.25.26.27.29.30.31.32.33.34】",
            "【35.38.39.40.41.43.44.45.46.47.48.49】",
        ))

    def test_wrong_issue_or_row_count_fails(self):
        for text in (HTML.replace("255期", "254期"), HTML.replace("35.38", "35.38.39")):
            with self.subTest(text=text), self.assertRaises(ScrapeError):
                extract_yangguang_mingmei(text, CONFIG)


if __name__ == "__main__":
    unittest.main()
