"""script_bundle 来源（kxusu 脚本包）的抓取层解包与解析接线测试。"""

from __future__ import annotations

import base64
import json
import unittest
import zlib
from dataclasses import replace

from dawei.application.scrape_service import load_source
from dawei.domain.errors import ScrapeError
from dawei.domain.models import SiteConfig
from dawei.infrastructure import source_adapters
from dawei.parsers import DEFAULT_REGISTRY
from dawei.parsers.common import html_to_lines  # noqa: F401  仅用于说明文档形状

SHELL_URL = "https://dh81162.0gln642de8.cyou/C0g7sscJIx.html"
BUNDLE_URL = "https://api.kxusu.com/js-153-10742?v=09184"


def encode_segment(text: str) -> str:
    """按真实站点的编码方式生成一段：raw deflate → url-safe base64。"""
    compressor = zlib.compressobj(level=9, wbits=-15)
    raw = compressor.compress(text.encode("utf-8")) + compressor.flush()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def make_bundle(segments: list[str], *, field: str = "_v54gOM") -> str:
    payload = json.dumps({field: [encode_segment(item) for item in segments], "_vt": "x"}, ensure_ascii=False)
    return f"var __jGr = '{payload}';"


def number_rows(numbers: list[str]) -> str:
    rows = [".".join(numbers[index:index + 12]) for index in (0, 12, 24)]
    return "<br>".join(rows)


def entry_html(issue: int, numbers: list[str], columns: str = "36码中特") -> str:
    return (
        f"<div>{issue}期╠<font color='#1140cb'>{columns}</font>╣开:發00准"
        f"<br><font color='#FF0000'>{number_rows(numbers)}</font></div>"
    )


def pool(offset: int) -> list[str]:
    base = [f"{value:02d}" for value in range(1, 50)]
    rotated = base[offset:] + base[:offset]
    return rotated[:36]


class DecodeScriptBundleTests(unittest.TestCase):
    def test_extract_bundle_url_requires_exactly_one(self) -> None:
        shell = f'<html><script src="{BUNDLE_URL}"></script></html>'
        self.assertEqual(source_adapters.extract_script_bundle_url(shell), BUNDLE_URL)
        with self.assertRaises(ScrapeError):
            source_adapters.extract_script_bundle_url("<html></html>")
        with self.assertRaises(ScrapeError):
            source_adapters.extract_script_bundle_url(
                '<script src="https://api.x.com/js-153-10742"></script>'
                '<script src="https://api.x.com/js-153-10743"></script>'
            )

    def test_extract_bundle_url_tolerates_asset_domain_rotation(self) -> None:
        """资源域名会轮换（实测 kxusu → ladrvh），按路径形态 + 同源识别。"""
        rotated = "https://api.ladrvh.com/js-153-10742?v=09194"
        shell = (
            '<script src="https://api.ladrvh.com/assets/js/runtime.js"></script>'
            f'<script src="{rotated}"></script>'
        )
        self.assertEqual(source_adapters.extract_script_bundle_url(shell), rotated)
        mismatched = (
            '<script src="https://api.ladrvh.com/assets/js/runtime.js"></script>'
            '<script src="https://evil.example/js-153-10742"></script>'
        )
        with self.assertRaises(ScrapeError) as context:
            source_adapters.extract_script_bundle_url(mismatched)
        self.assertIn("同源", str(context.exception))
        with self.assertRaises(ScrapeError):
            source_adapters.extract_script_bundle_url(
                '<script src="http://api.ladrvh.com/js-153-10742"></script>'
            )

    def test_decode_joins_segments_in_order(self) -> None:
        bundle = make_bundle(["<div>一</div>", "<div>二</div>"])
        self.assertEqual(source_adapters.decode_script_bundle(bundle), "<div>一</div><div>二</div>")

    def test_decode_rejects_missing_payload(self) -> None:
        with self.assertRaises(ScrapeError) as context:
            source_adapters.decode_script_bundle("var other = 1;")
        self.assertIn("__jGr", str(context.exception))

    def test_decode_rejects_bad_json_and_shape(self) -> None:
        with self.assertRaises(ScrapeError):
            source_adapters.decode_script_bundle("var __jGr = 'not-json';")
        with self.assertRaises(ScrapeError):
            source_adapters.decode_script_bundle("var __jGr = '[]';")
        with self.assertRaises(ScrapeError):
            source_adapters.decode_script_bundle(make_bundle([], field="other"))

    def test_decode_fails_closed_on_undecodable_segment(self) -> None:
        payload = json.dumps({"_v54gOM": [encode_segment("<div>好</div>"), "!!!not-base64!!!"]})
        with self.assertRaises(ScrapeError) as context:
            source_adapters.decode_script_bundle(f"var __jGr = '{payload}';")
        self.assertIn("第1段", str(context.exception))

    def test_decode_rejects_non_string_segment(self) -> None:
        payload = json.dumps({"_v54gOM": [123]})
        with self.assertRaises(ScrapeError):
            source_adapters.decode_script_bundle(f"var __jGr = '{payload}';")


class FetchScriptBundleDocumentTests(unittest.TestCase):
    def test_fetch_uses_shell_then_bundle(self) -> None:
        calls: list[tuple[str, int]] = []

        def fetcher(url: str, timeout: int) -> str:
            calls.append((url, timeout))
            if url == SHELL_URL:
                return f'<script src="{BUNDLE_URL}"></script>'
            return make_bundle(["<div>正文</div>"])

        config = site_config()
        document = source_adapters.fetch_script_bundle_document(config, 17, fetcher)
        self.assertEqual(document, "<div>正文</div>")
        self.assertEqual(calls, [(SHELL_URL, 17), (BUNDLE_URL, 17)])

    def test_fetch_rejects_empty_document(self) -> None:
        def fetcher(url: str, timeout: int) -> str:
            return f'<script src="{BUNDLE_URL}"></script>' if url == SHELL_URL else make_bundle(["   "])

        with self.assertRaises(ScrapeError) as context:
            source_adapters.fetch_script_bundle_document(site_config(), 5, fetcher)
        self.assertIn("空文档", str(context.exception))

    def test_load_source_uses_bundle_document_without_browser(self) -> None:
        def fetcher(url: str, timeout: int) -> str:
            return f'<script src="{BUNDLE_URL}"></script>' if url == SHELL_URL else make_bundle(["<div>正文</div>"])

        def forbidden(*args, **kwargs):  # pragma: no cover - 只用于断言不会被调用
            raise AssertionError("script_bundle 不应触发浏览器渲染")

        source = load_source(
            site_config(),
            20,
            fixed_issue=261,
            text_fetcher=fetcher,
            rendered_text_fetcher=forbidden,
            rendered_article_fetcher=forbidden,
        )
        self.assertEqual(source.document, "<div>正文</div>")
        self.assertFalse(source.rendered)
        self.assertIsNone(source.article)
        self.assertEqual(source.source_url, SHELL_URL)


class ScriptBundleParserIntegrationTests(unittest.TestCase):
    def test_three_rows_reads_target_period_from_bundle(self) -> None:
        expected = pool(0)
        other = pool(5)
        document = make_bundle([entry_html(261, expected), entry_html(260, other)])
        assembled = source_adapters.decode_script_bundle(document)
        parsed = DEFAULT_REGISTRY.parse(assembled, replace(site_config(), fixed_issue=261))
        self.assertEqual(parsed.issue, 261)
        self.assertEqual(list(parsed.numbers), expected)
        self.assertEqual(parsed.evidence.parser_id, "three_rows")

    def test_adjacent_period_parses(self) -> None:
        expected = pool(3)
        document = source_adapters.decode_script_bundle(
            make_bundle([entry_html(261, pool(0)), entry_html(260, expected)])
        )
        parsed = DEFAULT_REGISTRY.parse(document, replace(site_config(), fixed_issue=260))
        self.assertEqual(list(parsed.numbers), expected)

    def test_missing_period_fails(self) -> None:
        document = source_adapters.decode_script_bundle(make_bundle([entry_html(260, pool(0))]))
        with self.assertRaises(ScrapeError):
            DEFAULT_REGISTRY.parse(document, replace(site_config(), fixed_issue=999))

    def test_wrong_columns_keyword_yields_no_candidate(self) -> None:
        document = source_adapters.decode_script_bundle(make_bundle([entry_html(261, pool(0))]))
        wrong = replace(site_config(), keywords=("四字平特",), section_keywords=("四字平特",), fixed_issue=261)
        with self.assertRaises(ScrapeError):
            DEFAULT_REGISTRY.parse(document, wrong)

    def test_same_period_conflict_fails(self) -> None:
        document = source_adapters.decode_script_bundle(
            make_bundle([entry_html(261, pool(0)), entry_html(261, pool(7))])
        )
        with self.assertRaises(ScrapeError) as context:
            DEFAULT_REGISTRY.parse(document, replace(site_config(), fixed_issue=261))
        self.assertIn("冲突", str(context.exception))


def site_config() -> SiteConfig:
    return SiteConfig(
        "旦夕朝暮",
        SHELL_URL,
        keywords=("36码中特",),
        section_keywords=("36码中特",),
        position="tail",
        region="bottom",
        source_type="script_bundle",
        parser_id="three_rows",
        search_window=520,
    )


if __name__ == "__main__":
    unittest.main()
