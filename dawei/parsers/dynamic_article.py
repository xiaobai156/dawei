"""Pure parsing for structured dynamic and collection payloads."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace

from dawei.domain.errors import ScrapeError
from dawei.domain.models import ArticleRecord, SiteConfig
from dawei.domain.models import ParsedRecord as SiteResult
from dawei.parsers.common import ISSUE_RE, all_keywords_present, html_to_lines
from dawei.parsers.generic_36 import extract_generic_36

KUNNAN_LIST_URL_RE = re.compile(r"^(https?://.+?)/users/\d+/forums(?:\?\S*)?$")
KUNNAN_USER_RE = re.compile(r"/users/(\d+)/forums")


@dataclass(frozen=True)
class KunnanDetail:
    """完整正文：来自单篇详情接口，取代被截断的列表正文。"""

    url: str
    content: str


def kunnan_magazine_detail_url(api_url: str, record_id: str) -> str:
    """由列表接口地址推导单篇详情接口地址（去掉 /users/{id}，保留同一 API 前缀）。"""
    match = KUNNAN_LIST_URL_RE.match(api_url or "")
    if match is None:
        raise ScrapeError("困难杂志 API 地址无法推导详情地址")
    return f"{match.group(1)}/forums/{record_id}"


def parse_kunnan_detail(url: str, body: str) -> KunnanDetail:
    """把详情接口响应转成纯文本正文，不做任何期号或号码裁决。"""
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ScrapeError(f"困难杂志详情{url}不是有效JSON") from exc
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ScrapeError(f"困难杂志详情{url}缺少正文")
    lines = html_to_lines(content)
    if not lines:
        raise ScrapeError(f"困难杂志详情{url}正文为空")
    return KunnanDetail(url, "\n".join(lines))


def _payload_records(payload: str) -> tuple[list, bool]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ScrapeError("困难杂志 API response is not valid JSON") from exc
    wrapped_data = isinstance(data, dict) and "data" in data
    records = data.get("data") if wrapped_data else data
    if not isinstance(records, list):
        raise ScrapeError("困难杂志 API未返回记录列表")
    return records, wrapped_data


def _iter_kunnan_targets(records: list, config: SiteConfig) -> Iterator[tuple[int, dict]]:
    """按用户、状态、彩种、栏目筛选目标记录（与正式解析同一套条件）。"""
    user_match = KUNNAN_USER_RE.search(config.api_url or "")
    expected_user_id = user_match.group(1) if user_match else "3792"
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        if str(record.get("user_id", "")) != expected_user_id:
            continue
        if str(record.get("status", "published")) != "published":
            continue
        if str(record.get("lottery", "")) != "macao":
            continue
        topic = str(record.get("topic", "")).strip()
        if not all_keywords_present(topic, config.keywords):
            continue
        yield index, record


def kunnan_magazine_record_ids(payload: str, config: SiteConfig) -> tuple[str, ...]:
    """列出列表响应里需要补全正文的文章 ID，供抓取层逐篇取详情。"""
    records, _ = _payload_records(payload)
    return tuple(
        record_id
        for _, record in _iter_kunnan_targets(records, config)
        if (record_id := str(record.get("id", "")).strip())
    )


def _any_identity_keyword_present(text: str, keywords: tuple[str, ...]) -> bool:
    compact = re.sub(r"\s+", "", text)
    return any(re.sub(r"\s+", "", keyword) in compact for keyword in keywords if keyword)


def validate_article_identity(article: ArticleRecord, config: SiteConfig) -> None:
    """Validate business identity after source extraction, before candidate parsing."""
    if not article.author:
        raise ScrapeError("API作者缺失")
    if not article.title:
        raise ScrapeError("API标题缺失")
    if not ISSUE_RE.search(article.title) and not _any_identity_keyword_present(
        article.title,
        config.keywords,
    ):
        raise ScrapeError("API标题缺少期数或栏目关键词")
    section_context = "\n".join(
        part for part in (article.author, *article.section_names, article.title) if part
    )
    if not all_keywords_present(section_context, config.section_keywords):
        raise ScrapeError("API栏目关键词不匹配")
    if not all_keywords_present(
        f"{article.title}\n{article.body}",
        config.keywords,
    ):
        raise ScrapeError("API目标关键词不匹配")


def kunnan_magazine_records_from_payload(
    payload: str,
    config: SiteConfig,
    details: Mapping[str, KunnanDetail] | None = None,
) -> tuple[SiteResult, ...]:
    records, wrapped_data = _payload_records(payload)
    results: list[SiteResult] = []
    seen_ids: set[str] = set()
    seen_issues: set[int] = set()

    for index, record in _iter_kunnan_targets(records, config):
        record_id = str(record.get("id", "")).strip()
        if not record_id:
            raise ScrapeError(f"困难杂志第{index}条目标记录缺少文章ID")
        if record_id in seen_ids:
            raise ScrapeError(f"困难杂志存在重复文章ID: {record_id}")
        seen_ids.add(record_id)

        raw_issue = record.get("draw")
        if type(raw_issue) is int:
            issue = raw_issue
        elif (
            type(raw_issue) is str
            and raw_issue.isascii()
            and raw_issue.isdecimal()
        ):
            issue = int(raw_issue)
        else:
            issue = 0
        if issue <= 0:
            raise ScrapeError(f"困难杂志文章{record_id}缺少有效期号")
        if issue in seen_issues:
            raise ScrapeError(f"困难杂志{issue}期存在多个目标文章记录")

        detail = details.get(record_id) if details else None
        content = detail.content if detail is not None else record.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ScrapeError(f"困难杂志{issue}期文章正文缺失")
        record_config = replace(
            config,
            fixed_issue=issue,
            api_url=None,
            parser_id="generic_36",
        )
        result = extract_generic_36(content, record_config)
        if result.issue != issue or len(result.numbers) != 36:
            raise ScrapeError(f"困难杂志{issue}期文章数据未通过36码校验")
        root_path = "$.data" if wrapped_data else "$"
        source_path = (
            detail.url if detail is not None else f"{config.api_url}::{root_path}[{index}]"
        )
        if result.evidence is None:
            raise ScrapeError(f"困难杂志{issue}期文章缺少候选证据")
        origins = tuple(
            replace(
                origin,
                document_id=record_id,
                document_url=source_path,
                source_method="structured_api",
                block_id=f"{record_id}:api-record-{index}",
                block_start=index,
                block_end=index + 1,
                page_index=index,
                block_index=0,
            )
            for origin in result.evidence.origins
        )
        evidence = replace(
            result.evidence,
            document_id=record_id,
            document_url=source_path,
            source_method="structured_api",
            block_id=f"{record_id}:api-record-{index}",
            block_start=index,
            block_end=index + 1,
            page_index=index,
            block_index=0,
            origins=origins,
        )
        results.append(
            replace(
                result,
                record_id=record_id,
                record_path=source_path,
                raw_position=index,
                evidence=evidence,
            )
        )
        seen_issues.add(issue)

    if not results:
        raise ScrapeError("困难杂志 API没有找到符合用户、彩种、栏目和期号的记录")
    return tuple(results)
