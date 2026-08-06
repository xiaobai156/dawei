"""Test-only facade composed from the active V2 modules for regression coverage."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import time

from dawei.application import batch_service
from dawei.application.scrape_service import ScrapeService
from dawei.domain.errors import CacheConflictError, CacheRollbackError, ScrapeError
from dawei.domain.models import ArticleRecord, ParsedRecord as SiteResult, ScrapeRecord, SiteConfig
from dawei.infrastructure import browser_client, http_client, source_adapters
from dawei.infrastructure.cache_repository import CacheRepository
from dawei.infrastructure.config_repository import ConfigRepository
from dawei.parsers import DEFAULT_REGISTRY
from dawei.parsers import common, dynamic_article, generic_36, three_rows
from dawei.parsers.registry import resolve_parser_id


__all__ = ["ArticleRecord", "json"]


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SITES_PATH = PROJECT_ROOT / "sites_36.json"
DEFAULT_DUPLICATE_BACKUP_PATH = PROJECT_ROOT / "近10期重复检测备份.json"
DEFAULT_NETWORK_ATTEMPTS = http_client.DEFAULT_NETWORK_ATTEMPTS
PROXY_RETRIES = http_client.DEFAULT_PROXY_RETRIES
socket = http_client.socket
URLError = http_client.URLError

open_url_with_retries = http_client.open_url_with_retries
fetch_raw_with_curl = http_client.fetch_raw_with_curl
post_json_with_curl = http_client.post_json_with_curl
fetch_rendered_text = browser_client.fetch_rendered_text
fetch_rendered_article_record = browser_client.fetch_rendered_article_record
ReusableBrowserRenderer = browser_client.ReusableBrowserRenderer
TextFetchCache = http_client.TextFetchCache
decode_document_writeln_chunks = source_adapters.decode_document_writeln_chunks
candidate_context = generic_36.candidate_context
candidate_region = generic_36.candidate_region
section_ranges = common.section_ranges
candidate_foreign_strict_36_markers = common.candidate_foreign_strict_36_markers
collect_numbers_after_in_section = common.collect_numbers_after_in_section
select_candidate_for_position = generic_36.select_candidate_for_position
fenfatuqiang_candidates = three_rows.fenfatuqiang_candidates
kunnan_magazine_records_from_payload = dynamic_article.kunnan_magazine_records_from_payload
is_tls_error = http_client.is_tls_error
is_retryable_http_code = http_client.is_retryable_http_code
should_try_curl_fallback = http_client.should_try_curl_fallback
resolution_diagnostic = http_client.resolution_diagnostic
classify_failure = batch_service.classify_failure
format_failure = batch_service.format_failure
write_failures = batch_service.write_failures


def load_sites(path: Path = DEFAULT_SITES_PATH) -> tuple[SiteConfig, ...]:
    return ConfigRepository(path).load()


SITES = load_sites()
ONBOARDED_BBS_TOPIC_URLS = {
    site.name: site.url
    for site in SITES
    if resolve_parser_id(site) == "three_rows"
}


def fetch_raw(url: str, timeout: int = 20, extra_headers=None):
    return http_client.fetch_raw(
        url,
        timeout,
        extra_headers,
        open_url=open_url_with_retries,
        curl_fetcher=fetch_raw_with_curl,
        network_attempts=DEFAULT_NETWORK_ATTEMPTS,
        proxy_retries=PROXY_RETRIES,
    )


def post_json(url: str, payload: dict[str, object], timeout: int = 20):
    return http_client.post_json(
        url,
        payload,
        timeout,
        open_url=open_url_with_retries,
        curl_poster=post_json_with_curl,
        network_attempts=DEFAULT_NETWORK_ATTEMPTS,
        proxy_retries=PROXY_RETRIES,
    )


def extract_latest_36(text_or_html: str, config: SiteConfig) -> SiteResult:
    parser_id = resolve_parser_id(config)
    return DEFAULT_REGISTRY.parse(text_or_html, replace(config, parser_id=parser_id))


def scrape_site(
    config: SiteConfig,
    timeout: int = 20,
    fixed_issue: int | None = None,
    text_fetcher=http_client.fetch_text,
) -> SiteResult:
    service = ScrapeService(
        text_fetcher=text_fetcher,
        parser=extract_latest_36,
        rendered_text_fetcher=lambda *args, **kwargs: fetch_rendered_text(*args, **kwargs),
        rendered_article_fetcher=lambda *args, **kwargs: fetch_rendered_article_record(
            *args,
            **kwargs,
        ),
    )
    return service.scrape(config, timeout=timeout, fixed_issue=fixed_issue)


def timed_scrape_site_checked(
    site: SiteConfig,
    *,
    timeout: int,
    fixed_issue: int | None,
    health_check: bool,
    text_fetcher=http_client.fetch_text,
):
    started = time.perf_counter()
    try:
        if health_check:
            http_client.health_check_url(site.api_url or site.url, timeout=timeout)
        result = scrape_site(site, timeout, fixed_issue, text_fetcher)
        return result, time.perf_counter() - started, None
    except Exception as exc:
        return None, time.perf_counter() - started, exc


def is_onboarded_manager_article_config(config: SiteConfig) -> bool:
    return resolve_parser_id(config) == "three_rows"


def is_onboarded_bbs_topic_config(config: SiteConfig) -> bool:
    return ONBOARDED_BBS_TOPIC_URLS.get(config.name) == config.url


def onboarded_manager_api_payload_to_html(payload: str, config: SiteConfig) -> str:
    return source_adapters.article_record_from_payload(payload, config).document


def article_api_payload_to_html(payload: str, config: SiteConfig) -> str:
    return source_adapters.article_record_from_payload(payload, config).document


def output_line(result: SiteResult, include_issue: bool = True) -> str:
    line = f"{','.join(result.numbers)} {result.name}"
    return line + (f" {result.issue}期" if include_issue else "")


def validate_result(result: SiteResult, fixed_issue: int | None = None) -> list[str]:
    return batch_service.validate_result(result, fixed_issue or result.issue)


def result_to_backup_record(result: SiteResult) -> dict[str, object]:
    record: dict[str, object] = {"issue": result.issue, "numbers": list(result.numbers)}
    if result.record_id is not None:
        record["article_id"] = result.record_id
    if result.record_path is not None:
        record["source_path"] = result.record_path
    if result.raw_position is not None:
        record["raw_position"] = result.raw_position
    return record


def backup_record_to_result(name: str, url: str, record: dict[str, object]) -> SiteResult | None:
    issue = record.get("issue")
    values = record.get("numbers")
    if not isinstance(issue, int) or not isinstance(values, list):
        return None
    numbers = tuple(str(value).zfill(2) for value in values)
    if not common.valid_36_code_record(numbers):
        return None
    return SiteResult(
        name,
        url,
        issue,
        numbers,
        record_id=record.get("article_id") if isinstance(record.get("article_id"), str) else None,
        record_path=record.get("source_path") if isinstance(record.get("source_path"), str) else None,
        raw_position=record.get("raw_position") if isinstance(record.get("raw_position"), int) else None,
    )


def update_recent_duplicate_backup(
    results,
    failures,
    path: Path = DEFAULT_DUPLICATE_BACKUP_PATH,
    fixed_issue: int | None = None,
    periods: int = 10,
    preserve_existing_failures: bool = False,
) -> None:
    if fixed_issue is None:
        return
    configured = {(site.name, site.url): site for site in SITES}
    records = []
    for result in results:
        site = configured.get((result.name, result.url))
        records.append(
            ScrapeRecord(
                site_id=site.site_id if site else "test-site",
                name=result.name,
                url=result.url,
                issue=result.issue,
                numbers=result.numbers,
                record_id=result.record_id,
                source_path=result.record_path,
                raw_position=result.raw_position,
                parser_id=site.parser_id if site else "generic_36",
            )
        )
    try:
        CacheRepository(path).update(
            records,
            failures,
            fixed_issue=fixed_issue,
            periods=periods,
            preserve_existing_failures=preserve_existing_failures,
        )
    except CacheRollbackError:
        return
    except CacheConflictError as exc:
        raise ScrapeError(str(exc)) from exc
