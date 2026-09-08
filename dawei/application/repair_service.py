"""Isolated failed-site validation using one complete scrape execution."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from dawei.application.scrape_service import ScrapeExecution, ScrapeService
from dawei.domain.errors import ScrapeError
from dawei.domain.models import (
    CandidateEvidence,
    ParsedRecord,
    SiteConfig,
    derive_site_id,
)
from dawei.infrastructure import http_client
from dawei.infrastructure.config_repository import ConfigRepository
from dawei.parsers import DEFAULT_REGISTRY, common, generic_36
from dawei.parsers.registry import resolve_parser_id


@dataclass(frozen=True)
class ValidationCase:
    issue: int | None = None
    name: str | None = None
    url: str | None = None
    region: str | None = None
    keywords: tuple[str, ...] | None = None
    section_keywords: tuple[str, ...] | None = None
    search_window: int | None = None
    api_url: str | None = None
    render_browser: bool | None = None
    min_numbers_per_line: int | None = None
    numbers_before_issue: bool | None = None
    drop_zero_numbers: bool | None = None
    parser_id: str | None = None
    source_type: str | None = None
    record_id: str | None = None
    render_policy: str | None = None


@dataclass(frozen=True)
class ResolvedCase:
    config: SiteConfig
    issue: int


@dataclass(frozen=True)
class ContentDiagnostics:
    target_issue_found: bool
    numbers: tuple[str, ...]
    number_count: int
    region: str
    direction_pass: bool
    anchor_pass: bool
    keyword_pass: bool
    numbers_pass: bool
    same_issue_conflict: bool
    duplicate_numbers: tuple[str, ...]
    passed: bool
    failure_reason: str


@dataclass(frozen=True)
class ValidationReport:
    case: ResolvedCase
    source_kind: str
    diagnostics: ContentDiagnostics
    formal_result: ParsedRecord | None
    formal_error: str
    passed: bool
    failure_reason: str
    rendered: bool = False
    record_id: str | None = None
    source_path: str | None = None
    raw_position: int | None = None


SourceFetcher = Callable[..., tuple[str, str]]
SiteScraper = Callable[..., ParsedRecord]


def load_sites(path: str | Path) -> tuple[SiteConfig, ...]:
    return ConfigRepository(path).load()


def configure_proxy(proxy: str | None) -> None:
    http_client.configure_proxy(proxy)


def case_from_mapping(data: Mapping[str, object]) -> ValidationCase:
    values = dict(data)
    for key in ("keywords", "section_keywords"):
        if key in values and values[key] is not None:
            raw = values[key]
            if not isinstance(raw, (list, tuple)):
                raise ScrapeError(f"测试清单字段 {key} 必须是字符串列表")
            values[key] = tuple(str(item) for item in raw)
    try:
        return ValidationCase(**values)
    except TypeError as exc:
        raise ScrapeError(f"失败站点测试清单字段错误: {exc}") from exc


def normalize_url(value: str | None) -> str:
    return (value or "").strip().rstrip("/").lower()


def resolve_case(
    case: ValidationCase,
    sites: Iterable[SiteConfig],
    issue_override: int | None = None,
) -> ResolvedCase:
    issue = issue_override if issue_override is not None else case.issue
    if issue is None or issue < 1:
        raise ScrapeError("测试清单必须为每个站点指定正整数期数，例如196")
    if not case.name and not case.url:
        raise ScrapeError("测试清单必须提供站点名称或URL")
    matches = [
        site
        for site in sites
        if (case.name and site.name == case.name)
        or (case.url and normalize_url(site.url) == normalize_url(case.url))
    ]
    unique = {(site.site_id, site.name, normalize_url(site.url)): site for site in matches}
    if len(unique) > 1:
        identities = "；".join(f"{site.name} {site.url}" for site in unique.values())
        raise ScrapeError(f"名称/URL匹配到多个正式站点: {identities}")
    if unique:
        config = next(iter(unique.values()))
    else:
        if not case.name or not case.url:
            raise ScrapeError("未在正式配置找到站点；临时测试新站时必须同时提供name和url")
        config = SiteConfig(
            case.name,
            case.url,
            site_id=derive_site_id(case.name, case.url),
            parser_id=case.parser_id or "legacy",
            region=case.region or "bottom",
        )
    overrides: dict[str, object] = {}
    for field_name in (
        "region",
        "keywords",
        "section_keywords",
        "search_window",
        "api_url",
        "render_browser",
        "min_numbers_per_line",
        "numbers_before_issue",
        "drop_zero_numbers",
        "parser_id",
        "source_type",
        "record_id",
        "render_policy",
    ):
        value = getattr(case, field_name)
        if value is not None:
            overrides[field_name] = value
    if case.name:
        overrides["name"] = case.name
    if case.url:
        overrides["url"] = case.url
    return ResolvedCase(replace(config, **overrides), issue)


def dedicated_candidates(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[CandidateEvidence], list[tuple[int, int, str]], set[int]] | None:
    parser_id = resolve_parser_id(config)
    if parser_id == "generic_36" or not DEFAULT_REGISTRY.has_candidate_collector(parser_id):
        return None
    return DEFAULT_REGISTRY.candidate_collector(parser_id)(text_or_html, config)


def generic_candidates(
    text_or_html: str,
    config: SiteConfig,
    target_issue: int,
) -> tuple[
    list[CandidateEvidence],
    list[tuple[int, int, str]],
    set[int],
    bool,
    tuple[str, ...],
]:
    lines = common.html_to_lines(text_or_html)
    ranges = common.section_ranges(lines, config)
    anchor_pass = bool(ranges) or not config.section_keywords
    if not anchor_pass:
        return [], [], set(), False, ()
    candidates, invalid, seen = generic_36.generic_candidates(text_or_html, config)
    diagnostic_numbers: tuple[str, ...] = ()
    for line_range in ranges:
        for index in line_range:
            match = common.ISSUE_RE.search(lines[index])
            if not match or int(match.group(1)) != target_issue:
                continue
            context = generic_36.candidate_context(
                lines,
                index,
                config,
                block_start=line_range.start,
                block_end=line_range.stop,
            )
            if config.keywords and not common.all_keywords_present(context, config.keywords):
                continue
            values, _ = generic_36.collect_candidate_numbers_for_diagnostics(
                lines,
                index,
                config,
                stop=line_range.stop,
                block_start=line_range.start,
            )
            if values:
                diagnostic_numbers = values
                break
        if diagnostic_numbers:
            break
    return candidates, invalid, seen, anchor_pass, diagnostic_numbers


def empty_diagnostics(config: SiteConfig, reason: str) -> ContentDiagnostics:
    return ContentDiagnostics(
        False,
        (),
        0,
        generic_36.candidate_region(config),
        False,
        False,
        False,
        False,
        False,
        (),
        False,
        reason,
    )


def analyze_content(
    text_or_html: str,
    config: SiteConfig,
    target_issue: int,
) -> ContentDiagnostics:
    region = generic_36.candidate_region(config)
    try:
        special = dedicated_candidates(text_or_html, config)
        if special is None:
            candidates, invalid, seen, anchor_pass, diagnostic_numbers = generic_candidates(
                text_or_html,
                config,
                target_issue,
            )
        else:
            candidates, invalid, seen = special
            anchor_pass = True
            diagnostic_numbers = ()
    except ScrapeError as exc:
        return empty_diagnostics(config, str(exc))
    target_found = target_issue in seen or any(item.issue == target_issue for item in candidates)
    keyword_pass = target_found and (
        not config.keywords
        or any(item.issue == target_issue for item in candidates)
        or any(item[0] == target_issue for item in invalid)
    )
    selected_numbers = diagnostic_numbers
    direction_pass = False
    direction_error = ""
    conflict = False
    try:
        selectable = generic_36.exact_issue_candidates_for_selection(
            candidates,
            target_issue,
            config,
        )
        if selectable:
            selected_numbers = generic_36.select_candidate_for_position(
                selectable,
                config,
            ).numbers
            direction_pass = True
    except ScrapeError as exc:
        direction_error = str(exc)
        conflict = "多个高可信候选" in direction_error or "冲突" in direction_error
    duplicates = common.duplicate_numbers(selected_numbers) if selected_numbers else ()
    numbers_pass = common.valid_36_code_record(selected_numbers)
    reasons: list[str] = []
    if not anchor_pass:
        reasons.append("锚点未通过: " + "、".join(config.section_keywords))
    if not target_found:
        reasons.append(f"未找到指定{target_issue}期")
    if target_found and not keyword_pass:
        reasons.append("关键词未通过: " + "、".join(config.keywords))
    if conflict:
        reasons.append(f"同期候选冲突: {target_issue}期存在多组不同36码")
    if duplicates:
        reasons.append("重复数字: " + ",".join(duplicates))
    if target_found and not numbers_pass and not duplicates:
        exact_invalid = [item for item in invalid if item[0] == target_issue]
        if exact_invalid:
            reasons.append(generic_36.best_invalid_candidate_reason(exact_invalid))
        else:
            reasons.append(common.invalid_36_code_reason(selected_numbers))
    if target_found and numbers_pass and not direction_pass:
        reasons.append(direction_error or f"{region}方向候选未通过")
    passed = all(
        (
            target_found,
            anchor_pass,
            keyword_pass,
            numbers_pass,
            direction_pass,
            not conflict,
            not duplicates,
        )
    )
    return ContentDiagnostics(
        target_found,
        selected_numbers,
        len(selected_numbers),
        region,
        direction_pass,
        anchor_pass,
        keyword_pass,
        numbers_pass,
        conflict,
        tuple(duplicates),
        passed,
        "；".join(dict.fromkeys(reasons)),
    )


def diagnostics_from_result(
    result: ParsedRecord,
    config: SiteConfig,
    issue: int,
) -> ContentDiagnostics:
    duplicates = common.duplicate_numbers(result.numbers)
    numbers_pass = common.valid_36_code_record(result.numbers)
    passed = result.issue == issue and numbers_pass and not duplicates
    return ContentDiagnostics(
        result.issue == issue,
        result.numbers,
        len(result.numbers),
        generic_36.candidate_region(config),
        result.issue == issue,
        True,
        True,
        numbers_pass,
        False,
        tuple(duplicates),
        passed,
        "" if passed else "图片专属正式解析未通过指定期数或36码校验",
    )


def report_from_execution(case: ResolvedCase, execution: ScrapeExecution) -> ValidationReport:
    result = execution.result
    if execution.document:
        diagnostics = analyze_content(execution.document, case.config, case.issue)
    elif result is not None:
        diagnostics = diagnostics_from_result(result, case.config, case.issue)
    else:
        diagnostics = empty_diagnostics(case.config, execution.error or "真实正文为空")
    reasons = [diagnostics.failure_reason] if diagnostics.failure_reason else []
    if execution.error:
        reasons.append("正式抓取失败: " + execution.error)
    elif result is not None:
        if result.issue != case.issue:
            reasons.append(f"正式抓取期数错误: 实际{result.issue}期")
        if not common.valid_36_code_record(result.numbers):
            reasons.append("正式抓取数字无效: " + common.invalid_36_code_reason(result.numbers))
        if diagnostics.numbers and result.numbers != diagnostics.numbers:
            reasons.append("正式抓取数字与诊断候选不一致")
    passed = diagnostics.passed and result is not None and not reasons
    return ValidationReport(
        case,
        execution.source_kind,
        diagnostics,
        result,
        execution.error,
        passed,
        "；".join(dict.fromkeys(reasons)),
        rendered=execution.rendered,
        record_id=result.record_id if result else None,
        source_path=result.record_path if result else None,
        raw_position=result.raw_position if result else None,
    )


class RepairService:
    def __init__(self, scrape_service: ScrapeService | None = None) -> None:
        self.scrape_service = scrape_service or ScrapeService()

    def validate(self, case: ResolvedCase, *, timeout: int = 20) -> ValidationReport:
        execution = self.scrape_service.execute(
            case.config,
            timeout=timeout,
            fixed_issue=case.issue,
        )
        return report_from_execution(case, execution)


def validate_case(
    case: ResolvedCase,
    timeout: int,
    source_fetcher: SourceFetcher | None = None,
    site_scraper: SiteScraper | None = None,
) -> ValidationReport:
    if source_fetcher is None and site_scraper is None:
        return RepairService().validate(case, timeout=timeout)
    if source_fetcher is None or site_scraper is None:
        raise ScrapeError("测试注入必须同时提供source_fetcher和site_scraper")
    text_cache = http_client.TextFetchCache()
    try:
        source_text, source_kind = source_fetcher(
            case.config,
            case.issue,
            timeout,
            text_cache.fetch,
        )
    except Exception as exc:  # noqa: BLE001 - isolate injected/network failures per site
        reason = f"真实正文获取失败: {exc}"
        diagnostics = empty_diagnostics(case.config, reason)
        return ValidationReport(case, "获取失败", diagnostics, None, reason, False, reason)
    diagnostics = analyze_content(source_text, case.config, case.issue)
    result: ParsedRecord | None = None
    formal_error = ""
    try:
        result = site_scraper(
            case.config,
            timeout=timeout,
            fixed_issue=case.issue,
            health_check=False,
            text_fetcher=text_cache.fetch,
        )
    except Exception as exc:  # noqa: BLE001 - isolate injected/network failures per site
        formal_error = str(exc) or f"{exc.__class__.__name__} 无详细异常消息"
    execution = ScrapeExecution(
        case.config,
        case.issue,
        result,
        source_text,
        source_kind,
        "浏览器" in source_kind,
        formal_error,
    )
    return report_from_execution(case, execution)
