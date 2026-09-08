"""Strict adjacent-row parsers for structured 36-number pages."""

from __future__ import annotations

import re

from dawei.domain.errors import ScrapeError
from dawei.domain.models import CandidateEvidence, SiteConfig
from dawei.domain.models import ParsedRecord as SiteResult
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
    candidate_evidence,
    collect_candidate_numbers_for_diagnostics,
)
from dawei.parsers.registry import extract_from_candidates

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
    return extract_from_candidates(
        text_or_html,
        config,
        xiongchumo_candidates,
        no_candidates_message="熊出没内幕36码栏目没有找到符合条件的数据",
        issue_range_error=(
            f"no 36-number record found in issue range {DEFAULT_ISSUE_MIN}-{DEFAULT_ISSUE_MAX}"
        ),
    )


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
    return extract_from_candidates(
        text_or_html,
        config,
        taxue_candidates,
        no_candidates_message="踏雪专属内幕36码栏目没有找到符合条件的数据",
        issue_range_error="no 36-number record found in issue range",
        include_seen_issue_list=False,
        include_invalid_candidates=False,
    )


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
    return extract_from_candidates(
        text_or_html,
        config,
        fenfatuqiang_candidates,
        no_candidates_message="奋发图强专属36码中特栏目没有找到符合条件的数据",
        issue_range_error=(
            f"no 36-number record found in issue range {DEFAULT_ISSUE_MIN}-{DEFAULT_ISSUE_MAX}"
        ),
    )


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
    return extract_from_candidates(
        text_or_html,
        config,
        xueqiu_candidates,
        no_candidates_message="雪球专属36码特围栏目没有找到符合条件的数据",
        issue_range_error=(
            f"no 36-number record found in issue range {DEFAULT_ISSUE_MIN}-{DEFAULT_ISSUE_MAX}"
        ),
    )


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
    return extract_from_candidates(
        text_or_html,
        config,
        onboarded_manager_article_candidates,
        no_candidates_message=f"{config.name}专属三十六码栏目没有找到符合条件的数据",
        issue_range_error=(
            f"no 36-number record found in issue range {DEFAULT_ISSUE_MIN}-{DEFAULT_ISSUE_MAX}"
        ),
    )
