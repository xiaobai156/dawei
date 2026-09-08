"""New-site identity and recent-window hard gates."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from urllib.parse import parse_qs, urlsplit

from dawei.application.duplicate_service import (
    BackupSnapshot,
    DuplicateMatch,
    SiteWindow,
)
from dawei.domain.errors import ScrapeError
from dawei.domain.models import SiteConfig
from dawei.domain.validation import validate_candidate_evidence, validate_site_config
from dawei.infrastructure.config_repository import normalize_url_identity
from dawei.parsers import generic_36
from dawei.parsers.common import all_keywords_present, valid_36_code_record
from dawei.parsers.registry import resolve_parser_id

REQUIRED_ONBOARDING_PERIODS = 10


def site_key(site: SiteWindow | SiteConfig) -> tuple[str, str]:
    return site.name, site.url


def normalized_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def normalized_url(value: str | None) -> str:
    if not value:
        return ""
    return normalize_url_identity(value)


def site_identity(site: SiteWindow | SiteConfig) -> str:
    return f"{site.name} {site.url}"


def topic_identity(value: str) -> str:
    parts = urlsplit(value)
    query = parse_qs(parts.query)
    topic_id = next(
        (
            values[0]
            for key in ("id", "tid")
            if (values := query.get(key))
        ),
        "",
    )
    route = parts.path.rstrip("/").lower()
    if not topic_id:
        match = re.search(r"/(?:topic|read)/([^/.?#]+)(?:\.html)?$", route)
        topic_id = match.group(1) if match else ""
    if not topic_id:
        return ""
    return f"{parts.netloc.lower()}|{route}|{topic_id}"


def record_identities(site: SiteWindow | SiteConfig) -> tuple[str, ...]:
    if isinstance(site, SiteConfig):
        values = (site.record_id,)
    else:
        values = tuple(record.record_id for record in site.records)
    return tuple(value.strip().casefold() for value in values if value and value.strip())


def candidate_identity_conflicts(
    candidate_sites: Iterable[SiteConfig],
    configured_sites: Iterable[SiteConfig],
    backup_results: Iterable[SiteWindow],
) -> list[str]:
    conflicts: list[str] = []
    names: dict[str, str] = {}
    urls: dict[str, str] = {}
    api_urls: dict[str, str] = {}
    site_ids: dict[str, str] = {}
    record_ids: dict[str, str] = {}
    topics: dict[str, str] = {}

    def add_existing(site: SiteWindow | SiteConfig) -> None:
        names.setdefault(normalized_name(site.name), site_identity(site))
        urls.setdefault(normalized_url(site.url), site_identity(site))
        api_url = normalized_url(getattr(site, "api_url", ""))
        if api_url:
            api_urls.setdefault(api_url, site_identity(site))
        site_id = getattr(site, "site_id", "")
        if site_id:
            site_ids.setdefault(site_id, site_identity(site))
        for record_id in record_identities(site):
            record_ids.setdefault(record_id, site_identity(site))
        topic = topic_identity(site.url)
        if topic:
            topics.setdefault(topic, site_identity(site))

    for site in configured_sites:
        add_existing(site)
    for site in backup_results:
        add_existing(site)
    for candidate in candidate_sites:
        identity = site_identity(candidate)
        name_key = normalized_name(candidate.name)
        url_key = normalized_url(candidate.url)
        api_url_key = normalized_url(candidate.api_url)
        topic_key = topic_identity(candidate.url)
        if name_key in names:
            conflicts.append(f"同名候选站：{identity} 与已有站 {names[name_key]} 重复")
        if url_key in urls:
            conflicts.append(f"同URL候选站：{identity} 与已有站 {urls[url_key]} 重复")
        if api_url_key and api_url_key in api_urls:
            conflicts.append(f"同api_url候选站：{identity} 与已有站 {api_urls[api_url_key]} 重复")
        if candidate.site_id and candidate.site_id in site_ids:
            conflicts.append(
                f"同site_id候选站：{identity} 与已有站 {site_ids[candidate.site_id]} 重复"
            )
        for record_id in record_identities(candidate):
            if record_id in record_ids:
                conflicts.append(
                    f"同record_id候选站：{identity} 与已有站 {record_ids[record_id]} 重复"
                )
        if topic_key and topic_key in topics:
            conflicts.append(f"同topic候选站：{identity} 与已有站 {topics[topic_key]} 重复")
        add_existing(candidate)
    return conflicts


def validate_candidate_window(
    window: SiteWindow,
    backup_period: int,
    periods: int,
    *,
    config: SiteConfig,
) -> None:
    if type(periods) is not int or periods != REQUIRED_ONBOARDING_PERIODS:
        raise ScrapeError(
            "候选新站校验必须使用正好10期窗口，"
            f"当前请求{periods}期"
        )
    if type(backup_period) is not int or backup_period <= 0:
        raise ScrapeError("候选新站缓存基准期数无效，拒绝校验")
    try:
        validate_site_config(config)
    except Exception as exc:
        raise ScrapeError(f"候选新站配置未通过: {exc}") from exc
    if not config.keywords or any(not keyword.strip() for keyword in config.keywords):
        raise ScrapeError("候选新站必须配置非空专属数据关键词")
    if not config.section_keywords or any(
        not keyword.strip() for keyword in config.section_keywords
    ):
        raise ScrapeError("候选新站必须配置非空专属栏目关键词")
    if config.parser_id == "legacy":
        raise ScrapeError("候选新站必须显式配置已验证的专属解析器")

    window_start = backup_period - periods + 1
    records_by_issue = window.by_issue
    if len(window.records) != REQUIRED_ONBOARDING_PERIODS:
        raise ScrapeError(
            "候选新站必须提供正好10条窗口记录，"
            f"实际{len(window.records)}条"
        )
    records = {
        issue: record
        for issue, record in records_by_issue.items()
        if window_start <= issue <= backup_period
    }
    if len(records) != REQUIRED_ONBOARDING_PERIODS:
        raise ScrapeError(
            "候选新站验收窗口必须正好包含10个不同期数，"
            f"实际{len(records)}期"
        )
    try:
        expected_parser = resolve_parser_id(config)
        allowed_parsers = {
            expected_parser,
            *(("generic_36",) if expected_parser == "kunnan_magazine" else ()),
        }
        for record in records.values():
            evidence = validate_candidate_evidence(record)
            if evidence.parser_id not in allowed_parsers:
                raise ScrapeError(
                    f"{record.issue}期证据解析器不匹配: "
                    f"预期{expected_parser}，实际{evidence.parser_id}"
                )
            if not all_keywords_present(evidence.anchor_line, config.section_keywords):
                raise ScrapeError(f"{record.issue}期证据栏目锚点不属于专属栏目")
            candidate_text = " ".join(
                (
                    evidence.anchor_line,
                    evidence.raw_issue_line,
                    *evidence.raw_number_lines,
                )
            )
            if not all_keywords_present(candidate_text, config.keywords):
                raise ScrapeError(f"{record.issue}期证据未绑定专属数据关键词")
    except Exception as exc:
        raise ScrapeError(f"候选新站证据未通过: {exc}") from exc
    reference_issues = {backup_period, backup_period - 1}
    is_collection = expected_parser == "kunnan_magazine" or config.source_type == "dynamic_collection"
    if not is_collection:
        evidence = list(window.direction_candidates)
        if not evidence:
            evidence = [record.evidence for record in records.values() if record.evidence is not None]
        # Check every original candidate before applying the bounded direction
        # window.  Otherwise a conflicting row outside the trimmed ten records
        # could be hidden by the direction selection.
        for issue in sorted({candidate.issue for candidate in evidence}):
            generic_36.assert_no_conflicting_exact_issue_candidates(evidence, issue, config)
        direction_window = generic_36.latest_candidate_window(evidence, config)
        if not ({candidate.issue for candidate in direction_window} & reference_issues):
            actual = ", ".join(str(candidate.issue) for candidate in direction_window) or "无"
            raise ScrapeError(
                f"候选新站未在{generic_36.candidate_region(config)}方向候选范围命中"
                f"缓存最新近2期（{backup_period}期/{backup_period - 1}期）；"
                f"方向候选期数: {actual}"
            )
    if not (set(records) & reference_issues):
        raise ScrapeError(
            f"候选新站未抓到缓存最新近2期之一（{backup_period}期/{backup_period - 1}期）"
        )
    invalid = [issue for issue, record in records.items() if not valid_36_code_record(record.numbers)]
    if invalid:
        issues = "、".join(f"{issue}期" for issue in sorted(invalid, reverse=True))
        raise ScrapeError(f"候选新站{issues}不是有效36码")


def matches_involving_sites(
    matches: Iterable[DuplicateMatch],
    sites: Iterable[SiteConfig],
) -> list[DuplicateMatch]:
    keys = {site_key(site) for site in sites}
    return [
        match
        for match in matches
        if site_key(match.left) in keys or site_key(match.right) in keys
    ]


class OnboardingService:
    def validate(
        self,
        *,
        candidates: Iterable[SiteConfig],
        configured: Iterable[SiteConfig],
        backup: BackupSnapshot,
        windows: Iterable[SiteWindow],
        matches: Iterable[DuplicateMatch],
    ) -> tuple[DuplicateMatch, ...]:
        candidate_sites = tuple(candidates)
        if not candidate_sites:
            raise ScrapeError("候选新站为空，拒收")
        if type(backup.periods) is not int or backup.periods != REQUIRED_ONBOARDING_PERIODS:
            raise ScrapeError(
                "新增站点必须基于正好10期完整缓存，"
                f"当前缓存窗口为{backup.periods}期"
            )
        if type(backup.period) is not int or backup.period <= 0:
            raise ScrapeError("新增站点缓存基准期数无效，拒绝新增")
        if backup.incomplete or backup.failures:
            raise ScrapeError("近10期重复检测缓存不完整，禁止新增站点")
        conflicts = candidate_identity_conflicts(candidate_sites, configured, backup.sites)
        if conflicts:
            raise ScrapeError("；".join(conflicts))
        windows_by_key: dict[tuple[str, str], list[SiteWindow]] = {}
        for window in windows:
            windows_by_key.setdefault(site_key(window), []).append(window)
        for candidate in candidate_sites:
            candidate_windows = windows_by_key.get(site_key(candidate), [])
            if len(candidate_windows) != 1:
                raise ScrapeError(
                    f"候选新站必须唯一对应一个真实抓取窗口: {site_identity(candidate)}"
                )
            validate_candidate_window(
                candidate_windows[0],
                backup.period,
                REQUIRED_ONBOARDING_PERIODS,
                config=candidate,
            )
        candidate_matches = tuple(matches_involving_sites(matches, candidate_sites))
        if candidate_matches:
            details = "；".join(
                f"{match.status()}：{match.left.name}<=>{match.right.name}，"
                f"连续{match.consecutive_count}期"
                for match in candidate_matches
            )
            raise ScrapeError(details)
        return candidate_matches
