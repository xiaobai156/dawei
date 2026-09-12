"""Audit + regression pins for the two remaining optimization items.

Item 3 (stdlib connection reuse): evaluated and NOT enabled.  Every site is
fetched at most once per issue (plus one API call for dynamic sites), so
same-origin repeat requests are rare; Python's urllib has no connection pool,
and adding one would have to alter the TLS retry contexts, proxy opener,
gzip/deflate, curl and Node fallback branches.  The tests below pin the current
default so a future opt-in cannot silently change it.

Item 4 (networkidle shortening): NOT enabled.  There is no config field for a
"complete load signal" and no user-named site evidence, so the existing
``domcontentloaded`` navigation plus the 5s ``networkidle`` wait is preserved
and pinned here.
"""

from __future__ import annotations

import unittest
from unittest import mock

from dawei.infrastructure import http_client
from dawei.infrastructure.browser_client import (
    BrowserRenderState,
    ReusableBrowserRenderer,
)


class _Headers(dict):
    def get_content_charset(self):
        return "utf-8"


class _Response:
    def __init__(self, body: bytes = b"ok") -> None:
        self._body = body
        self.headers = _Headers()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ConnectionReuseAuditTests(unittest.TestCase):
    def test_fetch_raw_opens_one_connection_per_request_by_default(self) -> None:
        seen: list[str] = []

        def open_url(request, timeout):
            seen.append(request.full_url)
            return _Response()

        http_client.fetch_raw("https://one.test/a", open_url=open_url)
        http_client.fetch_raw("https://two.test/b", open_url=open_url)
        self.assertEqual(seen, ["https://one.test/a", "https://two.test/b"])

    def test_module_exposes_no_connection_pool(self) -> None:
        self.assertFalse(hasattr(http_client, "ConnectionPool"))
        self.assertFalse(hasattr(http_client, "_CONNECTION_POOL"))

    def test_proxy_branch_still_uses_installed_opener(self) -> None:
        with (
            mock.patch.object(http_client, "build_opener") as build,
            mock.patch.object(http_client, "install_opener") as install,
        ):
            http_client.configure_proxy("http://proxy.test:8080")
            build.assert_called_once()
            install.assert_called_once()
        http_client.configure_proxy(None)


class NetworkIdleWaitRegressionTests(unittest.TestCase):
    def test_goto_and_networkidle_wait_are_unchanged(self) -> None:
        page = mock.Mock()
        page.locator.return_value.inner_text.return_value = "正文"
        page.content.return_value = "<html>正文</html>"
        context = mock.Mock()
        context.new_page.return_value = page
        renderer = ReusableBrowserRenderer(lambda: object())
        renderer.local.state = BrowserRenderState(object(), object(), context)

        renderer.fetch("https://example.test", timeout=2)

        self.assertEqual(page.goto.call_args.kwargs["wait_until"], "domcontentloaded")
        page.wait_for_load_state.assert_called_once_with("networkidle", timeout=5000)


if __name__ == "__main__":
    unittest.main()
