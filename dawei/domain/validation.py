"""Strict, side-effect-free domain validation."""

from __future__ import annotations

from collections.abc import Iterable

from .errors import ValidationError
from .models import CandidateEvidence, CandidateOrigin, ParsedRecord, SiteConfig


def _require_non_empty_text(value: object, label: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValidationError(f"{label}必须为非空字符串")


def _require_optional_text(value: object, label: str) -> None:
    if value is not None and type(value) is not str:
        raise ValidationError(f"{label}必须为字符串或None")


def _require_positive_int(value: object, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValidationError(f"{label}必须为正整数")


def _require_non_negative_int(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValidationError(f"{label}必须为非负整数")


def _require_bool(value: object, label: str) -> None:
    if type(value) is not bool:
        raise ValidationError(f"{label}必须为bool")


def _require_text_tuple(value: object, label: str) -> None:
    if type(value) is not tuple or any(
        type(item) is not str or not item.strip() for item in value
    ):
        raise ValidationError(f"{label}必须为非空字符串tuple")


def _require_positive_int_tuple(value: object, label: str) -> None:
    if type(value) is not tuple or any(type(item) is not int or item <= 0 for item in value):
        raise ValidationError(f"{label}必须为正整数tuple")


def validate_36_numbers(values: Iterable[object]) -> tuple[str, ...]:
    numbers = tuple(values)
    if len(numbers) != 36:
        raise ValidationError(f"36码数量必须为36，实际{len(numbers)}")
    invalid = tuple(
        number
        for number in numbers
        if not (
            isinstance(number, str)
            and len(number) == 2
            and number.isascii()
            and number.isdigit()
            and 1 <= int(number) <= 49
        )
    )
    if invalid:
        raise ValidationError(
            "36码必须全部为两位字符串且在01-49范围: "
            + ",".join(str(number) for number in invalid)
        )
    duplicates = tuple(dict.fromkeys(number for number in numbers if numbers.count(number) > 1))
    if duplicates:
        raise ValidationError("36码存在重复数字: " + ",".join(duplicates))
    return numbers


def _validate_origin(origin: CandidateOrigin, *, allow_non_text: bool) -> None:
    if not isinstance(origin, CandidateOrigin):
        raise ValidationError("候选来源必须为CandidateOrigin")
    _require_text_tuple(origin.raw_number_lines, "原始号码行")
    required = {
        "原始期数行": origin.raw_issue_line,
        "栏目锚点": origin.anchor_line,
        "文档ID": origin.document_id,
        "文档URL": origin.document_url,
        "来源方式": origin.source_method,
        "区块ID": origin.block_id,
        "解析器ID": origin.parser_id,
    }
    for label, value in required.items():
        _require_non_empty_text(value, label)
    if not origin.raw_number_lines and not allow_non_text:
        raise ValidationError("候选证据缺少原始号码行")
    _require_non_negative_int(origin.block_start, "候选证据区块起点")
    _require_non_negative_int(origin.block_end, "候选证据区块终点")
    _require_non_negative_int(origin.page_index, "候选证据页面位置")
    _require_non_negative_int(origin.block_index, "候选证据区块内位置")
    if origin.block_end <= origin.block_start:
        raise ValidationError("候选证据区块边界无效")
    if not origin.block_start <= origin.page_index < origin.block_end:
        raise ValidationError("候选证据页面位置超出区块边界")
    if origin.block_index < 0:
        raise ValidationError("候选证据区块内位置无效")


def validate_candidate_evidence(record: ParsedRecord) -> CandidateEvidence:
    if not isinstance(record, ParsedRecord):
        raise ValidationError("解析结果必须为ParsedRecord")
    evidence = record.evidence
    if evidence is None:
        raise ValidationError("候选证据缺失")
    if not isinstance(evidence, CandidateEvidence):
        raise ValidationError("候选证据必须为CandidateEvidence")
    _require_non_empty_text(record.name, "解析结果名称")
    _require_non_empty_text(record.url, "解析结果URL")
    _require_positive_int(record.issue, "解析结果期数")
    _require_non_negative_int(record.raw_position, "解析结果raw_position")
    validate_36_numbers(record.numbers)
    _require_positive_int(evidence.issue, "候选证据期数")
    validate_36_numbers(evidence.numbers)
    if type(evidence.raw_number_lines) is not tuple:
        raise ValidationError("候选证据原始号码行必须为tuple")
    if type(evidence.origins) is not tuple or not evidence.origins:
        raise ValidationError("候选证据来源必须为非空tuple")
    if any(not isinstance(origin, CandidateOrigin) for origin in evidence.origins):
        raise ValidationError("候选证据来源必须为CandidateOrigin")
    if evidence.issue != record.issue:
        raise ValidationError(
            f"候选证据期数不一致: 结果{record.issue}期，证据{evidence.issue}期"
        )
    if evidence.numbers != record.numbers:
        raise ValidationError("候选证据36码与结果不一致")
    if record.raw_position != evidence.page_index:
        raise ValidationError("解析结果raw_position与候选证据页面位置不一致")
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
    for field in (
        "name",
        "url",
        "site_id",
        "source_type",
        "position",
        "parser_id",
        "render_policy",
    ):
        _require_non_empty_text(getattr(site, field), field)
    for field in ("region", "api_url", "image_decoder", "record_id", "onboarding_exception"):
        _require_optional_text(getattr(site, field), field)
    _require_positive_int(site.search_window, "search_window")
    _require_positive_int(site.min_numbers_per_line, "min_numbers_per_line")
    if site.fixed_issue is not None:
        _require_positive_int(site.fixed_issue, "fixed_issue")
    for field in ("numbers_before_issue", "drop_zero_numbers", "render_browser"):
        _require_bool(getattr(site, field), field)
    for field in ("keywords", "section_keywords", "navigation_keywords"):
        _require_text_tuple(getattr(site, field), field)
    for field in ("onboarding_valid_issues", "onboarding_missing_issues"):
        _require_positive_int_tuple(getattr(site, field), field)
    direction = site.direction
    directionless_image = (
        direction == "none"
        and (
            site.parser_id == "image_tuku2135"
            or site.image_decoder == "tuku2135_ocr"
        )
    )
    if direction not in {"top", "bottom"} and not directionless_image:
        raise ValidationError(f"未知top/bottom方向: {site.region or site.position}")
    if site.render_policy not in {"never", "fallback", "always"}:
        raise ValidationError(f"未知浏览器策略: {site.render_policy}")
    if site.source_type == "paginated_article_list" and (
        site.render_policy != "never" or site.render_browser
    ):
        raise ValidationError(
            "分页文章列表来源只允许HTTP抓取: render_policy必须为never且render_browser必须为False"
        )
    if site.onboarding_exception not in {
        None,
        "allow_insufficient_history",
        "allow_incomplete_backup",
    }:
        raise ValidationError(f"未知新增站特例: {site.onboarding_exception}")
    if set(site.onboarding_valid_issues) & set(site.onboarding_missing_issues):
        raise ValidationError("新增站特例的有效期与缺失期不能重叠")
    if site.source_type == "paginated_article_list" and not site.navigation_keywords:
        raise ValidationError("分页文章列表来源必须配置navigation_keywords")
    return site
