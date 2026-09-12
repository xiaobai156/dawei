"""Offline tests for gzip/deflate requests and bad-compression identity fallback."""

from __future__ import annotations

import gzip
import unittest
import zlib
from typing import Self

from dawei.domain.errors import ScrapeError
from dawei.infrastructure import http_client


class _FakeHeaders(dict):
    def get_content_charset(self) -> str:
        return "utf-8"


class _FakeResponse:
    def __init__(self, body: bytes, encoding: str = "") -> None:
        self._body = body
        self.headers = _FakeHeaders()
        if encoding:
            self.headers["Content-Encoding"] = encoding

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _raw_deflate(payload: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(payload) + compressor.flush()


class DecodeResponseBytesTests(unittest.TestCase):
    def test_gzip(self) -> None:
        self.assertEqual(
            http_client.decode_response_bytes(gzip.compress(b"hello"), "gzip"),
            b"hello",
        )

    def test_gzip_magic_without_header(self) -> None:
        self.assertEqual(
            http_client.decode_response_bytes(gzip.compress(b"hello"), "identity"),
            b"hello",
        )

    def test_deflate_zlib_wrapper(self) -> None:
        self.assertEqual(
            http_client.decode_response_bytes(zlib.compress(b"hello"), "deflate"),
            b"hello",
        )

    def test_deflate_raw(self) -> None:
        self.assertEqual(
            http_client.decode_response_bytes(_raw_deflate(b"hello"), "deflate"),
            b"hello",
        )

    def test_identity_and_unknown_encoding_passthrough(self) -> None:
        self.assertEqual(http_client.decode_response_bytes(b"plain", "identity"), b"plain")
        self.assertEqual(http_client.decode_response_bytes(b"plain", ""), b"plain")
        self.assertEqual(http_client.decode_response_bytes(b"raw-br", "br"), b"raw-br")

    def test_bad_gzip_raises(self) -> None:
        with self.assertRaises(http_client.DECOMPRESS_ERRORS):
            http_client.decode_response_bytes(b"\x1f\x8bBAD", "gzip")


class FetchTextCompressionTests(unittest.TestCase):
    def test_valid_gzip_uses_single_request(self) -> None:
        calls: list[dict[str, str] | None] = []

        def raw(url, timeout, extra_headers=None):
            calls.append(extra_headers)
            return gzip.compress("中文正文".encode()), "utf-8", "gzip"

        text = http_client.fetch_text(
            "https://example.test",
            raw_fetcher=raw,
            node_fetcher=lambda *a, **k: "",
        )
        self.assertEqual(text, "中文正文")
        self.assertEqual(len(calls), 1)

    def test_bad_gzip_retries_identity_once(self) -> None:
        calls: list[dict[str, str]] = []

        def raw(url, timeout, extra_headers=None):
            calls.append(dict(extra_headers or {}))
            if len(calls) == 1:
                return b"\x1f\x8bBAD", "utf-8", "gzip"
            return "恢复正文".encode(), "utf-8", "identity"

        text = http_client.fetch_text(
            "https://example.test",
            raw_fetcher=raw,
            node_fetcher=lambda *a, **k: "",
        )
        self.assertEqual(text, "恢复正文")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].get("Accept-Encoding"), "identity")

    def test_both_requests_bad_compression_raises(self) -> None:
        def raw(url, timeout, extra_headers=None):
            return b"\x1f\x8bBAD", "utf-8", "gzip"

        with self.assertRaises(ScrapeError):
            http_client.fetch_text(
                "https://example.test",
                raw_fetcher=raw,
                node_fetcher=lambda *a, **k: "",
            )

    def test_fetch_bytes_retries_identity_once(self) -> None:
        calls: list[dict[str, str]] = []

        def raw(url, timeout, extra_headers=None):
            calls.append(dict(extra_headers or {}))
            if len(calls) == 1:
                return b"\x1f\x8bBAD", "utf-8", "gzip"
            return b"bytes-ok", "utf-8", "identity"

        original = http_client.fetch_raw
        http_client.fetch_raw = raw
        try:
            self.assertEqual(http_client.fetch_bytes("https://example.test"), b"bytes-ok")
        finally:
            http_client.fetch_raw = original
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].get("Accept-Encoding"), "identity")


class FetchRawRequestHeaderTests(unittest.TestCase):
    def test_requests_gzip_and_deflate(self) -> None:
        captured: dict[str, str] = {}

        def open_url(request, timeout):
            captured.update({key.lower(): value for key, value in request.headers.items()})
            return _FakeResponse(b"ok")

        data, _, _ = http_client.fetch_raw(
            "https://example.test",
            open_url=open_url,
        )
        self.assertEqual(data, b"ok")
        self.assertEqual(captured.get("accept-encoding"), "gzip, deflate")


if __name__ == "__main__":
    unittest.main()
