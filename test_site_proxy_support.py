"""站点级代理（proxy）支持的自测。

覆盖：配置解析与默认值、指纹向后兼容、代理格式校验、配置加载后按主机名登记、
HTTP 抓取与 curl 兜底都走站点代理。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dawei.domain.errors import ConfigurationError, ValidationError
from dawei.domain.models import SiteConfig
from dawei.domain.validation import validate_site_config
from dawei.infrastructure import http_client
from dawei.infrastructure.config_repository import (
    ConfigRepository,
    config_fingerprint,
    migrate_site_mapping,
)

PROXY = "http://127.0.0.1:7897"
GRAB_URL = "https://mm.yc7877.com:1888/"


class _FakeResponse:
    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeOpener:
    def __init__(self) -> None:
        self.opened: list[tuple[str, object]] = []

    def open(self, request: object, timeout: int = 0) -> _FakeResponse:
        self.opened.append((getattr(request, "full_url", ""), timeout))
        return _FakeResponse()


def _site(**overrides: object) -> SiteConfig:
    values: dict[str, object] = {
        "name": "抓抓码",
        "url": GRAB_URL,
        "site_id": "site_grab",
        "region": "top",
    }
    values.update(overrides)
    return SiteConfig(**values)  # type: ignore[arg-type]


class ProxyConfigTests(unittest.TestCase):
    def test_proxy_defaults_to_none(self) -> None:
        self.assertIsNone(migrate_site_mapping({"name": "站", "url": GRAB_URL}, 1).proxy)

    def test_explicit_proxy_is_kept(self) -> None:
        site = migrate_site_mapping({"name": "站", "url": GRAB_URL, "proxy": PROXY}, 1)
        self.assertEqual(site.proxy, PROXY)

    def test_rejects_non_string_proxy(self) -> None:
        with self.assertRaises(ConfigurationError):
            migrate_site_mapping({"name": "站", "url": GRAB_URL, "proxy": 7897}, 1)

    def test_default_proxy_does_not_change_fingerprint(self) -> None:
        self.assertEqual(
            config_fingerprint([_site()]),
            config_fingerprint([_site(proxy=None)]),
        )

    def test_proxy_participates_in_fingerprint(self) -> None:
        self.assertNotEqual(
            config_fingerprint([_site()]),
            config_fingerprint([_site(proxy=PROXY)]),
        )


class ProxyValidationTests(unittest.TestCase):
    def test_accepts_http_proxy(self) -> None:
        validate_site_config(_site(proxy=PROXY))

    def test_rejects_proxy_without_scheme(self) -> None:
        with self.assertRaises(ValidationError):
            validate_site_config(_site(proxy="127.0.0.1:7897"))

    def test_rejects_proxy_without_port(self) -> None:
        with self.assertRaises(ValidationError):
            validate_site_config(_site(proxy="http://127.0.0.1"))

    def test_rejects_unsupported_scheme(self) -> None:
        with self.assertRaises(ValidationError):
            validate_site_config(_site(proxy="socks5://127.0.0.1:7897"))


class ProxyRegistryTests(unittest.TestCase):
    def tearDown(self) -> None:
        http_client.configure_site_proxies({})

    def test_registry_matches_host_case_insensitively(self) -> None:
        http_client.configure_site_proxies({"MM.YC7877.COM": PROXY})
        self.assertEqual(http_client.site_proxy_for(GRAB_URL), PROXY)

    def test_registry_ignores_empty_values(self) -> None:
        http_client.configure_site_proxies({"mm.yc7877.com": "  "})
        self.assertIsNone(http_client.site_proxy_for(GRAB_URL))

    def test_unknown_host_has_no_proxy(self) -> None:
        http_client.configure_site_proxies({"mm.yc7877.com": PROXY})
        self.assertIsNone(http_client.site_proxy_for("https://other.test/a"))

    def test_repository_load_registers_proxy(self) -> None:
        payload = [
            {
                "name": "抓抓码",
                "url": GRAB_URL,
                "site_id": "site_grab",
                "region": "top",
                "render_policy": "never",
                "proxy": PROXY,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sites_36.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            ConfigRepository(path).load()
        self.assertEqual(http_client.site_proxy_for(GRAB_URL), PROXY)


class ProxyTransportTests(unittest.TestCase):
    def tearDown(self) -> None:
        http_client.configure_site_proxies({})

    def test_open_uses_site_proxy_opener(self) -> None:
        http_client.configure_site_proxies({"mm.yc7877.com": PROXY})
        opener = _FakeOpener()
        with mock.patch.object(http_client, "build_opener", return_value=opener) as build:
            with mock.patch.object(http_client, "urlopen") as direct:
                http_client.open_url_with_retries(
                    http_client.Request(GRAB_URL), timeout=5
                )
        self.assertEqual(len(build.call_args_list), 1)
        self.assertEqual(len(opener.opened), 1)
        direct.assert_not_called()

    def test_open_without_site_proxy_uses_default_urlopen(self) -> None:
        with mock.patch.object(http_client, "urlopen", return_value=_FakeResponse()) as direct:
            http_client.open_url_with_retries(
                http_client.Request("https://other.test/a"), timeout=5
            )
        self.assertEqual(len(direct.call_args_list), 1)

    def test_curl_fallback_passes_site_proxy(self) -> None:
        http_client.configure_site_proxies({"mm.yc7877.com": PROXY})
        completed = mock.Mock(returncode=0, stdout=b"<html></html>", stderr=b"")
        with mock.patch.object(http_client, "_curl_executable", return_value="curl"):
            with mock.patch.object(
                http_client.subprocess, "run", return_value=completed
            ) as run:
                http_client.fetch_raw_with_curl(GRAB_URL, timeout=5)
        command = run.call_args_list[0].args[0]
        self.assertIn("--proxy", command)
        self.assertIn(PROXY, command)


if __name__ == "__main__":
    unittest.main()
