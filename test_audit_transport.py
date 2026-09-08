"""Transport-boundary regressions for the single-project scraper."""

from __future__ import annotations

import base64
import gzip
import json
import ssl
import threading
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.client import IncompleteRead
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError, URLError

from dawei.application import duplicate_service
from dawei.application.scrape_service import (
    ScrapeService,
    should_render_api_record_fallback,
)
from dawei.domain.errors import ScrapeError
from dawei.domain.models import ArticleRecord, ParsedRecord, SiteConfig
from dawei.infrastructure import browser_client, http_client, source_adapters
from dawei.parsers import DEFAULT_REGISTRY, dynamic_article

NUMBERS = tuple(f"{value:02d}" for value in range(1, 37))
BROWSER_DOCUMENT = "210期 三十六码\n测试站\n测试站 三十六码\n" + " ".join(NUMBERS)


def _config(*, policy: str = "fallback") -> SiteConfig:
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
        render_policy=policy,
    )


def _payload(body: str = "测试站 三十六码\n210期") -> str:
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


class TransportBoundaryTests(unittest.TestCase):
    def test_formal_config_has_no_active_image_or_ocr_dependency(self) -> None:
        with open("sites_36.json", encoding="utf-8-sig") as handle:
            sites = json.load(handle)

        self.assertTrue(sites)
        self.assertEqual(
            [site["name"] for site in sites if site.get("image_decoder")],
            [],
        )
        self.assertFalse(
            any(site.get("parser_id", "").startswith("image_") for site in sites)
        )
        self.assertNotIn("image_bb48kk", DEFAULT_REGISTRY.parser_ids)
        self.assertNotIn("image_tuku2135", DEFAULT_REGISTRY.parser_ids)

    def test_archived_config_keeps_historical_image_fields_readable(self) -> None:
        with open("archived_sites_36.json", encoding="utf-8-sig") as handle:
            payload = json.load(handle)

        archived = payload["sites"]
        self.assertTrue(archived)
        self.assertTrue(any(site.get("image_decoder") for site in archived))
        self.assertTrue(all("image_decoder" in site for site in archived))

    def test_http_entrypoints_reject_nonpositive_limits(self) -> None:
        with self.assertRaises(ScrapeError):
            http_client.health_check_url("https://example.test", timeout=0)
        with self.assertRaises(ScrapeError):
            http_client.health_check_url(
                "https://example.test", network_attempts=0
            )
        with self.assertRaises(ScrapeError):
            http_client.health_check_url("https://example.test", proxy_retries=0)
        with self.assertRaises(ScrapeError):
            http_client.fetch_raw("https://example.test", timeout=0)
        with self.assertRaises(ScrapeError):
            http_client.fetch_raw("https://example.test", network_attempts=0)
        with self.assertRaises(ScrapeError):
            http_client.fetch_raw("https://example.test", proxy_retries=0)
        with self.assertRaises(ScrapeError):
            http_client.fetch_raw_with_curl("https://example.test", timeout=0)
        with self.assertRaises(ScrapeError):
            http_client.fetch_text_with_node("https://example.test", timeout=0)
        with self.assertRaises(ScrapeError):
            http_client.fetch_text(
                "https://example.test",
                timeout=0,
                raw_fetcher=mock.Mock(),
            )
        for invalid_timeout in (False, True, 1.0, "1", None):
            with self.subTest(invalid_timeout=invalid_timeout), self.assertRaises(ScrapeError):
                http_client.health_check_url(
                    "https://example.test",
                    timeout=invalid_timeout,
                    open_url=mock.Mock(),
                )

    def test_http_decode_and_classifier_edges(self) -> None:
        plain = b"plain text"
        self.assertEqual(http_client.decode_response_bytes(plain, ""), plain)
        compressed = gzip.compress(plain)
        self.assertEqual(http_client.decode_response_bytes(compressed, ""), plain)
        self.assertEqual(http_client.decode_response_bytes(compressed, "gzip"), plain)
        deflated = zlib.compress(plain)
        self.assertEqual(http_client.decode_response_bytes(deflated, "deflate"), plain)
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        raw_deflated = compressor.compress(plain) + compressor.flush()
        self.assertEqual(http_client.decode_response_bytes(raw_deflated, "deflate"), plain)

        self.assertIsNone(http_client.sniff_charset(b"plain"))
        self.assertEqual(http_client.sniff_charset(b'<meta charset="GBK">'), "gb18030")
        self.assertEqual(http_client.sniff_charset(b"<meta charset=latin1>"), "latin1")
        self.assertEqual(http_client.decode_text("中文".encode(), None), "中文")
        self.assertEqual(http_client.decode_text(b"abc", "not-a-real-charset"), "abc")

        self.assertTrue(http_client.is_tls_error(ssl.SSLError("bad")))
        self.assertTrue(http_client.is_tls_error(FileNotFoundError("curl")))
        self.assertTrue(http_client.is_tls_error(URLError("TLS handshake failed")))
        self.assertTrue(http_client.is_tls_error(RuntimeError("schannel failure")))
        self.assertFalse(http_client.is_tls_error(RuntimeError("ordinary failure")))
        self.assertTrue(http_client.is_retryable_health_http(500))
        self.assertFalse(http_client.is_retryable_health_http(404))
        self.assertTrue(http_client.is_retryable_http_code(503))
        self.assertFalse(http_client.is_retryable_http_code(404))
        self.assertTrue(http_client.should_try_curl_fallback(ScrapeError("HTTP 503")))
        self.assertTrue(http_client.should_try_curl_fallback(ScrapeError("network timeout")))
        self.assertFalse(http_client.should_try_curl_fallback(ScrapeError("HTTP 404")))
        self.assertTrue(http_client.is_benchmark_net_ip("198.18.0.80"))
        self.assertFalse(http_client.is_benchmark_net_ip("10.0.0.1"))
        self.assertEqual(http_client.resolution_diagnostic("not a url"), "")
        with mock.patch.object(http_client.socket, "getaddrinfo", return_value=[]):
            self.assertEqual(http_client.resolution_diagnostic("https://example.test"), "")
        with mock.patch.object(
            http_client.socket,
            "getaddrinfo",
            return_value=[(None, None, None, None, ("198.18.0.80", 0))],
        ):
            self.assertIn("198.18.0.80", http_client.resolution_diagnostic("https://example.test"))
        with mock.patch.object(http_client.socket, "getaddrinfo", side_effect=OSError("dns")):
            self.assertEqual(http_client.resolution_diagnostic("https://example.test"), "")
        self.assertEqual(len(http_client._curl_variants()), 2)

    def test_http_open_and_health_retry_edges(self) -> None:
        request = http_client.Request("http://example.test")
        with mock.patch.object(http_client, "urlopen", return_value="plain") as open_url:
            self.assertEqual(http_client.open_url_with_retries(request, 1), "plain")
            open_url.assert_called_once_with(request, timeout=1)

        https_request = http_client.Request("https://example.test")
        context = object()
        with mock.patch.object(http_client, "TLS_RETRY_CONTEXTS", [("tls", context)]), mock.patch.object(
            http_client, "urlopen", return_value="secure"
        ) as open_url:
            http_client.TLS_CONTEXT_CACHE.clear()
            self.assertEqual(http_client.open_url_with_retries(https_request, 1), "secure")
            self.assertEqual(http_client.TLS_CONTEXT_CACHE["example.test"], "default")
            self.assertEqual(open_url.call_count, 1)

        with mock.patch.object(http_client, "urlopen", side_effect=HTTPError("url", 404, "no", {}, None)), self.assertRaises(HTTPError):
            http_client.open_url_with_retries(https_request, 1)
        with mock.patch.object(http_client, "urlopen", side_effect=URLError("ordinary")), self.assertRaises(URLError):
            http_client.open_url_with_retries(https_request, 1)
        with mock.patch.object(http_client, "TLS_RETRY_CONTEXTS", [("tls", context)]), mock.patch.object(
            http_client,
            "urlopen",
            side_effect=URLError(ssl.SSLError("handshake")),
        ):
            http_client.TLS_CONTEXT_CACHE.clear()
            with self.assertRaisesRegex(ScrapeError, "TLS handshake failed"):
                http_client.open_url_with_retries(https_request, 1)

        class OpenScope:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        http_client.health_check_url("https://example.test", open_url=lambda *_args, **_kwargs: OpenScope())
        http_client.health_check_url(
            "https://example.test",
            open_url=mock.Mock(side_effect=HTTPError("url", 404, "missing", {}, None)),
        )
        with mock.patch.object(http_client.time, "sleep") as sleep:
            opener = mock.Mock(
                side_effect=[HTTPError("url", 503, "busy", {}, None), OpenScope()]
            )
            http_client.health_check_url(
                "https://example.test",
                open_url=opener,
                network_attempts=2,
                proxy_retries=1,
            )
            sleep.assert_called_once_with(0.4)
        with mock.patch.object(http_client.time, "sleep") as sleep:
            opener = mock.Mock(side_effect=[URLError("down"), TimeoutError(), OSError("down")])
            with self.assertRaisesRegex(ScrapeError, "network error"):
                http_client.health_check_url(
                    "https://example.test",
                    open_url=opener,
                    network_attempts=3,
                    proxy_retries=1,
                )
            self.assertEqual(sleep.call_count, 2)
        with self.assertRaises(ScrapeError):
            http_client.health_check_url(
                "https://example.test",
                open_url=mock.Mock(side_effect=HTTPError("url", 503, "busy", {}, None)),
                network_attempts=2,
                proxy_retries=1,
            )

    def test_http_curl_node_raw_and_text_edges(self) -> None:
        with mock.patch.object(http_client.shutil, "which", return_value=None), self.assertRaisesRegex(
            ScrapeError, "curl fallback unavailable"
        ):
            http_client._curl_executable()

        completed = SimpleNamespace(returncode=0, stdout=b"body", stderr=b"")
        with mock.patch.object(http_client.shutil, "which", return_value="curl"), mock.patch.object(
            http_client.subprocess, "run", return_value=completed
        ) as run:
            self.assertEqual(http_client.fetch_raw_with_curl("https://example.test", 1)[0], b"body")
            self.assertTrue(run.call_args.kwargs["check"] is False)
        with mock.patch.object(http_client.shutil, "which", return_value="curl"), mock.patch.object(
            http_client.subprocess,
            "run",
            side_effect=[
                SimpleNamespace(returncode=2, stdout=b"", stderr=b"retry"),
                SimpleNamespace(returncode=0, stdout=b"body", stderr=b""),
            ],
        ):
            self.assertEqual(http_client.fetch_raw_with_curl("https://example.test", 1)[0], b"body")
        with mock.patch.object(http_client.shutil, "which", return_value="curl"), mock.patch.object(
            http_client.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=7, stdout=b"", stderr=b"bad"),
        ), self.assertRaisesRegex(ScrapeError, "curl exit 7"):
            http_client.fetch_raw_with_curl("https://example.test", 1)
        with mock.patch.object(http_client.shutil, "which", return_value="curl"), mock.patch.object(
            http_client.subprocess,
            "run",
            side_effect=http_client.subprocess.TimeoutExpired("curl", 1),
        ), self.assertRaisesRegex(ScrapeError, "network timeout"):
            http_client.fetch_raw_with_curl("https://example.test", 1)

        with mock.patch.object(http_client.shutil, "which", return_value=None), self.assertRaisesRegex(
            ScrapeError, "Node.js"
        ):
            http_client.fetch_text_with_node("https://example.test", 1)
        node_output = base64.b64encode("节点正文".encode()).decode()
        with mock.patch.object(http_client.shutil, "which", return_value="node"), mock.patch.object(
            http_client.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=node_output, stderr=""),
        ):
            self.assertEqual(http_client.fetch_text_with_node("https://example.test", 1), "节点正文")
        with mock.patch.object(http_client.shutil, "which", return_value="node"), mock.patch.object(
            http_client.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="failed"),
        ), self.assertRaisesRegex(ScrapeError, "node fetch failed"):
            http_client.fetch_text_with_node("https://example.test", 1)
        with mock.patch.object(http_client.shutil, "which", return_value="node"), mock.patch.object(
            http_client.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="a", stderr=""),
        ), self.assertRaisesRegex(ScrapeError, "invalid data"):
            http_client.fetch_text_with_node("https://example.test", 1)

        class Response:
            def __init__(self, data: bytes, encoding: str = "") -> None:
                self.data = data
                self.encoding = encoding
                self.headers = self

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get_content_charset(self):
                return "utf-8"

            def get(self, key: str, default: str = ""):
                return self.encoding if key == "Content-Encoding" else default

            def read(self):
                return self.data

        opener = mock.Mock(return_value=Response(b"raw text"))
        self.assertEqual(
            http_client.fetch_raw("https://example.test", 1, open_url=opener, network_attempts=1)[0],
            b"raw text",
        )
        with self.assertRaisesRegex(ScrapeError, "HTTP 404"):
            http_client.fetch_raw(
                "https://example.test",
                1,
                open_url=mock.Mock(side_effect=HTTPError("url", 404, "missing", {}, None)),
                network_attempts=1,
            )
        curl_ok = mock.Mock(return_value=(b"curl", "utf-8", ""))
        with mock.patch.object(http_client.time, "sleep"):
            self.assertEqual(
                http_client.fetch_raw(
                    "https://example.test",
                    1,
                    open_url=mock.Mock(side_effect=HTTPError("url", 503, "busy", {}, None)),
                    curl_fetcher=curl_ok,
                    network_attempts=1,
                )[0],
                b"curl",
            )
        with mock.patch.object(http_client, "resolution_diagnostic", return_value="污染诊断"), self.assertRaisesRegex(
            ScrapeError, "污染诊断"
        ):
            http_client.fetch_raw(
                "https://example.test",
                1,
                open_url=mock.Mock(side_effect=URLError("network down")),
                curl_fetcher=mock.Mock(side_effect=ScrapeError("curl failed")),
                network_attempts=1,
            )

        incomplete_response = Response(b"")
        incomplete_response.read = mock.Mock(side_effect=IncompleteRead(b"partial"))
        self.assertEqual(
            http_client.fetch_raw(
                "https://example.test",
                1,
                open_url=mock.Mock(return_value=incomplete_response),
                curl_fetcher=curl_ok,
                network_attempts=1,
            )[0],
            b"curl",
        )
        for failure in (
            ScrapeError("network source"),
            URLError("down"),
            TimeoutError(),
            OSError("down"),
        ):
            with self.subTest(failure=type(failure).__name__), mock.patch.object(
                http_client, "resolution_diagnostic", return_value=""
            ), self.assertRaises(ScrapeError):
                http_client.fetch_raw(
                    "https://example.test",
                    1,
                    open_url=mock.Mock(side_effect=failure),
                    curl_fetcher=mock.Mock(side_effect=ScrapeError("curl failed")),
                    network_attempts=1,
                )

        self.assertEqual(
            http_client.fetch_text(
                "https://example.test",
                1,
                raw_fetcher=lambda *_args, **_kwargs: (b"normal", "utf-8", ""),
            ),
            "normal",
        )
        self.assertEqual(
            http_client.fetch_text(
                "https://example.test",
                1,
                raw_fetcher=lambda *_args, **_kwargs: (gzip.compress(b"gzip"), "utf-8", "gzip"),
            ),
            "gzip",
        )
        node = mock.Mock(return_value="node fallback")
        self.assertEqual(
            http_client.fetch_text(
                "https://example.test",
                1,
                raw_fetcher=lambda *_args, **_kwargs: (b'"abcabc"', "utf-8", ""),
                node_fetcher=node,
            ),
            "node fallback",
        )
        self.assertEqual(
            http_client.fetch_text(
                "https://example.test",
                1,
                raw_fetcher=lambda *_args, **_kwargs: (b"abcabc", "utf-8", ""),
                node_fetcher=mock.Mock(side_effect=ScrapeError("node")),
            ),
            "abcabc",
        )
        with self.assertRaisesRegex(ScrapeError, "decode compressed"):
            http_client.fetch_text(
                "https://example.test",
                1,
                raw_fetcher=lambda *_args, **_kwargs: (b"bad", "utf-8", "gzip"),
            )

    def test_http_text_cache_deduplicates_and_recovers_after_failure(self) -> None:
        calls: list[tuple[str, int]] = []

        def fetcher(url: str, timeout: int) -> str:
            calls.append((url, timeout))
            return "value"

        cache = http_client.TextFetchCache(fetcher)
        self.assertEqual(cache.fetch("https://example.test", 1), "value")
        self.assertEqual(cache.fetch("https://example.test", 1), "value")
        self.assertEqual(calls, [("https://example.test", 1)])

        attempts = iter([ScrapeError("first"), "recovered"])

        def recovering_fetch(*_args) -> str:
            value = next(attempts)
            if isinstance(value, ScrapeError):
                raise value
            return value

        recovering = http_client.TextFetchCache(recovering_fetch)
        with self.assertRaises(ScrapeError):
            recovering.fetch("https://example.test", 1)
        self.assertEqual(recovering.fetch("https://example.test", 1), "recovered")

        entered = threading.Event()
        release = threading.Event()
        concurrent_calls = 0

        def slow_fetch(_url: str, _timeout: int) -> str:
            nonlocal concurrent_calls
            concurrent_calls += 1
            entered.set()
            release.wait(1)
            return "shared"

        cache = http_client.TextFetchCache(slow_fetch)
        results: list[str] = []

        def worker() -> None:
            results.append(cache.fetch("https://example.test", 1))

        first = threading.Thread(target=worker)
        second = threading.Thread(target=worker)
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        release.set()
        first.join()
        second.join()
        self.assertEqual(concurrent_calls, 1)
        self.assertEqual(results, ["shared", "shared"])

    def test_configure_proxy_none_restores_default_opener(self) -> None:
        with mock.patch.object(http_client, "build_opener") as build, mock.patch.object(
            http_client, "install_opener"
        ) as install:
            http_client.configure_proxy("http://proxy.test:8080")
            self.assertEqual(build.call_count, 1)
            install.assert_called_once_with(build.return_value)

            build.reset_mock()
            install.reset_mock()
            http_client.configure_proxy(None)

        build.assert_called_once_with()
        install.assert_called_once_with(build.return_value)

    def test_only_trusted_incomplete_api_content_is_browser_eligible(self) -> None:
        config = _config()
        allowed = (
            "API目标记录正文缺失",
            "API空壳: 未返回任何带记录ID的文章",
        )
        denied = (
            "API response is not valid JSON",
            "API ID不匹配: 未找到URL记录ID: target-id",
            "API作者缺失",
            "API标题缺少期数或栏目关键词",
            "API栏目关键词不匹配",
            "API目标关键词不匹配",
            "API存在多个同ID目标记录: target-id",
            "HTTP 500",
            "network error: TLS handshake failed",
        )
        for message in allowed:
            with self.subTest(message=message):
                self.assertTrue(should_render_api_record_fallback(config, ScrapeError(message)))
        for message in denied:
            with self.subTest(message=message):
                self.assertFalse(should_render_api_record_fallback(config, ScrapeError(message)))

    def test_api_section_keywords_can_match_same_object_sections(self) -> None:
        config = replace(
            _config(),
            keywords=("数据关键词",),
            section_keywords=("栏目锚点",),
        )
        payload = json.dumps(
            {
                "data": [
                    {
                        "id": "target-id",
                        "authorNickname": "测试站",
                        "title": "210期",
                        "formSections": [{"name": "栏目锚点"}],
                        "content": "数据关键词 正文",
                    }
                ]
            },
            ensure_ascii=False,
        )

        article = source_adapters.article_record_from_payload(payload, config)

        self.assertIn("栏目锚点", article.document)
        self.assertEqual(article.section_names, ("栏目锚点",))

    def test_dynamic_identity_rejects_section_keyword_forged_only_in_body(self) -> None:
        config = replace(
            _config(),
            section_keywords=("栏目锚点",),
        )
        payload = json.dumps(
            {
                "data": [
                    {
                        "id": "target-id",
                        "authorNickname": "测试站",
                        "title": "210期 三十六码",
                        "formSections": [{"name": "其他栏目"}],
                        "content": "三十六码 栏目锚点 正文",
                    }
                ]
            },
            ensure_ascii=False,
        )

        article = source_adapters.article_record_from_payload(payload, config)
        self.assertEqual(article.section_names, ("其他栏目",))
        with self.assertRaisesRegex(ScrapeError, "API栏目关键词不匹配"):
            dynamic_article.validate_article_identity(article, config)

    def test_api_adapter_does_not_use_sections_for_data_keywords(self) -> None:
        config = replace(
            _config(),
            keywords=("数据关键词",),
            section_keywords=("栏目锚点",),
        )
        payload = json.dumps(
            {
                "data": [
                    {
                        "id": "target-id",
                        "authorNickname": "测试站",
                        "title": "210期",
                        "formSections": [
                            {"name": "栏目锚点"},
                            {"name": "数据关键词"},
                        ],
                        "content": "正文",
                    }
                ]
            },
            ensure_ascii=False,
        )

        article = source_adapters.article_record_from_payload(payload, config)

        self.assertEqual(article.body, "正文")

    def test_source_adapters_pagination_and_decoding_edges(self) -> None:
        html = '<a href="/p2"> 下一 页 </a><a href="/detail">详情</a>'
        links = source_adapters.raw_anchor_links(html, "https://example.test/p1")
        self.assertEqual(links[0].href, "https://example.test/p2")
        self.assertEqual(links[1].text, "详情")

        pages = {
            "https://example.test/p1": '<a href="/p2">下一页</a>',
            "https://example.test/p2": "最后一页",
        }
        documents = source_adapters.fetch_paginated_list_documents(
            "https://example.test/p1",
            2,
            lambda url, _timeout: pages[url],
        )
        self.assertEqual([document.url for document in documents], list(pages))

        with self.assertRaisesRegex(ScrapeError, "URL无效"):
            source_adapters.fetch_paginated_list_documents("/relative", 1, lambda *_: "")
        with self.assertRaisesRegex(ScrapeError, "跨域"):
            source_adapters.fetch_paginated_list_documents(
                "https://example.test/p1",
                1,
                lambda *_: '<a href="https://other.test/p2">下一页</a>',
            )
        with self.assertRaisesRegex(ScrapeError, "多个下一页"):
            source_adapters.fetch_paginated_list_documents(
                "https://example.test/p1",
                1,
                lambda *_: '<a href="/p2">下一页</a><a href="/p3">下一页</a>',
            )
        cyclic = {
            "https://example.test/p1": '<a href="/p2">下一页</a>',
            "https://example.test/p2": '<a href="/p1">下一页</a>',
        }
        with self.assertRaisesRegex(ScrapeError, "分页链接循环"):
            source_adapters.fetch_paginated_list_documents(
                "https://example.test/p1", 1, lambda url, _timeout: cyclic[url]
            )
        self.assertEqual(
            len(
                source_adapters.fetch_paginated_list_documents(
                    "https://example.test/p1",
                    1,
                    lambda *_: '<a href="/p2#fragment">下一页</a>',
                )
            ),
            1,
        )

        def fifty_pages(url: str, _timeout: int) -> str:
            page = int(url.rsplit("p", 1)[1])
            return f'<a href="/p{page + 1}">下一页</a>'

        with self.assertRaisesRegex(ScrapeError, "超过50页"):
            source_adapters.fetch_paginated_list_documents(
                "https://example.test/p1", 1, fifty_pages
            )

        encoded = base64.b64encode("中文 <b>内容</b>".encode()).decode()
        self.assertEqual(source_adapters.decode_possible_base64_text(encoded), "中文 <b>内容</b>")
        for invalid in ("", "not base64!", "a", base64.b64encode(b"plain ascii").decode()):
            self.assertIsNone(source_adapters.decode_possible_base64_text(invalid))
        self.assertIsNone(
            source_adapters.decode_possible_base64_text(base64.b64encode(b"\xff").decode())
        )
        self.assertEqual(
            source_adapters.decode_possible_base64_text(encoded.replace("+", "-").replace("/", "_")),
            "中文 <b>内容</b>",
        )

    def test_source_adapters_record_and_payload_edges(self) -> None:
        config = _config()
        encoded = base64.b64encode("中文 <b>内容</b>".encode()).decode()
        self.assertEqual(source_adapters.article_detail_id(config), "target-id")
        with self.assertRaisesRegex(ScrapeError, "没有唯一记录ID"):
            source_adapters.article_detail_id(replace(config, url="https://example.test/topic"))
        with self.assertRaisesRegex(ScrapeError, "配置记录ID"):
            source_adapters.article_detail_id(replace(config, record_id="other-id"))

        nested = {"outer": [{"id": "target-id"}, {"record": {"name": "nested"}}]}
        paths = [path for path, _ in source_adapters.iter_json_dicts(nested)]
        self.assertIn("$.outer[0]", paths)
        self.assertIn("$.outer[1].record", paths)
        self.assertTrue(source_adapters.payload_contains_record_id(json.dumps(nested), "target-id"))
        self.assertFalse(source_adapters.payload_contains_record_id("{bad", "target-id"))

        self.assertEqual(
            source_adapters.nested_record_value({"data": {"title": "nested"}}, ("title",)),
            "nested",
        )
        self.assertIsNone(source_adapters.nested_record_value({"data": []}, ("title",)))
        self.assertEqual(source_adapters.article_body({"content": " direct "})[0], "direct")
        self.assertEqual(
            source_adapters.article_body({"data": {"html": "nested"}}, "$.root"),
            ("nested", "$.root.data.html"),
        )
        self.assertEqual(source_adapters.article_body({}), ("", ""))
        self.assertEqual(source_adapters.article_section_names({"formSections": [{"name": "A"}, {}]}), ("A",))
        self.assertEqual(source_adapters.article_section_names({"sections": "B"}), ("B",))
        self.assertEqual(source_adapters.article_section_names({}), ())

        title = base64.b64encode("210期 三十六码".encode()).decode()
        payload = json.dumps(
            {
                "data": [
                    {
                        "id": "target-id",
                        "authorNickname": "真实作者",
                        "title": title,
                        "sections": "栏目",
                        "html": "三十六码 正文",
                    }
                ]
            },
            ensure_ascii=False,
        )
        article = source_adapters.article_record_from_payload(payload, config, source_path="api")
        self.assertEqual(article.title, "210期 三十六码")
        self.assertEqual(article.record_path, "api::$.data[0]")
        self.assertIn("真实作者\n栏目\n210期", article.document)

        for bad_payload, message in (
            ("{bad", "not valid JSON"),
            (json.dumps({"data": [{"id": "other"}]}), "API ID不匹配"),
            (json.dumps({"data": [{"id": "target-id"}, {"id": "target-id"}]}), "多个同ID"),
            (json.dumps({"data": [{"id": "target-id", "title": "210期"}]}), "正文缺失"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ScrapeError, message):
                source_adapters.article_record_from_payload(bad_payload, config)
        with self.assertRaisesRegex(ScrapeError, "空壳"):
            source_adapters.article_record_from_payload("{\"data\": []}", config)

        self.assertEqual(
            source_adapters.api_payload_to_html(json.dumps({"data": {"content": "正文"}})),
            "正文",
        )
        self.assertEqual(
            source_adapters.api_payload_to_html(
                json.dumps([{"html": encoded}])
            ),
            "中文 <b>内容</b>",
        )
        with self.assertRaisesRegex(ScrapeError, "not valid JSON"):
            source_adapters.api_payload_to_html("{bad")
        with self.assertRaisesRegex(ScrapeError, "list of records"):
            source_adapters.api_payload_to_html(json.dumps({"data": "bad"}))
        with self.assertRaisesRegex(ScrapeError, "多篇记录"):
            source_adapters.api_payload_to_html(json.dumps([{"content": "a"}, {"content": "b"}]), allow_multiple=False)
        with self.assertRaisesRegex(ScrapeError, "record content"):
            source_adapters.api_payload_to_html(json.dumps([{}, "bad"]))

    def test_dynamic_identity_uses_author_section_anchor_not_output_name(self) -> None:
        config = replace(
            _config(),
            name="输出别名",
            section_keywords=("栏目锚点",),
        )
        payload = json.dumps(
            {
                "data": [
                    {
                        "id": "target-id",
                        "authorNickname": "真实作者",
                        "title": "210期 三十六码",
                        "formSections": [{"name": "栏目锚点"}],
                        "content": "三十六码 正文",
                    }
                ]
            },
            ensure_ascii=False,
        )

        article = source_adapters.article_record_from_payload(payload, config)
        dynamic_article.validate_article_identity(article, config)

        self.assertEqual(
            article.document.splitlines()[:3],
            ["真实作者", "栏目锚点", "210期 三十六码"],
        )

    def test_dynamic_identity_rejects_empty_author_or_missing_author_anchor(self) -> None:
        config = replace(_config(), section_keywords=("真实作者",))
        for label, author, sections, message in (
            ("empty author", "", [{"name": "真实作者"}], "API作者缺失"),
            (
                "wrong author without anchor",
                "其他作者",
                [{"name": "栏目锚点"}],
                "API栏目关键词不匹配",
            ),
        ):
            with self.subTest(label=label):
                payload = json.dumps(
                    {
                        "data": [
                            {
                                "id": "target-id",
                                "authorNickname": author,
                                "title": "210期 三十六码",
                                "formSections": sections,
                                "content": "三十六码 正文",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
                article = source_adapters.article_record_from_payload(payload, config)
                with self.assertRaisesRegex(ScrapeError, message):
                    dynamic_article.validate_article_identity(article, config)

    def test_dynamic_identity_title_and_collection_boundary_edges(self) -> None:
        config = replace(_config(), keywords=("数据",), section_keywords=())
        dynamic_article.validate_article_identity(
            ArticleRecord("id", "path", "数据栏目", "作者", "数据正文", "作者\n数据栏目\n数据正文"),
            config,
        )
        for article, message in (
            (ArticleRecord("id", "path", "", "作者", "数据正文", "作者"), "标题缺失"),
            (ArticleRecord("id", "path", "栏目", "作者", "数据正文", "作者\n栏目"), "标题缺少期数"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ScrapeError, message):
                dynamic_article.validate_article_identity(article, config)

        collection_config = replace(
            _config(),
            source_type="dynamic_collection",
            api_url="https://api.example/users/3792/forums",
            parser_id="kunnan_magazine",
        )
        numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        valid = {
            "user_id": "3792",
            "status": "published",
            "lottery": "macao",
            "topic": "专属 三十六码",
            "id": "valid",
            "draw": "236",
            "content": f"测试站 三十六码 专属\n236期\n{numbers}",
        }
        filtered = [
            "not a mapping",
            {**valid, "id": "wrong-user", "user_id": "1"},
            {**valid, "id": "wrong-status", "status": "draft"},
            {**valid, "id": "wrong-lottery", "lottery": "other"},
            {**valid, "id": "wrong-topic", "topic": "其他"},
            valid,
        ]
        result = dynamic_article.kunnan_magazine_records_from_payload(
            json.dumps({"data": filtered}, ensure_ascii=False), collection_config
        )
        self.assertEqual(result[0].record_id, "valid")
        with self.assertRaisesRegex(ScrapeError, "not valid JSON"):
            dynamic_article.kunnan_magazine_records_from_payload("{bad", collection_config)
        with self.assertRaisesRegex(ScrapeError, "记录列表"):
            dynamic_article.kunnan_magazine_records_from_payload("{}", collection_config)
        with self.assertRaisesRegex(ScrapeError, "没有找到符合"):
            dynamic_article.kunnan_magazine_records_from_payload(
                json.dumps([{**valid, "user_id": "other"}]), collection_config
            )
        with self.assertRaisesRegex(ScrapeError, "缺少文章ID"):
            dynamic_article.kunnan_magazine_records_from_payload(
                json.dumps([{**valid, "id": ""}]), collection_config
            )
        duplicate_id = [valid, {**valid, "draw": "235"}]
        with self.assertRaisesRegex(ScrapeError, "重复文章ID"):
            dynamic_article.kunnan_magazine_records_from_payload(
                json.dumps(duplicate_id), collection_config
            )
        duplicate_issue = [valid, {**valid, "id": "other"}]
        with self.assertRaisesRegex(ScrapeError, "存在多个目标文章"):
            dynamic_article.kunnan_magazine_records_from_payload(
                json.dumps(duplicate_issue), collection_config
            )
        with self.assertRaisesRegex(ScrapeError, "正文缺失"):
            dynamic_article.kunnan_magazine_records_from_payload(
                json.dumps([{**valid, "content": ""}]), collection_config
            )
        with mock.patch.object(
            dynamic_article,
            "extract_generic_36",
            return_value=ParsedRecord("x", "x", 235, NUMBERS),
        ), self.assertRaisesRegex(ScrapeError, "未通过36码校验"):
            dynamic_article.kunnan_magazine_records_from_payload(
                json.dumps([valid]), collection_config
            )
        with mock.patch.object(
            dynamic_article,
            "extract_generic_36",
            return_value=ParsedRecord("x", "x", 236, NUMBERS),
        ), self.assertRaisesRegex(ScrapeError, "缺少候选证据"):
            dynamic_article.kunnan_magazine_records_from_payload(
                json.dumps([valid]), collection_config
            )

    def test_formal_dynamic_services_reject_business_identity_mismatches(self) -> None:
        config = _config()
        base_record = {
            "id": "target-id",
            "authorNickname": "测试站",
            "title": "210期 三十六码",
            "formSections": [{"name": "栏目锚点"}],
            "content": "三十六码 正文",
        }
        cases = (
            (
                "empty author",
                replace(config, section_keywords=("栏目锚点",)),
                {"authorNickname": ""},
            ),
            (
                "section",
                replace(config, section_keywords=("真实栏目",)),
                {},
            ),
            (
                "keyword",
                config,
                {"title": "210期", "content": "正文"},
            ),
        )
        for label, case_config, changes in cases:
            with self.subTest(service="scrape", label=label):
                record = {**base_record, **changes}
                payload = json.dumps({"data": [record]}, ensure_ascii=False)
                parser = mock.Mock()
                service = ScrapeService(
                    text_fetcher=mock.Mock(return_value=payload),
                    parser=parser,
                )
                with self.assertRaises(ScrapeError):
                    service.scrape(case_config, fixed_issue=210)
                parser.assert_not_called()

            with self.subTest(service="duplicate", label=label):
                record = {**base_record, **changes}
                payload = json.dumps({"data": [record]}, ensure_ascii=False)
                with mock.patch.object(
                    duplicate_service,
                    "collect_issue_records",
                    side_effect=AssertionError("business identity must fail first"),
                ) as collect, self.assertRaises(ScrapeError):
                    duplicate_service.scrape_site_window(
                        case_config,
                        period=210,
                        periods=1,
                        min_records=1,
                        timeout=1,
                        text_fetcher=mock.Mock(return_value=payload),
                    )
                collect.assert_not_called()

    def test_never_policy_never_allows_api_record_fallback(self) -> None:
        config = _config(policy="never")
        self.assertFalse(
            should_render_api_record_fallback(config, ScrapeError("API目标记录正文缺失"))
        )

    def test_single_dynamic_api_only_renders_for_trusted_incomplete_content(self) -> None:
        config = _config()
        article = ArticleRecord(
            "target-id",
            "browser::$.data[0]",
            "210期 三十六码",
            "测试站",
            "测试站 三十六码\n210期\n01 02",
            BROWSER_DOCUMENT,
        )
        for payload, should_render in (
            (_payload(""), True),
            ("{bad", False),
            (_payload().replace("target-id", "other-id"), False),
        ):
            with self.subTest(should_render=should_render):
                browser = mock.Mock(return_value=article)
                service = ScrapeService(
                    text_fetcher=mock.Mock(return_value=payload),
                    parser=mock.Mock(side_effect=ScrapeError("业务解析不完整")),
                    rendered_article_fetcher=browser,
                )
                with self.assertRaises(ScrapeError):
                    service.scrape(config, fixed_issue=210)
                self.assertEqual(browser.call_count, int(should_render))

    def test_single_dynamic_api_parse_failure_is_hard_failure(self) -> None:
        config = _config(policy="always")
        browser = mock.Mock(side_effect=AssertionError("parser failure must not render"))
        service = ScrapeService(
            text_fetcher=mock.Mock(return_value=_payload("完整正文")),
            parser=mock.Mock(side_effect=ScrapeError("目标36码不完整")),
            rendered_article_fetcher=browser,
        )
        with self.assertRaises(ScrapeError):
            service.scrape(config, fixed_issue=210)
        browser.assert_not_called()

    def test_duplicate_dynamic_api_parse_failure_is_hard_failure(self) -> None:
        config = _config(policy="always")
        browser = mock.patch.object(
            duplicate_service.browser_client,
            "fetch_rendered_article_record",
            side_effect=AssertionError("parser failure must not render"),
        )
        with browser as render, mock.patch.object(
            duplicate_service,
            "collect_issue_records",
            side_effect=ScrapeError("目标36码不完整"),
        ), self.assertRaises(ScrapeError):
            duplicate_service.scrape_site_window(
                config,
                period=210,
                periods=1,
                min_records=1,
                timeout=1,
                text_fetcher=mock.Mock(return_value=_payload("完整正文")),
            )
        render.assert_not_called()

    def test_browser_renderer_reuses_context_per_thread(self) -> None:
        events: list[str] = []

        class FakeBody:
            def inner_text(self, timeout: int) -> str:
                return " ".join(f"{value:02d}" for value in range(1, 37))

        class FakePage:
            def __init__(self) -> None:
                self.body = FakeBody()
                self.handlers: dict[str, object] = {}

            def goto(self, *args, **kwargs) -> None:
                callback = self.handlers.get("response")
                if callback is not None:
                    callback(FakeResponse())

            def on(self, event: str, callback) -> None:
                self.handlers[event] = callback

            def locator(self, _selector: str) -> FakeBody:
                return self.body

            def wait_for_load_state(self, *args, **kwargs) -> None:
                return None

            def wait_for_timeout(self, _timeout: int) -> None:
                return None

            def content(self) -> str:
                return "<body>" + self.body.inner_text(1000) + "</body>"

            def close(self) -> None:
                events.append("page.close")

        class FakeResponse:
            def __init__(self) -> None:
                self.headers = {"content-type": "application/json"}
                self.url = "https://example.test/api/target-id"

            def body(self) -> bytes:
                return _payload().encode("utf-8")

        class FakeContext:
            def __init__(self) -> None:
                self.pages: list[FakePage] = []
                self.resource_route = None

            def route(self, pattern: str, handler) -> None:
                self.assert_pattern = pattern
                self.resource_route = handler

            def new_page(self) -> FakePage:
                page = FakePage()
                self.pages.append(page)
                return page

            def close(self) -> None:
                events.append("context.close")

        class FakeBrowser:
            def __init__(self) -> None:
                self.contexts: list[FakeContext] = []

            def new_context(self, **_kwargs) -> FakeContext:
                context = FakeContext()
                self.contexts.append(context)
                return context

            def new_page(self, **_kwargs):
                raise AssertionError("renderer must use the reusable context")

            def close(self) -> None:
                events.append("browser.close")

        class FakePlaywright:
            def __init__(self) -> None:
                self.browser = FakeBrowser()
                self.chromium = mock.Mock()
                self.chromium.launch.return_value = self.browser

            def stop(self) -> None:
                events.append("playwright.stop")

        playwright = FakePlaywright()
        renderer = browser_client.ReusableBrowserRenderer(lambda: playwright)
        renderer.fetch("https://example.test/", timeout=1)
        renderer.fetch("https://example.test/other", timeout=1)
        article = renderer.fetch_article(_config(), timeout=1)

        self.assertEqual(len(playwright.browser.contexts), 1)
        self.assertEqual(len(playwright.browser.contexts[0].pages), 3)
        self.assertEqual(article.record_id, "target-id")
        actions: list[str] = []

        class FakeRoute:
            def abort(self) -> None:
                actions.append("abort")

            def continue_(self) -> None:
                actions.append("continue")

        handler = playwright.browser.contexts[0].resource_route
        self.assertIsNotNone(handler)
        for resource_type in ("image", "media", "font", "document", "script", "xhr", "fetch", "stylesheet"):
            handler(FakeRoute(), SimpleNamespace(resource_type=resource_type))
        self.assertEqual(actions, ["abort", "abort", "abort", "continue", "continue", "continue", "continue", "continue"])
        renderer.close_all()
        self.assertEqual(
            events,
            [
                "page.close",
                "page.close",
                "page.close",
                "context.close",
                "browser.close",
                "playwright.stop",
            ],
        )

    def test_browser_renderer_serializes_all_browser_work_on_owner_thread(self) -> None:
        events: list[tuple[str, int]] = []
        events_lock = threading.Lock()
        numbers_text = " ".join(f"{value:02d}" for value in range(1, 37))

        def record(name: str) -> None:
            with events_lock:
                events.append((name, threading.get_ident()))

        class FakeBody:
            def inner_text(self, timeout: int) -> str:
                record(f"body.inner_text:{timeout}")
                return numbers_text

        class FakePage:
            def goto(self, *_args, **_kwargs) -> None:
                record("page.goto")

            def locator(self, _selector: str) -> FakeBody:
                record("page.locator")
                return FakeBody()

            def wait_for_load_state(self, *_args, **_kwargs) -> None:
                record("page.wait_for_load_state")

            def content(self) -> str:
                record("page.content")
                return numbers_text

            def close(self) -> None:
                record("page.close")

        class FakeContext:
            def route(self, *_args, **_kwargs) -> None:
                record("context.route")

            def new_page(self) -> FakePage:
                record("context.new_page")
                return FakePage()

            def close(self) -> None:
                record("context.close")

        class FakeBrowser:
            def new_context(self, **_kwargs) -> FakeContext:
                record("browser.new_context")
                return FakeContext()

            def close(self) -> None:
                record("browser.close")

        class FakeChromium:
            def launch(self, **_kwargs) -> FakeBrowser:
                record("chromium.launch")
                return FakeBrowser()

        class FakePlaywright:
            chromium = FakeChromium()

            def stop(self) -> None:
                record("playwright.stop")

        renderer = browser_client.ReusableBrowserRenderer(FakePlaywright)
        caller_threads: set[int] = set()

        def fetch(index: int) -> str:
            caller_threads.add(threading.get_ident())
            return renderer.fetch(f"https://example.test/{index}", timeout=1)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(fetch, range(12)))

        self.assertEqual(results, [numbers_text] * 12)
        operation_threads = {thread_id for _name, thread_id in events}
        self.assertEqual(len(operation_threads), 1)
        self.assertTrue(caller_threads)
        self.assertTrue(operation_threads.isdisjoint(caller_threads))
        owner_thread = next(iter(operation_threads))

        renderer.close_all()
        renderer.close_all()

        self.assertEqual(
            {thread_id for name, thread_id in events if name.endswith("close") or name == "playwright.stop"},
            {owner_thread},
        )
        self.assertEqual(
            [name for name, _thread_id in events if name in {"context.close", "browser.close", "playwright.stop"}],
            ["context.close", "browser.close", "playwright.stop"],
        )
        with self.assertRaisesRegex(RuntimeError, "closed"):
            renderer.fetch("https://example.test/after-close", timeout=1)

    def test_global_renderer_initialization_is_singleton_under_concurrency(self) -> None:
        browser_client.close_browser_renderer()
        created: list[object] = []
        closed: list[object] = []

        class FakeRenderer:
            def __init__(self, _starter) -> None:
                created.append(self)

            def close_all(self) -> None:
                closed.append(self)

        def get_renderer() -> object:
            return browser_client._get_renderer(lambda: object())

        with mock.patch.object(
            browser_client, "ReusableBrowserRenderer", FakeRenderer
        ), ThreadPoolExecutor(max_workers=8) as pool:
            renderers = list(pool.map(lambda _index: get_renderer(), range(32)))

        self.assertEqual(len(created), 1)
        self.assertEqual({id(renderer) for renderer in renderers}, {id(created[0])})
        browser_client.close_browser_renderer()
        self.assertEqual(closed, [created[0]])

    def test_global_renderer_rebuilds_after_close(self) -> None:
        browser_client.close_browser_renderer()
        created: list[object] = []

        class FakeRenderer:
            def __init__(self, _starter) -> None:
                created.append(self)

            def close_all(self) -> None:
                return None

        with mock.patch.object(browser_client, "ReusableBrowserRenderer", FakeRenderer):
            first = browser_client._get_renderer(lambda: object())
            browser_client.close_browser_renderer()
            second = browser_client._get_renderer(lambda: object())
            browser_client.close_browser_renderer()

        self.assertEqual(len(created), 2)
        self.assertIsNot(first, second)

    def test_plain_http_evidence_does_not_claim_expansion(self) -> None:
        config = SiteConfig(
            name="普通站",
            url="https://example.test/topic",
            source_type="generic_html",
        )
        self.assertEqual(ScrapeService._source_kind(config, False), "HTTP页面正文")
        self.assertEqual(ScrapeService._evidence_source_method(config, False), "http_html")
        self.assertFalse(hasattr(source_adapters, "fetch_expanded_html"))

    def test_browser_raw_fetch_has_no_business_arguments(self) -> None:
        raw_text = "页面原始正文，不含指定期数或36码"
        page = mock.Mock()
        page.locator.return_value.inner_text.return_value = raw_text
        page.content.return_value = raw_text
        context = mock.Mock()
        context.new_page.return_value = page
        renderer = browser_client.ReusableBrowserRenderer(lambda: object())
        renderer.local.state = browser_client.BrowserRenderState(object(), object(), context)

        self.assertFalse(hasattr(browser_client, "rendered_text_ready"))
        result = renderer.fetch("https://example.test/", timeout=1)

        self.assertEqual(result, raw_text)
        self.assertEqual(page.wait_for_load_state.call_count, 1)
        page.wait_for_timeout.assert_not_called()

    def test_browser_public_wrappers_translate_renderer_failures(self) -> None:
        article = ArticleRecord("id", "path", "标题", "作者", "正文", "作者\n标题\n正文")
        renderer = mock.Mock()
        renderer.fetch.return_value = "正文"
        renderer.fetch_article.return_value = article
        with mock.patch.object(browser_client, "_get_renderer", return_value=renderer):
            self.assertEqual(browser_client.fetch_rendered_text("https://example.test", 1), "正文")
            self.assertEqual(browser_client.fetch_rendered_article_record(_config(), 1), article)
            renderer.fetch.side_effect = RuntimeError("text render")
            with self.assertRaisesRegex(ScrapeError, "browser rendering failed"):
                browser_client.fetch_rendered_text("https://example.test", 1)
            renderer.fetch_article.side_effect = ScrapeError("article scrape")
            with self.assertRaisesRegex(ScrapeError, "article scrape"):
                browser_client.fetch_rendered_article_record(_config(), 1)
            renderer.fetch_article.side_effect = RuntimeError("article render")
            with self.assertRaisesRegex(ScrapeError, "browser article rendering failed"):
                browser_client.fetch_rendered_article_record(_config(), 1)

    def test_browser_resource_and_cleanup_error_edges(self) -> None:
        browser_client._install_resource_filter(SimpleNamespace())

        class BadBrowser:
            def new_context(self, **_kwargs):
                raise browser_client.PlaywrightError("context")

            def close(self) -> None:
                raise browser_client.PlaywrightError("browser close")

        class BadPlaywright:
            chromium = SimpleNamespace(launch=lambda **_kwargs: BadBrowser())

            def stop(self) -> None:
                raise browser_client.PlaywrightError("stop")

        with self.assertRaises(browser_client.PlaywrightError):
            browser_client.ReusableBrowserRenderer(BadPlaywright).state()
        page = SimpleNamespace()
        browser_client._close_page_best_effort(page)

        context = mock.Mock()
        browser = mock.Mock()
        playwright = mock.Mock()
        context.close.side_effect = browser_client.PlaywrightError("context")
        browser.close.side_effect = browser_client.PlaywrightError("browser")
        playwright.stop.side_effect = browser_client.PlaywrightError("stop")
        renderer = browser_client.ReusableBrowserRenderer(lambda: object())
        renderer.states.append(browser_client.BrowserRenderState(playwright, browser, context))
        renderer.close_all()

    def test_browser_fetch_wait_and_response_filter_edges(self) -> None:
        page = mock.Mock()
        page.locator.return_value.inner_text.return_value = "正文"
        page.content.return_value = "正文"
        page.wait_for_load_state.side_effect = browser_client.PlaywrightError("idle")
        context = mock.Mock()
        context.new_page.return_value = page
        renderer = browser_client.ReusableBrowserRenderer(lambda: object())
        renderer.local.state = browser_client.BrowserRenderState(object(), object(), context)
        self.assertEqual(renderer.fetch("https://example.test", 1), "正文")

        page = mock.Mock()
        page.locator.return_value.inner_text.return_value = ""
        page.wait_for_load_state.side_effect = browser_client.PlaywrightError("idle")
        response = mock.Mock(headers={"content-type": "text/html"}, url="https://example.test/page")
        api_response = mock.Mock(headers={"content-type": "application/json"}, url="https://example.test/api")
        api_response.body.side_effect = browser_client.PlaywrightError("body")
        wrong_response = mock.Mock(headers={"content-type": "application/json"}, url="https://example.test/api")
        wrong_response.body.return_value = b"{\"data\": [{\"id\": \"other\"}]}"
        page.on.side_effect = lambda _event, callback: (
            callback(response),
            callback(api_response),
            callback(wrong_response),
        )
        context = mock.Mock()
        context.new_page.return_value = page
        renderer = browser_client.ReusableBrowserRenderer(lambda: object())
        renderer.local.state = browser_client.BrowserRenderState(object(), object(), context)
        with self.assertRaisesRegex(ScrapeError, "未找到URL记录ID"):
            renderer.fetch_article(_config(), 1)

    def test_playwright_page_close_errors_do_not_replace_fetch_results(self) -> None:
        numbers_text = " ".join(f"{value:02d}" for value in range(1, 37))
        page = mock.Mock()
        page.locator.return_value.inner_text.return_value = numbers_text
        page.content.return_value = numbers_text
        page.close.side_effect = browser_client.PlaywrightError("page already closed")
        context = mock.Mock()
        context.new_page.return_value = page
        renderer = browser_client.ReusableBrowserRenderer(lambda: object())
        renderer.local.state = browser_client.BrowserRenderState(object(), object(), context)

        self.assertEqual(renderer.fetch("https://example.test/", timeout=1), numbers_text)

    def test_playwright_article_close_errors_do_not_replace_article(self) -> None:
        page = mock.Mock()
        page.locator.return_value.inner_text.return_value = ""
        response = mock.Mock(
            headers={"content-type": "application/json"},
            url="https://example.test/api/target-id",
        )
        response.body.return_value = _payload().encode("utf-8")
        page.on.side_effect = lambda _event, callback: callback(response)
        page.close.side_effect = browser_client.PlaywrightError("page already closed")
        context = mock.Mock()
        context.new_page.return_value = page
        renderer = browser_client.ReusableBrowserRenderer(lambda: object())
        renderer.local.state = browser_client.BrowserRenderState(object(), object(), context)

        article = renderer.fetch_article(_config(), timeout=1)

        self.assertEqual(article.record_id, "target-id")

    def test_playwright_article_rejects_same_id_author_conflict(self) -> None:
        page = mock.Mock()
        page.locator.return_value.inner_text.return_value = ""
        first = mock.Mock(
            headers={"content-type": "application/json"},
            url="https://example.test/api/target-id",
        )
        second = mock.Mock(
            headers={"content-type": "application/json"},
            url="https://example.test/api/target-id?retry=1",
        )
        first.body.return_value = _payload().encode("utf-8")
        conflicting = json.loads(_payload())
        conflicting["data"][0]["authorNickname"] = "其他站"
        second.body.return_value = json.dumps(conflicting, ensure_ascii=False).encode("utf-8")
        page.on.side_effect = lambda _event, callback: (callback(first), callback(second))
        context = mock.Mock()
        context.new_page.return_value = page
        renderer = browser_client.ReusableBrowserRenderer(lambda: object())
        renderer.local.state = browser_client.BrowserRenderState(object(), object(), context)

        with self.assertRaisesRegex(ScrapeError, "同ID记录内容冲突"):
            renderer.fetch_article(_config(), timeout=1)

    def test_playwright_article_rejects_same_id_section_conflict(self) -> None:
        page = mock.Mock()
        page.locator.return_value.inner_text.return_value = ""
        first = mock.Mock(
            headers={"content-type": "application/json"},
            url="https://example.test/api/target-id",
        )
        second = mock.Mock(
            headers={"content-type": "application/json"},
            url="https://example.test/api/target-id?retry=1",
        )
        first.body.return_value = _payload().encode("utf-8")
        second.body.return_value = _payload().encode("utf-8")
        page.on.side_effect = lambda _event, callback: (callback(first), callback(second))
        context = mock.Mock()
        context.new_page.return_value = page
        renderer = browser_client.ReusableBrowserRenderer(lambda: object())
        renderer.local.state = browser_client.BrowserRenderState(object(), object(), context)

        records = (
            ArticleRecord(
                "target-id",
                "first",
                "210期 三十六码",
                "测试站",
                "正文",
                "测试站\n210期 三十六码\n正文",
                ("栏目一",),
            ),
            ArticleRecord(
                "target-id",
                "second",
                "210期 三十六码",
                "测试站",
                "正文",
                "测试站\n210期 三十六码\n正文",
                ("栏目二",),
            ),
        )
        with mock.patch.object(
            browser_client,
            "article_record_from_payload",
            side_effect=records,
        ), self.assertRaisesRegex(ScrapeError, "同ID记录内容冲突"):
            renderer.fetch_article(_config(), timeout=1)

    def test_non_playwright_page_close_errors_still_propagate(self) -> None:
        page = mock.Mock()
        page.locator.return_value.inner_text.return_value = " ".join(
            f"{value:02d}" for value in range(1, 37)
        )
        page.content.return_value = page.locator.return_value.inner_text.return_value
        page.close.side_effect = RuntimeError("unexpected close failure")
        context = mock.Mock()
        context.new_page.return_value = page
        renderer = browser_client.ReusableBrowserRenderer(lambda: object())
        renderer.local.state = browser_client.BrowserRenderState(object(), object(), context)

        with self.assertRaisesRegex(RuntimeError, "unexpected close failure"):
            renderer.fetch("https://example.test/", timeout=1)

    def test_context_initialization_failure_closes_started_resources(self) -> None:
        events: list[str] = []

        class FakeBrowser:
            def new_context(self, **_kwargs):
                raise browser_client.PlaywrightError("context launch failed")

            def close(self) -> None:
                events.append("browser.close")

        class FakeChromium:
            def launch(self, **_kwargs) -> FakeBrowser:
                return FakeBrowser()

        class FakePlaywright:
            chromium = FakeChromium()

            def stop(self) -> None:
                events.append("playwright.stop")

        renderer = browser_client.ReusableBrowserRenderer(FakePlaywright)
        with self.assertRaises(browser_client.PlaywrightError):
            renderer.state()

        self.assertEqual(events, ["browser.close", "playwright.stop"])
        self.assertEqual(renderer.states, [])

    def test_parser_result_identity_must_match_config(self) -> None:
        config = _config()
        for field, value, message in (
            ("name", "其他站", "站名"),
            ("url", "https://example.test/article/manager/other-id?url=x", "URL"),
        ):
            with self.subTest(field=field):
                parsed = replace(
                    ParsedRecord(config.name, config.url, 210, tuple(NUMBERS)),
                    **{field: value},
                )
                service = ScrapeService(
                    text_fetcher=mock.Mock(return_value=_payload()),
                    parser=mock.Mock(return_value=parsed),
                )
                with self.assertRaisesRegex(ScrapeError, message):
                    service.scrape(config, fixed_issue=210)

    def test_duplicate_window_excludes_issues_outside_requested_range(self) -> None:
        config = SiteConfig(
            name="普通站",
            url="https://example.test/topic",
            source_type="generic_html",
            parser_id="generic_36",
            region="top",
        )
        records = tuple(
            ParsedRecord(config.name, config.url, issue, NUMBERS)
            for issue in (224, 234, 235, 236)
        )
        selected = duplicate_service.select_recent_records(records, 236, 3, 1)

        self.assertEqual([record.issue for record in selected], [236, 235, 234])
        with self.assertRaises(ScrapeError):
            duplicate_service.select_recent_records((records[0],), 236, 3, 1)

    def test_duplicate_window_latest_issue_keeps_direction_order(self) -> None:
        config = SiteConfig(
            name="普通站",
            url="https://example.test/topic",
            source_type="generic_html",
            parser_id="generic_36",
            region="top",
        )
        records = [
            ParsedRecord(config.name, config.url, issue, NUMBERS)
            for issue in (234, 236, 235)
        ]
        with mock.patch.object(
            duplicate_service,
            "collect_issue_records",
            return_value=(records, ()),
        ):
            window = duplicate_service.scrape_site_window(
                config,
                period=236,
                periods=2,
                min_records=1,
                timeout=1,
                text_fetcher=lambda _url, _timeout: "raw document",
            )

        # 236 is an anomalously large issue at the non-direction end; the
        # first collector result remains authoritative for this site's latest.
        self.assertEqual(window.latest_issue, 234)
        self.assertEqual([record.issue for record in window.records], [236, 235])


if __name__ == "__main__":
    unittest.main()
