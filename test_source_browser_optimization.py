"""Focused regressions for HTTP-first source loading and browser fallback."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest import mock

from dawei.application import duplicate_service, scrape_service
from dawei.application.scrape_service import (
    ScrapeService,
    should_render_api_fallback,
)
from dawei.domain.errors import ScrapeError
from dawei.domain.models import (
    ArticleRecord,
    CandidateEvidence,
    CandidateOrigin,
    ParsedRecord,
    SiteConfig,
)

NUMBERS = tuple(f"{value:02d}" for value in range(1, 37))
BROWSER_DOCUMENT = "210期 三十六码\n测试站\n测试站 三十六码\n" + " ".join(NUMBERS)


def _config(*, render_policy: str = "fallback") -> SiteConfig:
    return SiteConfig(
        name="测试站",
        url="https://example.test/article/manager/target-id?url=x",
        keywords=("三十六码",),
        section_keywords=("测试站",),
        region="bottom",
        site_id="site-test",
        source_type="dynamic_article",
        parser_id="generic_36",
        api_url="https://example.test/api/target-id",
        record_id="target-id",
        render_policy=render_policy,
    )


def _record(config: SiteConfig, issue: int = 210) -> ParsedRecord:
    origin = CandidateOrigin(
        raw_issue_line=f"{issue}期 三十六码",
        raw_number_lines=(" ".join(NUMBERS),),
        anchor_line=config.name,
        document_id="target-id",
        document_url="https://example.test/api/target-id::$.data[0]",
        source_method="structured_article",
        block_id="target-id:0-2",
        block_start=0,
        block_end=2,
        page_index=0,
        block_index=0,
        parser_id=config.parser_id,
    )
    evidence = CandidateEvidence(
        issue=issue,
        numbers=NUMBERS,
        raw_issue_line=origin.raw_issue_line,
        raw_number_lines=origin.raw_number_lines,
        anchor_line=origin.anchor_line,
        document_id=origin.document_id,
        document_url=origin.document_url,
        source_method=origin.source_method,
        block_id=origin.block_id,
        block_start=origin.block_start,
        block_end=origin.block_end,
        page_index=origin.page_index,
        block_index=origin.block_index,
        parser_id=origin.parser_id,
        origins=(origin,),
    )
    return ParsedRecord(
        config.name,
        config.url,
        issue,
        NUMBERS,
        raw_position=evidence.page_index,
        evidence=evidence,
    )


def _api_payload(body: str = "测试站 三十六码") -> str:
    return json.dumps(
        {
            "data": [
                {
                    "id": "target-id",
                    "authorNickname": "测试站",
                    "title": "210期 三十六码",
                    "content": body,
                }
            ]
        },
        ensure_ascii=False,
    )


class SourceBrowserOptimizationTests(unittest.TestCase):
    def test_plain_http_shell_uses_browser_after_target_parse_fails(self) -> None:
        config = SiteConfig(
            name="测试站",
            url="https://example.test/topic",
            keywords=("三十六码",),
            section_keywords=("测试站",),
            region="top",
            site_id="site-test",
            source_type="generic_html",
            parser_id="generic_36",
            render_policy="never",
        )
        raw_shell = '<script src="https://other.test/upload/script/data.js"></script>'
        rendered = "测试站 236期 三十六码\n" + " ".join(NUMBERS)
        browser = mock.Mock(return_value=rendered)
        http = mock.Mock(return_value=raw_shell)

        result = ScrapeService(
            text_fetcher=http,
            rendered_text_fetcher=browser,
        ).scrape(config, fixed_issue=236)

        self.assertEqual(result.issue, 236)
        self.assertEqual(result.numbers, NUMBERS)
        http.assert_called_once_with(config.url, 20)
        browser.assert_called_once_with(config.url, timeout=20)

    def test_missing_dedicated_section_uses_browser_after_http_shell(self) -> None:
        config = SiteConfig(
            name="主持人",
            url="https://example.test/",
            keywords=("第",),
            section_keywords=("36码围特",),
            region="top",
            site_id="site-host",
            source_type="generic_html",
            parser_id="zhuchiren_weite",
            render_policy="never",
        )
        browser = mock.Mock(return_value="浏览器完整正文")

        def parser(document: str, _config: SiteConfig) -> ParsedRecord:
            if document == "HTTP空壳":
                raise ScrapeError("未找到主持人专属栏目: 36码围特")
            return _record(config, issue=236)

        result = ScrapeService(
            text_fetcher=mock.Mock(return_value="HTTP空壳"),
            rendered_text_fetcher=browser,
            parser=parser,
        ).scrape(config, fixed_issue=236)

        self.assertEqual(result.issue, 236)
        browser.assert_called_once_with(config.url, timeout=20)

    def test_single_issue_parse_failure_is_hard_failure(self) -> None:
        config = _config()
        browser_article = ArticleRecord(
            "target-id",
            "https://example.test/api/articles::$.data[0]",
            "210期 三十六码",
            "测试站",
            "测试站 三十六码\n210期\n" + " ".join(NUMBERS),
            BROWSER_DOCUMENT,
        )
        browser_calls: list[str] = []

        def parser(document: str, _config: SiteConfig) -> ParsedRecord:
            if document != BROWSER_DOCUMENT:
                raise ScrapeError("HTTP目标正文没有完整36码")
            return _record(config)

        def render_article(*args, **kwargs) -> ArticleRecord:
            browser_calls.append("render")
            return browser_article

        service = ScrapeService(
            text_fetcher=lambda _url, _timeout: _api_payload(),
            parser=parser,
            rendered_article_fetcher=render_article,
        )

        with self.assertRaises(ScrapeError):
            service.scrape(config, fixed_issue=210)
        self.assertEqual(browser_calls, [])

    def test_always_policy_uses_complete_http_before_browser(self) -> None:
        config = _config(render_policy="always")
        http_calls: list[str] = []
        browser = mock.Mock(side_effect=AssertionError("complete HTTP must not render"))
        complete_payload = _api_payload("测试站 三十六码\n210期\n" + " ".join(NUMBERS))

        result = ScrapeService(
            text_fetcher=lambda url, _timeout: http_calls.append(url) or complete_payload,
            parser=lambda _document, _config: _record(config),
            rendered_article_fetcher=browser,
        ).scrape(config, fixed_issue=210)

        self.assertEqual(http_calls, [config.api_url])
        browser.assert_not_called()
        self.assertEqual(result.record_id, "target-id")

    def test_allowed_api_shell_errors_render_once_for_fallback_and_always(self) -> None:
        for policy in ("fallback", "always"):
            cases = (
                ("HTTP 404", mock.Mock(side_effect=ScrapeError("HTTP 404"))),
                ("API shell", mock.Mock(return_value=json.dumps({"data": []}))),
            )
            for failure, text_fetcher in cases:
                with self.subTest(policy=policy, failure=failure):
                    config = _config(render_policy=policy)
                    browser_article = ArticleRecord(
                        "target-id",
                        "browser-response::$.data[0]",
                        "210期 三十六码",
                        "测试站",
                        "测试站 三十六码\n210期\n" + " ".join(NUMBERS),
                        BROWSER_DOCUMENT,
                    )
                    browser = mock.Mock(return_value=browser_article)
                    result = ScrapeService(
                        text_fetcher=text_fetcher,
                        parser=lambda _document, _config, config=config: _record(config),
                        rendered_article_fetcher=browser,
                    ).scrape(config, fixed_issue=210)

                    text_fetcher.assert_called_once_with(config.api_url, 20)
                    browser.assert_called_once_with(config, timeout=20)
                    self.assertEqual(result.record_id, "target-id")

    def test_never_policy_never_renders_for_allowed_api_shell_errors(self) -> None:
        cases = (
            ("HTTP 404", mock.Mock(side_effect=ScrapeError("HTTP 404"))),
            ("API shell", mock.Mock(return_value=json.dumps({"data": []}))),
        )
        for failure, text_fetcher in cases:
            with self.subTest(failure=failure):
                config = _config(render_policy="never")
                browser = mock.Mock(side_effect=AssertionError("never must not render"))

                with self.assertRaises(ScrapeError):
                    ScrapeService(
                        text_fetcher=text_fetcher,
                        parser=lambda _document, _config, config=config: _record(config),
                        rendered_article_fetcher=browser,
                    ).scrape(config, fixed_issue=210)

                text_fetcher.assert_called_once_with(config.api_url, 20)
                browser.assert_not_called()

    def test_api_record_validation_errors_only_empty_body_renders(self) -> None:
        wrong_id_payload = json.loads(_api_payload())
        wrong_id_payload["data"][0]["id"] = "other-id"
        cases = (
            ("ID mismatch", json.dumps(wrong_id_payload, ensure_ascii=False), False),
            ("body missing", _api_payload(""), True),
            ("body whitespace", _api_payload("   "), True),
        )
        for policy in ("fallback", "always"):
            for failure, payload, should_render in cases:
                with self.subTest(policy=policy, failure=failure):
                    config = _config(render_policy=policy)
                    browser_article = ArticleRecord(
                        "target-id",
                        "browser-response::$.data[0]",
                        "210期 三十六码",
                        "测试站",
                        "测试站 三十六码\n210期\n" + " ".join(NUMBERS),
                        BROWSER_DOCUMENT,
                    )
                    browser = mock.Mock(return_value=browser_article)
                    service = ScrapeService(
                        text_fetcher=mock.Mock(return_value=payload),
                        parser=lambda _document, _config, config=config: _record(config),
                        rendered_article_fetcher=browser,
                    )
                    if should_render:
                        result = service.scrape(config, fixed_issue=210)
                        browser.assert_called_once_with(config, timeout=20)
                        self.assertEqual(result.record_id, "target-id")
                    else:
                        with self.assertRaises(ScrapeError):
                            service.scrape(config, fixed_issue=210)
                        browser.assert_not_called()

    def test_duplicate_id_and_http500_never_fallback(self) -> None:
        duplicate_payload = json.loads(_api_payload())
        duplicate_payload["data"].append(dict(duplicate_payload["data"][0]))
        cases = (
            ("duplicate ID", json.dumps(duplicate_payload, ensure_ascii=False), None),
            ("HTTP 500", None, ScrapeError("HTTP 500")),
        )
        for failure, payload, fetch_error in cases:
            with self.subTest(failure=failure):
                config = _config(render_policy="always")
                browser = mock.Mock(side_effect=AssertionError("hard failure must not render"))
                if fetch_error is not None:
                    text_fetcher = mock.Mock(side_effect=fetch_error)
                else:
                    text_fetcher = mock.Mock(return_value=payload)
                service = ScrapeService(
                    text_fetcher=text_fetcher,
                    parser=lambda _document, _config, config=config: _record(config),
                    rendered_article_fetcher=browser,
                )

                with self.assertRaises(ScrapeError):
                    service.scrape(config, fixed_issue=210)
                browser.assert_not_called()

    def test_never_policy_never_renders_for_api_record_validation_errors(self) -> None:
        wrong_id_payload = json.loads(_api_payload())
        wrong_id_payload["data"][0]["id"] = "other-id"
        for failure, payload in (
            ("ID mismatch", json.dumps(wrong_id_payload, ensure_ascii=False)),
            ("body missing", _api_payload("")),
        ):
            with self.subTest(failure=failure):
                config = _config(render_policy="never")
                browser = mock.Mock(side_effect=AssertionError("never must not render"))
                service = ScrapeService(
                    text_fetcher=mock.Mock(return_value=payload),
                    parser=lambda _document, _config, config=config: _record(config),
                    rendered_article_fetcher=browser,
                )

                with self.assertRaises(ScrapeError):
                    service.scrape(config, fixed_issue=210)
                browser.assert_not_called()

    def test_duplicate_api_record_validation_errors_only_empty_body_renders(self) -> None:
        wrong_id_payload = json.loads(_api_payload())
        wrong_id_payload["data"][0]["id"] = "other-id"
        for policy in ("fallback", "always"):
            for failure, payload, should_render in (
                ("ID mismatch", json.dumps(wrong_id_payload, ensure_ascii=False), False),
                ("body missing", _api_payload(""), True),
                ("body whitespace", _api_payload("   "), True),
            ):
                with self.subTest(policy=policy, failure=failure):
                    config = _config(render_policy=policy)
                    expected = _record(config)
                    browser_article = ArticleRecord(
                        "target-id",
                        "browser-response::$.data[0]",
                        "210期 三十六码",
                        "测试站",
                        "测试站 三十六码\n210期\n" + " ".join(NUMBERS),
                        BROWSER_DOCUMENT,
                    )
                    with mock.patch.object(
                        duplicate_service,
                        "collect_issue_records",
                        return_value=([expected], ()),
                    ), mock.patch.object(
                        duplicate_service.browser_client,
                        "fetch_rendered_article_record",
                        return_value=browser_article,
                    ) as render:
                        if should_render:
                            window = duplicate_service.scrape_site_window(
                                config,
                                period=210,
                                periods=1,
                                min_records=1,
                                timeout=1,
                                text_fetcher=mock.Mock(return_value=payload),
                            )
                        else:
                            with self.assertRaises(ScrapeError):
                                duplicate_service.scrape_site_window(
                                    config,
                                    period=210,
                                    periods=1,
                                    min_records=1,
                                    timeout=1,
                                    text_fetcher=mock.Mock(return_value=payload),
                                )

                    if should_render:
                        render.assert_called_once_with(config, timeout=1)
                        self.assertEqual(window.records[0].record_id, "target-id")
                    else:
                        render.assert_not_called()

    def test_disallowed_api_failures_never_fallback_to_browser(self) -> None:
        for failure in (
            "HTTP 500",
            "network error: connection refused",
            "network error: TLS handshake failed after retries",
            "API存在多个同ID目标记录: target-id",
        ):
            with self.subTest(failure=failure):
                config = _config(render_policy="always")
                browser = mock.Mock(side_effect=AssertionError("disallowed fallback"))
                service = ScrapeService(
                    text_fetcher=mock.Mock(side_effect=ScrapeError(failure)),
                    parser=lambda _document, _config, config=config: _record(config),
                    rendered_article_fetcher=browser,
                )

                with self.assertRaisesRegex(ScrapeError, failure):
                    service.scrape(config, fixed_issue=210)
                browser.assert_not_called()

    def test_duplicate_always_policy_uses_complete_http_before_browser(self) -> None:
        config = _config(render_policy="always")
        expected = _record(config)
        complete_payload = _api_payload("测试站 三十六码\n210期\n" + " ".join(NUMBERS))
        http_calls: list[str] = []
        browser = mock.Mock(side_effect=AssertionError("complete HTTP must not render"))

        with mock.patch.object(
            duplicate_service,
            "collect_issue_records",
            return_value=([expected], ()),
        ):
            window = duplicate_service.scrape_site_window(
                config,
                period=210,
                periods=1,
                min_records=1,
                timeout=1,
                text_fetcher=lambda url, _timeout: http_calls.append(url) or complete_payload,
            )

        self.assertEqual(http_calls, [config.api_url])
        browser.assert_not_called()
        self.assertEqual(window.records[0].record_id, "target-id")

    def test_duplicate_window_parse_failure_is_hard_failure(self) -> None:
        config = _config()
        browser_article = ArticleRecord(
            "target-id",
            "browser-response::$.data[0]",
            "210期 三十六码",
            "测试站",
            "测试站 三十六码\n210期\n" + " ".join(NUMBERS),
            BROWSER_DOCUMENT,
        )
        with mock.patch.object(
            duplicate_service,
            "collect_issue_records",
            side_effect=ScrapeError("HTTP目标正文没有完整36码"),
        ) as collect, mock.patch.object(
            duplicate_service.browser_client,
            "fetch_rendered_article_record",
            return_value=browser_article,
        ) as render, self.assertRaises(ScrapeError):
            duplicate_service.scrape_site_window(
                config,
                period=210,
                periods=1,
                min_records=1,
                timeout=1,
                text_fetcher=lambda _url, _timeout: _api_payload(),
            )

        self.assertEqual(collect.call_count, 1)
        render.assert_not_called()

    def test_duplicate_window_fails_when_http_history_is_incomplete(self) -> None:
        config = _config(render_policy="fallback")
        http_records = [_record(config, issue=210)]
        browser_article = ArticleRecord(
            "target-id",
            "browser-response::$.data[0]",
            "210期 三十六码",
            "测试站",
            "测试站 三十六码\n210期\n" + " ".join(NUMBERS),
            BROWSER_DOCUMENT,
        )
        with mock.patch.object(
            duplicate_service,
            "collect_issue_records",
            return_value=(http_records, ()),
        ) as collect, mock.patch.object(
            duplicate_service.browser_client,
            "fetch_rendered_article_record",
            return_value=browser_article,
        ) as render, self.assertRaises(ScrapeError):
            duplicate_service.scrape_site_window(
                config,
                period=210,
                periods=2,
                min_records=2,
                timeout=1,
                text_fetcher=lambda _url, _timeout: _api_payload(),
            )

        self.assertEqual(collect.call_count, 1)
        render.assert_not_called()

    def test_http_client_404_message_is_the_only_transport_fallback_marker(self) -> None:
        config = _config()
        self.assertTrue(should_render_api_fallback(config, ScrapeError("HTTP 404")))
        self.assertFalse(should_render_api_fallback(config, ScrapeError("HTTP 4040")))
        self.assertFalse(should_render_api_fallback(config, ScrapeError("HTTP 500")))

    def test_duplicate_window_never_policy_keeps_incomplete_http_failure(self) -> None:
        config = _config(render_policy="never")
        with mock.patch.object(
            duplicate_service,
            "collect_issue_records",
             return_value=([_record(config, issue=210)], ()),
        ), mock.patch.object(
            duplicate_service.browser_client,
            "fetch_rendered_article_record",
            side_effect=AssertionError("never must not render"),
        ) as render, self.assertRaises(ScrapeError):
            duplicate_service.scrape_site_window(
                config,
                period=210,
                periods=2,
                min_records=2,
                timeout=1,
                text_fetcher=lambda _url, _timeout: _api_payload(),
            )

        render.assert_not_called()

    def test_single_and_window_share_api_transport_policy(self) -> None:
        for failure, payload, should_render in (
            ("API shell", json.dumps({"data": []}), True),
            ("HTTP 500", None, False),
        ):
            with self.subTest(failure=failure):
                config = _config(render_policy="always")
                single_http = mock.Mock(
                    side_effect=ScrapeError("HTTP 500") if payload is None else None,
                    return_value=payload,
                )
                single_browser = mock.Mock(
                    return_value=ArticleRecord(
                        "target-id",
                        "browser-response::$.data[0]",
                        "210期 三十六码",
                        "测试站",
                        "测试站 三十六码\n210期\n" + " ".join(NUMBERS),
                        BROWSER_DOCUMENT,
                    )
                )
                single = ScrapeService(
                    text_fetcher=single_http,
                    parser=lambda _document, _config, config=config: _record(config),
                    rendered_article_fetcher=single_browser,
                )
                window_http = mock.Mock(
                    side_effect=ScrapeError("HTTP 500") if payload is None else None,
                    return_value=payload,
                )
                window_browser = mock.Mock(
                    return_value=ArticleRecord(
                        "target-id",
                        "browser-response::$.data[0]",
                        "210期 三十六码",
                        "测试站",
                        "测试站 三十六码\n210期\n" + " ".join(NUMBERS),
                        BROWSER_DOCUMENT,
                    )
                )
                with mock.patch.object(
                    duplicate_service.browser_client,
                    "fetch_rendered_article_record",
                    window_browser,
                ), mock.patch.object(
                    duplicate_service,
                    "collect_issue_records",
                    return_value=([_record(config)], ()),
                ):
                    if should_render:
                        single.scrape(config, fixed_issue=210)
                        duplicate_service.scrape_site_window(
                            config,
                            period=210,
                            periods=1,
                            min_records=1,
                            timeout=1,
                            text_fetcher=window_http,
                        )
                    else:
                        with self.assertRaises(ScrapeError):
                            single.scrape(config, fixed_issue=210)
                        with self.assertRaises(ScrapeError):
                            duplicate_service.scrape_site_window(
                                config,
                                period=210,
                                periods=1,
                                min_records=1,
                                timeout=1,
                                text_fetcher=window_http,
                            )

                if should_render:
                    single_browser.assert_called_once_with(config, timeout=20)
                    window_browser.assert_called_once_with(config, timeout=1)
                else:
                    single_browser.assert_not_called()
                    window_browser.assert_not_called()

    def test_single_and_window_share_ordinary_http_then_one_browser_fallback(self) -> None:
        config = _config(render_policy="fallback")
        config = SiteConfig(
            name=config.name,
            url="https://example.test/topic",
            keywords=config.keywords,
            section_keywords=config.section_keywords,
            region=config.region,
            site_id=config.site_id,
            source_type="generic_html",
            parser_id="generic_36",
            render_policy=config.render_policy,
        )
        ordinary_record = replace(_record(config), raw_position=0)
        single_text = mock.Mock(return_value="HTTP incomplete")
        single_browser = mock.Mock(return_value=BROWSER_DOCUMENT)

        def single_parser(document: str, _config: SiteConfig) -> ParsedRecord:
            if document == "HTTP incomplete":
                raise ScrapeError("HTTP正文不完整")
            return ordinary_record

        ScrapeService(
            text_fetcher=single_text,
            parser=single_parser,
            rendered_text_fetcher=single_browser,
        ).scrape(config, fixed_issue=210)

        window_text = mock.Mock(return_value="HTTP incomplete")
        window_browser = mock.Mock(return_value=BROWSER_DOCUMENT)
        with mock.patch.object(
            duplicate_service.browser_client,
            "fetch_rendered_text",
            window_browser,
        ), mock.patch.object(
            duplicate_service,
            "collect_issue_records",
            side_effect=[ScrapeError("HTTP正文不完整"), ([ordinary_record], ())],
        ):
            duplicate_service.scrape_site_window(
                config,
                period=210,
                periods=1,
                min_records=1,
                timeout=1,
                text_fetcher=window_text,
            )

        single_text.assert_called_once_with(config.url, 20)
        single_browser.assert_called_once_with(config.url, timeout=20)
        window_text.assert_called_once_with(config.url, 1)
        window_browser.assert_called_once_with(config.url, timeout=1)

    def test_paginated_parse_failure_never_crosses_to_list_browser_source(self) -> None:
        config = SiteConfig(
            name="分页站",
            url="https://example.test/list",
            keywords=("三十六码",),
            section_keywords=("分页站",),
            region="bottom",
            site_id="site-paginated",
            source_type="paginated_article_list",
            parser_id="generic_36",
            render_policy="fallback",
            navigation_keywords=("详情",),
        )
        with mock.patch.object(
            scrape_service,
            "fetch_paginated_article_document",
            return_value=("HTTP incomplete", "https://example.test/detail"),
        ) as fetch_page, mock.patch.object(
            duplicate_service.browser_client,
            "fetch_rendered_text",
            side_effect=AssertionError("paginated list must not render"),
        ) as duplicate_browser:
            single_browser = mock.Mock(side_effect=AssertionError("paginated list must not render"))
            with self.assertRaises(ScrapeError):
                ScrapeService(
                    text_fetcher=mock.Mock(),
                    parser=mock.Mock(side_effect=ScrapeError("详情正文不完整")),
                    rendered_text_fetcher=single_browser,
                ).scrape(config, fixed_issue=210)
            with mock.patch.object(
                duplicate_service,
                "collect_issue_records",
                side_effect=ScrapeError("详情正文不完整"),
            ) as collect, self.assertRaises(ScrapeError):
                duplicate_service.scrape_site_window(
                    config,
                    period=210,
                    periods=1,
                    min_records=1,
                    timeout=1,
                    text_fetcher=mock.Mock(),
                )

        fetch_page.assert_has_calls(
            [
                mock.call(config, 20, 210, mock.ANY),
                mock.call(config, 1, None, mock.ANY),
            ]
        )
        self.assertEqual(fetch_page.call_count, 2)
        single_browser.assert_not_called()
        duplicate_browser.assert_not_called()
        collect.assert_called_once_with("HTTP incomplete", mock.ANY)


if __name__ == "__main__":
    unittest.main()
