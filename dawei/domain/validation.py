"""Strict, side-effect-free domain validation."""

from __future__ import annotations

from collections.abc import Iterable

from .errors import ValidationError
from .models import CandidateEvidence, CandidateOrigin, ParsedRecord, SiteConfig


def validate_36_numbers(values: Iterable[object]) -> tuple[str, ...]:
    numbers = tuple(str(value).strip().zfill(2) for value in values)
    if len(numbers) != 36:
        raise ValidationError(f"36码数量必须为36，实际{len(numbers)}")
    invalid = tuple(number for number in numbers if not number.isdigit() or not 1 <= int(number) <= 49)
    if invalid:
        raise ValidationError("36码必须全部在01-49范围: " + ",".join(invalid))
    duplicates = tuple(dict.fromkeys(number for number in numbers if numbers.count(number) > 1))
    if duplicates:
        raise ValidationError("36码存在重复数字: " + ",".join(duplicates))
    return numbers


def _validate_origin(origin: CandidateOrigin, *, allow_non_text: bool) -> None:
    required = {
        "原始期数行": origin.raw_issue_line,
        "栏目锚点": origin.anchor_line,
        "文档ID": origin.document_id,
        "文档URL": origin.document_url,
        "来源方式": origin.source_method,
        "区块ID": origin.block_id,
        "解析器ID": origin.parser_id,
    }
    missing = [label for label, value in required.items() if not str(value).strip()]
    if missing:
        raise ValidationError("候选证据缺少" + "、".join(missing))
    if not origin.raw_number_lines and not allow_non_text:
        raise ValidationError("候选证据缺少原始号码行")
    if origin.block_start < 0 or origin.block_end <= origin.block_start:
        raise ValidationError("候选证据区块边界无效")
    if not origin.block_start <= origin.page_index < origin.block_end:
        raise ValidationError("候选证据页面位置超出区块边界")
    if origin.block_index < 0:
        raise ValidationError("候选证据区块内位置无效")


def validate_candidate_evidence(record: ParsedRecord) -> CandidateEvidence:
    evidence = record.evidence
    if evidence is None:
        raise ValidationError("候选证据缺失")
    if evidence.issue != record.issue:
        raise ValidationError(
            f"候选证据期数不一致: 结果{record.issue}期，证据{evidence.issue}期"
        )
    if evidence.numbers != record.numbers:
        raise ValidationError("候选证据36码与结果不一致")
    if not evidence.origins:
        raise ValidationError("候选证据缺少原始来源")
    allow_non_text = evidence.source_method in {"image_fixed", "image_ocr"}
    top_level = CandidateOrigin(
        evidence.raw_issue_line,
        evidence.raw_number_lines,
        evidence.anchor_line,
        evidence.document_id,
        evidence.document_url,
        evidence.source_method,
        evidence.block_id,
        evidence.block_start,
        evidence.block_end,
        evidence.page_index,
        evidence.block_index,
        evidence.parser_id,
    )
    _validate_origin(top_level, allow_non_text=allow_non_text)
    if top_level not in evidence.origins:
        raise ValidationError("候选顶层证据没有对应的同文档原始来源")
    for origin in evidence.origins:
        _validate_origin(
            origin,
            allow_non_text=origin.source_method in {"image_fixed", "image_ocr"},
        )
    return evidence


def validate_site_config(site: SiteConfig) -> SiteConfig:
    if not site.site_id.strip():
        raise ValidationError("site_id不能为空")
    if not site.name.strip():
        raise ValidationError("站点名称不能为空")
    if not site.url.strip():
        raise ValidationError("站点URL不能为空")
    if site.direction not in {"top", "bottom"}:
        raise ValidationError(f"未知top/bottom方向: {site.region or site.position}")
    if not site.parser_id.strip():
        raise ValidationError("parser_id不能为空")
    if site.render_policy not in {"never", "fallback", "always"}:
        raise ValidationError(f"未知浏览器策略: {site.render_policy}")
    if site.search_window <= 0:
        raise ValidationError("search_window必须大于0")
    if site.min_numbers_per_line <= 0:
        raise ValidationError("min_numbers_per_line必须大于0")
    if site.onboarding_exception not in {None, "allow_insufficient_history"}:
        raise ValidationError(f"未知新增站特例: {site.onboarding_exception}")
    if set(site.onboarding_valid_issues) & set(site.onboarding_missing_issues):
        raise ValidationError("新增站特例的有效期与缺失期不能重叠")
    if site.source_type == "paginated_article_list" and not site.navigation_keywords:
        raise ValidationError("分页文章列表来源必须配置navigation_keywords")
    return site
