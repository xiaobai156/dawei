"""Generic strict 36-number candidate selection parser."""

from __future__ import annotations

from dataclasses import replace

from dawei.domain.errors import ScrapeError
from dawei.domain.models import (
    ArticleRecord,
    CandidateEvidence,
    CandidateOrigin,
    ParsedRecord as SiteResult,
    SiteConfig,
)
from dawei.parsers.common import (
    ISSUE_RE,
    all_keywords_present,
    candidate_foreign_strict_36_markers,
    collect_non_zero_numbers_after_for_diagnostics,
    collect_non_zero_numbers_after_in_section,
    collect_numbers_after_for_diagnostics,
    collect_numbers_after_in_section,
    collect_numbers_before,
    collect_numbers_before_for_diagnostics,
    collect_numbers_inline,
    document_range_for_index,
    html_to_lines,
    invalid_36_code_diagnostic_reason,
    is_number_block_stop_line,
    line_numbers,
    line_numbers_with_zero,
    raw_digit_tokens,
    section_ranges,
    valid_36_code_record,
)


DEFAULT_ISSUE_MIN = 126
DEFAULT_ISSUE_MAX = 126
STRICT_RECENT_CANDIDATE_LIMIT = 3


def candidate_region(config: SiteConfig) -> str:
    value = (config.region or config.position or "bottom").strip().lower()
    if value in {"top", "upper", "first", "顶部", "上"}:
        return "top"
    if value in {"bottom", "tail", "lower", "last", "尾部", "底部", "下"}:
        return "bottom"
    raise ScrapeError(f"未知候选区域配置: {config.region or config.position}")


def collect_candidate_numbers_for_diagnostics(
    lines: list[str],
    index: int,
    config: SiteConfig,
    stop: int | None = None,
    block_start: int = 0,
) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
    inline = collect_numbers_inline(lines[index])
    if inline:
        return inline, tuple(raw_digit_tokens(lines[index])[:80])
    if config.numbers_before_issue:
        return collect_numbers_before_for_diagnostics(
            lines,
            index,
            config.min_numbers_per_line,
            block_start=block_start,
        )
    if config.drop_zero_numbers:
        return collect_non_zero_numbers_after_for_diagnostics(lines, index, config.min_numbers_per_line, config)
    return collect_numbers_after_for_diagnostics(
        lines,
        index,
        config.min_numbers_per_line,
        stop=stop,
    )


def best_invalid_candidate_reason(candidates: list[tuple[int, int, str]]) -> str:
    with_numbers = [
        candidate
        for candidate in candidates
        if not candidate[2].startswith("没有收集到")
    ]
    if with_numbers:
        return max(with_numbers, key=lambda item: item[1])[2]
    return max(candidates, key=lambda item: item[1])[2]


def candidate_context(
    lines: list[str],
    index: int,
    config: SiteConfig | None = None,
    *,
    block_start: int | None = None,
    block_end: int | None = None,
) -> str:
    start = max(0, block_start if block_start is not None else index - 8)
    stop = min(len(lines), block_end if block_end is not None else index + 4)
    after: list[str] = []
    for position in range(index + 1, stop):
        line = lines[position]
        if common_document_boundary(line) or ISSUE_RE.search(line):
            break
        after.append(line)
    anchor = ""
    if config is not None and config.section_keywords and block_start is not None and start < index:
        anchor_issue = ISSUE_RE.search(lines[start])
        candidate_issue = ISSUE_RE.search(lines[index])
        if anchor_issue is None or (
            candidate_issue is not None
            and anchor_issue.group(1) == candidate_issue.group(1)
        ):
            anchor = lines[start]
    parts = [anchor, lines[index], *after]
    return " ".join(dict.fromkeys(part for part in parts if part))


def common_document_boundary(line: str) -> bool:
    return "DAWEI_DOCUMENT_BOUNDARY" in line


def result_raw_position(text_or_html: str, result: SiteResult, config: SiteConfig) -> int | None:
    lines = html_to_lines(text_or_html)
    positions = [
        index
        for index, line in enumerate(lines)
        if (match := ISSUE_RE.search(line))
        and int(match.group(1)) == result.issue
        and all_keywords_present(candidate_context(lines, index, config), config.section_keywords)
    ]
    if not positions:
        positions = [
            index
            for index, line in enumerate(lines)
            if (match := ISSUE_RE.search(line)) and int(match.group(1)) == result.issue
        ]
    if not positions:
        return None
    return min(positions) if candidate_region(config) == "top" else max(positions)


def attach_article_identity(
    result: SiteResult,
    article: ArticleRecord,
    document: str,
    config: SiteConfig,
) -> SiteResult:
    evidence = result.evidence
    if evidence is not None:
        origins = tuple(
            replace(
                origin,
                document_id=article.record_id,
                document_url=article.record_path,
                source_method="structured_article",
                block_id=f"{article.record_id}:{origin.block_start}-{origin.block_end}",
            )
            for origin in evidence.origins
        )
        evidence = replace(
            evidence,
            document_id=article.record_id,
            document_url=article.record_path,
            source_method="structured_article",
            block_id=f"{article.record_id}:{evidence.block_start}-{evidence.block_end}",
            origins=origins,
        )
    return replace(
        result,
        record_id=article.record_id,
        record_path=article.record_path,
        raw_position=(
            evidence.page_index
            if evidence is not None
            else result_raw_position(document, result, config)
        ),
        evidence=evidence,
    )


def issue_in_range(issue: int, issue_min: int, issue_max: int) -> bool:
    return issue_min <= issue <= issue_max


def prefer_candidate_index(new_index: int, current_index: int, config: SiteConfig) -> bool:
    region = candidate_region(config)
    if region == "top":
        return new_index < current_index
    if region == "bottom":
        return new_index > current_index
    raise ScrapeError(f"未知候选区域配置: {config.region or config.position}")


def select_candidate_for_position(
    candidates: list[CandidateEvidence],
    config: SiteConfig,
) -> CandidateEvidence:
    if not candidates:
        raise ScrapeError("没有可选36码数据")
    require_candidate_evidence(candidates)
    region = candidate_region(config)
    if region == "top":
        return min(candidates, key=lambda item: item.page_index)
    if region == "bottom":
        return max(candidates, key=lambda item: item.page_index)
    raise ScrapeError(f"未知候选区域配置: {config.region or config.position}")


def parsed_record_from_candidate(config: SiteConfig, candidate: CandidateEvidence) -> SiteResult:
    return SiteResult(
        config.name,
        config.url,
        candidate.issue,
        candidate.numbers,
        raw_position=candidate.page_index,
        evidence=candidate,
    )


def require_candidate_evidence(candidates: list[object]) -> None:
    if any(not isinstance(candidate, CandidateEvidence) for candidate in candidates):
        raise ScrapeError("解析器候选必须为完整CandidateEvidence，禁止旧三元组候选")


def unique_candidates(
    candidates: list[CandidateEvidence],
    config: SiteConfig | None = None,
) -> list[CandidateEvidence]:
    effective_config = config or SiteConfig("", "")
    require_candidate_evidence(candidates)
    grouped: dict[tuple[int, tuple[str, ...]], list[CandidateEvidence]] = {}
    for candidate in candidates:
        grouped.setdefault((candidate.issue, candidate.numbers), []).append(candidate)
    unique: list[CandidateEvidence] = []
    for values in grouped.values():
        representative = select_candidate_for_position(values, effective_config)
        origins = tuple(
            dict.fromkeys(
                origin
                for value in sorted(values, key=lambda item: item.page_index)
                for origin in value.origins
            )
        )
        unique.append(replace(representative, origins=origins))
    return unique


def latest_candidate_window(
    candidates: list[CandidateEvidence],
    config: SiteConfig,
) -> list[CandidateEvidence]:
    require_candidate_evidence(candidates)
    ordered = sorted(candidates, key=lambda item: item.page_index)
    if candidate_region(config) == "top":
        return ordered[:STRICT_RECENT_CANDIDATE_LIMIT]
    return ordered[-STRICT_RECENT_CANDIDATE_LIMIT:]


def exact_issue_candidates_for_selection(
    candidates: list[CandidateEvidence],
    fixed_issue: int,
    config: SiteConfig,
) -> list[CandidateEvidence]:
    assert_no_conflicting_exact_issue_candidates(candidates, fixed_issue, config)
    candidates = unique_candidates(candidates, config)
    window = latest_candidate_window(candidates, config)
    exact = [candidate for candidate in window if candidate.issue == fixed_issue]
    if exact:
        return exact

    window_issues = ", ".join(str(candidate.issue) for candidate in window) or "无"
    raise ScrapeError(
        f"指定{fixed_issue}期超出严格候选范围: 候选{len(candidates)}组，"
        f"仅允许按{candidate_region(config)}方向最近{STRICT_RECENT_CANDIDATE_LIMIT}组内选择；"
        f"窗口期数: {window_issues}"
    )


def select_latest_issue_candidate(
    candidates: list[CandidateEvidence],
    config: SiteConfig,
) -> CandidateEvidence:
    all_candidates = list(candidates)
    window = latest_candidate_window(unique_candidates(all_candidates, config), config)
    latest_issue = max(candidate.issue for candidate in window)
    assert_no_conflicting_exact_issue_candidates(all_candidates, latest_issue, config)
    same_issue = [candidate for candidate in window if candidate.issue == latest_issue]
    return select_candidate_for_position(same_issue, config)


def assert_no_conflicting_exact_issue_candidates(
    candidates: list[CandidateEvidence],
    fixed_issue: int,
    config: SiteConfig | None = None,
) -> None:
    require_candidate_evidence(candidates)
    exact = [candidate for candidate in candidates if candidate.issue == fixed_issue]
    number_sets = {candidate.numbers for candidate in exact}
    if len(number_sets) <= 1:
        return
    positions = ", ".join(str(candidate.page_index) for candidate in exact)
    raise ScrapeError(
        f"{fixed_issue}期存在多个高可信候选且36码冲突，拒绝写成功；候选位置: {positions}"
    )


def candidate_number_lines(
    lines: list[str],
    index: int,
    config: SiteConfig,
    *,
    block_start: int,
    block_end: int,
) -> tuple[str, ...]:
    if collect_numbers_inline(lines[index]):
        return (lines[index],)
    rows: list[str] = []
    total = 0
    if config.numbers_before_issue:
        for position in range(index - 1, block_start - 1, -1):
            if is_number_block_stop_line(lines[position], config):
                break
            current = line_numbers(lines[position])
            if len(current) >= config.min_numbers_per_line:
                rows.append(lines[position])
                total += len(current)
                if total >= 36:
                    return tuple(reversed(rows))
            elif rows:
                break
        return tuple(reversed(rows))
    for position in range(index + 1, block_end):
        if is_number_block_stop_line(lines[position], config):
            break
        current = (
            [number for number in line_numbers_with_zero(lines[position]) if number != "00"]
            if config.drop_zero_numbers
            else line_numbers(lines[position])
        )
        if len(current) >= config.min_numbers_per_line:
            rows.append(lines[position])
            total += len(current)
            if total >= 36:
                break
        elif rows:
            break
    return tuple(rows)


def candidate_evidence(
    lines: list[str],
    index: int,
    issue: int,
    numbers: tuple[str, ...],
    config: SiteConfig,
    line_range: range,
    *,
    raw_number_lines: tuple[str, ...] | None = None,
    anchor_line: str | None = None,
) -> CandidateEvidence:
    document_range = document_range_for_index(lines, index)
    if line_range.start < document_range.start or line_range.start >= document_range.stop:
        raise ScrapeError("栏目锚点与候选期数跨文档，拒绝写成功")
    block_start = max(line_range.start, document_range.start)
    block_end = min(line_range.stop, document_range.stop)
    if not block_start <= index < block_end:
        raise ScrapeError("候选期数超出同文档区块边界，拒绝写成功")
    line_range = range(block_start, block_end)
    document_number = sum(
        1 for position in range(index) if common_document_boundary(lines[position])
    )
    document_id = f"document-{document_number}"
    actual_anchor_line = (
        anchor_line
        if anchor_line is not None
        else lines[line_range.start] if config.section_keywords else lines[index]
    )
    actual_number_lines = raw_number_lines or candidate_number_lines(
        lines,
        index,
        config,
        block_start=line_range.start,
        block_end=line_range.stop,
    )
    origin = CandidateOrigin(
        raw_issue_line=lines[index],
        raw_number_lines=actual_number_lines,
        anchor_line=actual_anchor_line,
        document_id=document_id,
        document_url=config.url,
        source_method=config.source_type,
        block_id=f"{document_id}:{line_range.start}-{line_range.stop}",
        block_start=line_range.start,
        block_end=line_range.stop,
        page_index=index,
        block_index=index - line_range.start,
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


def generic_candidates(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[CandidateEvidence], list[tuple[int, int, str]], set[int]]:
    lines = html_to_lines(text_or_html)
    candidates: list[CandidateEvidence] = []
    invalid_candidates: list[tuple[int, int, str]] = []
    seen_matching_issues: set[int] = set()
    ranges = section_ranges(lines, config)
    if config.section_keywords and not ranges:
        raise ScrapeError("未找到栏目关键词: " + "、".join(config.section_keywords))
    for line_range in ranges:
        for index in line_range:
            issue_match = ISSUE_RE.search(lines[index])
            if not issue_match or candidate_foreign_strict_36_markers(lines[index], config):
                continue
            context = candidate_context(
                lines,
                index,
                config,
                block_start=line_range.start,
                block_end=line_range.stop,
            )
            if config.keywords and not all_keywords_present(context, config.keywords):
                continue
            issue = int(issue_match.group(1))
            seen_matching_issues.add(issue)
            if config.numbers_before_issue:
                numbers = collect_numbers_before(
                    lines,
                    index,
                    config.min_numbers_per_line,
                    block_start=line_range.start,
                )
            elif config.drop_zero_numbers:
                numbers = collect_non_zero_numbers_after_in_section(
                    lines,
                    index,
                    config,
                    stop=line_range.stop,
                    block_start=line_range.start,
                )
            else:
                numbers = collect_numbers_inline(lines[index]) or collect_numbers_after_in_section(
                    lines,
                    index,
                    config,
                    stop=line_range.stop,
                )
            if not numbers or not valid_36_code_record(numbers):
                diagnostic_numbers, raw_tokens = collect_candidate_numbers_for_diagnostics(
                    lines,
                    index,
                    config,
                    stop=line_range.stop,
                )
                invalid_candidates.append(
                    (
                        issue,
                        index,
                        invalid_36_code_diagnostic_reason(diagnostic_numbers, raw_tokens),
                    )
                )
                continue
            candidates.append(candidate_evidence(lines, index, issue, numbers, config, line_range))
    return candidates, invalid_candidates, seen_matching_issues


def extract_generic_36(text_or_html: str, config: SiteConfig) -> SiteResult:
    candidates, invalid_candidates, seen_matching_issues = generic_candidates(
        text_or_html,
        config,
    )

    if not candidates:
        if config.fixed_issue is not None:
            exact_invalid = [
                candidate for candidate in invalid_candidates if candidate[0] == config.fixed_issue
            ]
            if exact_invalid:
                reason = best_invalid_candidate_reason(exact_invalid)
                raise ScrapeError(f"{config.fixed_issue}期数据无效: {reason}")
            if config.fixed_issue in seen_matching_issues:
                raise ScrapeError(f"{config.fixed_issue}期没有找到完整36码数据")
            if seen_matching_issues:
                issues = ", ".join(str(issue) for issue in sorted(seen_matching_issues, reverse=True)[:8])
                raise ScrapeError(f"未找到指定{config.fixed_issue}期；页面可命中的期数: {issues}")
        if invalid_candidates:
            issue, _, reason = max(invalid_candidates, key=lambda item: (item[0], item[1]))
            raise ScrapeError(f"{issue}期数据无效: {reason}")
        raise ScrapeError("没有找到符合关键词的36码数据")

    if config.fixed_issue is not None:
        if not any(candidate.issue == config.fixed_issue for candidate in candidates):
            exact_invalid = [
                candidate for candidate in invalid_candidates if candidate[0] == config.fixed_issue
            ]
            if exact_invalid:
                reason = best_invalid_candidate_reason(exact_invalid)
                raise ScrapeError(f"{config.fixed_issue}期数据无效: {reason}")
        exact = exact_issue_candidates_for_selection(candidates, config.fixed_issue, config)
        if not exact:
            exact_invalid = [
                candidate for candidate in invalid_candidates if candidate[0] == config.fixed_issue
            ]
            if exact_invalid:
                reason = best_invalid_candidate_reason(exact_invalid)
                raise ScrapeError(f"{config.fixed_issue}期数据无效: {reason}")
            if config.fixed_issue in seen_matching_issues:
                raise ScrapeError(f"{config.fixed_issue}期没有找到完整36码数据")
            seen_valid = sorted({candidate.issue for candidate in candidates}, reverse=True)
            if seen_valid:
                issues = ", ".join(str(issue) for issue in seen_valid[:8])
                raise ScrapeError(f"未找到指定{config.fixed_issue}期；可用有效期数: {issues}")
            raise ScrapeError(f"no latest 36-number record found for issue {config.fixed_issue}")
        return parsed_record_from_candidate(config, select_candidate_for_position(exact, config))

    candidates = [
        candidate
        for candidate in candidates
        if issue_in_range(candidate.issue, DEFAULT_ISSUE_MIN, DEFAULT_ISSUE_MAX)
    ]
    if not candidates:
        raise ScrapeError(f"no 36-number record found in issue range {DEFAULT_ISSUE_MIN}-{DEFAULT_ISSUE_MAX}")

    return parsed_record_from_candidate(config, select_latest_issue_candidate(candidates, config))
