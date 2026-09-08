"""Single-issue batch orchestration, outputs, progress, and cache coordination."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dawei.application.scrape_service import ScrapeService
from dawei.domain.errors import (
    CacheConflictError,
    CacheError,
    CacheRollbackError,
    ScrapeError,
    ValidationError,
)
from dawei.domain.models import ParsedRecord, ScrapeRecord, SiteConfig
from dawei.domain.validation import validate_36_numbers, validate_candidate_evidence
from dawei.infrastructure import http_client
from dawei.infrastructure.cache_repository import CacheRepository, ProcessFileLock, atomic_write_text
from dawei.infrastructure.config_repository import (
    ConfigRepository,
    config_fingerprint,
    normalize_url_identity,
)

DEFAULT_OUTPUT_DIR = Path(
    r"C:\Users\Administrator\Desktop\每天工具\爬虫合集\大围杀号生肖数据统一归纳"
)
DEFAULT_BACKUP_PATH = Path(__file__).resolve().parents[2] / "recent_10_cache.json"


@dataclass(frozen=True)
class BatchOptions:
    fixed_issue: int
    output_path: Path
    error_output_path: Path
    timeout: int = 20
    workers: int = 8
    health_check: bool = False
    cache_dir: Path | None = None
    update_recent_cache: bool = True
    recent_cache_path: Path = DEFAULT_BACKUP_PATH
    recent_periods: int = 10
    preserve_existing_failures: bool = False
    append_success: bool = False
    merge_with: Path | None = None
    proxy_retries: int = 1


@dataclass(frozen=True)
class BatchRunResult:
    results: tuple[ParsedRecord, ...]
    failures: tuple[str, ...]
    timings: tuple[tuple[float, str, str], ...]
    total_sites: int
    cache_updated: bool = False
    cache_error: str = ""

    @property
    def exit_code(self) -> int:
        return 1 if self.failures or self.cache_error else 0


SiteScraper = Callable[..., ParsedRecord]
ProgressSink = Callable[[str], None]
def output_lock(path: Path) -> ProcessFileLock:
    return ProcessFileLock(path.with_name(f".{path.name}.lock"))


def default_output_path(issue: int) -> Path:
    return DEFAULT_OUTPUT_DIR / f"{issue}期-大围-成功.txt"


def default_error_output_path(issue: int) -> Path:
    return DEFAULT_OUTPUT_DIR / f"{issue}期-大围-失败.txt"


def classify_failure(exc: BaseException) -> str:
    text = str(exc).lower()
    if "unexpected error" in text:
        return "程序异常"
    if "198.18." in text or "198.19." in text or "dns" in text:
        return "DNS/线路失败"
    if "health check failed" in text:
        return "健康检测失败"
    if "校验失败" in text:
        return "校验失败"
    if "tls" in text or "ssl" in text or "handshake" in text:
        return "TLS失败"
    if "http " in text:
        return "HTTP失败"
    if "timeout" in text or "timed out" in text:
        return "超时"
    if "未找到栏目关键词" in text or "没有找到符合关键词" in text or (
        "未找到" in text and "专属栏目" in text
    ):
        return "栏目/关键词解析失败"
    if "多个高可信候选" in text or "冲突" in text:
        return "候选冲突失败"
    if "不是完整36码" in text or "does not contain 36 numbers" in text or "got " in text:
        return "36码数量失败"
    if "未找到指定" in text or "no latest 36-number record found" in text:
        return "指定期数缺失"
    if "no record" in text or "no valid" in text or "expected" in text or "invalid" in text or "期" in text:
        return "解析失败"
    if "network" in text:
        return "网络失败"
    return f"未分类失败/{exc.__class__.__name__}"


def format_failure(site: SiteConfig, exc: BaseException, fixed_issue: int) -> str:
    detail = " ".join(str(exc).split()) or f"{exc.__class__.__name__} 无详细异常消息"
    return (
        f"{site.name} {site.url} [{classify_failure(exc)}] "
        f"方向:{site.direction}；指定期数:{fixed_issue}期；原因:{detail}"
    )


def output_line(result: ParsedRecord) -> str:
    return f"{','.join(result.numbers)} {result.name}"


def write_results(results: Iterable[ParsedRecord], output_path: Path) -> None:
    lines = [output_line(result) for result in results]
    atomic_write_text(output_path, "\n".join(lines) + ("\n" if lines else ""))


def append_results(results: Iterable[ParsedRecord], output_path: Path) -> None:
    lines = [output_line(result) for result in results]
    if not lines:
        return
    with output_lock(output_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        existing_text = output_path.read_text(encoding="utf-8-sig") if output_path.exists() else ""
        existing = set(existing_text.splitlines())
        lines = [line for line in lines if line not in existing]
        if not lines:
            return
        separator = "" if not existing_text or existing_text.endswith(("\n", "\r")) else "\n"
        encoding = "utf-8" if output_path.exists() else "utf-8-sig"
        with output_path.open("ab") as target:
            target.write((separator + "\n".join(lines) + "\n").encode(encoding))


def write_failures(failures: Iterable[str], output_path: Path) -> None:
    lines = list(failures)
    if not lines:
        output_path.unlink(missing_ok=True)
        return
    with output_lock(output_path):
        atomic_write_text(output_path, "\n\n".join(lines) + "\n")


def read_failure_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8-sig").splitlines()


def parse_failure_identity(line: str) -> tuple[str, str] | None:
    match = re.match(r"^(.*?)\s+(https?://\S+)(?:\s|$)", line)
    if not match:
        return None
    return match.group(1), normalize_url_identity(match.group(2))


def failure_site_keys(path: Path) -> tuple[tuple[str, str], ...]:
    keys = []
    for line in read_failure_lines(path):
        identity = parse_failure_identity(line)
        if identity is None:
            continue
        keys.append(identity)
    return tuple(dict.fromkeys(keys))


def read_issue_failures(issue: int, output_dir: Path) -> dict[tuple[str, str], str]:
    path = output_dir / f"{issue}期-大围-失败.txt"
    if not path.exists():
        return {}
    failures: dict[tuple[str, str], str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) == 3:
            failures[(parts[0], parts[1])] = parts[2]
    return failures


def write_multi_failure_summary(issues: Iterable[int], output_dir: Path) -> Path:
    issue_list = tuple(dict.fromkeys(issues))
    if not issue_list:
        raise ScrapeError("no issues provided")
    by_issue = {issue: read_issue_failures(issue, output_dir) for issue in issue_list}
    common = set(by_issue[issue_list[0]])
    for issue in issue_list[1:]:
        common &= set(by_issue[issue])
    lines = [
        f"多期全部失败目录汇总：{', '.join(str(issue) + '期' for issue in issue_list)}",
        f"判定规则：只列所有输入期数都失败的目录，共 {len(common)} 个",
        "",
    ]
    for index, key in enumerate(sorted(common, key=lambda item: item[0]), start=1):
        name, url = key
        lines.append(f"{index}. {name} {url}")
        lines.extend(f"   {issue}期: {by_issue[issue][key]}" for issue in issue_list)
        lines.append("")
    path = output_dir / f"多期-{'_'.join(str(issue) for issue in issue_list)}-大围-全部失败汇总.txt"
    atomic_write_text(path, "\n".join(lines).rstrip() + "\n")
    return path


def failure_site_names(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ScrapeError(f"failed to read failed-site file {path}: {exc}") from exc
    names = []
    for line in lines:
        identity = parse_failure_identity(line)
        if identity:
            names.append(identity[0])
    return names


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


def validate_result(
    result: ParsedRecord,
    fixed_issue: int,
    expected_site: SiteConfig | None = None,
) -> list[str]:
    errors: list[str] = []
    if expected_site is not None:
        if result.name != expected_site.name:
            errors.append(
                f"{expected_site.name} 返回站点身份错误: 结果名称为{result.name!r}"
            )
        if normalize_url_identity(result.url) != normalize_url_identity(expected_site.url):
            errors.append(
                f"{expected_site.name} 返回URL身份错误: 结果URL为{result.url!r}"
            )
    if result.issue != fixed_issue:
        errors.append(f"{result.name} 抓错期数: 期望{fixed_issue}期，实际{result.issue}期")
    try:
        validate_36_numbers(result.numbers)
    except ValidationError as exc:
        errors.append(f"{result.name}: {exc}")
    try:
        validate_candidate_evidence(result)
    except ValidationError as exc:
        errors.append(f"{result.name}: {exc}")
    return errors


def _validate_batch_options(options: BatchOptions) -> None:
    checks = {
        "fixed_issue": options.fixed_issue,
        "timeout": options.timeout,
        "workers": options.workers,
        "proxy_retries": options.proxy_retries,
        "recent_periods": options.recent_periods,
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in checks.values()
    ):
        invalid = next(name for name, value in checks.items() if (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ))
        raise ScrapeError(f"{invalid} must be a positive integer")


class SingleIssueBatchService:
    def __init__(
        self,
        *,
        site_scraper: SiteScraper | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> None:
        self.site_scraper = site_scraper
        self.progress_sink = progress_sink or print

    def run_configured(
        self,
        config_path: Path,
        options: BatchOptions,
        *,
        only: Iterable[str] = (),
        retry_failed: Path | None = None,
        proxy: str | None = None,
    ) -> BatchRunResult:
        _validate_batch_options(options)
        paths = {p.resolve().as_posix().casefold() for p in (options.output_path, options.error_output_path, config_path, options.recent_cache_path)}
        if len(paths) != 4:
            raise ScrapeError("成功TXT、失败TXT、配置和缓存路径不能重合")
        sites = ConfigRepository(config_path).load()
        http_client.configure_proxy(proxy)
        names = [*only]
        if retry_failed is not None:
            if not read_failure_lines(retry_failed):
                raise ScrapeError(f"失败报告为空，拒绝回退全站重抓: {retry_failed}")
            if retry_failed.resolve() != options.error_output_path.resolve():
                raise ScrapeError("失败报告必须与当前期输出文件绑定")
            failed_keys = set(failure_site_keys(retry_failed))
            if names:
                selected_keys = {
                    (site.name, normalize_url_identity(site.url))
                    for site in sites
                    if site.name in set(names)
                }
                failed_keys &= selected_keys
                if not failed_keys:
                    raise ScrapeError("--only 与失败报告没有交集，拒绝扩大重试范围")
            names = [name for name, _ in failed_keys]
        chosen = selected_sites(names, sites)
        if retry_failed is not None:
            wanted = set(failure_site_keys(retry_failed))
            chosen = tuple(
                site for site in sites
                if (site.name, normalize_url_identity(site.url)) in wanted
                and (not only or site.name in set(only))
            )
        if names and not chosen:
            raise ScrapeError("no matching sites selected")
        configured_options = BatchOptions(
            **{
                **options.__dict__,
                "preserve_existing_failures": bool(names),
                "append_success": bool(names),
            }
        )
        return self.run(chosen, configured_options, all_sites=sites)

    def run(
        self,
        sites: Iterable[SiteConfig],
        options: BatchOptions,
        *,
        all_sites: Iterable[SiteConfig] | None = None,
    ) -> BatchRunResult:
        _validate_batch_options(options)
        if options.merge_with is not None:
            raise ScrapeError(
                "--merge-with 禁止用于正式成功数据；旧TXT缺少本次网页来源证据，只能人工对照"
            )
        site_list = tuple(sites)
        if not site_list:
            raise ScrapeError("no sites provided")
        all_site_list = tuple(all_sites or site_list)
        site_keys = {(site.name, normalize_url_identity(site.url)) for site in site_list}
        all_site_keys = {
            (site.name, normalize_url_identity(site.url)) for site in all_site_list
        }
        subset_run = len(site_list) != len(all_site_list) or site_keys != all_site_keys
        scraper = self.site_scraper or self._default_scraper(options)
        workers = max(1, min(options.workers, len(site_list) or 1))
        total = len(site_list)
        self.progress_sink(
            f"开始抓取：单期模式，仅抓取指定{options.fixed_issue}期；"
            f"共 {total} 个站点，并发 {workers}"
        )
        started = time.perf_counter()
        results_by_index: dict[int, ParsedRecord] = {}
        failures: list[str] = []
        timings: list[tuple[float, str, str]] = []
        success_count = 0
        failure_count = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self._timed_scrape, scraper, site, options): (index, site)
                for index, site in enumerate(site_list)
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                index, site = futures[future]
                result, elapsed, error = future.result()
                if error is not None:
                    failure = error if isinstance(error, ScrapeError) else RuntimeError(
                        f"unexpected error: {error}"
                    )
                    failures.append(format_failure(site, failure, options.fixed_issue))
                    timings.append((elapsed, site.name, "失败"))
                    failure_count += 1
                else:
                    assert result is not None
                    errors = validate_result(result, options.fixed_issue, site)
                    if errors:
                        failures.append(
                            format_failure(
                                site,
                                ScrapeError("校验失败：" + "；".join(errors)),
                                options.fixed_issue,
                            )
                        )
                        timings.append((elapsed, site.name, "失败"))
                        failure_count += 1
                    else:
                        results_by_index[index] = result
                        timings.append((elapsed, site.name, "成功"))
                        success_count += 1
                percent = int(completed * 100 / total) if total else 100
                self.progress_sink(
                    f"[进度 {completed}/{total} {percent}% 成功 {success_count} "
                    f"失败 {failure_count} 用时 {time.perf_counter() - started:.1f}s] 当前: {site.name}"
                )
        results = [results_by_index[index] for index in sorted(results_by_index)]
        if options.append_success or subset_run:
            append_results(results, options.output_path)
        else:
            write_results(results, options.output_path)
        if options.preserve_existing_failures:
            processed = {
                (site.name, normalize_url_identity(site.url))
                for site in site_list
            }
            retained = []
            for line in read_failure_lines(options.error_output_path):
                identity = parse_failure_identity(line)
                if identity and identity in processed:
                    continue
                retained.append(line)
            write_failures([*retained, *failures], options.error_output_path)
        else:
            write_failures(failures, options.error_output_path)
        cache_updated = False
        cache_error = ""
        if options.update_recent_cache:
            try:
                cache_updated = self._update_cache(
                    results,
                    failures,
                    all_site_list,
                    options,
                    subset_run=subset_run,
                )
            except (CacheError, OSError, ScrapeError) as exc:
                cache_error = "缓存更新未完成: " + (" ".join(str(exc).split()) or exc.__class__.__name__)
                self.progress_sink(cache_error)
        return BatchRunResult(
            tuple(results),
            tuple(failures),
            tuple(timings),
            total,
            cache_updated=cache_updated,
            cache_error=cache_error,
        )

    def _default_scraper(self, options: BatchOptions) -> SiteScraper:
        def raw_fetcher(url: str, timeout: int, extra_headers=None):
            return http_client.fetch_raw(
                url,
                timeout,
                extra_headers,
                proxy_retries=options.proxy_retries,
            )

        def text_fetcher(url: str, timeout: int) -> str:
            return http_client.fetch_text(url, timeout, raw_fetcher=raw_fetcher)

        cache = http_client.TextFetchCache(text_fetcher)

        def failure_sink(config: SiteConfig, document: str) -> None:
            if options.cache_dir is None:
                return
            path = options.cache_dir / str(options.fixed_issue) / f"{self._safe_name(config.name)}.html"
            atomic_write_text(path, document)

        service = ScrapeService(text_fetcher=cache.fetch, failure_sink=failure_sink)

        def scrape(config: SiteConfig, *, timeout: int, fixed_issue: int | None):
            if options.health_check:
                http_client.health_check_url(
                    config.api_url or config.url,
                    timeout=timeout,
                    proxy_retries=options.proxy_retries,
                )
            return service.scrape(config, timeout=timeout, fixed_issue=fixed_issue)

        return scrape

    @staticmethod
    def _timed_scrape(
        scraper: SiteScraper,
        site: SiteConfig,
        options: BatchOptions,
    ) -> tuple[ParsedRecord | None, float, Exception | None]:
        started = time.perf_counter()
        try:
            result = scraper(
                site,
                timeout=options.timeout,
                fixed_issue=options.fixed_issue,
            )
            return result, time.perf_counter() - started, None
        except Exception as exc:  # noqa: BLE001 - isolate one site from the batch
            return None, time.perf_counter() - started, exc

    @staticmethod
    def _safe_name(value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip().strip(".")
        return cleaned or "site"

    @staticmethod
    def _update_cache(
        results: Iterable[ParsedRecord],
        failures: Iterable[str],
        sites: Iterable[SiteConfig],
        options: BatchOptions,
        *,
        subset_run: bool = False,
    ) -> bool:
        site_list = tuple(sites)
        configured = {
            (site.name, normalize_url_identity(site.url)): site
            for site in site_list
        }
        records = []
        for result in results:
            site = configured.get((result.name, normalize_url_identity(result.url)))
            if site is None:
                raise ScrapeError(
                    f"缓存更新拒绝未知站点身份: {result.name} {result.url}"
                )
            records.append(
                ScrapeRecord(
                    site_id=site.site_id,
                    name=result.name,
                    url=result.url,
                    issue=result.issue,
                    numbers=result.numbers,
                    record_id=result.record_id,
                    source_path=(
                        result.record_path
                        or (
                            f"{result.evidence.document_url}::{result.evidence.document_id}"
                            if result.evidence is not None
                            else None
                        )
                    ),
                    raw_position=(
                        result.raw_position
                        if result.raw_position is not None
                        else (
                            result.evidence.page_index
                            if result.evidence is not None
                            else None
                        )
                    ),
                    parser_id=site.parser_id,
                )
            )
        repository = CacheRepository(options.recent_cache_path)
        if subset_run:
            snapshot = repository.load()
            if snapshot.period != options.fixed_issue:
                raise ScrapeError(
                    "子集运行禁止推进缓存：仅允许在缓存已有相同期数 "
                    f"{options.fixed_issue} 期时修补，当前缓存期数为{snapshot.period!r}"
                )
        try:
            fingerprint = config_fingerprint(site_list)
            expected_site_identities = {
                site.site_id: (
                    site.name,
                    site.url,
                    site.parser_id,
                    site.record_id if site.source_type == "dynamic_article" else None,
                )
                for site in site_list
            }
            repository.update(
                records,
                failures,
                fixed_issue=options.fixed_issue,
                periods=options.recent_periods,
                preserve_existing_failures=options.preserve_existing_failures,
                config_fingerprint=fingerprint,
                expected_site_identities=expected_site_identities,
                allow_missing_fingerprint_binding=not subset_run,
                preserve_site_order=subset_run,
            )
        except CacheRollbackError:
            raise ScrapeError("缓存更新未完成: 目标期数早于缓存最新期，拒绝回滚")
        except CacheConflictError as exc:
            raise ScrapeError(str(exc)) from exc
        return True
