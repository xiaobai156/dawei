"""End-to-end duplicate detection and candidate onboarding orchestration."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dawei.application import duplicate_service, onboarding_service
from dawei.domain.errors import ScrapeError
from dawei.domain.models import SiteConfig
from dawei.infrastructure import http_client
from dawei.infrastructure.cache_repository import CacheRepository
from dawei.infrastructure.config_repository import (
    ConfigRepository,
    config_fingerprint,
    migrate_site_mapping,
    normalize_url_identity,
)

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


def format_failure(
    site: SiteConfig,
    reason: object,
    period: int | None = 9999,
    *,
    stage: str = "抓取窗口",
) -> str:
    benchmark = "自动识别" if period in {None, 9999} else f"{period}期"
    return (
        f"{site.name} {site.url} 方向:{site.direction}；基准期:{benchmark}；"
        f"失败阶段:{stage}；原因:{describe_failure_reason(reason)}"
    )


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
        try:
            source = Path(value)
            if not source.exists():
                raise ScrapeError(f"候选新站配置文件不存在: {value}")
            if not source.is_file():
                raise ScrapeError(f"候选新站配置必须是普通文件: {value}")
            payload = json.loads(source.read_text(encoding="utf-8-sig"))
        except ScrapeError:
            raise
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ScrapeError(f"读取候选新站配置失败: {exc}") from exc
        records = payload if isinstance(payload, list) else [payload]
        for record in records:
            if not isinstance(record, dict):
                raise ScrapeError("候选新站配置必须是JSON对象或JSON对象列表")
            _validate_candidate_mapping(record, len(sites) + 1)
            sites.append(migrate_site_mapping(record, len(sites) + 1))
    return tuple(sites)


def _validate_candidate_mapping(record: dict[str, object], index: int) -> None:
    required = (
        "name",
        "url",
        "site_id",
        "source_type",
        "parser_id",
        "region",
        "render_policy",
        "section_keywords",
        "keywords",
    )
    missing = [field for field in required if field not in record]
    if missing:
        raise ScrapeError(
            f"候选新站#{index}必须显式配置: " + ", ".join(missing)
        )
    for field in ("name", "url", "site_id", "source_type", "parser_id"):
        value = record[field]
        if type(value) is not str or not value.strip():
            raise ScrapeError(f"候选新站#{index}字段{field}必须为非空字符串")
    region = record["region"]
    if type(region) is not str or region not in {"top", "bottom"}:
        raise ScrapeError(f"候选新站#{index}字段region必须为top或bottom")
    render_policy = record["render_policy"]
    if type(render_policy) is not str or render_policy not in {"never", "fallback", "always"}:
        raise ScrapeError(f"候选新站#{index}字段render_policy必须为never/fallback/always")
    source_type = record["source_type"]
    if source_type == "paginated_article_list" and (
        render_policy != "never" or record.get("render_browser", False) is not False
    ):
        raise ScrapeError(
            f"候选新站#{index}的分页文章列表来源只允许HTTP抓取: "
            "render_policy必须为never且render_browser必须为False"
        )
    for field in ("keywords", "section_keywords"):
        values = record[field]
        if type(values) is not list or not values or any(
            type(value) is not str or not value.strip() for value in values
        ):
            raise ScrapeError(f"候选新站#{index}字段{field}必须为非空字符串列表")
    for field in ("navigation_keywords",):
        if field in record:
            values = record[field]
            if type(values) is not list or not values or any(
                type(value) is not str or not value.strip() for value in values
            ):
                raise ScrapeError(f"候选新站#{index}字段{field}必须为非空字符串列表")
    for field in ("onboarding_valid_issues", "onboarding_missing_issues"):
        if field in record:
            values = record[field]
            if type(values) is not list or not values or any(
                type(value) is not int or value <= 0 for value in values
            ):
                raise ScrapeError(f"候选新站#{index}字段{field}必须为正整数列表")
    dynamic_required = {
        "dynamic_article": ("api_url", "record_id"),
        "dynamic_collection": ("api_url",),
    }.get(source_type, ())
    missing_dynamic = [field for field in dynamic_required if field not in record]
    if missing_dynamic:
        raise ScrapeError(
            f"候选新站#{index}的{source_type}必须显式配置: "
            + ", ".join(missing_dynamic)
        )
    for field in dynamic_required:
        value = record[field]
        if type(value) is not str or not value.strip():
            raise ScrapeError(f"候选新站#{index}字段{field}必须为非空字符串")


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


def _validate_duplicate_options(options: DuplicateOptions) -> None:
    checks = {
        "periods": options.periods,
        "timeout": options.timeout,
        "workers": options.workers,
        "min_common": options.min_common,
        "duplicate_common": options.duplicate_common,
        "proxy_retries": options.proxy_retries,
    }
    if options.period is not None:
        checks["period"] = options.period
    for name, value in checks.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ScrapeError(f"{name} must be a positive integer")
    if not options.min_common <= options.duplicate_common <= options.periods:
        raise ScrapeError("require min_common <= duplicate_common <= periods")


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
        _validate_duplicate_options(options)
        if options.candidate_sites and options.periods != 10:
            raise ScrapeError("新增站点判重必须使用正好10期窗口，禁止使用其他periods")
        if options.update_backup:
            raise ScrapeError(
                "正式判重禁止更新 recent_10_cache.json；缓存只能由单期指定期流程更新"
            )
        if options.candidate_sites and not options.use_backup:
            raise ScrapeError(
                "新增候选站必须使用 --use-backup 读取近10期备份JSON，"
                "禁止实时全站抓取作为判重依据"
            )
        configured = ConfigRepository(options.sites_config).load()
        candidates = load_candidate_sites(options.candidate_sites)
        if options.candidate_sites and not candidates:
            raise ScrapeError("候选新站为空，拒收")
        disallowed_exceptions = [
            site.name
            for site in candidates
            if site.onboarding_exception in {
                "allow_insufficient_history",
                "allow_incomplete_backup",
            }
        ]
        if disallowed_exceptions:
            raise ScrapeError(
                "大围项目新增站点禁止历史不足或不完整缓存特例: "
                + ", ".join(disallowed_exceptions)
            )
        http_client.configure_proxy(options.proxy)
        backup = (
            duplicate_service.load_backup_snapshot(options.backup_path)
            if options.use_backup
            else None
        )
        if backup is not None:
            self._validate_backup_identity(backup, configured, options.backup_path)
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
        if backup and (backup.incomplete or backup.failures):
            failures.append(
                "近10期重复检测缓存不完整，禁止新增站点"
                if candidates
                else "近10期重复检测缓存不完整，禁止判重"
            )
            return self._empty_result(period, failures, auto_period)
        sites = selected_sites(
            options.only,
            candidates if options.use_backup else (*configured, *candidates),
        )
        if options.only and not sites:
            raise ScrapeError("no matching sites selected")
        text_cache = http_client.TextFetchCache(self._text_fetcher(options.proxy_retries))
        candidate_keys = {onboarding_service.site_key(site) for site in candidates}
        site_by_identity = {
            onboarding_service.site_key(site): site
            for site in (*configured, *candidates)
        }
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
                    options.periods
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
                except Exception as exc:  # noqa: BLE001 - isolate one site from the batch
                    error = exc if isinstance(exc, ScrapeError) else RuntimeError(
                        f"unexpected error: {exc}"
                    )
                    failures.append(format_failure(site, error, period))
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
                site = site_by_identity.get((window.name, window.url))
                if site is None:
                    failures.append(f"{window.name} {window.url} {exc}")
                else:
                    failures.append(format_failure(site, exc, period, stage="周期筛选"))
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
        return DuplicateRunResult(
            period,
            tuple(comparable),
            tuple(tuple(group) for group in groups),
            tuple(matches),
            tuple(blocking),
            tuple(failures),
            onboarding_error,
            auto_period=auto_period,
        )

    @staticmethod
    def _validate_backup_identity(
        backup: duplicate_service.BackupSnapshot,
        configured: tuple[SiteConfig, ...],
        backup_path: Path,
    ) -> None:
        """Reject a cache that cannot be proven to match current config."""
        if type(backup.periods) is not int or backup.periods != 10:
            raise ScrapeError(
                "新增站点判重要求备份缓存正好10期，"
                f"当前缓存窗口为{backup.periods}期"
            )
        repository = CacheRepository(backup_path)
        stored_fingerprint = repository.load_config_fingerprint()
        expected_fingerprint = config_fingerprint(configured)
        if stored_fingerprint is None:
            raise ScrapeError(
                f"缓存 {backup_path} 缺少配置指纹，无法证明与当前站点配置一致；请授权重建缓存"
            )
        if stored_fingerprint != expected_fingerprint:
            raise ScrapeError("近10期缓存配置指纹与当前正式配置不一致，判重已拒绝")
        by_id = {site.site_id: site for site in configured}
        if len(by_id) != len(configured):
            raise ScrapeError("当前正式配置存在重复site_id，判重已拒绝")
        if len(backup.sites) != len(configured):
            raise ScrapeError("近10期缓存站点数量与当前正式配置不一致，判重已拒绝")
        for cached in backup.sites:
            site = by_id.get(cached.site_id)
            if site is None:
                raise ScrapeError(f"缓存包含配置外站点: {cached.name}")
            if cached.name != site.name or normalize_url_identity(cached.url) != normalize_url_identity(site.url):
                raise ScrapeError(f"缓存站点身份与配置不一致: {cached.name}")
            expected_parser = site.parser_id
            if cached.parser_id != expected_parser:
                raise ScrapeError(f"缓存解析器与配置不一致: {cached.name}")
            if site.source_type == "dynamic_article" and any(
                record.record_id != site.record_id for record in cached.records
            ):
                raise ScrapeError(f"缓存动态文章record_id与配置不一致: {cached.name}")

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
                proxy_retries=proxy_retries,
            )

        def fetch(url: str, timeout: int) -> str:
            return http_client.fetch_text(url, timeout, raw_fetcher=raw_fetcher)

        return fetch
