"""困难杂志详情正文补全（列表接口截断）行为测试。"""

from __future__ import annotations

import json
import unittest

from dawei.application.scrape_service import ScrapeService, load_collection_details
from dawei.domain.errors import ScrapeError
from dawei.domain.models import SiteConfig
from dawei.parsers import dynamic_article

FULL_NUMBERS = tuple(f"{number:02d}" for number in range(1, 37))
TRUNCATED_NUMBERS = FULL_NUMBERS[:24]


def _config() -> SiteConfig:
    return SiteConfig(
        site_id="site_kunnan",
        name="困难杂志",
        url="https://example.test/kunnan",
        keywords=("三十六码特",),
        section_keywords=(),
        position="tail",
        region="top",
        source_type="dynamic_collection",
        parser_id="kunnan_magazine",
        api_url="https://api.example/api/v1/users/3792/forums",
        search_window=260,
    )


def _rows(numbers: tuple[str, ...]) -> tuple[str, ...]:
    return (
        " ".join(numbers[:12]),
        " ".join(numbers[12:24]),
        " ".join(numbers[24:]),
    )


def _content(issue: int, numbers: tuple[str, ...]) -> str:
    """列表接口口径的正文：栏目行 + 期号行 + 数据行。"""
    rows = [row for row in _rows(numbers) if row]
    return "\n".join(("困难杂志 三十六码特", f"{issue}期【三十六码特】开00对", *rows))


def _detail_body(issue: int, numbers: tuple[str, ...]) -> str:
    rows = [row for row in _rows(numbers) if row]
    body = "".join(f"<p>{row}</p>" for row in rows)
    return json.dumps(
        {
            "id": "1",
            "content": (
                "<p>困难杂志 三十六码特</p>"
                f"<p>{issue}期【三十六码特】开00对</p>"
                f"{body}"
            ),
        },
        ensure_ascii=False,
    )


def _record(record_id: str, issue: int, content: str) -> dict:
    return {
        "user_id": "3792",
        "status": "published",
        "lottery": "macao",
        "topic": "困难杂志 三十六码特",
        "id": record_id,
        "draw": issue,
        "content": content,
    }


def _detail(url: str, issue: int, numbers: tuple[str, ...]) -> dynamic_article.KunnanDetail:
    return dynamic_article.parse_kunnan_detail(url, _detail_body(issue, numbers))


class DetailUrlTests(unittest.TestCase):
    def test_detail_url_keeps_api_prefix(self) -> None:
        url = dynamic_article.kunnan_magazine_detail_url(
            "https://api.example/api/v1/users/3792/forums",
            "16370085",
        )
        self.assertEqual("https://api.example/api/v1/forums/16370085", url)

    def test_detail_url_rejects_foreign_api_url(self) -> None:
        with self.assertRaisesRegex(ScrapeError, "无法推导详情地址"):
            dynamic_article.kunnan_magazine_detail_url("https://api.example/list", "1")


class ParseDetailTests(unittest.TestCase):
    def test_parse_detail_returns_text_lines(self) -> None:
        detail = _detail("https://api.example/api/v1/forums/1", 314, FULL_NUMBERS)
        self.assertEqual("https://api.example/api/v1/forums/1", detail.url)
        self.assertIn("314期【三十六码特】开00对", detail.content)
        self.assertEqual(5, len(detail.content.splitlines()))

    def test_parse_detail_requires_content(self) -> None:
        with self.assertRaisesRegex(ScrapeError, "缺少正文"):
            dynamic_article.parse_kunnan_detail("https://x/1", '{"id": "1"}')

    def test_parse_detail_requires_json(self) -> None:
        with self.assertRaisesRegex(ScrapeError, "不是有效JSON"):
            dynamic_article.parse_kunnan_detail("https://x/1", "<html></html>")


class EnrichmentTests(unittest.TestCase):
    def test_truncated_list_content_would_fail_without_detail(self) -> None:
        payload = json.dumps([_record("a", 314, _content(314, TRUNCATED_NUMBERS))])
        with self.assertRaisesRegex(ScrapeError, "36"):
            dynamic_article.kunnan_magazine_records_from_payload(payload, _config())

    def test_detail_content_replaces_truncated_list_content(self) -> None:
        payload = json.dumps([_record("a", 314, _content(314, TRUNCATED_NUMBERS))])
        details = {"a": _detail("https://api.example/api/v1/forums/a", 314, FULL_NUMBERS)}
        records = dynamic_article.kunnan_magazine_records_from_payload(
            payload,
            _config(),
            details,
        )
        self.assertEqual(1, len(records))
        self.assertEqual(314, records[0].issue)
        self.assertEqual(FULL_NUMBERS, records[0].numbers)
        self.assertEqual(
            "https://api.example/api/v1/forums/a",
            records[0].evidence.document_url,
        )
        self.assertEqual(
            "https://api.example/api/v1/forums/a",
            records[0].evidence.origins[0].document_url,
        )

    def test_record_without_detail_keeps_list_document_url(self) -> None:
        payload = json.dumps(
            [
                _record("a", 313, _content(313, FULL_NUMBERS)),
                _record("b", 314, _content(314, TRUNCATED_NUMBERS)),
            ]
        )
        details = {"b": _detail("https://api.example/api/v1/forums/b", 314, FULL_NUMBERS)}
        records = dynamic_article.kunnan_magazine_records_from_payload(
            payload,
            _config(),
            details,
        )
        by_issue = {record.issue: record for record in records}
        self.assertEqual(
            "https://api.example/api/v1/forums/b",
            by_issue[314].evidence.document_url,
        )
        self.assertEqual(
            "https://api.example/api/v1/users/3792/forums::$[0]",
            by_issue[313].evidence.document_url,
        )

    def test_record_ids_only_lists_matching_records(self) -> None:
        payload = json.dumps(
            [
                _record("a", 314, "x"),
                {**_record("b", 313, "x"), "lottery": "other"},
                {**_record("c", 312, "x"), "topic": "其他栏目"},
                {**_record("d", 311, "x"), "status": "draft"},
            ]
        )
        self.assertEqual(("a",), dynamic_article.kunnan_magazine_record_ids(payload, _config()))


class LoadCollectionDetailsTests(unittest.TestCase):
    def test_details_are_fetched_per_record(self) -> None:
        payload = json.dumps([_record("a", 314, "x"), _record("b", 313, "x")])
        seen: list[str] = []

        def fetcher(url: str, _timeout: int) -> str:
            seen.append(url)
            return _detail_body(314, FULL_NUMBERS)

        details = load_collection_details(_config(), payload, fetcher, 5)
        self.assertEqual(
            [
                "https://api.example/api/v1/forums/a",
                "https://api.example/api/v1/forums/b",
            ],
            seen,
        )
        self.assertEqual({"a", "b"}, set(details))

    def test_detail_fetch_failure_is_reported(self) -> None:
        payload = json.dumps([_record("a", 314, "x")])

        def fetcher(_url: str, _timeout: int) -> str:
            raise ScrapeError("network error: timeout")

        with self.assertRaisesRegex(ScrapeError, "详情正文获取失败"):
            load_collection_details(_config(), payload, fetcher, 5)

    def test_invalid_list_payload_yields_no_details(self) -> None:
        self.assertEqual({}, load_collection_details(_config(), "{bad", lambda _u, _t: "", 5))


class ServiceIntegrationTests(unittest.TestCase):
    def test_service_uses_detail_body_for_fixed_issue(self) -> None:
        payload = json.dumps(
            [
                _record("a", 313, _content(313, FULL_NUMBERS)),
                _record("b", 314, _content(314, TRUNCATED_NUMBERS)),
            ]
        )
        bodies = {
            "a": _detail_body(313, FULL_NUMBERS),
            "b": _detail_body(314, FULL_NUMBERS),
        }

        def fetcher(url: str, _timeout: int) -> str:
            if "/forums/" in url:
                return bodies[url.rsplit("/", 1)[-1]]
            return payload

        result = ScrapeService(text_fetcher=fetcher).scrape(_config(), fixed_issue=314)

        self.assertEqual(314, result.issue)
        self.assertEqual(FULL_NUMBERS, result.numbers)
        self.assertEqual("b", result.record_id)


if __name__ == "__main__":
    unittest.main()
