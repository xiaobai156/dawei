import unittest

from dawei.application.scrape_service import ScrapeService
from dawei.domain.errors import ScrapeError
from dawei.domain.models import SiteConfig
from dawei.infrastructure.source_adapters import (
    decode_baicaitong_script,
    fetch_baicaitong_document,
)


class BaiCaiTongSourceTests(unittest.TestCase):
    def test_decodes_literal_writeln_without_corrupting_unicode(self):
        script = (
            '\ufeff<!-- static -->\n'
            'document.writeln("<div>澳门百彩通【36码中特】</div>");\n'
            'document.writeln("<zt>第265期</zt>");\n'
        )
        self.assertEqual(
            decode_baicaitong_script(script),
            "<div>澳门百彩通【36码中特】</div><zt>第265期</zt>",
        )

    def test_rejects_script_without_target_markup(self):
        with self.assertRaises(ScrapeError):
            decode_baicaitong_script('document.writeln("<div>广告</div>");')

    def test_fetches_observed_same_site_script_only(self):
        config = SiteConfig("澳门百彩通", "https://a.909922.com/")
        script = 'document.writeln("<div>澳门百彩通【36码中特】 第265期</div>");'
        calls = []

        def fetch(url, timeout):
            calls.append((url, timeout))
            return script

        document, source_url = fetch_baicaitong_document(config, 20, fetch)
        self.assertIn("第265期", document)
        self.assertEqual(source_url, "https://a.909922.com/amsslm.aspx?&ContentType=js?v=0")
        self.assertEqual(len(calls), 1)

    def test_scrape_service_parses_target_issue_from_script_document(self):
        config = SiteConfig(
            "澳门百彩通",
            "https://a.909922.com/",
            keywords=("36码",),
            section_keywords=("澳门百彩通", "36码中特"),
            position="tail",
            region="bottom",
            source_type="script_document",
            parser_id="generic_36",
        )
        rows = (
            "21.24.06.02.32.47.49.41.11",
            "39.25.17.22.34.05.10.35.44",
            "37.09.31.14.48.03.04.15.12",
            "27.16.18.45.33.13.20.38.43",
        )
        script = 'document.writeln("<div>澳门百彩通【36码中特】</div>");'
        script += 'document.writeln("<tr><td>第265期<br>36码</td><td>");'
        script += "".join(f'document.writeln("<hs>{row}</hs><br>");' for row in rows)
        script += 'document.writeln("</td></tr>");'
        shell = "<!--36码中特--><script src='/amsslm.aspx?&ContentType=js?v='></script>"

        def fetch(url, timeout):
            return shell if url == config.url else script

        def unexpected_browser(*args, **kwargs):
            raise AssertionError("澳门百彩通不得走浏览器首页")

        result = ScrapeService(
            text_fetcher=fetch,
            rendered_text_fetcher=unexpected_browser,
            browser_first=True,
        ).scrape(
            config, timeout=20, fixed_issue=265
        )
        self.assertEqual(result.issue, 265)
        self.assertEqual(result.numbers, tuple(number for row in rows for number in row.split(".")))
        self.assertEqual(result.evidence.source_method, "script_document")
        self.assertIn("amsslm.aspx", result.evidence.document_url)


if __name__ == "__main__":
    unittest.main()
