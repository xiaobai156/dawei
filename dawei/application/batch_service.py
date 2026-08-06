"""Single-issue batch orchestration, outputs, progress, and cache coordination."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from dawei.application.scrape_service import ScrapeService
from dawei.domain.errors import CacheConflictError, CacheRollbackError, ScrapeError
from dawei.domain.models import ParsedRecord, ScrapeRecord, SiteConfig, derive_site_id
from dawei.domain.validation import validate_36_numbers, validate_candidate_evidence
from dawei.infrastructure import http_client
from dawei.infrastructure.cache_repository import CacheRepository, atomic_write_text
from dawei.infrastructure.config_repository import ConfigRepository


DEFAULT_OUTPUT_DIR = Path(
    r"C:\Users\Administrator\Desktop\每天工具\爬虫合集\大围杀号生肖数据统一归纳"
)
DEFAULT_BACKUP_PATH = Path(__file__).resolve().parents[2] / "近10期重复检测备份.json"
CACHE_UPDATE_SUCCESS_PERCENT = 85


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
    merge_with: Path | None = None
    proxy_retries: int = 1


@dataclass(frozen=True)
class BatchRunResult:
    results: tuple[ParsedRecord, ...]
    failures: tuple[str, ...]
    timings: tuple[tuple[float, str, str], ...]
    total_sites: int
    cache_updated: bool = False

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0


SiteScraper = Callable[..., ParsedRecord]
ProgressSink = Callable[[str], None]


def default_output_path(issue: int) -> Path:
    return DEFAULT_OUTPUT_DIR / f"{issue}期-大围-成功.txt"


def default_error_output_path(issue: int) -> Path:
    return DEFAULT_OUTPUT_DIR / f"{issue}期-大围-失败.txt"


def cache_update_allowed(success_count: int, total_sites: int) -> bool:
    """Return whether the current run may update the recent cache.

    The threshold is deliberately strict: exactly 85% is not enough.
    Integer arithmetic keeps the boundary deterministic for any site count.
    """
    if total_sites < 0 or success_count < 0 or success_count > total_sites:
        raise ValueError("success_count and total_sites must satisfy 0 <= success <= total")
    return total_sites > 0 and success_count * 100 > total_sites * CACHE_UPDATE_SUCCESS_PERCENT


def classify_failure(exc: BaseException) -> str:
    text = str(exc).lower()
    if "unexpected error" in text:
        return "程序异常"
    if "198.18." in text or "198.19." in text or "dns" in text:
        return "DNS/线路失败"
    if "health check failed" in text:
        return "健康检测失败"
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


def format_failure(site: SiteConfig, exc: BaseException) -> str:
    detail = " ".join(str(exc).split()) or f"{exc.__class__.__name__} 无详细异常消息"
    return f"{site.name} {site.url} [{classify_failure(exc)}] {detail}"


def output_line(result: ParsedRecord) -> str:
    return f"{','.join(result.numbers)} {result.name}"


def write_results(results: Iterable[ParsedRecord], output_path: Path) -> None:
    lines = [output_line(result) for result in results]
    atomic_write_text(output_path, "\n".join(lines) + ("\n" if lines else ""))


def write_failures(failures: Iterable[str], output_path: Path) -> None:
    lines = list(failures)
    if not lines:
        output_path.unlink(missing_ok=True)
        return
    atomic_write_text(output_path, "\n\n".join(lines) + "\n")


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
    return [line.split(maxsplit=1)[0] for line in lines if line.strip()]


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


def validate_result(result: ParsedRecord, fixed_issue: int) -> list[str]:
    errors: list[str] = []
    if result.issue != fixed_issue:
        errors.append(f"{result.name} 抓错期数: 期望{fixed_issue}期，实际{result.issue}期")
    try:
        validate_36_numbers(result.numbers)
    except Exception as exc:
        errors.append(f"{result.name}: {exc}")
    try:
        validate_candidate_evidence(result)
    except Exception as exc:
        errors.append(f"{result.name}: {exc}")
    return errors


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
        sites = ConfigRepository(config_path).load()
        http_client.configure_proxy(proxy)
        names = [*only]
        if retry_failed is not None:
            names.extend(failure_site_names(retry_failed))
        chosen = selected_sites(names, sites)
        if names and not chosen:
            raise ScrapeError("no matching sites selected")
        configured_options = BatchOptions(
            **{
                **options.__dict__,
                "preserve_existing_failures": bool(names),
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
        if options.merge_with is not None:
            raise ScrapeError(
                "--merge-with 禁止用于正式成功数据；旧TXT缺少本次网页来源证据，只能人工对照"
            )
        site_list = tuple(sites)
        all_site_list = tuple(all_sites or site_list)
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
                    failures.append(format_failure(site, failure))
                    timings.append((elapsed, site.name, "失败"))
                    failure_count += 1
                else:
                    assert result is not None
                    errors = validate_result(result, options.fixed_issue)
                    if errors:
                        failures.extend(f"校验失败 {error}" for error in errors)
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
        write_results(results, options.output_path)
        write_failures(failures, options.error_output_path)
        cache_updated = False
        if options.update_recent_cache and cache_update_allowed(success_count, total):
            cache_updated = self._update_cache(results, failures, all_site_list, options)
        elif options.update_recent_cache:
            self.progress_sink(
                f"缓存未更新：成功 {success_count}/{total}，成功率未超过"
                f"{CACHE_UPDATE_SUCCESS_PERCENT}%"
            )
        return BatchRunResult(
            tuple(results),
            tuple(failures),
            tuple(timings),
            total,
            cache_updated=cache_updated,
        )

    def _default_scraper(self, options: BatchOptions) -> SiteScraper:
        def raw_fetcher(url: str, timeout: int, extra_headers=None):
            return http_client.fetch_raw(
                url,
                timeout,
                extra_headers,
                proxy_retries=max(1, options.proxy_retries),
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
                    proxy_retries=max(1, options.proxy_retries),
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
        except Exception as exc:
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
    ) -> bool:
        configured = {(site.name, site.url): site for site in sites}
        records = []
        for result in results:
            site = configured.get((result.name, result.url))
            records.append(
                ScrapeRecord(
                    site_id=site.site_id if site else derive_site_id(result.name, result.url),
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
                    parser_id=site.parser_id if site else "legacy",
                )
            )
        try:
            CacheRepository(options.recent_cache_path).update(
                records,
                failures,
                fixed_issue=options.fixed_issue,
                periods=options.recent_periods,
                preserve_existing_failures=options.preserve_existing_failures,
            )
        except CacheRollbackError:
            return False
        except CacheConflictError as exc:
            raise ScrapeError(str(exc)) from exc
        return True
