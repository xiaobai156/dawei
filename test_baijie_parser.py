import unittest

from dawei.domain.errors import ScrapeError
from dawei.domain.models import SiteConfig
from dawei.parsers.specials.dedicated import extract_baijie_zuizhun

CONFIG = SiteConfig(
    name="白姐最准",
    url="https://66642.net/play_detail.php?lottery=MACAU&id=4&issue=2026265",
    keywords=("白姐最准",),
    section_keywords=("高手36码",),
    region="top",
    site_id="candidate",
    source_type="generic_html",
    parser_id="baijie_zuizhun",
    fixed_issue=265,
)

HTML = """
<div class="pf-default-line">第265期: <b>白姐最准</b>【高手36码】：
01、02、03、04、05、06、07、08、09、12、13、14、20、21、23、24、27、28、29、30、31、32、33、34、35、36、37、38、39、40、41、42、43、45、48、49 开：? 待开</div>
<div class="pf-default-line">第264期: <b>白姐最准</b>【高手36码】：
02、03、04、05、08、09、11、12、13、14、15、17、20、21、23、24、26、28、29、30、32、33、34、36、37、38、39、40、41、42、43、44、45、46、47、49 开：21 准</div>
"""


class BaijieParserTests(unittest.TestCase):
    def test_parses_same_line_numbers_after_anchor(self):
        result = extract_baijie_zuizhun(HTML, CONFIG)
        self.assertEqual(result.issue, 265)
        self.assertEqual(len(result.numbers), 36)
        self.assertEqual(result.numbers[:4], ("01", "02", "03", "04"))
        self.assertEqual(result.numbers[-2:], ("48", "49"))
        self.assertIn("白姐最准", result.evidence.anchor_line)
        self.assertIn("高手36码", result.evidence.raw_issue_line)

    def test_rejects_wrong_title_or_incomplete_numbers(self):
        wrong_title = HTML.replace("白姐最准", "其他站点")
        incomplete = HTML.replace("、49 开", " 开")
        for text in (wrong_title, incomplete):
            with self.subTest(text=text), self.assertRaises(ScrapeError):
                extract_baijie_zuizhun(text, CONFIG)


if __name__ == "__main__":
    unittest.main()
