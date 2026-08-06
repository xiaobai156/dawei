"""Pure issue alignment and duplicate-site comparison service."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from dawei.application.scrape_service import (
    decode_bb48kk_issue_candidates,
    decode_tuku2135_issue_candidates,
    fetch_paginated_article_document,
    is_dynamic_article_site,
    should_render_dynamic_fallback,
)
from dawei.domain.errors import CacheError, ScrapeError
from dawei.domain.models import (
    ArticleRecord,
    CandidateEvidence,
    ParsedRecord,
    ScrapeRecord,
    SiteConfig,
)
from dawei.infrastructure import browser_client, http_client, image_client, source_adapters
from dawei.infrastructure.cache_repository import CacheRepository
from dawei.parsers import DEFAULT_REGISTRY
from dawei.parsers import dynamic_article
from dawei.parsers import generic_36
from dawei.parsers import image_36
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
    windows = tuple(
        SiteWindow(
            site.name,
            site.url,
            snapshot.period,
            snapshot.periods,
            tuple(
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
            ),
            latest_issue=snapshot.period,
            site_id=site.site_id,
            parser_id=site.records[0].parser_id if site.records else "",
        )
        for site in snapshot.sites
    )
    return BackupSnapshot(
        snapshot.period,
        snapshot.periods,
        windows,
        snapshot.failures,
        incomplete=snapshot.incomplete,
    )


def load_backup_json(path: str | Path) -> list[SiteWindow]:
    return list(load_backup_snapshot(path).sites)


def write_backup_json(
    results: Iterable[SiteWindow],
    output_path: str | Path,
    period: int,
    periods: int,
    failures: Iterable[str] = (),
) -> None:
    records: list[ScrapeRecord] = []
    for window in results:
        selected = select_recent_records(window.records, period, periods, 1)
        for record in selected:
            records.append(
                ScrapeRecord(
                    site_id=window.site_id,
                    name=window.name,
                    url=window.url,
                    issue=record.issue,
                    numbers=record.numbers,
                    record_id=record.record_id,
                    source_path=record.record_path,
                    raw_position=record.raw_position,
                    parser_id=window.parser_id or "generic_36",
                )
            )
    try:
        CacheRepository(output_path).replace_window(
            records,
            failures,
            fixed_issue=period,
            periods=periods,
        )
    except CacheError as exc:
        raise ScrapeError(f"写入备份JSON失败: {exc}") from exc


def detect_latest_period(results: Iterable[SiteWindow]) -> int:
    issues = [
        result.latest_issue if result.latest_issue is not None else result.records[0].issue
        for result in results
        if result.latest_issue is not None or result.records
    ]
    if not issues:
        raise ScrapeError("无法自动识别最新期数：没有抓到任何可用记录")
    counts = Counter(issues)
    return max(counts, key=lambda issue: (counts[issue], issue))


def select_recent_records(
    records: Iterable[ParsedRecord],
    period: int,
    periods: int,
    min_records: int,
) -> tuple[ParsedRecord, ...]:
    if period == 9999:
        selected = list(records)
    else:
        selected = [record for record in sorted(records, key=lambda item: item.issue, reverse=True) if record.issue <= period]
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


def collect_issue_records(text_or_html: str, config: SiteConfig) -> list[ParsedRecord]:
    parser_id = resolve_parser_id(config)
    collector = DEFAULT_REGISTRY.candidate_collector(parser_id)
    candidates, _, _ = collector(text_or_html, config)
    return site_results_from_candidates(candidates, config)


def scrape_bb48kk_window(
    config: SiteConfig,
    period: int,
    periods: int,
    min_records: int,
    timeout: int,
) -> SiteWindow:
    expanded_html = source_adapters.fetch_expanded_html(
        config.url,
        timeout,
        http_client.fetch_text,
    )
    images = image_36.bb48kk_images_from(expanded_html)
    if not images:
        raise ScrapeError("no bb48kk image records found")
    indexed = list(enumerate(images))
    selected = sorted({image[0] for _, image in indexed if image[0] <= period}, reverse=True)
    if len(selected) < min_records:
        selected = sorted({image[0] for _, image in indexed}, reverse=True)
    wanted = tuple(selected[:periods])
    if len(wanted) < min_records:
        found = ",".join(str(issue) for issue in wanted) or "none"
        raise ScrapeError(f"only found {len(wanted)}/{min_records} comparable issues near {period}: {found}")
    records: list[ParsedRecord] = []
    for issue in wanted:
        records.append(
            decode_bb48kk_issue_candidates(
                config,
                [entry for entry in indexed if entry[1][0] == issue],
                timeout,
            )
        )
    return SiteWindow(
        config.name,
        config.url,
        period,
        periods,
        tuple(records),
        latest_issue=records[0].issue if records else None,
        site_id=config.site_id,
        parser_id=resolve_parser_id(config),
    )


def scrape_tuku2135_window(
    config: SiteConfig,
    period: int,
    periods: int,
    min_records: int,
    timeout: int,
) -> SiteWindow:
    all_records = image_client.tuku2135_issue_records(config, timeout=timeout)
    indexed = list(enumerate(all_records))
    issues = sorted({record[0] for _, record in indexed if record[0] <= period}, reverse=True)
    if len(issues) < min_records:
        issues = sorted({record[0] for _, record in indexed}, reverse=True)
    wanted = issues[:periods]
    if len(wanted) < min_records:
        found = ",".join(str(issue) for issue in wanted) or "none"
        raise ScrapeError(f"only found {len(wanted)}/{min_records} comparable issues near {period}: {found}")
    records: list[ParsedRecord] = []
    for issue in wanted:
        records.append(
            decode_tuku2135_issue_candidates(
                config,
                [entry for entry in indexed if entry[1][0] == issue],
                timeout,
            )
        )
    return SiteWindow(
        config.name,
        config.url,
        period,
        periods,
        tuple(records),
        latest_issue=records[0].issue if records else None,
        site_id=config.site_id,
        parser_id=resolve_parser_id(config),
    )


def scrape_site_window(
    config: SiteConfig,
    period: int,
    periods: int,
    min_records: int,
    timeout: int,
    text_fetcher: TextFetcher = http_client.fetch_text,
) -> SiteWindow:
    if config.image_decoder == "bb48kk_fixed":
        return scrape_bb48kk_window(config, period, periods, min_records, timeout)
    if config.image_decoder == "tuku2135_ocr":
        return scrape_tuku2135_window(config, period, periods, min_records, timeout)
    if resolve_parser_id(config) == "kunnan_magazine":
        if not config.api_url:
            raise ScrapeError("困难杂志缺少专属 API 地址")
        records = select_recent_records(
            dynamic_article.kunnan_magazine_records_from_payload(
                text_fetcher(config.api_url, timeout),
                config,
            ),
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
            latest_issue=records[0].issue if records else None,
            site_id=config.site_id,
            parser_id=resolve_parser_id(config),
        )

    expected_keywords = (config.name, *config.keywords, *config.section_keywords)
    expected_issue = period if period != 9999 else None
    article: ArticleRecord | None = None
    source_parse_config = config
    if config.source_type == "paginated_article_list":
        document, source_url = fetch_paginated_article_document(
            config,
            timeout,
            None,
            text_fetcher,
        )
        source_parse_config = replace(config, url=source_url)
    elif config.api_url:
        try:
            payload = text_fetcher(config.api_url, timeout)
            if is_dynamic_article_site(config):
                article = source_adapters.article_record_from_payload(
                    payload,
                    config,
                    source_path=config.api_url,
                )
                document = article.document
            else:
                document = source_adapters.api_payload_to_html(payload, allow_multiple=False)
        except ScrapeError as exc:
            if not should_render_dynamic_fallback(config, exc):
                raise
            article = browser_client.fetch_rendered_article_record(
                config,
                timeout=timeout,
                expected_issue=expected_issue,
                expected_keywords=expected_keywords,
            )
            document = article.document
    elif config.render_policy == "always" or config.render_browser or is_dynamic_article_site(config):
        if is_dynamic_article_site(config):
            article = browser_client.fetch_rendered_article_record(
                config,
                timeout=timeout,
                expected_issue=expected_issue,
                expected_keywords=expected_keywords,
            )
            document = article.document
        else:
            document = browser_client.fetch_rendered_text(
                config.url,
                timeout=timeout,
                expected_issue=expected_issue,
                expected_keywords=expected_keywords,
            )
    else:
        document = source_adapters.fetch_expanded_html(config.url, timeout, text_fetcher)

    results = collect_issue_records(document, source_parse_config)
    latest_issue = results[0].issue if results else None
    records = select_recent_records(results, period, periods, min_records)
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


def are_duplicate(
    left: SiteWindow,
    right: SiteWindow,
    duplicate_common: int = DEFAULT_DUPLICATE_COMMON,
) -> bool:
    return bool(matching_consecutive_issues(left, right, duplicate_common))


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


def duplicate_groups(results: list[SiteWindow], min_common: int) -> list[list[SiteWindow]]:
    groups, _ = duplicate_groups_and_matches(results, min_common, DEFAULT_DUPLICATE_COMMON)
    return groups


class DuplicateService:
    def compare(
        self,
        windows: Iterable[SiteWindow],
        *,
        min_common: int = 3,
        duplicate_common: int = DEFAULT_DUPLICATE_COMMON,
    ) -> tuple[list[list[SiteWindow]], list[DuplicateMatch]]:
        return duplicate_groups_and_matches(list(windows), min_common, duplicate_common)
