"""Explicit parser registration keyed only by persisted parser_id."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import re

from dawei.domain.errors import ConfigurationError, ScrapeError
from dawei.domain.models import CandidateEvidence, ParsedRecord, SiteConfig


Parser = Callable[[str, SiteConfig], ParsedRecord]
Candidate = CandidateEvidence
CandidateCollector = Callable[
    [str, SiteConfig],
    tuple[list[Candidate], list[tuple[int, int, str]], set[int]],
]

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
    if config.image_decoder == "bb48kk_fixed":
        return "image_bb48kk"
    if config.image_decoder == "tuku2135_ocr":
        return "image_tuku2135"
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
        if config.parser_id not in self._candidate_collectors:
            if result.evidence is None:
                raise ScrapeError(
                    f"{config.parser_id}正式解析结果缺少CandidateEvidence，拒绝写成功"
                )
            return result
        from dawei.parsers.generic_36 import (
            exact_issue_candidates_for_selection,
            select_candidate_for_position,
            select_latest_issue_candidate,
        )

        candidates, _, _ = self.candidate_collector(config.parser_id)(text_or_html, config)
        if config.fixed_issue is not None:
            exact = exact_issue_candidates_for_selection(
                candidates,
                config.fixed_issue,
                config,
            )
            selected = select_candidate_for_position(exact, config)
        else:
            selected = select_latest_issue_candidate(candidates, config)
        if selected.issue != result.issue or selected.numbers != result.numbers:
            raise ScrapeError(
                f"{config.parser_id}解析结果与统一候选证据不一致，拒绝写成功"
            )
        return replace(
            result,
            raw_position=selected.page_index,
            evidence=selected,
        )

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
