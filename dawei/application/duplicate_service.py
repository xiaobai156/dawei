"""Pure issue alignment and duplicate-site comparison service."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from dawei.application.scrape_service import (
    load_browser_source,
    load_source,
    should_render_html_fallback,
)
from dawei.domain.errors import CacheError, ScrapeError
from dawei.domain.models import CandidateEvidence, ParsedRecord, SiteConfig
from dawei.infrastructure import browser_client, http_client
from dawei.infrastructure.cache_repository import CacheRepository
from dawei.parsers import DEFAULT_REGISTRY, dynamic_article, generic_36
from dawei.parsers.registry import resolve_parser_id

DEFAULT_DUPLICATE_COMMON = 6
TextFetcher = Callable[[str, int], str]


@dataclass(frozen=True)
class SiteWindow:
    name: str
    url: str
    period: int
    periods: int
    records: tuple[ParsedRecord, ...]
    latest_issue: int | None = None
    site_id: str = ""
    parser_id: str = ""
    # Keep the untrimmed parser candidates for onboarding's direction gate.
    # ``records`` remains the comparable recent window used by duplicate checks.
    direction_candidates: tuple[CandidateEvidence, ...] = ()

    @property
    def key(self) -> tuple[tuple[str, ...], ...]:
        return tuple(record.numbers for record in self.records)

    @property
    def by_issue(self) -> dict[int, ParsedRecord]:
        records: dict[int, ParsedRecord] = {}
        for record in self.records:
            existing = records.get(record.issue)
            if existing is not None and existing.numbers != record.numbers:
                raise ScrapeError(f"{self.name} {record.issue}期存在同期36码冲突")
            records.setdefault(record.issue, record)
        return records


@dataclass(frozen=True)
class BackupSnapshot:
    period: int
    periods: int
    sites: tuple[SiteWindow, ...]
    failures: tuple[str, ...] = ()
    incomplete: bool = False


@dataclass(frozen=True)
class DuplicateMatch:
    left: SiteWindow
    right: SiteWindow
    issues: tuple[int, ...]

    @property
    def consecutive_count(self) -> int:
        return len(self.issues)

    def status(self, duplicate_common: int = DEFAULT_DUPLICATE_COMMON) -> str:
        return "重复" if self.consecutive_count >= duplicate_common else "疑似重复"


def load_backup_snapshot(path: str | Path) -> BackupSnapshot:
    try:
        snapshot = CacheRepository(path).load()
    except CacheError as exc:
        raise ScrapeError(f"读取备份JSON失败: {exc}") from exc
    if snapshot.period is None:
        raise ScrapeError("备份JSON没有有效期数")
    windows: list[SiteWindow] = []
    for site in snapshot.sites:
        if not site.records:
            raise ScrapeError(f"备份JSON站点没有有效记录: {site.name}")
        records = tuple(
            ParsedRecord(
                record.name,
                record.url,
                record.issue,
                record.numbers,
                record_id=record.record_id,
                record_path=record.source_path,
                raw_position=record.raw_position,
            )
            for record in site.records
        )
        windows.append(
            SiteWindow(
                site.name,
                site.url,
                snapshot.period,
                snapshot.periods,
                records,
                latest_issue=max(record.issue for record in records),
                site_id=site.site_id,
                parser_id=site.records[0].parser_id,
            )
        )
    return BackupSnapshot(
        snapshot.period,
        snapshot.periods,
        tuple(windows),
        snapshot.failures,
        incomplete=snapshot.incomplete,
    )


def detect_latest_period(results: Iterable[SiteWindow]) -> int:
    issues = [
        result.latest_issue if result.latest_issue is not None else result.records[0].issue
        for result in results
        if result.latest_issue is not None or result.records
    ]
    if not issues:
        raise ScrapeError("无法自动识别最新期数：没有抓到任何可用记录")
    counts = Counter(issues)
    highest_count = max(counts.values())
    winners = tuple(sorted(issue for issue, count in counts.items() if count == highest_count))
    if len(winners) != 1:
        raise ScrapeError(
            "无法自动识别最新期数：最新期数并列（"
            + ",".join(str(issue) for issue in winners)
            + "）"
        )
    return winners[0]


def select_recent_records(
    records: Iterable[ParsedRecord],
    period: int,
    periods: int,
    min_records: int,
) -> tuple[ParsedRecord, ...]:
    if period == 9999:
        selected = list(records)
    else:
        lower_bound = period - periods + 1
        selected = [
            record
            for record in sorted(records, key=lambda item: item.issue, reverse=True)
            if lower_bound <= record.issue <= period
        ]
    if len(selected) < min_records:
        found = ",".join(str(record.issue) for record in selected) or "none"
        raise ScrapeError(
            f"only found {len(selected)}/{min_records} comparable issues near {period}: {found}"
        )
    return tuple(selected[:periods])


def with_period(result: SiteWindow, period: int, periods: int) -> SiteWindow:
    return SiteWindow(
        result.name,
        result.url,
        period,
        periods,
        select_recent_records(result.records, period, periods, 1),
        latest_issue=result.latest_issue,
        site_id=result.site_id,
        parser_id=result.parser_id,
        direction_candidates=result.direction_candidates,
    )


def site_results_from_candidates(
    candidates: Iterable[CandidateEvidence],
    config: SiteConfig,
) -> list[ParsedRecord]:
    candidate_list = generic_36.unique_candidates(list(candidates), config)
    numbers_by_issue: dict[int, set[tuple[str, ...]]] = {}
    for candidate in candidate_list:
        numbers_by_issue.setdefault(candidate.issue, set()).add(candidate.numbers)
    conflicting = sorted(issue for issue, values in numbers_by_issue.items() if len(values) > 1)
    if conflicting:
        issues = "、".join(f"{issue}期" for issue in conflicting)
        raise ScrapeError(f"{config.name} {issues}存在多个高可信候选36码冲突")

    selected: dict[int, CandidateEvidence] = {}
    for candidate in candidate_list:
        current = selected.get(candidate.issue)
        if current is None or generic_36.prefer_candidate_index(
            candidate.page_index,
            current.page_index,
            config,
        ):
            selected[candidate.issue] = candidate
    reverse = generic_36.candidate_region(config) == "bottom"
    return [
        ParsedRecord(
            config.name,
            config.url,
            issue,
            candidate.numbers,
            raw_position=candidate.page_index,
            evidence=candidate,
        )
        for issue, candidate in sorted(
            selected.items(),
            key=lambda item: item[1].page_index,
            reverse=reverse,
        )
    ]


def collect_issue_records(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[ParsedRecord], tuple[CandidateEvidence, ...]]:
    parser_id = resolve_parser_id(config)
    collector = DEFAULT_REGISTRY.candidate_collector(parser_id)
    candidates, _, _ = collector(text_or_html, config)
    raw_candidates = tuple(candidates)
    return site_results_from_candidates(raw_candidates, config), raw_candidates


def scrape_site_window(
    config: SiteConfig,
    period: int,
    periods: int,
    min_records: int,
    timeout: int,
    text_fetcher: TextFetcher = http_client.fetch_text,
) -> SiteWindow:
    if resolve_parser_id(config) == "kunnan_magazine":
        if not config.api_url:
            raise ScrapeError("困难杂志缺少专属 API 地址")
        all_records = dynamic_article.kunnan_magazine_records_from_payload(
            text_fetcher(config.api_url, timeout),
            config,
        )
        records = select_recent_records(
            all_records,
            period,
            periods,
            min_records,
        )
        return SiteWindow(
            config.name,
            config.url,
            period,
            periods,
            records,
            latest_issue=max((record.issue for record in records), default=None),
            site_id=config.site_id,
            parser_id=resolve_parser_id(config),
            direction_candidates=tuple(
                record.evidence for record in all_records if record.evidence is not None
            ),
        )

    source = load_source(
        config,
        timeout,
        text_fetcher=text_fetcher,
        rendered_text_fetcher=browser_client.fetch_rendered_text,
        rendered_article_fetcher=browser_client.fetch_rendered_article_record,
    )
    document = source.document
    article = source.article
    rendered = source.rendered
    source_parse_config = replace(config, url=source.source_url)
    direction_candidates: tuple[CandidateEvidence, ...] = ()

    try:
        if article is not None:
            dynamic_article.validate_article_identity(article, source_parse_config)
        results, direction_candidates = collect_issue_records(document, source_parse_config)
        records = select_recent_records(results, period, periods, min_records)
    except ScrapeError as exc:
        if (
            rendered
            or config.api_url
            or config.source_type == "paginated_article_list"
            or not should_render_html_fallback(config, exc)
        ):
            raise
        source = load_browser_source(
            config,
            timeout,
            rendered_text_fetcher=browser_client.fetch_rendered_text,
            rendered_article_fetcher=browser_client.fetch_rendered_article_record,
        )
        document = source.document
        article = source.article
        rendered = source.rendered
        if article is not None:
            dynamic_article.validate_article_identity(article, source_parse_config)
        results, direction_candidates = collect_issue_records(document, source_parse_config)
        records = select_recent_records(results, period, periods, min_records)
    # For ordinary pages, collector order is the configured top/bottom direction.
    # Do not let an out-of-position anomalous issue redefine this site's latest.
    latest_issue = results[0].issue if results else None
    records = tuple(replace(record, url=config.url) for record in records)
    if article is not None:
        records = tuple(
            generic_36.attach_article_identity(record, article, document, config)
            for record in records
        )
    return SiteWindow(
        config.name,
        config.url,
        period,
        periods,
        records,
        latest_issue=latest_issue,
        site_id=config.site_id,
        parser_id=resolve_parser_id(config),
        direction_candidates=direction_candidates,
    )


def matching_consecutive_issues(
    left: SiteWindow,
    right: SiteWindow,
    min_common: int,
) -> tuple[int, ...]:
    left_by_issue = left.by_issue
    right_by_issue = right.by_issue
    common_issues = sorted(set(left_by_issue) & set(right_by_issue), reverse=True)
    if len(common_issues) < min_common:
        return ()
    best_run: list[int] = []
    current_run: list[int] = []
    for index, issue in enumerate(common_issues):
        if left_by_issue[issue].numbers == right_by_issue[issue].numbers:
            if index == 0 or common_issues[index - 1] - issue == 1:
                current_run.append(issue)
            else:
                current_run = [issue]
            if len(current_run) > len(best_run):
                best_run = list(current_run)
        else:
            current_run = []
    return tuple(best_run) if len(best_run) >= min_common else ()


def duplicate_groups_and_matches(
    results: list[SiteWindow],
    min_common: int,
    duplicate_common: int = DEFAULT_DUPLICATE_COMMON,
) -> tuple[list[list[SiteWindow]], list[DuplicateMatch]]:
    parent = list(range(len(results)))
    matches: list[DuplicateMatch] = []

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left_index in range(len(results)):
        for right_index in range(left_index + 1, len(results)):
            issues = matching_consecutive_issues(results[left_index], results[right_index], min_common)
            if issues:
                matches.append(DuplicateMatch(results[left_index], results[right_index], issues))
                if len(issues) >= duplicate_common:
                    union(left_index, right_index)
    grouped: dict[int, list[SiteWindow]] = {}
    for index, result in enumerate(results):
        grouped.setdefault(find(index), []).append(result)
    return [members for members in grouped.values() if len(members) >= 2], matches
