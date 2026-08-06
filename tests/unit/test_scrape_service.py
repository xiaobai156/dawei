from __future__ import annotations

from dataclasses import replace
import json
import unittest

from dawei.domain.errors import ScrapeError
from dawei.domain.models import ArticleRecord, ParsedRecord, SiteConfig
from dawei.application.scrape_service import ScrapeService
from tests.support.evidence_factory import record as evidence_record


NUMBERS = tuple(f"{value:02d}" for value in range(1, 37))


def generic_site() -> SiteConfig:
    return SiteConfig(
        name="测试站",
        url="https://example.test/topic/1",
        keywords=("三十六码",),
        section_keywords=("测试站",),
        region="bottom",
        site_id="site-test",
        source_type="topic_page",
        parser_id="generic_36",
    )


class ScrapeServiceTests(unittest.TestCase):
    def test_execute_returns_the_same_document_used_by_formal_parser(self) -> None:
        calls: list[str] = []
        parsed_documents: list[str] = []

        def fetcher(url: str, timeout: int) -> str:
            calls.append(url)
            return "唯一真实正文"

        def parser(document: str, site: SiteConfig) -> ParsedRecord:
            parsed_documents.append(document)
            return evidence_record(site.name, site.url, 210, NUMBERS)

        trace = ScrapeService(text_fetcher=fetcher, parser=parser).execute(
            generic_site(),
            fixed_issue=210,
        )

        self.assertEqual(calls, [generic_site().url])
        self.assertEqual(parsed_documents, ["唯一真实正文"])
        self.assertEqual(trace.document, "唯一真实正文")
        self.assertEqual(trace.result.numbers, NUMBERS)
        self.assertEqual(trace.error, "")

    def test_manual_single_issue_fetches_only_one_document(self) -> None:
        calls: list[tuple[str, int]] = []
        parsed_configs: list[SiteConfig] = []

        def fetcher(url: str, timeout: int) -> str:
            calls.append((url, timeout))
            return "document"

        def parser(document: str, site: SiteConfig) -> ParsedRecord:
            parsed_configs.append(site)
            return evidence_record(site.name, site.url, site.fixed_issue or 0, NUMBERS)

        result = ScrapeService(text_fetcher=fetcher, parser=parser).scrape(
            generic_site(),
            fixed_issue=210,
            timeout=3,
        )

        self.assertEqual(result.issue, 210)
        self.assertEqual(calls, [(generic_site().url, 3)])
        self.assertEqual(parsed_configs[0].fixed_issue, 210)
        self.assertEqual(parsed_configs[0].parser_id, "generic_36")
        self.assertEqual(parsed_configs[0].site_id, "site-test")

    def test_dynamic_api_failure_uses_strict_rendered_article_record(self) -> None:
        site = replace(
            generic_site(),
            url="https://example.test/article/manager/target-id?url=x",
            api_url="https://example.test/api/target-id",
            source_type="dynamic_article",
            record_id="target-id",
        )
        rendered_calls: list[int | None] = []

        def fetcher(url: str, timeout: int) -> str:
            raise ScrapeError("HTTP 404")

        def render_article(
            config: SiteConfig,
            timeout: int,
            expected_issue: int | None,
            expected_keywords=(),
            force_full_wait: bool = False,
        ) -> ArticleRecord:
            rendered_calls.append(expected_issue)
            body = "测试站 三十六码\n210期 三十六码\n" + " ".join(NUMBERS)
            return ArticleRecord("target-id", "api::$.data[0]", "210期 三十六码", "测试站", body, body)

        def parser(document: str, config: SiteConfig) -> ParsedRecord:
            return evidence_record(config.name, config.url, 210, NUMBERS)

        result = ScrapeService(
            text_fetcher=fetcher,
            parser=parser,
            rendered_article_fetcher=render_article,
        ).scrape(site, fixed_issue=210)

        self.assertEqual(rendered_calls, [210])
        self.assertEqual(result.record_id, "target-id")
        self.assertEqual(result.record_path, "api::$.data[0]")

    def test_dynamic_api_success_preserves_full_api_source_path(self) -> None:
        site = replace(
            generic_site(),
            url="https://example.test/article/manager/target-id?url=x",
            api_url="https://example.test/api/target-id",
            source_type="dynamic_article",
            record_id="target-id",
        )
        payload = json.dumps(
            {
                "data": [
                    {
                        "id": "target-id",
                        "authorNickname": "测试站",
                        "title": "210期 三十六码",
                        "content": "测试站 三十六码\n210期\n" + " ".join(NUMBERS),
                    }
                ]
            },
            ensure_ascii=False,
        )
        result = ScrapeService(
            text_fetcher=lambda url, timeout: payload,
            parser=lambda document, config: evidence_record(
                config.name,
                config.url,
                210,
                NUMBERS,
            ),
        ).scrape(site, fixed_issue=210)

        expected_path = site.api_url + "::$.data[0]"
        self.assertEqual(result.record_path, expected_path)
        self.assertEqual(result.evidence.document_url, expected_path)

    def test_dynamic_api_exact_id_error_is_not_replaced_by_other_record(self) -> None:
        site = replace(
            generic_site(),
            url="https://example.test/article/manager/target-id?url=x",
            api_url="https://example.test/api/target-id",
            source_type="dynamic_article",
            record_id="target-id",
        )
        payload = json.dumps(
            {
                "data": [
                    {
                        "id": "decoy-id",
                        "authorNickname": "测试站",
                        "title": "210期 三十六码",
                        "content": "测试站 三十六码 " + " ".join(NUMBERS),
                    }
                ]
            },
            ensure_ascii=False,
        )

        def fail_render(*args, **kwargs):
            raise ScrapeError("浏览器兜底未找到URL记录ID: target-id")

        service = ScrapeService(
            text_fetcher=lambda url, timeout: payload,
            parser=lambda document, config: evidence_record(config.name, config.url, 210, NUMBERS),
            rendered_article_fetcher=fail_render,
        )
        with self.assertRaisesRegex(ScrapeError, "未找到URL记录ID"):
            service.scrape(site, fixed_issue=210)


if __name__ == "__main__":
    unittest.main()
