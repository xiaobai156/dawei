from __future__ import annotations

import json
import unittest
import base64

from dawei.domain.errors import ScrapeError
from dawei.domain.models import SiteConfig
from dawei.infrastructure.source_adapters import (
    api_payload_to_html,
    article_detail_id,
    article_record_from_payload,
    decode_base64_chunks,
    decode_document_writeln_chunks,
    decode_possible_base64_text,
    fetch_expanded_html,
    iframe_urls_from,
    payload_contains_record_id,
    script_urls_from,
)
from dawei.parsers.generic_36 import extract_generic_36


def config() -> SiteConfig:
    return SiteConfig(
        name="目标作者",
        url="https://example.test/article/manager/target-id?url=x",
        keywords=("三十六码",),
        section_keywords=("目标作者",),
        region="bottom",
        site_id="target-site",
        source_type="dynamic_article",
        parser_id="dynamic_article",
        record_id="target-id",
        render_policy="fallback",
    )


def article(record_id: str, marker: str, author: str = "目标作者") -> dict[str, object]:
    return {
        "id": record_id,
        "authorNickname": author,
        "title": "210期 三十六码",
        "content": f"目标作者 三十六码 {marker}",
    }


class SourceAdapterTests(unittest.TestCase):
    def test_encoded_and_script_document_helpers_preserve_only_valid_content(self) -> None:
        encoded = base64.b64encode("<p>中文栏目</p>".encode()).decode()

        self.assertEqual(decode_base64_chunks(f'strdecode("{encoded}")'), "<p>中文栏目</p>")
        self.assertEqual(decode_possible_base64_text(encoded), "<p>中文栏目</p>")
        self.assertIsNone(decode_possible_base64_text("plain text"))
        self.assertEqual(
            decode_document_writeln_chunks(r'document.writeln("<p>\'引号\'</p>");'),
            "<p>'引号'</p>",
        )

    def test_script_and_iframe_discovery_enforces_document_origins(self) -> None:
        html = """
        <script src="/upload/script/a.js"></script>
        <script src="https://cdn.test/ordinary.js"></script>
        <script src="https://cdn.test/upload/script/b.js"></script>
        <iframe src="/frame"></iframe>
        <iframe src="https://other.test/frame"></iframe>
        <iframe src="{template[value]}"></iframe>
        """

        self.assertEqual(
            script_urls_from(html, "https://example.test/page"),
            [
                "https://example.test/upload/script/a.js",
                "https://cdn.test/upload/script/b.js",
            ],
        )
        self.assertEqual(
            iframe_urls_from(html, "https://example.test/page"),
            ["https://example.test/frame"],
        )

    def test_expansion_ignores_failed_optional_script_but_limits_iframe_depth(self) -> None:
        def failed_script_fetcher(url: str, _timeout: int) -> str:
            if url.endswith("a.js"):
                raise ScrapeError("optional script failed")
            return '<p>正文</p><script src="/upload/script/a.js"></script>'

        self.assertIn(
            "正文",
            fetch_expanded_html("https://example.test/page", 1, failed_script_fetcher),
        )

        def recursive_fetcher(url: str, _timeout: int) -> str:
            index = int(url.rsplit("/", 1)[-1])
            return f'<iframe src="/{index + 1}"></iframe>'

        with self.assertRaisesRegex(ScrapeError, "过多iframe"):
            fetch_expanded_html("https://example.test/0", 1, recursive_fetcher)

    def test_dynamic_identity_helpers_reject_missing_or_mismatched_ids(self) -> None:
        self.assertTrue(payload_contains_record_id('{"data":{"id":"target-id"}}', "target-id"))
        self.assertFalse(payload_contains_record_id("not-json", "target-id"))
        missing = SiteConfig(name="目标作者", url="https://example.test/home")
        mismatch = SiteConfig(
            name="目标作者",
            url="https://example.test/article/admin/url-id",
            record_id="configured-id",
        )
        with self.assertRaisesRegex(ScrapeError, "没有唯一记录ID"):
            article_detail_id(missing)
        with self.assertRaisesRegex(ScrapeError, "配置记录ID与URL不一致"):
            article_detail_id(mismatch)

    def test_dynamic_payload_and_aggregate_payload_fail_closed(self) -> None:
        with self.assertRaisesRegex(ScrapeError, "not valid JSON"):
            article_record_from_payload("not-json", config())
        with self.assertRaisesRegex(ScrapeError, "API空壳"):
            article_record_from_payload('{"data": {"title": "empty"}}', config())
        with self.assertRaisesRegex(ScrapeError, "ID不匹配"):
            article_record_from_payload('{"data": {"id": "other"}}', config())
        with self.assertRaisesRegex(ScrapeError, "not valid JSON"):
            api_payload_to_html("not-json")
        with self.assertRaisesRegex(ScrapeError, "没有唯一记录ID"):
            api_payload_to_html('{"data":[{"content":"a"},{"content":"b"}]}', False)
        with self.assertRaisesRegex(ScrapeError, "did not contain record content"):
            api_payload_to_html('{"data":[{"id":"one"}]}')
        self.assertEqual(api_payload_to_html('{"data":{"content":"正文"}}'), "正文")

    def test_dynamic_payload_selects_only_exact_url_record_id(self) -> None:
        payload = json.dumps(
            {"data": [article("decoy-id", "DECOY"), article("target-id", "TARGET")]},
            ensure_ascii=False,
        )
        result = article_record_from_payload(payload, config(), source_path="https://api.test")

        self.assertEqual(result.record_id, "target-id")
        self.assertIn("TARGET", result.document)
        self.assertNotIn("DECOY", result.document)
        self.assertEqual(result.record_path, "https://api.test::$.data[1]")

    def test_dynamic_payload_rejects_duplicate_target_id(self) -> None:
        payload = json.dumps(
            {"data": [article("target-id", "ONE"), article("target-id", "TWO")]},
            ensure_ascii=False,
        )
        with self.assertRaisesRegex(ScrapeError, "多个同ID"):
            article_record_from_payload(payload, config())

    def test_title_and_body_cannot_be_combined_across_records(self) -> None:
        payload = json.dumps(
            {
                "data": [
                    {
                        "id": "target-id",
                        "authorNickname": "目标作者",
                        "title": "210期 三十六码",
                        "content": "目标作者 三十六码 TARGET_ONLY",
                    },
                    article("decoy-id", "DECOY_NUMBERS"),
                ]
            },
            ensure_ascii=False,
        )
        result = article_record_from_payload(payload, config())
        self.assertIn("TARGET_ONLY", result.body)
        self.assertNotIn("DECOY_NUMBERS", result.body)

    def test_dynamic_payload_rejects_wrong_author_after_id_match(self) -> None:
        payload = json.dumps({"data": [article("target-id", "TARGET", "其他作者")]}, ensure_ascii=False)
        with self.assertRaisesRegex(ScrapeError, "作者不匹配"):
            article_record_from_payload(payload, config())

    def test_script_expansion_uses_injected_fetcher(self) -> None:
        pages = {
            "https://example.test/page": '<script src="/upload/script/a.js"></script>',
            "https://example.test/upload/script/a.js": 'document.writeln("<p>210期 三十六码</p>");',
        }
        result = fetch_expanded_html(
            "https://example.test/page",
            timeout=2,
            fetcher=lambda url, timeout: pages[url],
        )
        self.assertIn("210期 三十六码", result)

    def test_same_origin_iframe_is_fetched_as_a_separate_document(self) -> None:
        pages = {
            "https://example.test/page": '<iframe src="/frame"></iframe>',
            "https://example.test/frame": "<h2>子文档</h2><p>210期 三十六码</p>",
        }
        result = fetch_expanded_html(
            "https://example.test/page",
            timeout=2,
            fetcher=lambda url, timeout: pages[url],
        )
        self.assertIn("210期 三十六码", result)
        self.assertIn("DAWEI_DOCUMENT_BOUNDARY", result)

    def test_template_placeholder_is_not_fetched_as_an_iframe(self) -> None:
        calls: list[str] = []

        def fetcher(url: str, _timeout: int) -> str:
            calls.append(url)
            return '<h2>目标站</h2><iframe src="${data[index]}"></iframe>'

        result = fetch_expanded_html(
            "https://example.test/page",
            timeout=2,
            fetcher=fetcher,
        )

        self.assertIn("目标站", result)
        self.assertEqual(calls, ["https://example.test/page"])

    def test_parent_anchor_cannot_be_combined_with_iframe_numbers(self) -> None:
        numbers = " ".join(f"{value:02d}" for value in range(1, 37))
        pages = {
            "https://example.test/page": '<h2>目标站</h2><iframe src="/frame"></iframe>',
            "https://example.test/frame": f"<p>210期 三十六码</p><p>{numbers}</p>",
        }
        document = fetch_expanded_html(
            "https://example.test/page",
            timeout=2,
            fetcher=lambda url, timeout: pages[url],
        )
        site = SiteConfig(
            "目标站",
            "https://example.test/page",
            ("三十六码",),
            ("目标站",),
            fixed_issue=210,
            region="bottom",
            site_id="target",
            parser_id="generic_36",
        )
        with self.assertRaises(ScrapeError):
            extract_generic_36(document, site)


if __name__ == "__main__":
    unittest.main()
