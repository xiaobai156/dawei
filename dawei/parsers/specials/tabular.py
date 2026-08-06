"""Strict tabular and multi-grid special parsers."""

from __future__ import annotations

import re

from dawei.domain.errors import ScrapeError
from dawei.domain.models import CandidateEvidence, ParsedRecord as SiteResult, SiteConfig
from dawei.parsers.common import (
    ISSUE_RE,
    NUMBER_BLOCK_STOP_KEYWORDS,
    STANDALONE_ISSUE_RE,
    collect_non_zero_numbers_after_for_diagnostics,
    collect_numbers_after_in_section,
    document_range_for_index,
    html_to_lines,
    invalid_36_code_diagnostic_reason,
    line_numbers,
    line_numbers_with_zero,
    raw_digit_tokens,
    valid_36_code_record,
)
from dawei.parsers.generic_36 import (
    DEFAULT_ISSUE_MAX,
    DEFAULT_ISSUE_MIN,
    best_invalid_candidate_reason,
    candidate_evidence,
    collect_candidate_numbers_for_diagnostics,
    exact_issue_candidates_for_selection,
    issue_in_range,
    parsed_record_from_candidate,
    select_candidate_for_position,
    select_latest_issue_candidate,
)


BAOMA_XUANJI_RE = re.compile(
    r"(?<!\d)(\d{3})\s*期必中12码肖.*?"
    r"第一份[:：]\s*([0-9\s]+)"
    r"第二份[:：]\s*([0-9\s]+)"
    r"第三份[:：]\s*([0-9\s]+)"
)
XIAOYUER_RESULT_RE = re.compile(r"(?<!\d)(\d{3})\s*期\s*:\s*开奖结果")


def baoma_xuanji_candidates(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[CandidateEvidence], list[tuple[int, int, str]], set[int]]:
    lines = html_to_lines(text_or_html)
    anchor_indexes = [index for index, line in enumerate(lines) if "综合特码" in line]
    if not anchor_indexes:
        raise ScrapeError("未找到宝马玄机专属栏目: 综合特码")

    start_index = min(anchor_indexes)
    document_range = document_range_for_index(lines, start_index)
    candidates: list[CandidateEvidence] = []
    invalid_candidates: list[tuple[int, int, str]] = []
    seen_matching_issues: set[int] = set()

    for index in range(start_index + 1, document_range.stop):
        line = lines[index]
        if index > start_index + 1 and "必中12码肖" not in line and not BAOMA_XUANJI_RE.search(line):
            if any(keyword in line for keyword in NUMBER_BLOCK_STOP_KEYWORDS):
                break
        match = BAOMA_XUANJI_RE.search(line)
        if not match:
            if ISSUE_RE.search(line) and "必中12码肖" in line:
                issue = int(ISSUE_RE.search(line).group(1))
                seen_matching_issues.add(issue)
                diagnostic_numbers = tuple(number for number in line_numbers_with_zero(line) if number != "00")
                invalid_candidates.append((issue, index, invalid_36_code_diagnostic_reason(diagnostic_numbers, raw_digit_tokens(line))))
            continue

        issue = int(match.group(1))
        seen_matching_issues.add(issue)
        numbers = tuple(number for group in match.groups()[1:] for number in line_numbers(group))
        if not valid_36_code_record(numbers):
            invalid_candidates.append((issue, index, invalid_36_code_diagnostic_reason(numbers, raw_digit_tokens(line))))
            continue
        candidates.append(
            candidate_evidence(
                lines,
                index,
                issue,
                numbers,
                config,
                range(start_index, document_range.stop),
                raw_number_lines=(line,),
                anchor_line=lines[start_index],
            )
        )

    return candidates, invalid_candidates, seen_matching_issues


def extract_baoma_xuanji(text_or_html: str, config: SiteConfig) -> SiteResult:
    candidates, invalid_candidates, seen_matching_issues = baoma_xuanji_candidates(text_or_html, config)
    if not candidates:
        if config.fixed_issue is not None:
            exact_invalid = [candidate for candidate in invalid_candidates if candidate[0] == config.fixed_issue]
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
        raise ScrapeError("宝马玄机综合特码栏目没有找到符合条件的数据")

    if config.fixed_issue is not None:
        exact = exact_issue_candidates_for_selection(candidates, config.fixed_issue, config)
        if not exact:
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


def zhuchiren_tab_anchor(config: SiteConfig) -> str:
    if config.parser_id == "zhuchiren_weite":
        return "36码围特"
    if config.parser_id == "zhuchiren_baote":
        return "36码爆特"
    raise ScrapeError(f"未知主持人专属栏目: {config.name}")


def zhuchiren_tab_issue_match(line: str, config: SiteConfig) -> re.Match[str] | None:
    if config.parser_id == "zhuchiren_weite":
        return STANDALONE_ISSUE_RE.search(line)
    if config.parser_id == "zhuchiren_baote":
        match = ISSUE_RE.search(line)
        if match and "36码爆特" in line:
            return match
        return None
    return None


def zhuchiren_tab_candidates(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[CandidateEvidence], list[tuple[int, int, str]], set[int]]:
    lines = html_to_lines(text_or_html)
    anchor = zhuchiren_tab_anchor(config)
    anchor_indexes = [
        index
        for index, line in enumerate(lines)
        if line.strip() == anchor
    ]
    if not anchor_indexes:
        raise ScrapeError(f"未找到主持人专属栏目: {anchor}")

    start_index = min(anchor_indexes)
    document_range = document_range_for_index(lines, start_index)
    candidates: list[CandidateEvidence] = []
    invalid_candidates: list[tuple[int, int, str]] = []
    seen_matching_issues: set[int] = set()

    for index in range(start_index + 1, document_range.stop):
        issue_match = zhuchiren_tab_issue_match(lines[index], config)
        if not issue_match:
            continue

        issue = int(issue_match.group(1))
        seen_matching_issues.add(issue)
        numbers = collect_numbers_after_in_section(
            lines,
            index,
            config,
            stop=document_range.stop,
        )
        if not numbers or not valid_36_code_record(numbers):
            diagnostic_numbers, raw_tokens = collect_candidate_numbers_for_diagnostics(
                lines,
                index,
                config,
                stop=document_range.stop,
            )
            invalid_candidates.append((issue, index, invalid_36_code_diagnostic_reason(diagnostic_numbers, raw_tokens)))
            continue
        candidates.append(
            candidate_evidence(
                lines,
                index,
                issue,
                numbers,
                config,
                range(start_index, document_range.stop),
                anchor_line=lines[start_index],
            )
        )

    return candidates, invalid_candidates, seen_matching_issues


def extract_zhuchiren_tab(text_or_html: str, config: SiteConfig) -> SiteResult:
    candidates, invalid_candidates, seen_matching_issues = zhuchiren_tab_candidates(text_or_html, config)
    if not candidates:
        if config.fixed_issue is not None:
            exact_invalid = [candidate for candidate in invalid_candidates if candidate[0] == config.fixed_issue]
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
        raise ScrapeError(f"{config.name}{zhuchiren_tab_anchor(config)}栏目没有找到符合条件的数据")

    if config.fixed_issue is not None:
        exact = exact_issue_candidates_for_selection(candidates, config.fixed_issue, config)
        if not exact:
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


def collect_xiaoyuer_grid_numbers(
    lines: list[str],
    start: int,
    stop: int,
) -> tuple[str, ...] | None:
    numbers: list[str] = []
    started = False
    for index in range(start + 1, stop):
        line = lines[index].strip()
        if not line:
            continue
        if ISSUE_RE.search(line) or any(keyword in line for keyword in NUMBER_BLOCK_STOP_KEYWORDS):
            break
        tokens = line_numbers_with_zero(line)
        if not tokens:
            if started:
                continue
            continue
        started = True
        numbers.extend(number for number in tokens if number != "00")
        if valid_36_code_record(tuple(numbers)):
            return tuple(numbers)
        if len(numbers) > 36:
            break
    numbers_tuple = tuple(numbers)
    return numbers_tuple if valid_36_code_record(numbers_tuple) else None


def xiaoyuer_candidates(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[CandidateEvidence], list[tuple[int, int, str]], set[int]]:
    lines = html_to_lines(text_or_html)
    anchor_indexes = [
        index
        for index, line in enumerate(lines)
        if "澳门精准36码" in line
    ]
    if not anchor_indexes:
        raise ScrapeError("未找到小鱼儿专属栏目: 澳门精准36码")

    start_index = min(anchor_indexes)
    document_range = document_range_for_index(lines, start_index)
    first_result = next(
        (
            index
            for index in range(start_index + 1, document_range.stop)
            if XIAOYUER_RESULT_RE.search(lines[index])
        ),
        None,
    )
    if first_result is None:
        raise ScrapeError("小鱼儿澳门精准36码表格没有找到符合条件的数据")
    boundary_indexes = [
        index
        for index in range(first_result + 1, document_range.stop)
        if "小鱼儿官方网址" in lines[index] or "未来对每个彩民来说" in lines[index]
    ]
    if not boundary_indexes:
        raise ScrapeError("小鱼儿当前期表格结束边界缺失")
    section_stop = min(boundary_indexes)
    candidates: list[CandidateEvidence] = []
    invalid_candidates: list[tuple[int, int, str]] = []
    seen_matching_issues: set[int] = set()

    for index in range(start_index + 1, section_stop):
        match = XIAOYUER_RESULT_RE.search(lines[index])
        if not match:
            continue
        issue = int(match.group(1))
        seen_matching_issues.add(issue)
        numbers = collect_xiaoyuer_grid_numbers(lines, index, section_stop)
        if not numbers:
            diagnostic_numbers, raw_tokens = collect_non_zero_numbers_after_for_diagnostics(
                lines,
                index,
                1,
                config,
                stop=section_stop,
            )
            invalid_candidates.append((issue, index, invalid_36_code_diagnostic_reason(diagnostic_numbers, raw_tokens)))
            continue
        candidates.append(
            candidate_evidence(
                lines,
                index,
                issue,
                numbers,
                config,
                range(start_index, section_stop),
                anchor_line=lines[start_index],
            )
        )

    return candidates, invalid_candidates, seen_matching_issues


def extract_xiaoyuer(text_or_html: str, config: SiteConfig) -> SiteResult:
    candidates, invalid_candidates, seen_matching_issues = xiaoyuer_candidates(text_or_html, config)
    if not candidates:
        if config.fixed_issue is not None:
            exact_invalid = [candidate for candidate in invalid_candidates if candidate[0] == config.fixed_issue]
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
        raise ScrapeError("小鱼儿澳门精准36码表格没有找到符合条件的数据")

    if config.fixed_issue is not None:
        exact = exact_issue_candidates_for_selection(candidates, config.fixed_issue, config)
        if not exact:
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
