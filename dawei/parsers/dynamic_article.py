"""Pure parsing for structured dynamic and collection payloads."""

from __future__ import annotations

import json
import re
from dataclasses import replace

from dawei.domain.errors import ScrapeError
from dawei.domain.models import ArticleRecord, SiteConfig
from dawei.domain.models import ParsedRecord as SiteResult
from dawei.parsers.common import ISSUE_RE, all_keywords_present
from dawei.parsers.generic_36 import extract_generic_36


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
) -> tuple[SiteResult, ...]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ScrapeError("困难杂志 API response is not valid JSON") from exc

    wrapped_data = isinstance(data, dict) and "data" in data
    records = data.get("data") if wrapped_data else data
    if not isinstance(records, list):
        raise ScrapeError("困难杂志 API未返回记录列表")
    user_match = re.search(r"/users/(\d+)/forums", config.api_url or "")
    expected_user_id = user_match.group(1) if user_match else "3792"
    results: list[SiteResult] = []
    seen_ids: set[str] = set()
    seen_issues: set[int] = set()

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

        content = record.get("content")
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
        source_path = f"{config.api_url}::{root_path}[{index}]"
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
