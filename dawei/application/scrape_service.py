"""Single-site scrape orchestration without output or cache side effects."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
import re

from dawei.domain.errors import ScrapeError
from dawei.domain.models import (
    ArticleRecord,
    CandidateEvidence,
    CandidateOrigin,
    ParsedRecord,
    SiteConfig,
)
from dawei.domain.validation import validate_candidate_evidence
from dawei.infrastructure import browser_client, http_client, image_client, source_adapters
from dawei.parsers import DEFAULT_REGISTRY
from dawei.parsers import dynamic_article, generic_36, image_36
from dawei.parsers.generic_36 import attach_article_identity
from dawei.parsers.paginated_article import resolve_paginated_article
from dawei.parsers.registry import resolve_parser_id


TextFetcher = Callable[[str, int], str]
Parser = Callable[[str, SiteConfig], ParsedRecord]
RenderedTextFetcher = Callable[..., str]
RenderedArticleFetcher = Callable[..., ArticleRecord]
ImageScraper = Callable[[SiteConfig, int, int | None], ParsedRecord]
FailureSink = Callable[[SiteConfig, str], None]


@dataclass(frozen=True)
class ScrapeExecution:
    config: SiteConfig
    fixed_issue: int | None
    result: ParsedRecord | None
    document: str
    source_kind: str
    rendered: bool
    error: str = ""


def non_text_candidate_evidence(
    config: SiteConfig,
    issue: int,
    numbers: tuple[str, ...],
    source_url: str,
    page_index: int,
    source_method: str,
) -> CandidateEvidence:
    origin = CandidateOrigin(
        raw_issue_line=f"{issue}期",
        raw_number_lines=("图片解码36码: " + ",".join(numbers),),
        anchor_line=" ".join(config.section_keywords) or config.name,
        document_id=source_url,
        document_url=source_url,
        source_method=source_method,
        block_id=f"{source_url}#{issue}",
        block_start=page_index,
        block_end=page_index + 1,
        page_index=page_index,
        block_index=0,
        parser_id=config.parser_id,
    )
    return CandidateEvidence(
        issue=issue,
        numbers=numbers,
        raw_issue_line=origin.raw_issue_line,
        raw_number_lines=origin.raw_number_lines,
        anchor_line=origin.anchor_line,
        document_id=origin.document_id,
        document_url=origin.document_url,
        source_method=origin.source_method,
        block_id=origin.block_id,
        block_start=origin.block_start,
        block_end=origin.block_end,
        page_index=origin.page_index,
        block_index=origin.block_index,
        parser_id=origin.parser_id,
        origins=(origin,),
    )


def scrape_bb48kk_image_site(
    config: SiteConfig,
    timeout: int,
    fixed_issue: int | None,
    *,
    text_fetcher: TextFetcher = http_client.fetch_text,
    byte_fetcher: Callable[..., bytes] = http_client.fetch_bytes,
) -> ParsedRecord:
    expanded_html = source_adapters.fetch_expanded_html(config.url, timeout, text_fetcher)
    images = image_36.bb48kk_images_from(expanded_html)
    if not images:
        raise ScrapeError("no bb48kk image records found")
    indexed = list(enumerate(images))
    visible = [item for item in indexed if item[1][2]]
    eligible = visible or indexed
    if generic_36.candidate_region(config) == "top":
        direction_window = eligible[: generic_36.STRICT_RECENT_CANDIDATE_LIMIT]
    else:
        direction_window = eligible[-generic_36.STRICT_RECENT_CANDIDATE_LIMIT :]
    if fixed_issue is None:
        selected_issue = max(image[0] for _, image in direction_window)
    else:
        selected_issue = fixed_issue
        if not any(image[0] == fixed_issue for _, image in direction_window):
            issues = ", ".join(str(image[0]) for _, image in direction_window) or "无"
            raise ScrapeError(
                f"指定{fixed_issue}期超出图片严格候选范围；"
                f"仅允许按{generic_36.candidate_region(config)}方向最近3组内选择；"
                f"窗口期数: {issues}"
            )

    selected_entries = [entry for entry in eligible if entry[1][0] == selected_issue]
    return decode_bb48kk_issue_candidates(
        config,
        selected_entries,
        timeout,
        byte_fetcher=byte_fetcher,
    )


def decode_bb48kk_issue_candidates(
    config: SiteConfig,
    entries: list[tuple[int, tuple[int, str, bool]]],
    timeout: int,
    *,
    byte_fetcher: Callable[..., bytes] = http_client.fetch_bytes,
) -> ParsedRecord:
    if not entries:
        raise ScrapeError("没有可解码的bb48kk图片候选")
    visible = [entry for entry in entries if entry[1][2]]
    candidates = visible or entries
    decoded: list[CandidateEvidence] = []
    for page_index, (issue, image_url, _) in candidates:
        jpeg = image_36.decode_keyed_jpeg(byte_fetcher(image_url, timeout=timeout))
        try:
            numbers = image_36.extract_fixed_image_numbers(image_client.jpeg_to_bmp_bytes(jpeg))
            source_method = "image_fixed"
        except ScrapeError:
            numbers = image_client.ocr_bb48kk_image_numbers(jpeg)
            source_method = "image_ocr"
        decoded.append(
            non_text_candidate_evidence(
                config,
                issue,
                numbers,
                image_url,
                page_index,
                source_method,
            )
        )
    selected_issue = decoded[0].issue
    if len({candidate.numbers for candidate in decoded}) > 1:
        positions = ", ".join(str(candidate.page_index) for candidate in decoded)
        raise ScrapeError(
            f"{selected_issue}期图片候选36码冲突，拒绝写成功；候选位置: {positions}"
        )
    merged = generic_36.unique_candidates(decoded, config)
    selected = generic_36.select_candidate_for_position(merged, config)
    return replace(
        generic_36.parsed_record_from_candidate(config, selected),
        record_path=selected.document_url,
    )


def decode_tuku2135_issue_candidates(
    config: SiteConfig,
    entries: list[tuple[int, tuple[int, int, str]]],
    timeout: int,
    *,
    byte_fetcher: Callable[..., bytes] = http_client.fetch_bytes,
) -> ParsedRecord:
    if not entries:
        raise ScrapeError("没有可解码的2135图片候选")
    decoded: list[CandidateEvidence] = []
    for page_index, (issue, year, _) in entries:
        image_url = image_client.tuku2135_image_url(config, issue, year)
        image_bytes = byte_fetcher(
            image_url,
            timeout=timeout,
            extra_headers={"Referer": "https://aa.2135a.cc:1888/"},
        )
        numbers = image_client.ocr_2135_image_numbers(image_bytes)
        decoded.append(
            non_text_candidate_evidence(
                config,
                issue,
                numbers,
                image_url,
                page_index,
                "image_ocr",
            )
        )
    selected_issue = decoded[0].issue
    if len({candidate.numbers for candidate in decoded}) > 1:
        positions = ", ".join(str(candidate.page_index) for candidate in decoded)
        raise ScrapeError(
            f"{selected_issue}期图片候选36码冲突，拒绝写成功；候选位置: {positions}"
        )
    selected = generic_36.select_candidate_for_position(
        generic_36.unique_candidates(decoded, config),
        config,
    )
    return replace(
        generic_36.parsed_record_from_candidate(config, selected),
        record_path=selected.document_url,
    )


def scrape_tuku2135_image_site(
    config: SiteConfig,
    timeout: int,
    fixed_issue: int | None,
    *,
    byte_fetcher: Callable[..., bytes] = http_client.fetch_bytes,
) -> ParsedRecord:
    records = image_client.tuku2135_issue_records(config, timeout=timeout)
    indexed = list(enumerate(records))
    if generic_36.candidate_region(config) == "top":
        direction_window = indexed[: generic_36.STRICT_RECENT_CANDIDATE_LIMIT]
    else:
        direction_window = indexed[-generic_36.STRICT_RECENT_CANDIDATE_LIMIT :]
    if fixed_issue is None:
        selected_issue = max(record[0] for _, record in direction_window)
    else:
        selected_issue = fixed_issue
        if not any(record[0] == fixed_issue for _, record in direction_window):
            issues = ", ".join(str(record[0]) for _, record in direction_window) or "无"
            raise ScrapeError(
                f"指定{fixed_issue}期超出图片严格候选范围；"
                f"仅允许按{generic_36.candidate_region(config)}方向最近3组内选择；"
                f"窗口期数: {issues}"
            )
    return decode_tuku2135_issue_candidates(
        config,
        [entry for entry in indexed if entry[1][0] == selected_issue],
        timeout,
        byte_fetcher=byte_fetcher,
    )


def is_dynamic_article_site(config: SiteConfig) -> bool:
    return config.source_type == "dynamic_article" or bool(
        re.search(r"/article/(?:admin|manager|lottery)/[^/?#]+", config.url, re.IGNORECASE)
    )


def should_render_dynamic_fallback(config: SiteConfig, exc: BaseException) -> bool:
    if not is_dynamic_article_site(config):
        return False
    text = str(exc).lower()
    if "http" in text:
        return "404" in text
    return any(
        reason in text
        for reason in (
            "not valid json",
            "did not contain record content",
            "did not contain a list of records",
            "api空壳",
        )
    )


def default_parser(text_or_html: str, config: SiteConfig) -> ParsedRecord:
    parser_id = resolve_parser_id(config)
    return DEFAULT_REGISTRY.parse(text_or_html, replace(config, parser_id=parser_id))


def fetch_paginated_article_document(
    config: SiteConfig,
    timeout: int,
    fixed_issue: int | None,
    text_fetcher: TextFetcher,
) -> tuple[str, str]:
    documents = source_adapters.fetch_paginated_list_documents(
        config.url,
        timeout,
        text_fetcher,
    )
    target = resolve_paginated_article(documents, config, fixed_issue=fixed_issue)
    return text_fetcher(target.url, timeout), target.url


class ScrapeService:
    def __init__(
        self,
        *,
        text_fetcher: TextFetcher | None = None,
        parser: Parser | None = None,
        rendered_text_fetcher: RenderedTextFetcher | None = None,
        rendered_article_fetcher: RenderedArticleFetcher | None = None,
        image_scrapers: dict[str, ImageScraper] | None = None,
        failure_sink: FailureSink | None = None,
    ) -> None:
        self.text_fetcher = text_fetcher or http_client.fetch_text
        self.parser = parser or default_parser
        self.rendered_text_fetcher = rendered_text_fetcher or browser_client.fetch_rendered_text
        self.rendered_article_fetcher = (
            rendered_article_fetcher or browser_client.fetch_rendered_article_record
        )
        self.image_scrapers = image_scrapers or {
            "image_bb48kk": lambda config, timeout, issue: scrape_bb48kk_image_site(
                config,
                timeout,
                issue,
                text_fetcher=self.text_fetcher,
            ),
            "image_tuku2135": scrape_tuku2135_image_site,
        }
        self.failure_sink = failure_sink or (lambda config, document: None)

    def execute(
        self,
        config: SiteConfig,
        *,
        timeout: int = 20,
        fixed_issue: int | None = None,
    ) -> ScrapeExecution:
        documents: list[str] = []
        rendered = False

        def traced_parser(document: str, site: SiteConfig) -> ParsedRecord:
            documents.append(document)
            return self.parser(document, site)

        def traced_rendered_text(*args, **kwargs) -> str:
            nonlocal rendered
            rendered = True
            document = self.rendered_text_fetcher(*args, **kwargs)
            documents.append(document)
            return document

        def traced_rendered_article(*args, **kwargs) -> ArticleRecord:
            nonlocal rendered
            rendered = True
            article = self.rendered_article_fetcher(*args, **kwargs)
            documents.append(article.document)
            return article

        traced_service = ScrapeService(
            text_fetcher=self.text_fetcher,
            parser=traced_parser,
            rendered_text_fetcher=traced_rendered_text,
            rendered_article_fetcher=traced_rendered_article,
            image_scrapers=self.image_scrapers,
            failure_sink=self.failure_sink,
        )
        try:
            result = traced_service.scrape(
                config,
                timeout=timeout,
                fixed_issue=fixed_issue,
            )
        except ScrapeError as exc:
            return ScrapeExecution(
                config,
                fixed_issue,
                None,
                documents[-1] if documents else "",
                self._source_kind(config, rendered),
                rendered,
                str(exc) or f"{exc.__class__.__name__} 无详细异常消息",
            )
        return ScrapeExecution(
            config,
            fixed_issue,
            result,
            documents[-1] if documents else "",
            self._source_kind(config, rendered),
            rendered,
        )

    @staticmethod
    def _source_kind(config: SiteConfig, rendered: bool) -> str:
        if rendered:
            return "浏览器结构化兜底" if is_dynamic_article_site(config) else "浏览器兜底"
        if config.image_decoder:
            return "图片专属解析"
        if config.source_type == "paginated_article_list":
            return "分页文章专属解析"
        if config.api_url:
            return "专属API"
        return "页面/脚本展开正文"

    def scrape(
        self,
        config: SiteConfig,
        *,
        timeout: int = 20,
        fixed_issue: int | None = None,
    ) -> ParsedRecord:
        parser_id = resolve_parser_id(config)
        config = replace(config, parser_id=parser_id)
        image_scraper = self.image_scrapers.get(parser_id)
        if image_scraper is not None:
            return image_scraper(config, timeout, fixed_issue)
        if parser_id == "kunnan_magazine":
            return self._scrape_collection(config, timeout, fixed_issue)

        expected_keywords: tuple[str, ...] = ()
        rendered = False
        article: ArticleRecord | None = None
        source_document_url = config.url
        if config.source_type == "paginated_article_list":
            document, source_document_url = fetch_paginated_article_document(
                config,
                timeout,
                fixed_issue,
                self.text_fetcher,
            )
        elif config.api_url:
            try:
                payload = self.text_fetcher(config.api_url, timeout)
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
                expected_keywords = self._expected_keywords(config)
                article = self._render_article(
                    config,
                    timeout,
                    fixed_issue,
                    expected_keywords,
                )
                document = article.document
                rendered = True
        elif config.render_policy == "always" or config.render_browser:
            expected_keywords = self._expected_keywords(config)
            if is_dynamic_article_site(config):
                article = self._render_article(
                    config,
                    timeout,
                    fixed_issue,
                    expected_keywords,
                )
                document = article.document
            else:
                document = self.rendered_text_fetcher(
                    config.url,
                    timeout=timeout,
                    expected_issue=fixed_issue,
                    expected_keywords=expected_keywords,
                )
            rendered = True
        elif is_dynamic_article_site(config):
            expected_keywords = self._expected_keywords(config)
            article = self._render_article(
                config,
                timeout,
                fixed_issue,
                expected_keywords,
            )
            document = article.document
            rendered = True
        else:
            document = source_adapters.fetch_expanded_html(
                config.url,
                timeout,
                self.text_fetcher,
            )

        parse_config = replace(config, url=source_document_url)
        if fixed_issue is not None:
            parse_config = replace(parse_config, fixed_issue=fixed_issue)
        try:
            result = self._parse(
                document,
                parse_config,
                article,
                self._evidence_source_method(config, rendered),
            )
            return replace(result, url=config.url)
        except ScrapeError as first_error:
            if rendered:
                if is_dynamic_article_site(config):
                    article = self._render_article(
                        config,
                        timeout,
                        fixed_issue,
                        expected_keywords,
                        force_full_wait=True,
                    )
                    document = article.document
                else:
                    document = self.rendered_text_fetcher(
                        config.url,
                        timeout=timeout,
                        expected_issue=fixed_issue,
                        expected_keywords=expected_keywords,
                        force_full_wait=True,
                    )
                try:
                    return self._parse(
                        document,
                        parse_config,
                        article,
                        self._evidence_source_method(config, rendered),
                    )
                except ScrapeError:
                    self.failure_sink(config, document)
                    raise
            self.failure_sink(config, document)
            raise first_error

    def _scrape_collection(
        self,
        config: SiteConfig,
        timeout: int,
        fixed_issue: int | None,
    ) -> ParsedRecord:
        if not config.api_url:
            raise ScrapeError("困难杂志缺少专属 API 地址")
        records = dynamic_article.kunnan_magazine_records_from_payload(
            self.text_fetcher(config.api_url, timeout),
            config,
        )
        candidates = [record.evidence for record in records if record.evidence is not None]
        if len(candidates) != len(records):
            raise ScrapeError("困难杂志存在缺少CandidateEvidence的目标记录")
        if fixed_issue is None:
            selected = generic_36.select_latest_issue_candidate(candidates, config)
        else:
            exact = generic_36.exact_issue_candidates_for_selection(
                candidates,
                fixed_issue,
                config,
            )
            selected = generic_36.select_candidate_for_position(exact, config)
        return next(
            record
            for record in records
            if record.evidence == selected
        )

    def _parse(
        self,
        document: str,
        config: SiteConfig,
        article: ArticleRecord | None,
        source_method: str,
    ) -> ParsedRecord:
        result = self.parser(document, config)
        result = attach_article_identity(result, article, document, config) if article else result
        if result.evidence is None:
            raise ScrapeError("正式解析结果缺少CandidateEvidence，拒绝写成功")
        origins = tuple(
            replace(origin, source_method=source_method)
            for origin in result.evidence.origins
        )
        result = replace(
            result,
            evidence=replace(
                result.evidence,
                source_method=source_method,
                origins=origins,
            ),
        )
        try:
            validate_candidate_evidence(result)
        except Exception as exc:
            raise ScrapeError(f"正式解析候选证据无效: {exc}") from exc
        return result

    @staticmethod
    def _evidence_source_method(config: SiteConfig, rendered: bool) -> str:
        if rendered:
            return "browser_rendered"
        if config.api_url:
            return "structured_api"
        if config.source_type == "paginated_article_list":
            return "paginated_article"
        return "expanded_html"

    def _render_article(
        self,
        config: SiteConfig,
        timeout: int,
        fixed_issue: int | None,
        expected_keywords: Iterable[str],
        force_full_wait: bool = False,
    ) -> ArticleRecord:
        return self.rendered_article_fetcher(
            config,
            timeout=timeout,
            expected_issue=fixed_issue,
            expected_keywords=expected_keywords,
            force_full_wait=force_full_wait,
        )

    @staticmethod
    def _expected_keywords(config: SiteConfig) -> tuple[str, ...]:
        return (config.name, *config.keywords, *config.section_keywords)
