"""Pure resolver for article links exposed by a paginated list page."""

from __future__ import annotations

import re
from dataclasses import dataclass

from dawei.domain.errors import ScrapeError
from dawei.domain.models import RawDocument, SiteConfig
from dawei.parsers.common import ISSUE_RE


@dataclass(frozen=True)
class PaginatedArticleLink:
    issue: int
    title: str
    url: str
    list_url: str
    page_index: int


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _is_article_url(url: str) -> bool:
    return bool(re.search(r"/article\.aspx(?:[?#]|$)", url, re.IGNORECASE))


def _all_navigation_keywords_present(title: str, config: SiteConfig) -> bool:
    compact_title = _compact(title)
    return all(_compact(keyword) in compact_title for keyword in config.navigation_keywords if keyword)


def resolve_paginated_article(
    documents: tuple[RawDocument, ...],
    config: SiteConfig,
    fixed_issue: int | None = None,
) -> PaginatedArticleLink:
    if not documents:
        raise ScrapeError("分页文章列表为空")
    if not config.navigation_keywords:
        raise ScrapeError("分页文章列表缺少navigation_keywords")
    candidates: list[PaginatedArticleLink] = []
    for page_index, document in enumerate(documents):
        for link in document.links:
            if not _is_article_url(link.href) or not _all_navigation_keywords_present(link.text, config):
                continue
            match = ISSUE_RE.search(link.text)
            if match is None:
                continue
            candidates.append(
                PaginatedArticleLink(
                    issue=int(match.group(1)),
                    title=link.text,
                    url=link.href,
                    list_url=document.url,
                    page_index=page_index,
                )
            )
    if fixed_issue is not None:
        candidates = [candidate for candidate in candidates if candidate.issue == fixed_issue]
        if not candidates:
            raise ScrapeError(f"列表未找到指定{fixed_issue}期目标文章")
    elif candidates:
        latest = max(candidate.issue for candidate in candidates)
        candidates = [candidate for candidate in candidates if candidate.issue == latest]
    else:
        raise ScrapeError("列表未找到期数+目标关键词文章")

    unique = {(candidate.issue, candidate.url): candidate for candidate in candidates}
    candidates = list(unique.values())
    urls = {candidate.url for candidate in candidates}
    if len(urls) != 1:
        issue = candidates[0].issue
        raise ScrapeError(f"{issue}期同期目标文章冲突")
    return min(candidates, key=lambda candidate: candidate.page_index)
