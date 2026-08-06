"""Pure parser implementations and default registry."""

from __future__ import annotations

from dawei.parsers.generic_36 import extract_generic_36, generic_candidates
from dawei.parsers.registry import ParserRegistry
from dawei.parsers.specials.dedicated import (
    extract_fengkuang_zhongma,
    extract_marker_after_issue_3x12,
    extract_meirenyu,
    extract_renjianrenai,
    extract_section_3x12,
    extract_topic_content_3x12,
    extract_yiyiba,
    fengkuang_zhongma_candidates,
    marker_after_issue_3x12_candidates,
    meirenyu_candidates,
    renjianrenai_candidates,
    section_3x12_candidates,
    topic_content_3x12_candidates,
    yiyiba_candidates,
)
from dawei.parsers.specials.tabular import (
    extract_baoma_xuanji,
    extract_xiaoyuer,
    extract_zhuchiren_tab,
    baoma_xuanji_candidates,
    xiaoyuer_candidates,
    zhuchiren_tab_candidates,
)
from dawei.parsers.three_rows import (
    extract_fenfatuqiang,
    extract_onboarded_manager_article,
    extract_taxue,
    extract_xiongchumo,
    extract_xueqiu,
    fenfatuqiang_candidates,
    onboarded_manager_article_candidates,
    taxue_candidates,
    xiongchumo_candidates,
    xueqiu_candidates,
)


def build_default_registry() -> ParserRegistry:
    registry = ParserRegistry()
    registry.register("generic_36", extract_generic_36, generic_candidates)
    registry.register("paginated_article_36", extract_generic_36, generic_candidates)
    registry.register("xiongchumo", extract_xiongchumo, xiongchumo_candidates)
    registry.register("taxue_four_rows", extract_taxue, taxue_candidates)
    registry.register("fenfatuqiang", extract_fenfatuqiang, fenfatuqiang_candidates)
    registry.register("xueqiu", extract_xueqiu, xueqiu_candidates)
    registry.register(
        "three_rows",
        extract_onboarded_manager_article,
        onboarded_manager_article_candidates,
    )
    registry.register("baoma_xuanji", extract_baoma_xuanji, baoma_xuanji_candidates)
    registry.register("zhuchiren_weite", extract_zhuchiren_tab, zhuchiren_tab_candidates)
    registry.register("zhuchiren_baote", extract_zhuchiren_tab, zhuchiren_tab_candidates)
    registry.register("xiaoyuer", extract_xiaoyuer, xiaoyuer_candidates)
    registry.register("renjianrenai", extract_renjianrenai, renjianrenai_candidates)
    registry.register("meirenyu", extract_meirenyu, meirenyu_candidates)
    registry.register(
        "fengkuang_zhongma",
        extract_fengkuang_zhongma,
        fengkuang_zhongma_candidates,
    )
    registry.register(
        "topic_content_3x12",
        extract_topic_content_3x12,
        topic_content_3x12_candidates,
    )
    registry.register(
        "marker_after_issue_3x12",
        extract_marker_after_issue_3x12,
        marker_after_issue_3x12_candidates,
    )
    registry.register(
        "section_3x12",
        extract_section_3x12,
        section_3x12_candidates,
    )
    registry.register("yiyiba", extract_yiyiba, yiyiba_candidates)
    registry.register("kunnan_magazine", extract_generic_36)
    return registry


DEFAULT_REGISTRY = build_default_registry()

__all__ = ["DEFAULT_REGISTRY", "ParserRegistry", "build_default_registry"]
