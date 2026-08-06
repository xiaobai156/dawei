"""End-to-end duplicate detection and candidate onboarding orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from dawei.application import duplicate_service, onboarding_service
from dawei.domain.errors import ScrapeError
from dawei.domain.models import SiteConfig
from dawei.infrastructure import http_client
from dawei.infrastructure.config_repository import ConfigRepository, migrate_site_mapping


WindowScraper = Callable[..., duplicate_service.SiteWindow]
ProgressSink = Callable[[str], None]


def describe_failure_reason(reason: object) -> str:
    text = str(reason).strip()
    if not text:
        return f"程序异常：{reason.__class__.__name__} 无详细异常消息"
    match = re.fullmatch(r"only found (\d+)/(\d+) comparable issues near (\d+): (.*)", text)
    if match:
        found, needed, period, issues = match.groups()
        issue_text = "无" if issues == "none" else issues.replace(",", "、")
        return (
            f"可对比期数不足：需要至少{needed}期，实际找到{found}期；"
            f"指定期数{period}附近找到：{issue_text}"
        )
    if text.startswith("unexpected error:"):
        detail = text.removeprefix("unexpected error:").strip()
        return "程序异常：" + (detail or "无详细异常消息")
    if text.startswith("network error:"):
        return "网络请求失败：" + text.removeprefix("network error:").strip()
    if "IncompleteRead" in text:
        return "网络读取不完整：" + text
    return text


def format_failure(site: SiteConfig, reason: object) -> str:
    return f"{site.name} {site.url} {describe_failure_reason(reason)}"


@dataclass(frozen=True)
class DuplicateOptions:
    sites_config: Path
    backup_path: Path
    period: int | None = None
    periods: int = 10
    timeout: int = 20
    workers: int = 8
    min_common: int = 3
    duplicate_common: int = 6
    only: tuple[str, ...] = ()
    candidate_sites: tuple[str, ...] = ()
    use_backup: bool = False
    update_backup: bool = False
    proxy: str | None = None
    proxy_retries: int = 1


@dataclass(frozen=True)
class DuplicateRunResult:
    period: int
    windows: tuple[duplicate_service.SiteWindow, ...]
    groups: tuple[tuple[duplicate_service.SiteWindow, ...], ...]
    matches: tuple[duplicate_service.DuplicateMatch, ...]
    blocking_matches: tuple[duplicate_service.DuplicateMatch, ...]
    failures: tuple[str, ...]
    onboarding_error: str = ""
    backup_updated: bool = False
    auto_period: bool = False

    @property
    def exit_code(self) -> int:
        return 1 if self.failures or self.blocking_matches or self.onboarding_error else 0


def load_candidate_sites(values: Iterable[str]) -> tuple[SiteConfig, ...]:
    sites: list[SiteConfig] = []
    for value in values:
        source = Path(value)
        try:
            payload = json.loads(
                source.read_text(encoding="utf-8-sig") if source.exists() else value
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ScrapeError(f"读取候选新站配置失败: {exc}") from exc
        records = payload if isinstance(payload, list) else [payload]
        for record in records:
            if not isinstance(record, dict):
                raise ScrapeError("候选新站配置必须是JSON对象或JSON对象列表")
            sites.append(migrate_site_mapping(record, len(sites) + 1))
    return tuple(sites)


def selected_sites(names: Iterable[str], sites: Iterable[SiteConfig]) -> tuple[SiteConfig, ...]:
    wanted = set(names)
    site_list = tuple(sites)
    if not wanted:
        return site_list
    return tuple(
        site
        for site in site_list
        if site.name in wanted or urlsplit(site.url).netloc in wanted
    )


class DuplicateRunner:
    def __init__(
        self,
        *,
        window_scraper: WindowScraper | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> None:
        self.window_scraper = window_scraper or duplicate_service.scrape_site_window
        self.progress_sink = progress_sink or print

    def run(self, options: DuplicateOptions) -> DuplicateRunResult:
        if options.candidate_sites and not options.use_backup:
            raise ScrapeError(
                "新增候选站必须使用 --use-backup 读取近10期备份JSON，"
                "禁止实时全站抓取作为判重依据"
            )
        configured = ConfigRepository(options.sites_config).load()
        candidates = load_candidate_sites(options.candidate_sites)
        if options.candidate_sites and not candidates:
            raise ScrapeError("候选新站为空，拒收")
        http_client.configure_proxy(options.proxy)
        backup = (
            duplicate_service.load_backup_snapshot(options.backup_path)
            if options.use_backup
            else None
        )
        backup_windows = list(backup.sites) if backup else []
        period = options.period if options.period is not None else (
            backup.period if backup else 9999
        )
        auto_period = options.period is None
        failures: list[str] = []
        if candidates:
            conflicts = onboarding_service.candidate_identity_conflicts(
                candidates,
                configured,
                backup_windows,
            )
            if conflicts:
                failures.extend(conflicts)
                return self._empty_result(period, failures, auto_period)
            if backup and backup.incomplete:
                failures.append("近10期重复检测缓存不完整，禁止新增站点")
                return self._empty_result(period, failures, auto_period)
        sites = selected_sites(
            options.only,
            candidates if options.use_backup else (*configured, *candidates),
        )
        if options.only and not sites:
            raise ScrapeError("no matching sites selected")
        text_cache = http_client.TextFetchCache(self._text_fetcher(options.proxy_retries))
        candidate_keys = {onboarding_service.site_key(site) for site in candidates}
        indexed: dict[int, duplicate_service.SiteWindow] = {
            -(index + 1): window for index, window in enumerate(backup_windows)
        }
        workers = max(1, min(options.workers, len(sites) or 1))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self.window_scraper,
                    site,
                    period,
                    options.periods,
                    1
                    if (
                        onboarding_service.site_key(site) in candidate_keys
                        and site.onboarding_exception == "allow_insufficient_history"
                    )
                    else options.periods
                    if onboarding_service.site_key(site) in candidate_keys
                    else 1,
                    options.timeout,
                    text_cache.fetch,
                ): (index, site)
                for index, site in enumerate(sites)
            }
            for future in as_completed(futures):
                index, site = futures[future]
                try:
                    window = future.result()
                    if backup and onboarding_service.site_key(site) in candidate_keys:
                        onboarding_service.validate_candidate_window(
                            window,
                            backup.period,
                            options.periods,
                            config=site,
                        )
                except Exception as exc:
                    error = exc if isinstance(exc, ScrapeError) else RuntimeError(
                        f"unexpected error: {exc}"
                    )
                    failures.append(format_failure(site, error))
                    continue
                indexed[index] = window
                issues = ",".join(str(record.issue) for record in window.records)
                self.progress_sink(f"{site.name}: issues {issues}")
        windows = [indexed[index] for index in sorted(indexed)]
        if options.period is None and backup is None:
            period = duplicate_service.detect_latest_period(windows)
        comparable: list[duplicate_service.SiteWindow] = []
        for window in windows:
            try:
                comparable.append(
                    duplicate_service.with_period(window, period, options.periods)
                )
            except ScrapeError as exc:
                failures.append(f"{window.name} {window.url} {exc}")
        groups, matches = duplicate_service.duplicate_groups_and_matches(
            comparable,
            options.min_common,
            options.duplicate_common,
        )
        candidate_matches = onboarding_service.matches_involving_sites(matches, candidates)
        blocking = candidate_matches if candidates else matches
        onboarding_error = ""
        if candidates and backup and not failures:
            try:
                onboarding_service.OnboardingService().validate(
                    candidates=candidates,
                    configured=configured,
                    backup=backup,
                    windows=comparable,
                    matches=matches,
                )
            except ScrapeError as exc:
                onboarding_error = str(exc)
        backup_updated = False
        if options.update_backup and not (
            options.use_backup and candidates and (failures or candidate_matches or onboarding_error)
        ):
            prior_failures = backup.failures if backup else ()
            combined = tuple(dict.fromkeys((*prior_failures, *failures)))
            try:
                duplicate_service.write_backup_json(
                    comparable,
                    options.backup_path,
                    period,
                    options.periods,
                    combined,
                )
                backup_updated = True
            except ScrapeError as exc:
                failures.append(str(exc))
        return DuplicateRunResult(
            period,
            tuple(comparable),
            tuple(tuple(group) for group in groups),
            tuple(matches),
            tuple(blocking),
            tuple(failures),
            onboarding_error,
            backup_updated,
            auto_period,
        )

    @staticmethod
    def _empty_result(
        period: int,
        failures: Iterable[str],
        auto_period: bool,
    ) -> DuplicateRunResult:
        return DuplicateRunResult(
            period,
            (),
            (),
            (),
            (),
            tuple(failures),
            auto_period=auto_period,
        )

    @staticmethod
    def _text_fetcher(proxy_retries: int):
        def raw_fetcher(url: str, timeout: int, extra_headers=None):
            return http_client.fetch_raw(
                url,
                timeout,
                extra_headers,
                proxy_retries=max(1, proxy_retries),
            )

        def fetch(url: str, timeout: int) -> str:
            return http_client.fetch_text(url, timeout, raw_fetcher=raw_fetcher)

        return fetch
