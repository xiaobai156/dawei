"""New-site identity and recent-window hard gates."""

from __future__ import annotations

from collections.abc import Iterable
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from dawei.application.duplicate_service import BackupSnapshot, DuplicateMatch, SiteWindow
from dawei.domain.errors import ScrapeError
from dawei.domain.models import SiteConfig
from dawei.domain.validation import validate_candidate_evidence, validate_site_config
from dawei.parsers import generic_36
from dawei.parsers.common import all_keywords_present, valid_36_code_record
from dawei.parsers.registry import resolve_parser_id


def site_key(site: SiteWindow | SiteConfig) -> tuple[str, str]:
    return site.name, site.url


def normalized_name(value: str) -> str:
    return value.strip().casefold()


def normalized_url(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip()
    parts = urlsplit(text)
    if not parts.scheme and not parts.netloc:
        return text.rstrip("/")
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


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
    host = urlsplit(site.url).netloc.lower()
    return tuple(f"{host}|{value}" for value in values if value)


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
    records = {
        record.issue: record
        for record in window.records
        if window_start <= record.issue <= backup_period
    }
    if not records:
        raise ScrapeError(f"候选新站未抓到缓存近{periods}期窗口内有效期数")
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
    evidence = [record.evidence for record in records.values() if record.evidence is not None]
    direction_window = generic_36.latest_candidate_window(
        generic_36.unique_candidates(evidence, config),
        config,
    )
    reference_issues = {backup_period, backup_period - 1}
    if not ({candidate.issue for candidate in direction_window} & reference_issues):
        actual = ", ".join(str(candidate.issue) for candidate in direction_window) or "无"
        raise ScrapeError(
            f"候选新站未在{generic_36.candidate_region(config)}方向最近3组命中"
            f"缓存最新近2期（{backup_period}期/{backup_period - 1}期）；"
            f"方向最近3组: {actual}"
        )
    if not (set(records) & reference_issues):
        raise ScrapeError(
            f"候选新站未抓到缓存最新近2期之一（{backup_period}期/{backup_period - 1}期）"
        )
    invalid = [issue for issue, record in records.items() if not valid_36_code_record(record.numbers)]
    if invalid:
        issues = "、".join(f"{issue}期" for issue in sorted(invalid, reverse=True))
        raise ScrapeError(f"候选新站{issues}不是有效36码")
    if config.onboarding_exception == "allow_insufficient_history":
        expected_valid = set(config.onboarding_valid_issues)
        actual_valid = set(records)
        expected_window = set(range(window_start, backup_period + 1))
        actual_missing = expected_window - actual_valid
        if expected_valid != actual_valid:
            raise ScrapeError(
                "新增站特例记录的有效期与现场抓取不一致："
                f"配置{sorted(expected_valid, reverse=True)}，现场{sorted(actual_valid, reverse=True)}"
            )
        if set(config.onboarding_missing_issues) != actual_missing:
            raise ScrapeError(
                "新增站特例记录的缺失期与现场窗口不一致："
                f"配置{sorted(config.onboarding_missing_issues, reverse=True)}，"
                f"现场{sorted(actual_missing, reverse=True)}"
            )
    elif len(records) < periods:
        raise ScrapeError(f"候选新站近{periods}期有效数据不足：需要{periods}期，实际{len(records)}期")


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
        if any(site.onboarding_exception for site in candidate_sites) and len(candidate_sites) != 1:
            raise ScrapeError("历史不足特例只能用于单个候选站")
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
                backup.periods,
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
