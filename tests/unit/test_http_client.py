from __future__ import annotations

from http.client import IncompleteRead
import ssl
import unittest
from unittest import mock

from dawei.domain.errors import ScrapeError
from dawei.infrastructure import http_client


class Headers:
    def get_content_charset(self):
        return "utf-8"

    def get(self, name, default=""):
        return default


class Response:
    headers = Headers()

    def __init__(self, body: bytes | None = None, error: BaseException | None = None):
        self.body = body
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        if self.error:
            raise self.error
        return self.body or b""


class HttpClientTests(unittest.TestCase):
    def test_every_tls_retry_context_keeps_certificate_verification(self) -> None:
        contexts = http_client.build_tls_retry_contexts()
        self.assertTrue(contexts)
        for label, context in contexts:
            with self.subTest(label=label):
                self.assertNotEqual(context.verify_mode, ssl.CERT_NONE)
                self.assertTrue(context.check_hostname)

    def test_incomplete_response_is_rejected_without_using_partial_body(self) -> None:
        partial = IncompleteRead("210期 ".encode() + b"01 " * 36, 999)

        with self.assertRaisesRegex(ScrapeError, "incomplete read"):
            http_client.fetch_raw(
                "https://example.test",
                timeout=1,
                open_url=lambda request, timeout: Response(error=partial),
                curl_fetcher=lambda *args, **kwargs: (_ for _ in ()).throw(
                    ScrapeError("curl unavailable")
                ),
                network_attempts=1,
                proxy_retries=1,
            )

    def test_post_json_uses_injected_curl_fallback_after_network_error(self) -> None:
        captured: list[bytes] = []

        def fail_open(request, timeout):
            raise ScrapeError("network error: test")

        def curl(url, data, timeout=20):
            captured.append(data)
            return b'{"ok": true}'

        result = http_client.post_json(
            "https://example.test/api",
            {"type": "am"},
            timeout=1,
            open_url=fail_open,
            curl_poster=curl,
            network_attempts=1,
            proxy_retries=1,
        )

        self.assertEqual(result, {"ok": True})
        self.assertIn(b'"type": "am"', captured[0])

    def test_text_fetch_cache_collapses_same_request(self) -> None:
        calls: list[tuple[str, int]] = []

        def fetcher(url: str, timeout: int) -> str:
            calls.append((url, timeout))
            return "body"

        cache = http_client.TextFetchCache(fetcher)
        self.assertEqual(cache.fetch("https://example.test", 3), "body")
        self.assertEqual(cache.fetch("https://example.test", 3), "body")
        self.assertEqual(calls, [("https://example.test", 3)])

    def test_curl_fallback_never_disables_certificate_verification(self) -> None:
        completed = mock.Mock(returncode=0, stdout=b"body", stderr=b"")
        with (
            mock.patch.object(http_client, "_curl_executable", return_value="curl"),
            mock.patch.object(http_client.subprocess, "run", return_value=completed) as run,
        ):
            http_client.fetch_raw_with_curl("https://example.test", timeout=1)

        command = run.call_args.args[0]
        self.assertNotIn("-k", command)
        self.assertNotIn("--insecure", command)


if __name__ == "__main__":
    unittest.main()
