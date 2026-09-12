"""Single-site scrape orchestration without output or cache side effects."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from urllib.parse import urljoin

from dawei.domain.errors import ScrapeError, ValidationError
from dawei.domain.models import (
    ArticleRecord,
    ParsedRecord,
    SiteConfig,
)
from dawei.domain.validation import validate_candidate_evidence
from dawei.infrastructure import (
    browser_client,
    http_client,
    image_client,
    source_adapters,
)
from dawei.infrastructure.timing import AuditTiming, timing_scope
from dawei.parsers import DEFAULT_REGISTRY, dynamic_article, generic_36
from dawei.parsers.generic_36 import attach_article_identity
from dawei.parsers.image_36 import resolve_image_url
from dawei.parsers.paginated_article import resolve_paginated_article
from dawei.parsers.registry import resolve_parser_id

TextFetcher = Callable[[str, int], str]
Parser = Callable[[str, SiteConfig], ParsedRecord]
RenderedTextFetcher = Callable[..., str]
RenderedArticleFetcher = Callable[..., ArticleRecord]
FailureSink = Callable[[SiteConfig, str], None]
HTML_BROWSER_FALLBACK_SOURCE_TYPES = frozenset({"generic_html", "topic_page"})


@dataclass(frozen=True)
class ScrapeExecution:
    config: SiteConfig
    fixed_issue: int | None
    result: ParsedRecord | None
    document: str
    source_kind: str
    rendered: bool
    error: str = ""
    stages: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class SourceLoadResult:
    """Raw source data shared by single-issue and window scraping."""

    document: str
    article: ArticleRecord | None
    source_url: str
    rendered: bool


def is_dynamic_article_site(config: SiteConfig) -> bool:
    return config.source_type == "dynamic_article" or bool(
        re.search(r"/article/(?:admin|manager|lottery)/[^/?#]+", config.url, re.IGNORECASE)
    )


def render_policy_allows_fallback(config: SiteConfig) -> bool:
    return (
        config.render_policy in {"fallback", "always"}
        or config.render_browser
        or config.source_type in HTML_BROWSER_FALLBACK_SOURCE_TYPES
    )


def should_render_api_fallback(config: SiteConfig, exc: BaseException) -> bool:
    if not render_policy_allows_fallback(config):
        return False
    text = str(exc).lower()
    return bool(re.search(r"\bhttp\s+404\b", text))


def should_render_api_record_fallback(config: SiteConfig, exc: BaseException) -> bool:
    if not render_policy_allows_fallback(config) or not is_dynamic_article_site(config):
        return False
    text = str(exc).lower()
    return any(
        reason in text
        for reason in (
            "api空壳: 未返回任何带记录id的文章",
            "api目标记录正文缺失",
        )
    )


def should_render_html_fallback(config: SiteConfig, exc: BaseException) -> bool:
    if not render_policy_allows_fallback(config):
        return False
    text = str(exc).lower()
    return ("未找到" in text and "专属栏目" in text) or any(
        reason in text
        for reason in (
            "未找到栏目",
            "没有找到符合",
            "未找到指定",
            "没有找到完整",
            "不完整",
            "解析失败",
            "专属页面身份缺失",
            "专属正文边界缺失",
            "no latest 36-number record",
            "no valid",
            "no record",
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


def load_browser_source(
    config: SiteConfig,
    timeout: int,
    *,
    rendered_text_fetcher: RenderedTextFetcher,
    rendered_article_fetcher: RenderedArticleFetcher,
) -> SourceLoadResult:
    """Load one source through the browser without applying business rules."""
    if is_dynamic_article_site(config):
        article = rendered_article_fetcher(config, timeout=timeout)
        return SourceLoadResult(article.document, article, config.url, True)
    document = rendered_text_fetcher(config.url, timeout=timeout)
    return SourceLoadResult(document, None, config.url, True)


def load_source(
    config: SiteConfig,
    timeout: int,
    *,
    fixed_issue: int | None = None,
    text_fetcher: TextFetcher,
    rendered_text_fetcher: RenderedTextFetcher,
    rendered_article_fetcher: RenderedArticleFetcher,
) -> SourceLoadResult:
    """Load raw source data; parsing and business validation stay with callers."""
    if config.parser_id == "image_tuku2135":
        parse_config = replace(config, fixed_issue=fixed_issue) if fixed_issue is not None else config
        if parse_config.fixed_issue is None:
            raise ScrapeError("六合王图片抓取必须指定期数，不扫描历史图片")
        payload = http_client.post_json(urljoin(config.url, "/api/qishu"), {"type": "am"}, timeout)
        image_url = resolve_image_url(payload, parse_config)
        data = http_client.fetch_bytes(image_url, timeout=timeout, extra_headers={"Referer": config.url})
        recognition = image_client.ocr_image(data)
        document = json.dumps({
            **recognition, "image_url": image_url, "image_sha256": hashlib.sha256(data).hexdigest(),
        }, ensure_ascii=False)
        return SourceLoadResult(document, None, config.url, False)
    if config.source_type == "paginated_article_list":
        document, source_url = fetch_paginated_article_document(
            config,
            timeout,
            fixed_issue,
            text_fetcher,
        )
        return SourceLoadResult(document, None, source_url, False)

    if config.api_url:
        api_fallback = False
        try:
            payload = text_fetcher(config.api_url, timeout)
        except ScrapeError as exc:
            if not should_render_api_fallback(config, exc):
                raise
            api_fallback = True

        if not api_fallback:
            try:
                if is_dynamic_article_site(config):
                    article = source_adapters.article_record_from_payload(
                        payload,
                        config,
                        source_path=config.api_url,
                    )
                    return SourceLoadResult(article.document, article, config.url, False)
                document = source_adapters.api_payload_to_html(
                    payload,
                    allow_multiple=False,
                )
                return SourceLoadResult(document, None, config.url, False)
            except ScrapeError as exc:
                if not should_render_api_record_fallback(config, exc):
                    raise
                api_fallback = True

        if api_fallback:
            return load_browser_source(
                config,
                timeout,
                rendered_text_fetcher=rendered_text_fetcher,
                rendered_article_fetcher=rendered_article_fetcher,
            )

    if config.render_policy == "always" or config.render_browser:
        return load_browser_source(
            config,
            timeout,
            rendered_text_fetcher=rendered_text_fetcher,
            rendered_article_fetcher=rendered_article_fetcher,
        )
    return SourceLoadResult(text_fetcher(config.url, timeout), None, config.url, False)


class ScrapeService:
    def __init__(
        self,
        *,
        text_fetcher: TextFetcher | None = None,
        parser: Parser | None = None,
        rendered_text_fetcher: RenderedTextFetcher | None = None,
        rendered_article_fetcher: RenderedArticleFetcher | None = None,
        failure_sink: FailureSink | None = None,
    ) -> None:
        self.text_fetcher = text_fetcher or http_client.fetch_text
        self.parser = parser or default_parser
        self.rendered_text_fetcher = rendered_text_fetcher or browser_client.fetch_rendered_text
        self.rendered_article_fetcher = (
            rendered_article_fetcher or browser_client.fetch_rendered_article_record
        )
        self.failure_sink = failure_sink or (lambda config, document: None)

    def execute(
        self,
        config: SiteConfig,
        *,
        timeout: int = 20,
        fixed_issue: int | None = None,
        timing: AuditTiming | None = None,
    ) -> ScrapeExecution:
        documents: list[str] = []
        rendered = False
        stage_times: dict[str, float] = {}

        def add_stage(name: str, started: float) -> None:
            stage_times[name] = stage_times.get(name, 0.0) + (time.perf_counter() - started)

        def timed_text_fetcher(*args, **kwargs) -> str:
            started = time.perf_counter()
            try:
                return self.text_fetcher(*args, **kwargs)
            finally:
                add_stage("fetch", started)

        def traced_parser(document: str, site: SiteConfig) -> ParsedRecord:
            documents.append(document)
            started = time.perf_counter()
            try:
                return self.parser(document, site)
            finally:
                add_stage("parse", started)

        def traced_rendered_text(*args, **kwargs) -> str:
            nonlocal rendered
            rendered = True
            started = time.perf_counter()
            try:
                document = self.rendered_text_fetcher(*args, **kwargs)
            finally:
                add_stage("browser", started)
            documents.append(document)
            return document

        def traced_rendered_article(*args, **kwargs) -> ArticleRecord:
            nonlocal rendered
            rendered = True
            started = time.perf_counter()
            try:
                article = self.rendered_article_fetcher(*args, **kwargs)
            finally:
                add_stage("browser", started)
            documents.append(article.document)
            return article

        traced_service = ScrapeService(
            text_fetcher=timed_text_fetcher,
            parser=traced_parser,
            rendered_text_fetcher=traced_rendered_text,
            rendered_article_fetcher=traced_rendered_article,
            failure_sink=self.failure_sink,
        )
        started = time.perf_counter()
        try:
            with timing_scope(timing):
                result = traced_service.scrape(
                    config,
                    timeout=timeout,
                    fixed_issue=fixed_issue,
                )
            error = ""
        except ScrapeError as exc:
            result = None
            error = str(exc) or f"{exc.__class__.__name__} 无详细异常消息"
        add_stage("total", started)
        stages = tuple(
            (name, stage_times[name])
            for name in ("fetch", "parse", "browser", "total")
            if name in stage_times
        )
        if timing is not None:
            for name, seconds in stages:
                timing.add(name, seconds)
        return ScrapeExecution(
            config,
            fixed_issue,
            result,
            documents[-1] if documents else "",
            self._source_kind(config, rendered),
            rendered,
            error,
            stages,
        )

    @staticmethod
    def _source_kind(config: SiteConfig, rendered: bool) -> str:
        if config.parser_id == "image_tuku2135":
            return "图片OCR"
        if rendered:
            return "浏览器结构化兜底" if is_dynamic_article_site(config) else "浏览器兜底"
        if config.source_type == "paginated_article_list":
            return "分页文章专属解析"
        if config.api_url:
            return "专属API"
        return "HTTP页面正文"

    def scrape(
        self,
        config: SiteConfig,
        *,
        timeout: int = 20,
        fixed_issue: int | None = None,
    ) -> ParsedRecord:
        parser_id = resolve_parser_id(config)
        config = replace(config, parser_id=parser_id)
        if parser_id == "kunnan_magazine":
            return self._scrape_collection(config, timeout, fixed_issue)

        source = load_source(
            config,
            timeout,
            fixed_issue=fixed_issue,
            text_fetcher=self.text_fetcher,
            rendered_text_fetcher=self.rendered_text_fetcher,
            rendered_article_fetcher=self.rendered_article_fetcher,
        )
        document = source.document
        article = source.article
        rendered = source.rendered

        parse_config = replace(config, url=source.source_url)
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
        except ScrapeError as exc:
            if (
                not rendered
                and parser_id != "image_tuku2135"
                and not config.api_url
                and config.source_type != "paginated_article_list"
                and should_render_html_fallback(config, exc)
            ):
                source = load_browser_source(
                    config,
                    timeout,
                    rendered_text_fetcher=self.rendered_text_fetcher,
                    rendered_article_fetcher=self.rendered_article_fetcher,
                )
                document = source.document
                article = source.article
                rendered = source.rendered
                try:
                    return replace(
                        self._parse(
                            document,
                            parse_config,
                            article,
                            self._evidence_source_method(config, rendered),
                        ),
                        url=config.url,
                    )
                except ScrapeError:
                    self.failure_sink(config, document)
                    raise
            if rendered:
                self.failure_sink(config, document)
                raise
            self.failure_sink(config, document)
            raise

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
            latest_issue = max(candidate.issue for candidate in candidates)
            exact = generic_36.exact_issue_candidates_for_selection(
                candidates,
                latest_issue,
                config,
            )
            selected = generic_36.select_candidate_for_position(exact, config)
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
        if article is not None:
            dynamic_article.validate_article_identity(article, config)
        result = self.parser(document, config)
        if result.name != config.name:
            raise ScrapeError("正式解析结果站名与配置不一致")
        if result.url != config.url:
            raise ScrapeError("正式解析结果URL与配置不一致")
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
        except ValidationError as exc:
            raise ScrapeError(f"正式解析候选证据无效: {exc}") from exc
        return result

    @staticmethod
    def _evidence_source_method(config: SiteConfig, rendered: bool) -> str:
        if config.parser_id == "image_tuku2135":
            return "image_ocr"
        if rendered:
            return "browser_rendered"
        if config.api_url:
            return "structured_api"
        if config.source_type == "paginated_article_list":
            return "paginated_article"
        return "http_html"
