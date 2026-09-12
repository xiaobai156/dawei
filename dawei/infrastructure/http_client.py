"""Strict HTTP, TLS, decoding, retry, and request de-duplication."""

from __future__ import annotations

import base64
import gzip
import json
import shutil
import socket
import ssl
import subprocess
import threading
import time
import warnings
import zlib
from collections.abc import Callable
from http.client import IncompleteRead
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener, install_opener, urlopen

from dawei.domain.errors import ScrapeError

DEFAULT_TIMEOUT = 20
DEFAULT_NETWORK_ATTEMPTS = 3
DEFAULT_PROXY_RETRIES = 1
RETRYABLE_HTTP_CODES = {502, 503, 504, 520, 521, 522, 523, 524}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _require_positive(value: int, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ScrapeError(f"{name} must be positive")


def decode_response_bytes(data: bytes, content_encoding: str) -> bytes:
    encoding = content_encoding.lower().strip()
    if encoding == "gzip" or data.startswith(b"\x1f\x8b"):
        return gzip.decompress(data)
    if encoding == "deflate":
        try:
            return zlib.decompress(data)
        except zlib.error:
            return zlib.decompress(data, -zlib.MAX_WBITS)
    return data


def sniff_charset(data: bytes) -> str | None:
    import re

    head = data[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9_-]+)", head, re.IGNORECASE)
    if not match:
        return None
    charset = match.group(1).lower()
    return "gb18030" if charset in {"gbk", "gb2312"} else charset


def decode_text(data: bytes, charset: str | None) -> str:
    candidates = [charset] if charset else []
    candidates.extend(["utf-8", "gb18030"])
    best_text = ""
    best_bad: int | None = None
    for candidate in dict.fromkeys(candidates):
        try:
            text = data.decode(candidate, errors="replace")
        except LookupError:
            continue
        bad = text.count("\ufffd")
        if best_bad is None or bad < best_bad:
            best_text = text
            best_bad = bad
        if bad == 0:
            break
    return best_text


def build_tls_retry_contexts() -> list[tuple[str, ssl.SSLContext]]:
    contexts: list[tuple[str, ssl.SSLContext]] = []
    tls12_context = ssl.create_default_context()
    if hasattr(ssl, "TLSVersion"):
        tls12_context.maximum_version = ssl.TLSVersion.TLSv1_2
    elif hasattr(ssl, "OP_NO_TLSv1_3"):
        tls12_context.options |= ssl.OP_NO_TLSv1_3
    try:
        tls12_context.set_ciphers("DEFAULT:@SECLEVEL=1")
    except ssl.SSLError:
        pass
    contexts.append(("tls12", tls12_context))

    legacy_context = ssl.create_default_context()
    if hasattr(ssl, "TLSVersion"):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            legacy_context.minimum_version = ssl.TLSVersion.TLSv1
        legacy_context.maximum_version = ssl.TLSVersion.TLSv1_2
    elif hasattr(ssl, "OP_NO_TLSv1_3"):
        legacy_context.options |= ssl.OP_NO_TLSv1_3
    if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
        legacy_context.options |= ssl.OP_LEGACY_SERVER_CONNECT
    try:
        legacy_context.set_ciphers("DEFAULT:@SECLEVEL=0")
    except ssl.SSLError:
        pass
    contexts.append(("legacy", legacy_context))
    return contexts


TLS_RETRY_CONTEXTS = build_tls_retry_contexts()
TLS_CONTEXT_CACHE_LOCK = threading.Lock()
TLS_CONTEXT_CACHE: dict[str, str] = {}


def is_tls_error(exc: BaseException) -> bool:
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, (ssl.SSLError, FileNotFoundError)):
        return True
    text = f"{reason} {exc}".lower()
    return any(
        token in text
        for token in (
            "ssl",
            "tls",
            "handshake",
            "alert",
            "schannel",
            "curl exit 35",
            "failed to receive handshake",
            "no such file or directory",
        )
    )


def open_url_with_retries(request: Request, timeout: int):
    _require_positive(timeout, "timeout")
    if urlsplit(request.full_url).scheme != "https":
        return urlopen(request, timeout=timeout)
    errors: list[str] = []
    attempts: list[tuple[str, ssl.SSLContext | None]] = [("default", None), *TLS_RETRY_CONTEXTS]
    host = urlsplit(request.full_url).hostname or ""
    if host:
        with TLS_CONTEXT_CACHE_LOCK:
            cached_label = TLS_CONTEXT_CACHE.get(host)
        if cached_label:
            attempts.sort(key=lambda item: item[0] != cached_label)
    for label, context in attempts:
        try:
            response = (
                urlopen(request, timeout=timeout)
                if context is None
                else urlopen(request, timeout=timeout, context=context)
            )
            if host:
                with TLS_CONTEXT_CACHE_LOCK:
                    TLS_CONTEXT_CACHE[host] = label
            return response
        except HTTPError:
            raise
        except (URLError, OSError) as exc:
            if not is_tls_error(exc):
                raise
            errors.append(f"{label}: {exc}")
    raise ScrapeError("network error: TLS handshake failed after retries: " + " | ".join(errors))


def configure_proxy(proxy: str | None) -> None:
    if proxy:
        install_opener(build_opener(ProxyHandler({"http": proxy, "https": proxy})))
    else:
        install_opener(build_opener())


def is_retryable_health_http(code: int) -> bool:
    return code >= 500


def is_retryable_http_code(code: int) -> bool:
    return code in RETRYABLE_HTTP_CODES


def should_try_curl_fallback(exc: BaseException) -> bool:
    text = str(exc).lower()
    if is_tls_error(exc) or "network" in text or "timeout" in text or "timed out" in text:
        return True
    return any(f"http {code}" in text for code in RETRYABLE_HTTP_CODES)


def is_benchmark_net_ip(ip: str) -> bool:
    return ip.startswith(("198.18.", "198.19."))


def resolution_diagnostic(url: str) -> str:
    host = urlsplit(url).hostname
    if not host:
        return ""
    try:
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, None) if item[4]})
    except OSError:
        return ""
    polluted = [ip for ip in addresses if is_benchmark_net_ip(ip)]
    return f"本机DNS解析到疑似代理/污染地址: {', '.join(polluted)}" if polluted else ""


def health_check_url(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    *,
    open_url: Callable[..., Any] | None = None,
    network_attempts: int = DEFAULT_NETWORK_ATTEMPTS,
    proxy_retries: int = DEFAULT_PROXY_RETRIES,
) -> None:
    _require_positive(timeout, "timeout")
    _require_positive(network_attempts, "network_attempts")
    _require_positive(proxy_retries, "proxy_retries")
    opener = open_url or open_url_with_retries
    attempts = network_attempts * proxy_retries
    last_error: ScrapeError | None = None
    for attempt in range(1, attempts + 1):
        request = Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"},
            method="HEAD",
        )
        try:
            with opener(request, timeout=min(timeout, 10)):
                return
        except HTTPError as exc:
            if not is_retryable_health_http(exc.code):
                return
            last_error = ScrapeError(f"health check failed: HTTP {exc.code}")
        except URLError as exc:
            last_error = ScrapeError(f"health check failed: network error: {exc.reason}")
        except TimeoutError:
            last_error = ScrapeError("health check failed: network timeout")
        except OSError as exc:
            last_error = ScrapeError(f"health check failed: network error: {exc}")
        if attempt < attempts:
            time.sleep(0.4 * attempt)
    if last_error:
        raise last_error


def _curl_executable() -> str:
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if not curl:
        raise ScrapeError("curl fallback unavailable")
    return curl


def _curl_variants() -> tuple[tuple[str, ...], ...]:
    return (("--ssl-no-revoke",), ())


def fetch_raw_with_curl(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    extra_headers: dict[str, str] | None = None,
) -> tuple[bytes, str, str]:
    _require_positive(timeout, "timeout")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
    }
    if extra_headers:
        headers.update(extra_headers)
    base = [
        _curl_executable(), "-L", "-f", "-sS", "--http1.1", "--connect-timeout",
        str(min(timeout, 10)), "--max-time", str(timeout),
    ]
    for key, value in headers.items():
        base.extend(["-H", f"{key}: {value}"])
    errors: list[str] = []
    for variant in _curl_variants():
        command = base[:1] + list(variant) + base[1:] + [url]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=timeout + 5,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ScrapeError("curl 连接失败: network timeout") from exc
        if completed.returncode == 0:
            return completed.stdout, "utf-8", ""
        detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace").strip()
        errors.append(f"curl exit {completed.returncode}: {detail or 'unknown error'}")
        if completed.returncode != 2:
            break
    raise ScrapeError("curl 连接失败: RuntimeError: " + " | ".join(errors))


def fetch_raw(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    extra_headers: dict[str, str] | None = None,
    *,
    open_url: Callable[..., Any] | None = None,
    curl_fetcher: Callable[..., tuple[bytes, str, str]] | None = None,
    network_attempts: int = DEFAULT_NETWORK_ATTEMPTS,
    proxy_retries: int = DEFAULT_PROXY_RETRIES,
) -> tuple[bytes, str, str]:
    _require_positive(timeout, "timeout")
    _require_positive(network_attempts, "network_attempts")
    _require_positive(proxy_retries, "proxy_retries")
    opener = open_url or open_url_with_retries
    curl = curl_fetcher or fetch_raw_with_curl
    attempts = network_attempts * proxy_retries
    last_error: ScrapeError | None = None
    for attempt in range(1, attempts + 1):
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "identity",
        }
        if extra_headers:
            headers.update(extra_headers)
        try:
            with opener(Request(url, headers=headers), timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                content_encoding = response.headers.get("Content-Encoding", "")
                try:
                    data = response.read()
                except IncompleteRead as exc:
                    raise ScrapeError(f"network incomplete read: {len(exc.partial)} bytes read") from exc
                return data, charset, content_encoding
        except HTTPError as exc:
            last_error = ScrapeError(f"HTTP {exc.code}")
            if not is_retryable_http_code(exc.code):
                raise last_error from exc
        except IncompleteRead as exc:
            last_error = ScrapeError(f"network incomplete read: {len(exc.partial)} bytes read")
        except ScrapeError as exc:
            last_error = exc
        except URLError as exc:
            last_error = ScrapeError(f"network error: {exc.reason}")
        except TimeoutError:
            last_error = ScrapeError("network timeout")
        except OSError as exc:
            last_error = ScrapeError(f"network error: {exc}")
        if attempt < attempts:
            time.sleep(0.4 * attempt)
    if last_error is None:
        raise ScrapeError("network error")
    if should_try_curl_fallback(last_error):
        try:
            return curl(url, timeout=timeout, extra_headers=extra_headers)
        except ScrapeError as curl_error:
            diagnostic = resolution_diagnostic(url)
            suffix = f"; {diagnostic}" if diagnostic else ""
            raise ScrapeError(f"{last_error}; curl fallback failed: {curl_error}{suffix}") from curl_error
    raise last_error


def fetch_bytes(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    data, _, encoding = fetch_raw(url, timeout=timeout, extra_headers=extra_headers)
    try:
        return decode_response_bytes(data, encoding)
    except (OSError, EOFError, zlib.error) as exc:
        raise ScrapeError("failed to decode binary response") from exc


def post_json(url: str, payload: dict[str, object], timeout: int = DEFAULT_TIMEOUT) -> object:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with open_url_with_retries(request, timeout=timeout) as response:
            data = decode_response_bytes(response.read(), response.headers.get("Content-Encoding", ""))
            return json.loads(decode_text(data, response.headers.get_content_charset()))
    except (OSError, EOFError, ValueError, zlib.error) as exc:
        raise ScrapeError(f"JSON request failed: {exc}") from exc


def fetch_text_with_node(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    extra_headers: dict[str, str] | None = None,
) -> str:
    _require_positive(timeout, "timeout")
    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        raise ScrapeError("Node.js is required for this fetch fallback")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if extra_headers:
        headers.update(extra_headers)
    script = (
        "const url=process.argv[1];const headers=JSON.parse(process.argv[2]);"
        "const timeoutMs=Number(process.argv[3]);const controller=new AbortController();"
        "const timer=setTimeout(()=>controller.abort(),timeoutMs);"
        "fetch(url,{headers,signal:controller.signal}).then(async response=>{"
        "if(!response.ok)throw new Error('HTTP '+response.status);const text=await response.text();"
        "clearTimeout(timer);process.stdout.write(Buffer.from(text,'utf8').toString('base64'));})"
        ".catch(error=>{clearTimeout(timer);console.error(error.name+': '+error.message);process.exit(1);});"
    )
    completed = subprocess.run(
        [node, "-e", script, url, json.dumps(headers, ensure_ascii=False), str(timeout * 1000)],
        capture_output=True,
        text=True,
        timeout=timeout + 5,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise ScrapeError(f"node fetch failed: {detail}")
    try:
        return base64.b64decode(completed.stdout.strip()).decode("utf-8", errors="replace")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ScrapeError("node fetch returned invalid data") from exc


def fetch_text(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    extra_headers: dict[str, str] | None = None,
    *,
    raw_fetcher: Callable[..., tuple[bytes, str, str]] | None = None,
    node_fetcher: Callable[..., str] | None = None,
) -> str:
    _require_positive(timeout, "timeout")
    raw = raw_fetcher or fetch_raw
    node = node_fetcher or fetch_text_with_node
    data, charset, content_encoding = raw(url, timeout, extra_headers=extra_headers)
    try:
        data = decode_response_bytes(data, content_encoding)
    except (gzip.BadGzipFile, zlib.error) as exc:
        raise ScrapeError("failed to decode compressed response") from exc
    text = decode_text(data, sniff_charset(data) or charset)
    if text.strip() in {'"abcabc"', "abcabc"}:
        try:
            return node(url, timeout, extra_headers=extra_headers)
        except ScrapeError:
            return text
    return text


class TextFetchCache:
    def __init__(self, fetcher: Callable[[str, int], str] = fetch_text):
        self.fetcher = fetcher
        self.lock = threading.Lock()
        self.values: dict[tuple[str, int], str] = {}
        self.inflight: dict[tuple[str, int], threading.Event] = {}

    def fetch(self, url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
        key = (url, timeout)
        owner: threading.Event | None = None
        while True:
            with self.lock:
                if key in self.values:
                    return self.values[key]
                event = self.inflight.get(key)
                if event is None:
                    owner = threading.Event()
                    self.inflight[key] = owner
                    break
            event.wait()
        assert owner is not None
        try:
            value = self.fetcher(url, timeout)
        except Exception:
            with self.lock:
                self.inflight.pop(key, None)
                owner.set()
            raise
        with self.lock:
            stored = self.values.setdefault(key, value)
            self.inflight.pop(key, None)
            owner.set()
            return stored
