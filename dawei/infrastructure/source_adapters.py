"""Document expansion and strict dynamic-record boundary adapters."""

from __future__ import annotations

import ast
import base64
from collections.abc import Callable, Iterable
import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from dawei.domain.errors import ScrapeError
from dawei.domain.models import ArticleRecord, RawAnchor, RawDocument, SiteConfig


SCRIPT_URL_RE = re.compile(
    r"(?P<url>https?://[^\"'<>]+/upload/script/[^\"'<>]+\.js|/upload/script/[^\"'<>]+\.js)",
    re.IGNORECASE,
)
SCRIPT_SRC_RE = re.compile(r"<script\b[^>]*\bsrc=[\"'](?P<url>[^\"']+)[\"']", re.IGNORECASE)
IFRAME_SRC_RE = re.compile(r"<iframe\b[^>]*\bsrc=[\"'](?P<url>[^\"']+)[\"']", re.IGNORECASE)
BASE64_CHUNK_RE = re.compile(r"strdecode\(\"([^\"]+)\"\)")
PAGE_DATA_RE = re.compile(r"__PAGE_DATA__\s*=\s*['\"]([^'\"]+)['\"]")
DECRYPT_ARG_RE = re.compile(r"decrypt\([^,]+,\s*['\"]([^'\"]+)['\"]")
DOCUMENT_WRITELN_RE = re.compile(
    r"document\.writeln?\(\s*(?P<quote>[\"'])(?P<body>.*?)(?P=quote)\s*\)\s*;?"
)
ISSUE_RE = re.compile(r"(?<!\d)(\d{3})\s*期")
ARTICLE_ID_FIELDS = ("id", "_id", "articleId", "article_id", "recordId", "record_id")
ARTICLE_BODY_FIELDS = ("content", "html", "body", "articleContent", "article_content")
ARTICLE_NESTED_FIELDS = ("data", "article", "record", "attributes")
DOCUMENT_BOUNDARY = "DAWEI_DOCUMENT_BOUNDARY"
MAX_EXPANDED_DOCUMENTS = 16
MAX_PAGINATED_LIST_PAGES = 50


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


def decode_base64_chunks(text: str) -> str:
    chunks: list[str] = []
    encoded_chunks = BASE64_CHUNK_RE.findall(text) + PAGE_DATA_RE.findall(text) + DECRYPT_ARG_RE.findall(text)
    for encoded in encoded_chunks:
        try:
            data = base64.b64decode(encoded)
        except ValueError:
            continue
        for charset in ("utf-8", "gb18030"):
            try:
                chunks.append(data.decode(charset))
                break
            except UnicodeDecodeError:
                continue
    return "\n".join(chunks)


def decode_document_writeln_chunks(text: str) -> str:
    chunks: list[str] = []
    for match in DOCUMENT_WRITELN_RE.finditer(text):
        body = match.group("body")
        quote = match.group("quote")
        try:
            chunks.append(ast.literal_eval(quote + body + quote))
        except (SyntaxError, ValueError):
            chunks.append(body.replace(r"\/", "/").replace(r"\'", "'").replace(r'\"', '"'))
    return "\n".join(chunks)


def script_urls_from(html: str, page_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    page_host = urlsplit(page_url).netloc
    for match in SCRIPT_URL_RE.finditer(html):
        url = urljoin(page_url, match.group("url"))
        if url not in seen:
            seen.add(url)
            urls.append(url)
    for match in SCRIPT_SRC_RE.finditer(html):
        url = urljoin(page_url, match.group("url"))
        split_url = urlsplit(url)
        if split_url.netloc != page_host and "/upload/script/" not in split_url.path:
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def iframe_urls_from(html: str, page_url: str) -> list[str]:
    page_host = urlsplit(page_url).netloc.lower()
    urls: list[str] = []
    for match in IFRAME_SRC_RE.finditer(html):
        raw_url = match.group("url").strip()
        if "${" in raw_url or any(character in raw_url for character in "{}[]"):
            continue
        url = urljoin(page_url, raw_url)
        if urlsplit(url).netloc.lower() == page_host and url not in urls:
            urls.append(url)
    return urls


def expanded_document_parts(
    html: str,
    page_url: str,
    timeout: int,
    fetcher: Callable[[str, int], str],
) -> list[str]:
    parts = [html, decode_base64_chunks(html), decode_document_writeln_chunks(html)]
    for script_url in script_urls_from(html, page_url):
        try:
            script_text = fetcher(script_url, timeout)
        except (ScrapeError, UnicodeEncodeError, ValueError):
            continue
        parts.append(decode_base64_chunks(script_text))
        parts.append(decode_document_writeln_chunks(script_text))
    return [part for part in parts if part]


def fetch_expanded_html(
    url: str,
    timeout: int,
    fetcher: Callable[[str, int], str],
) -> str:
    queue = [url]
    seen: set[str] = set()
    documents: list[str] = []
    while queue:
        document_url = queue.pop(0)
        if document_url in seen:
            continue
        if len(seen) >= MAX_EXPANDED_DOCUMENTS:
            raise ScrapeError("页面包含过多iframe文档，拒绝截断解析")
        seen.add(document_url)
        html = fetcher(document_url, timeout)
        documents.append(
            "\n".join(expanded_document_parts(html, document_url, timeout, fetcher))
        )
        queue.extend(
            iframe_url
            for iframe_url in iframe_urls_from(html, document_url)
            if iframe_url not in seen
        )
    return f"\n{DOCUMENT_BOUNDARY}\n".join(documents)


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
            text = str(value)
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


def _all_keywords_present(text: str, keywords: Iterable[str]) -> bool:
    compact = re.sub(r"\s+", "", text)
    return all(re.sub(r"\s+", "", keyword) in compact for keyword in keywords if keyword)


def _any_keyword_present(text: str, keywords: Iterable[str]) -> bool:
    compact = re.sub(r"\s+", "", text)
    return any(re.sub(r"\s+", "", keyword) in compact for keyword in keywords if keyword)


def article_record_from_mapping(
    record: dict[str, object],
    record_path: str,
    config: SiteConfig,
) -> ArticleRecord:
    record_id = str(nested_record_value(record, ARTICLE_ID_FIELDS) or "").strip()
    author = str(nested_record_value(record, ("authorNickname", "author")) or "").strip()
    if author != config.name:
        raise ScrapeError(f"API作者不匹配: 预期{config.name}，实际{author or '空'}")
    raw_title = nested_record_value(record, ("title", "subject", "name"))
    title = decode_possible_base64_text(str(raw_title)) if raw_title else None
    title = title or str(raw_title or "").strip()
    if not title:
        raise ScrapeError("API标题缺失")
    if not ISSUE_RE.search(title) and not _any_keyword_present(title, config.keywords):
        raise ScrapeError("API标题缺少期数或栏目关键词")
    body, _ = article_body(record, record_path)
    if not body:
        raise ScrapeError("API目标记录正文缺失")
    sections = article_section_names(record)
    document = "\n".join((title, *sections, body))
    identity_text = "\n".join((title, body))
    if not _all_keywords_present(identity_text, config.section_keywords):
        raise ScrapeError("API栏目关键词不匹配")
    if not _all_keywords_present(identity_text, config.keywords):
        raise ScrapeError("API目标关键词不匹配")
    return ArticleRecord(record_id, record_path, title, author, body, document)


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
    result = article_record_from_mapping(record, path, config)
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
