"""Explicit parser registration keyed only by persisted parser_id."""

from __future__ import annotations

import re
from collections.abc import Callable

from dawei.domain.errors import ConfigurationError, ScrapeError, ValidationError
from dawei.domain.models import CandidateEvidence, ParsedRecord, SiteConfig
from dawei.domain.validation import validate_candidate_evidence

Parser = Callable[[str, SiteConfig], ParsedRecord]
Candidate = CandidateEvidence
CandidateCollector = Callable[
    [str, SiteConfig],
    tuple[list[Candidate], list[tuple[int, int, str]], set[int]],
]


def extract_from_candidates(
    text_or_html: str,
    config: SiteConfig,
    candidate_collector: CandidateCollector,
    *,
    no_candidates_message: str,
    issue_range_error: str,
    include_seen_issue_list: bool = True,
    include_invalid_candidates: bool = True,
) -> ParsedRecord:
    """Apply the shared candidate selection contract after format-specific collection."""
    from dawei.parsers.generic_36 import (
        DEFAULT_ISSUE_MAX,
        DEFAULT_ISSUE_MIN,
        best_invalid_candidate_reason,
        exact_issue_candidates_for_selection,
        issue_in_range,
        parsed_record_from_candidate,
        select_candidate_for_position,
        select_latest_issue_candidate,
    )

    candidates, invalid_candidates, seen_matching_issues = candidate_collector(
        text_or_html,
        config,
    )
    if not candidates:
        if config.fixed_issue is not None:
            exact_invalid = [
                candidate
                for candidate in invalid_candidates
                if candidate[0] == config.fixed_issue
            ]
            if exact_invalid:
                reason = best_invalid_candidate_reason(exact_invalid)
                raise ScrapeError(f"{config.fixed_issue}期数据无效: {reason}")
            if config.fixed_issue in seen_matching_issues:
                raise ScrapeError(f"{config.fixed_issue}期没有找到完整36码数据")
            if include_seen_issue_list and seen_matching_issues:
                issues = ", ".join(
                    str(issue)
                    for issue in sorted(seen_matching_issues, reverse=True)[:8]
                )
                raise ScrapeError(
                    f"未找到指定{config.fixed_issue}期；页面可命中的期数: {issues}"
                )
        if include_invalid_candidates and invalid_candidates:
            issue, _, reason = max(
                invalid_candidates,
                key=lambda item: (item[0], item[1]),
            )
            raise ScrapeError(f"{issue}期数据无效: {reason}")
        raise ScrapeError(no_candidates_message)

    if config.fixed_issue is not None:
        exact = exact_issue_candidates_for_selection(
            candidates,
            config.fixed_issue,
            config,
        )
        if not exact:
            seen_valid = sorted({candidate.issue for candidate in candidates}, reverse=True)
            if seen_valid:
                issues = ", ".join(str(issue) for issue in seen_valid[:8])
                raise ScrapeError(
                    f"未找到指定{config.fixed_issue}期；可用有效期数: {issues}"
                )
            raise ScrapeError(
                f"no latest 36-number record found for issue {config.fixed_issue}"
            )
        return parsed_record_from_candidate(
            config,
            select_candidate_for_position(exact, config),
        )

    candidates = [
        candidate
        for candidate in candidates
        if issue_in_range(candidate.issue, DEFAULT_ISSUE_MIN, DEFAULT_ISSUE_MAX)
    ]
    if not candidates:
        raise ScrapeError(issue_range_error)
    return parsed_record_from_candidate(
        config,
        select_latest_issue_candidate(candidates, config),
    )

LEGACY_RECORD_ID_PARSERS = {
    "6a0445a74ea5c20141013e81": "renjianrenai",
    "6a3eb217018539c611cd17f5": "fengkuang_zhongma",
    "6a081bc1e0d076537e1df8aa": "three_rows",
    "6a09710b291caff3edcb8c25": "three_rows",
    "6a5117245e6c7637a3f4eb7a": "three_rows",
    "6a096aca291caff3edcb8b8c": "three_rows",
    "6a58fc58f447e21b02db9c97": "three_rows",
    "6a5eef5ef447e21b02dd548a": "three_rows",
    "6a155508d9d9fc2cea524219": "three_rows",
    "6a156eeb8be59b17287c6dce": "three_rows",
}
LEGACY_URL_PARSERS = {
    "https://nlafoq9v.dh5565656.xyz/bbs/topic.php?id=1065": "three_rows",
    "https://slyaoiw577.772149.shop/bbs/topic.php?id=1035": "three_rows",
    "https://slyaoiw577.772149.shop/bbs/topic.php?id=1050": "three_rows",
    "https://i97lk4jb16.333741bbs1.shop/bbs/topic.php?id=2700": "three_rows",
    "https://i97lk4jb16.333741bbs1.shop/bbs/topic.php?id=2690": "three_rows",
    "https://x1rvueyk50.669332.shop/bbs/topic.php?id=2652": "three_rows",
    "https://pxeeptca.oc0g1-jyj04-onsfnv.xyz:16677/topic/529475.html": "three_rows",
    "https://18118.73829.com/read.php?tid=957": "three_rows",
    "https://x1rvueyk50.669332.shop/bbs/topic.php?id=2549": "three_rows",
    "https://x1rvueyk50.669332.shop/bbs/topic.php?id=2757": "three_rows",
}
LEGACY_URL_FRAGMENTS = (
    ("topic/547563.html", "fenfatuqiang"),
    ("ocnrhq.du156-vb27w-tmhsed.xyz:16677", "xueqiu"),
    ("topic/238933.html", "yiyiba"),
    ("topic/459501.html", "xiongchumo"),
)


def resolve_parser_id(config: SiteConfig) -> str:
    if config.parser_id and config.parser_id != "legacy":
        return config.parser_id
    if config.source_type == "dynamic_collection" or "/users/3792/forums" in (config.api_url or ""):
        return "kunnan_magazine"
    record_id = config.record_id
    if not record_id:
        match = re.search(r"/article/(?:admin|manager|lottery)/([^/?#]+)", config.url)
        record_id = match.group(1) if match else None
    if record_id in LEGACY_RECORD_ID_PARSERS:
        return LEGACY_RECORD_ID_PARSERS[record_id]
    if config.url in LEGACY_URL_PARSERS:
        return LEGACY_URL_PARSERS[config.url]
    for fragment, parser_id in LEGACY_URL_FRAGMENTS:
        if fragment in config.url:
            return parser_id
    if "综合特码" in config.section_keywords:
        return "baoma_xuanji"
    if "澳门精准36码" in config.section_keywords:
        return "xiaoyuer"
    if "lokzmlcf.0jbc9-wavec-csoybc.xyz:16677" in config.url:
        if config.keywords == ("第",) and "36码围特" in config.section_keywords:
            return "zhuchiren_weite"
        if config.keywords == ("36码爆特",) and config.section_keywords == ("36码爆特",):
            return "zhuchiren_baote"
    return "generic_36"


class ParserRegistry:
    def __init__(self) -> None:
        self._parsers: dict[str, Parser] = {}
        self._candidate_collectors: dict[str, CandidateCollector] = {}

    def register(
        self,
        parser_id: str,
        parser: Parser,
        candidate_collector: CandidateCollector | None = None,
    ) -> None:
        key = parser_id.strip()
        if not key:
            raise ConfigurationError("parser_id不能为空")
        if key in self._parsers:
            raise ConfigurationError(f"parser_id重复注册: {key}")
        self._parsers[key] = parser
        if candidate_collector is not None:
            self._candidate_collectors[key] = candidate_collector

    def get(self, parser_id: str) -> Parser:
        try:
            return self._parsers[parser_id]
        except KeyError as exc:
            raise ConfigurationError(f"未注册parser_id: {parser_id}") from exc

    def parse(self, text_or_html: str, config: SiteConfig) -> ParsedRecord:
        result = self.get(config.parser_id)(text_or_html, config)
        try:
            validate_candidate_evidence(result)
        except ValidationError as exc:
            raise ScrapeError(
                f"{config.parser_id}正式解析结果证据校验失败: {exc}"
            ) from exc
        return result

    def candidate_collector(self, parser_id: str) -> CandidateCollector:
        try:
            collector = self._candidate_collectors[parser_id]
        except KeyError as exc:
            raise ConfigurationError(f"parser_id未注册多期候选解析器: {parser_id}") from exc

        def collect(text_or_html: str, config: SiteConfig):
            candidates, invalid, seen = collector(text_or_html, config)
            if any(not isinstance(candidate, CandidateEvidence) for candidate in candidates):
                raise ScrapeError(
                    f"{parser_id}候选解析器返回旧三元组，必须直接生成CandidateEvidence"
                )
            return candidates, invalid, seen

        return collect

    def has_candidate_collector(self, parser_id: str) -> bool:
        return parser_id in self._candidate_collectors

    @property
    def parser_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._parsers))
