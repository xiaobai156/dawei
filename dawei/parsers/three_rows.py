"""Strict adjacent-row parsers for structured 36-number pages."""

from __future__ import annotations

import re

from dawei.domain.errors import ScrapeError
from dawei.domain.models import CandidateEvidence, ParsedRecord as SiteResult, SiteConfig
from dawei.parsers.common import (
    ISSUE_RE,
    all_keywords_present,
    collect_numbers_after_in_section,
    document_range_for_index,
    html_to_lines,
    invalid_36_code_diagnostic_reason,
    line_numbers,
    merged_digit_tokens,
    raw_digit_tokens,
    section_ranges,
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


XIONGCHUMO_RESULT_RE = re.compile(r"(?<!\d)(\d{3})\s*期\s*【[^】]+】\s*开")
TAXUE_RESULT_RE = re.compile(r"(?<!\d)(\d{3})\s*期\s*【[^】]*】\s*开")
FENFATUQIANG_RESULT_RE = re.compile(r"(?<!\d)(\d{3})\s*期\s*[:：]\s*『【36码中特】』\s*开")
FENFATUQIANG_ARTICLE_ENDS = ("上一篇：", "上一篇:")
XUEQIU_RESULT_RE = re.compile(r"(?<!\d)(\d{3})\s*期\s*【\s*3\s*6\s*码特围\s*】\s*开")


def collect_xiongchumo_rows(lines: list[str], index: int) -> tuple[str, ...] | None:
    rows: list[str] = []
    expected_labels = ("A", "B", "C", "D")
    for offset, label in enumerate(expected_labels, start=1):
        row_index = index + offset
        if row_index >= len(lines) or ISSUE_RE.search(lines[row_index]):
            return None
        line = lines[row_index]
        if f"[{label}]" not in line and f"【{label}】" not in line:
            return None
        numbers = line_numbers(line)
        if len(numbers) != 9:
            return None
        rows.extend(numbers)
    numbers_tuple = tuple(rows)
    return numbers_tuple if valid_36_code_record(numbers_tuple) else None


def xiongchumo_candidates(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[CandidateEvidence], list[tuple[int, int, str]], set[int]]:
    lines = html_to_lines(text_or_html)
    ranges = section_ranges(lines, config)
    if config.section_keywords and not ranges:
        raise ScrapeError("未找到栏目关键词: " + "、".join(config.section_keywords))

    candidates: list[CandidateEvidence] = []
    invalid_candidates: list[tuple[int, int, str]] = []
    seen_matching_issues: set[int] = set()
    for line_range in ranges:
        for index in line_range:
            match = XIONGCHUMO_RESULT_RE.search(lines[index])
            if not match:
                continue
            issue = int(match.group(1))
            seen_matching_issues.add(issue)
            numbers = collect_xiongchumo_rows(lines, index)
            if not numbers:
                diagnostic_numbers, raw_tokens = collect_candidate_numbers_for_diagnostics(lines, index, config)
                invalid_candidates.append((issue, index, invalid_36_code_diagnostic_reason(diagnostic_numbers, raw_tokens)))
                continue
            candidates.append(
                candidate_evidence(
                    lines,
                    index,
                    issue,
                    numbers,
                    config,
                    line_range,
                    raw_number_lines=tuple(lines[index + 1 : index + 5]),
                    anchor_line=lines[index],
                )
            )
    return candidates, invalid_candidates, seen_matching_issues


def extract_xiongchumo(text_or_html: str, config: SiteConfig) -> SiteResult:
    candidates, invalid_candidates, seen_matching_issues = xiongchumo_candidates(text_or_html, config)
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
        raise ScrapeError("熊出没内幕36码栏目没有找到符合条件的数据")

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


def taxue_candidates(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[CandidateEvidence], list[tuple[int, int, str]], set[int]]:
    lines = html_to_lines(text_or_html)
    ranges = section_ranges(lines, config)
    if config.section_keywords and not ranges:
        raise ScrapeError("未找到栏目关键词: " + "、".join(config.section_keywords))

    candidates: list[CandidateEvidence] = []
    invalid_candidates: list[tuple[int, int, str]] = []
    seen_matching_issues: set[int] = set()
    for line_range in ranges:
        for index in line_range:
            match = TAXUE_RESULT_RE.search(lines[index])
            if not match:
                continue
            issue = int(match.group(1))
            seen_matching_issues.add(issue)
            numbers = collect_xiongchumo_rows(lines, index)
            if not numbers:
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
            candidates.append(
                candidate_evidence(
                    lines,
                    index,
                    issue,
                    numbers,
                    config,
                    line_range,
                    raw_number_lines=tuple(lines[index + 1 : index + 5]),
                    anchor_line=lines[index],
                )
            )
    return candidates, invalid_candidates, seen_matching_issues


def extract_taxue(text_or_html: str, config: SiteConfig) -> SiteResult:
    candidates, invalid_candidates, seen_matching_issues = taxue_candidates(
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
        raise ScrapeError("踏雪专属内幕36码栏目没有找到符合条件的数据")
    if config.fixed_issue is not None:
        exact = exact_issue_candidates_for_selection(candidates, config.fixed_issue, config)
        if not exact:
            seen_valid = sorted({candidate.issue for candidate in candidates}, reverse=True)
            issues = ", ".join(str(issue) for issue in seen_valid[:8]) or "无"
            raise ScrapeError(
                f"未找到指定{config.fixed_issue}期；可用有效期数: {issues}"
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
        raise ScrapeError("no 36-number record found in issue range")
    return parsed_record_from_candidate(config, select_latest_issue_candidate(candidates, config))


def fenfatuqiang_candidates(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[CandidateEvidence], list[tuple[int, int, str]], set[int]]:
    lines = html_to_lines(text_or_html)
    starts = [
        index
        for index, line in enumerate(lines)
        if all_keywords_present(line, config.section_keywords)
    ]
    if config.section_keywords and not starts:
        raise ScrapeError("未找到栏目关键词: " + "、".join(config.section_keywords))

    candidates: list[CandidateEvidence] = []
    invalid_candidates: list[tuple[int, int, str]] = []
    seen_matching_issues: set[int] = set()
    for start in starts:
        document_range = document_range_for_index(lines, start)
        stop = next(
            (
                index
                for index in range(start + 1, document_range.stop)
                if lines[index] in FENFATUQIANG_ARTICLE_ENDS
            ),
            None,
        )
        if stop is None:
            raise ScrapeError("奋发图强专属正文边界缺失: 未找到上一篇")
        for index in range(start + 1, stop):
            match = FENFATUQIANG_RESULT_RE.search(lines[index])
            if not match:
                continue
            issue = int(match.group(1))
            seen_matching_issues.add(issue)
            numbers = collect_numbers_after_in_section(lines, index, config, stop=stop)
            if not numbers or not valid_36_code_record(numbers):
                diagnostic_numbers, raw_tokens = collect_candidate_numbers_for_diagnostics(
                    lines, index, config, stop=stop
                )
                invalid_candidates.append((issue, index, invalid_36_code_diagnostic_reason(diagnostic_numbers, raw_tokens)))
                continue
            candidates.append(
                candidate_evidence(lines, index, issue, numbers, config, range(start, stop))
            )
    return candidates, invalid_candidates, seen_matching_issues


def extract_fenfatuqiang(text_or_html: str, config: SiteConfig) -> SiteResult:
    candidates, invalid_candidates, seen_matching_issues = fenfatuqiang_candidates(text_or_html, config)
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
        raise ScrapeError("奋发图强专属36码中特栏目没有找到符合条件的数据")

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


def xueqiu_candidates(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[CandidateEvidence], list[tuple[int, int, str]], set[int]]:
    lines = html_to_lines(text_or_html)
    starts = [
        index
        for index, line in enumerate(lines)
        if all_keywords_present(line, config.section_keywords)
    ]
    if config.section_keywords and not starts:
        raise ScrapeError("未找到栏目关键词: " + "、".join(config.section_keywords))

    ranges: list[range] = []
    for start in starts:
        document_range = document_range_for_index(lines, start)
        stop = min(document_range.stop, start + config.search_window)
        if any(XUEQIU_RESULT_RE.search(lines[index]) for index in range(start + 1, stop)):
            ranges.append(range(start, stop))

    candidates: list[CandidateEvidence] = []
    invalid_candidates: list[tuple[int, int, str]] = []
    seen_matching_issues: set[int] = set()
    for line_range in ranges:
        for index in line_range:
            match = XUEQIU_RESULT_RE.search(lines[index])
            if not match:
                continue
            issue = int(match.group(1))
            seen_matching_issues.add(issue)
            numbers = collect_numbers_after_in_section(lines, index, config, stop=line_range.stop)
            if not numbers or not valid_36_code_record(numbers):
                diagnostic_numbers, raw_tokens = collect_candidate_numbers_for_diagnostics(
                    lines, index, config, stop=line_range.stop
                )
                invalid_candidates.append((issue, index, invalid_36_code_diagnostic_reason(diagnostic_numbers, raw_tokens)))
                continue
            candidates.append(candidate_evidence(lines, index, issue, numbers, config, line_range))
    return candidates, invalid_candidates, seen_matching_issues


def extract_xueqiu(text_or_html: str, config: SiteConfig) -> SiteResult:
    candidates, invalid_candidates, seen_matching_issues = xueqiu_candidates(text_or_html, config)
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
        raise ScrapeError("雪球专属36码特围栏目没有找到符合条件的数据")

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


def collect_onboarded_manager_rows(lines: list[str], index: int) -> tuple[str, ...] | None:
    numbers: list[str] = []
    for offset in range(1, 4):
        row_index = index + offset
        if row_index >= len(lines) or ISSUE_RE.search(lines[row_index]):
            return None
        raw_tokens = raw_digit_tokens(lines[row_index])
        row_numbers = line_numbers(lines[row_index])
        if merged_digit_tokens(raw_tokens) or len(raw_tokens) != 12 or len(row_numbers) != 12:
            return None
        if any(not (1 <= int(token) <= 49) for token in raw_tokens):
            return None
        numbers.extend(row_numbers)
    numbers_tuple = tuple(numbers)
    return numbers_tuple if valid_36_code_record(numbers_tuple) else None


def onboarded_manager_article_candidates(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[CandidateEvidence], list[tuple[int, int, str]], set[int]]:
    lines = html_to_lines(text_or_html)
    candidates: list[CandidateEvidence] = []
    invalid_candidates: list[tuple[int, int, str]] = []
    seen_matching_issues: set[int] = set()
    for index, line in enumerate(lines):
        match = ISSUE_RE.search(line)
        if not match:
            continue
        if not all_keywords_present(line, config.section_keywords):
            continue
        if not all_keywords_present(line, config.keywords):
            continue
        issue = int(match.group(1))
        seen_matching_issues.add(issue)
        numbers = collect_onboarded_manager_rows(lines, index)
        if numbers:
            candidates.append(
                candidate_evidence(
                    lines,
                    index,
                    issue,
                    numbers,
                    config,
                    range(index, min(len(lines), index + 4)),
                    raw_number_lines=tuple(lines[index + 1 : index + 4]),
                    anchor_line=line,
                )
            )
            continue
        stop = min(len(lines), index + 4)
        diagnostic_numbers = tuple(
            number
            for row in lines[index + 1 : stop]
            for number in line_numbers(row)
        )
        raw_tokens = tuple(
            token
            for row in lines[index + 1 : stop]
            for token in raw_digit_tokens(row)
        )
        invalid_candidates.append(
            (issue, index, invalid_36_code_diagnostic_reason(diagnostic_numbers, raw_tokens))
        )
    return candidates, invalid_candidates, seen_matching_issues


def extract_onboarded_manager_article(text_or_html: str, config: SiteConfig) -> SiteResult:
    candidates, invalid_candidates, seen_matching_issues = onboarded_manager_article_candidates(
        text_or_html, config
    )
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
        raise ScrapeError(f"{config.name}专属三十六码栏目没有找到符合条件的数据")

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
