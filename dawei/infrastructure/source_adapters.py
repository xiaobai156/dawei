"""Raw source adapters and strict dynamic-record boundary adapters."""

from __future__ import annotations

import ast
import base64
import binascii
import json
import re
import zlib
from collections.abc import Callable, Iterable
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from dawei.domain.errors import ScrapeError
from dawei.domain.models import ArticleRecord, RawAnchor, RawDocument, SiteConfig

ARTICLE_ID_FIELDS = ("id", "_id", "articleId", "article_id", "recordId", "record_id")
ARTICLE_BODY_FIELDS = ("content", "html", "body", "articleContent", "article_content")
ARTICLE_NESTED_FIELDS = ("data", "article", "record", "attributes")
MAX_PAGINATED_LIST_PAGES = 50
# 该 CMS 把整页正文压进配套脚本包。资源域名会轮换（已实测 api.kxusu.com →
# api.ladrvh.com），因此按"路径形态 + 与页面资源同源"识别，不绑定具体域名。
SCRIPT_TAG_SRC_RE = re.compile(r'<script[^>]+src="([^"]+)"', re.IGNORECASE)
SCRIPT_BUNDLE_PATH_RE = re.compile(r"^/js-\d+-\d+$")
SCRIPT_ASSET_PATH_MARKER = "/assets/js/"
SCRIPT_BUNDLE_PAYLOAD_RE = re.compile(r"var __jGr = '(.*)';", re.DOTALL)
SCRIPT_BUNDLE_SEGMENT_FIELD = "_v54gOM"
BAICAITONG_WRITELN_RE = re.compile(
    r"document\.writeln\((\"(?:\\.|[^\"\\])*\")\)\s*;",
    re.IGNORECASE,
)


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._href: str | None = None
        self._text: list[str] = []
        self.links: list[RawAnchor] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a" or self._href is not None:
            return
        attributes = dict(attrs)
        href = attributes.get("href")
        if href:
            self._href = href.strip()
            self._text = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        self.links.append(RawAnchor(" ".join("".join(self._text).split()), self._href))
        self._href = None
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)


def raw_anchor_links(html: str, page_url: str) -> tuple[RawAnchor, ...]:
    collector = _AnchorCollector()
    collector.feed(html)
    return tuple(
        RawAnchor(link.text, urljoin(page_url, link.href))
        for link in collector.links
        if link.href
    )


def fetch_paginated_list_documents(
    url: str,
    timeout: int,
    fetcher: Callable[[str, int], str],
) -> tuple[RawDocument, ...]:
    start = urlsplit(url)
    if not start.scheme or not start.netloc:
        raise ScrapeError(f"分页文章列表URL无效: {url}")
    documents: list[RawDocument] = []
    visited: set[str] = set()
    current = url
    for _ in range(MAX_PAGINATED_LIST_PAGES):
        if current in visited:
            raise ScrapeError(f"分页链接循环: {current}")
        current_parts = urlsplit(current)
        if current_parts.scheme.lower() != start.scheme.lower() or current_parts.netloc.lower() != start.netloc.lower():
            raise ScrapeError(f"分页链接跨域: {current}")
        visited.add(current)
        html = fetcher(current, timeout)
        links = raw_anchor_links(html, current)
        documents.append(RawDocument(current, html, links))
        next_links = [
            link
            for link in links
            if re.sub(r"\s+", "", link.text) == "下一页"
            and urlsplit(link.href).fragment == ""
        ]
        distinct_next = list(dict.fromkeys(link.href for link in next_links))
        if len(distinct_next) > 1:
            raise ScrapeError(f"同一列表页存在多个下一页链接: {current}")
        if not distinct_next:
            return tuple(documents)
        if distinct_next[0] == current:
            return tuple(documents)
        current = distinct_next[0]
    raise ScrapeError(f"分页文章列表超过{MAX_PAGINATED_LIST_PAGES}页，拒绝截断")


def extract_script_bundle_url(shell: str) -> str:
    """从 JS 壳页面取出唯一的脚本包地址。

    该 CMS 会轮换资源域名，所以不写死域名：脚本包按路径形态
    ``/js-<站点号>-<文章号>`` 识别，并要求全页恰好一个候选、必须是 https、
    且与页面其它 ``/assets/js/`` 脚本同源。任一条不满足即失败。
    """
    candidates: list[str] = []
    asset_hosts: set[str] = set()
    for raw in SCRIPT_TAG_SRC_RE.findall(shell):
        url = raw.strip()
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            continue
        if SCRIPT_ASSET_PATH_MARKER in parts.path:
            asset_hosts.add(parts.netloc.casefold())
        if SCRIPT_BUNDLE_PATH_RE.match(parts.path.rstrip("/")):
            candidates.append(url)
    unique = list(dict.fromkeys(candidates))
    if len(unique) != 1:
        raise ScrapeError(f"脚本包地址不唯一或缺失: {len(unique)} 个")
    target = urlsplit(unique[0])
    if target.scheme.casefold() != "https":
        raise ScrapeError(f"脚本包地址必须是 https: {unique[0]}")
    if asset_hosts and target.netloc.casefold() not in asset_hosts:
        raise ScrapeError(f"脚本包地址与页面资源域不同源: {target.netloc}")
    return unique[0]


def decode_script_bundle(bundle: str) -> str:
    """解包：__jGr 载荷 → _v54gOM 各段 base64 + raw deflate → HTML 片段。

    任何一段解不开都直接失败，绝不静默丢段——残缺正文会导致选错期或漏号。
    """
    payload_match = SCRIPT_BUNDLE_PAYLOAD_RE.search(bundle)
    if payload_match is None:
        raise ScrapeError("脚本包缺少 __jGr 载荷")
    try:
        payload = json.loads(payload_match.group(1))
    except json.JSONDecodeError as exc:
        raise ScrapeError(f"脚本包 __jGr 载荷不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ScrapeError("脚本包 __jGr 载荷不是对象")
    segments = payload.get(SCRIPT_BUNDLE_SEGMENT_FIELD)
    if not isinstance(segments, list) or not segments:
        raise ScrapeError(f"脚本包缺少 {SCRIPT_BUNDLE_SEGMENT_FIELD} 数据段")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        if not isinstance(segment, str):
            raise ScrapeError(f"脚本包第{index}段不是字符串")
        candidate = re.sub(r"\s+", "", segment).replace("-", "+").replace("_", "/")
        candidate += "=" * ((4 - len(candidate) % 4) % 4)
        try:
            raw = base64.b64decode(candidate)
            parts.append(zlib.decompress(raw, -15).decode("utf-8", "replace"))
        except (binascii.Error, ValueError, zlib.error) as exc:
            raise ScrapeError(f"脚本包第{index}段解码失败: {exc}") from exc
    return "".join(parts)


def fetch_script_bundle_document(
    config: SiteConfig,
    timeout: int,
    fetcher: Callable[[str, int], str],
) -> str:
    """两步取数：页面壳 → 脚本包 → 解包后的 HTML（解析器只在这份文档上工作）。"""
    shell = fetcher(config.url, timeout)
    bundle_url = extract_script_bundle_url(shell)
    bundle = fetcher(bundle_url, timeout)
    document = decode_script_bundle(bundle)
    if not document.strip():
        raise ScrapeError(f"{config.name} 脚本包解出空文档")
    return document


def decode_baicaitong_script(script: str) -> str:
    """Decode only literal document.writeln calls; never execute page JavaScript."""
    matches = list(BAICAITONG_WRITELN_RE.finditer(script))
    if not matches:
        raise ScrapeError("澳门百彩通脚本没有静态document.writeln数据")
    cursor = 0
    parts: list[str] = []
    for match in matches:
        gap = re.sub(r"<!--.*?-->", "", script[cursor:match.start()], flags=re.DOTALL)
        if gap.lstrip("\ufeff").strip():
            raise ScrapeError("澳门百彩通脚本含未解析的动态代码")
        try:
            parts.append(ast.literal_eval(match.group(1)))
        except (SyntaxError, ValueError) as exc:
            raise ScrapeError("澳门百彩通脚本字符串解码失败") from exc
        cursor = match.end()
    if script[cursor:].strip():
        raise ScrapeError("澳门百彩通脚本含未解析的尾部代码")
    document = "".join(parts)
    if "36码" not in document or not re.search(r"第\s*\d+\s*期", document):
        raise ScrapeError("澳门百彩通脚本缺少36码期数数据")
    return document


def fetch_baicaitong_document(
    config: SiteConfig,
    timeout: int,
    fetcher: Callable[[str, int], str],
) -> tuple[str, str]:
    """Read the observed same-site data script, bypassing the ad-heavy shell."""
    page = urlsplit(config.url)
    if page.scheme.casefold() not in {"http", "https"} or not page.netloc:
        raise ScrapeError("澳门百彩通配置URL无效")
    script_url = urljoin(config.url, "/amsslm.aspx?&ContentType=js?v=0")
    return decode_baicaitong_script(fetcher(script_url, timeout)), script_url


def decode_possible_base64_text(value: str) -> str | None:
    text = value.strip()
    if not text or not re.fullmatch(r"[A-Za-z0-9+/=_-]+", text):
        return None
    try:
        data = base64.b64decode(
            text.replace("-", "+").replace("_", "/") + "=" * ((4 - len(text) % 4) % 4)
        )
    except ValueError:
        return None
    decoded = data.decode("utf-8", errors="replace")
    if "�" in decoded or not re.search(r"[\u4e00-\u9fff]|<[^>]+>", decoded):
        return None
    return decoded


def article_detail_id(config: SiteConfig) -> str:
    match = re.search(
        r"/article/(?:admin|manager|lottery)/([^/?#]+)",
        config.url,
        re.IGNORECASE,
    )
    if not match:
        raise ScrapeError(f"动态详情页URL没有唯一记录ID: {config.url}")
    record_id = match.group(1)
    if config.record_id and config.record_id != record_id:
        raise ScrapeError(
            f"配置记录ID与URL不一致: 配置{config.record_id}，URL{record_id}"
        )
    return record_id


def iter_json_dicts(value: object, path: str = "$") -> Iterable[tuple[str, dict[str, object]]]:
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from iter_json_dicts(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from iter_json_dicts(child, f"{path}[{index}]")


def payload_contains_record_id(payload: str, record_id: str) -> bool:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return False
    return any(
        str(node.get(field, "")) == record_id
        for _, node in iter_json_dicts(data)
        for field in ARTICLE_ID_FIELDS
    )


def nested_record_value(record: dict[str, object], fields: tuple[str, ...]) -> object:
    for field in fields:
        value = record.get(field)
        if value not in (None, ""):
            return value
    for field in ARTICLE_NESTED_FIELDS:
        child = record.get(field)
        if isinstance(child, dict):
            value = nested_record_value(child, fields)
            if value not in (None, ""):
                return value
    return None


def article_body(record: dict[str, object], path: str = "$") -> tuple[str, str]:
    for field in ARTICLE_BODY_FIELDS:
        value = record.get(field)
        if value not in (None, ""):
            text = str(value).strip()
            if text:
                return decode_possible_base64_text(text) or text, f"{path}.{field}"
    for field in ARTICLE_NESTED_FIELDS:
        child = record.get(field)
        if isinstance(child, dict):
            body, body_path = article_body(child, f"{path}.{field}")
            if body:
                return body, body_path
    return "", ""


def article_section_names(record: dict[str, object]) -> tuple[str, ...]:
    sections = record.get("formSections") or record.get("sections")
    if isinstance(sections, list):
        return tuple(
            str(item.get("name")).strip()
            for item in sections
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        )
    if isinstance(sections, str) and sections.strip():
        return (sections.strip(),)
    return ()


def article_record_from_mapping(
    record: dict[str, object],
    record_path: str,
) -> ArticleRecord:
    record_id = str(nested_record_value(record, ARTICLE_ID_FIELDS) or "").strip()
    author = str(nested_record_value(record, ("authorNickname", "author")) or "").strip()
    raw_title = nested_record_value(record, ("title", "subject", "name"))
    title = decode_possible_base64_text(str(raw_title)) if raw_title else None
    title = title or str(raw_title or "").strip()
    body, _ = article_body(record, record_path)
    if not body:
        raise ScrapeError("API目标记录正文缺失")
    sections = article_section_names(record)
    document_parts = [part for part in (author, *sections, title, body) if part]
    document = "\n".join(document_parts)
    return ArticleRecord(record_id, record_path, title, author, body, document, sections)


def article_record_from_payload(
    payload: str,
    config: SiteConfig,
    source_path: str = "",
) -> ArticleRecord:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ScrapeError("API response is not valid JSON") from exc
    expected_id = article_detail_id(config)
    matches = [
        (path, record)
        for path, record in iter_json_dicts(data)
        if any(str(record.get(field, "")) == expected_id for field in ARTICLE_ID_FIELDS)
    ]
    if not matches:
        available_ids = {
            str(record.get(field, "")).strip()
            for _, record in iter_json_dicts(data)
            for field in ARTICLE_ID_FIELDS
            if str(record.get(field, "")).strip()
        }
        if not available_ids:
            raise ScrapeError("API空壳: 未返回任何带记录ID的文章")
        raise ScrapeError(f"API ID不匹配: 未找到URL记录ID: {expected_id}")
    if len(matches) > 1:
        raise ScrapeError(f"API存在多个同ID目标记录: {expected_id}")
    path, record = matches[0]
    result = article_record_from_mapping(record, path)
    if result.record_id != expected_id:
        raise ScrapeError(f"API记录边界ID不一致: 预期{expected_id}，实际{result.record_id}")
    if source_path:
        return ArticleRecord(
            result.record_id,
            f"{source_path}::{path}",
            result.title,
            result.author,
            result.body,
            result.document,
            result.section_names,
        )
    return result


def api_payload_to_html(payload: str, allow_multiple: bool = True) -> str:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ScrapeError("API response is not valid JSON") from exc
    records = data.get("data") if isinstance(data, dict) and "data" in data else data
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        raise ScrapeError("API response did not contain a list of records")
    if not allow_multiple and len(records) > 1:
        raise ScrapeError("聚合接口包含多篇记录，但URL没有唯一记录ID")
    parts: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        content = record.get("content") or record.get("html")
        if content:
            text = str(content)
            parts.append(decode_possible_base64_text(text) or text)
    if not parts:
        raise ScrapeError("API response did not contain record content")
    return "\n".join(parts)
