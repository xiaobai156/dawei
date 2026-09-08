"""Dedicated strict parsers with site-specific document boundaries."""

from __future__ import annotations

import re

from dawei.domain.errors import ScrapeError
from dawei.domain.models import CandidateEvidence, SiteConfig
from dawei.domain.models import ParsedRecord as SiteResult
from dawei.parsers.common import (
    ISSUE_RE,
    all_keywords_present,
    candidate_foreign_strict_36_markers,
    collect_numbers_after_in_section,
    document_range_for_index,
    first_topic_content_lines,
    html_to_lines,
    invalid_36_code_diagnostic_reason,
    line_numbers,
    raw_digit_tokens,
    section_ranges,
    valid_36_code_record,
)
from dawei.parsers.generic_36 import (
    DEFAULT_ISSUE_MAX,
    DEFAULT_ISSUE_MIN,
    candidate_context,
    candidate_evidence,
    collect_candidate_numbers_for_diagnostics,
    extract_generic_36,
    generic_candidates,
)
from dawei.parsers.registry import extract_from_candidates


def meirenyu_effective_config(text_or_html: str, config: SiteConfig) -> SiteConfig:
    lines = html_to_lines(text_or_html)
    if not any("美人鱼-澳门" in line for line in lines):
        raise ScrapeError("美人鱼专属页面身份缺失")
    if not any(line.strip() == "36码" for line in lines):
        raise ScrapeError("美人鱼无错36码区块边界缺失")
    return SiteConfig(
        **{
            **config.__dict__,
            "parser_id": "generic_36",
            "section_keywords": ("无错36码",),
            "keywords": ("无错36码",),
        }
    )


def meirenyu_candidates(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[CandidateEvidence], list[tuple[int, int, str]], set[int]]:
    return generic_candidates(text_or_html, meirenyu_effective_config(text_or_html, config))


def extract_meirenyu(text_or_html: str, config: SiteConfig) -> SiteResult:
    return extract_generic_36(
        text_or_html,
        meirenyu_effective_config(text_or_html, config),
    )


def renjianrenai_candidates(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[CandidateEvidence], list[tuple[int, int, str]], set[int]]:
    lines = html_to_lines(text_or_html)
    candidates: list[CandidateEvidence] = []
    invalid_candidates: list[tuple[int, int, str]] = []
    seen_matching_issues: set[int] = set()

    for index, line in enumerate(lines):
        issue_match = ISSUE_RE.search(line)
        if not issue_match:
            continue
        compact = re.sub(r"\s+", "", line)
        if "人见人爱" not in compact or "三十六码" not in compact:
            continue

        issue = int(issue_match.group(1))
        seen_matching_issues.add(issue)
        document_range = document_range_for_index(lines, index)
        block_end = next(
            (
                position
                for position in range(index + 1, document_range.stop)
                if ISSUE_RE.search(lines[position])
            ),
            document_range.stop,
        )
        numbers = collect_numbers_after_in_section(
            lines,
            index,
            config,
            stop=block_end,
        )
        if not numbers or not valid_36_code_record(numbers):
            diagnostic_numbers, raw_tokens = collect_candidate_numbers_for_diagnostics(
                lines,
                index,
                config,
                stop=block_end,
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
                range(index, block_end),
                anchor_line=line,
            )
        )

    return candidates, invalid_candidates, seen_matching_issues


def extract_renjianrenai(text_or_html: str, config: SiteConfig) -> SiteResult:
    return extract_from_candidates(
        text_or_html,
        config,
        renjianrenai_candidates,
        no_candidates_message="人见人爱专属解析没有找到符合条件的数据",
        issue_range_error=(
            f"no 36-number record found in issue range {DEFAULT_ISSUE_MIN}-{DEFAULT_ISSUE_MAX}"
        ),
    )


def topic_content_3x12_candidates(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[CandidateEvidence], list[tuple[int, int, str]], set[int]]:
    if not re.search(
        r"<div\b[^>]*\bclass\s*=\s*['\"][^'\"]*\btopic-content\b",
        text_or_html,
        re.IGNORECASE,
    ):
        raise ScrapeError("专属正文边界缺失: 未找到topic-content")
    lines = first_topic_content_lines(text_or_html)
    if not lines:
        raise ScrapeError("专属正文为空")

    candidates: list[CandidateEvidence] = []
    invalid_candidates: list[tuple[int, int, str]] = []
    seen_matching_issues: set[int] = set()
    run_stop = len(lines)
    previous_issue: int | None = None
    for index, line in enumerate(lines):
        issue_match = ISSUE_RE.search(line)
        if issue_match:
            issue = int(issue_match.group(1))
            if previous_issue is not None and issue > previous_issue:
                run_stop = index
                break
            previous_issue = issue
    full_range = range(run_stop)
    for index, line in enumerate(lines[:run_stop]):
        issue_match = ISSUE_RE.search(line)
        if not issue_match or candidate_foreign_strict_36_markers(line, config):
            continue
        next_issue = next(
            (
                position
                for position in range(index + 1, run_stop)
                if ISSUE_RE.search(lines[position])
            ),
            run_stop,
        )
        context = candidate_context(
            lines,
            index,
            config,
            block_start=0,
            block_end=next_issue,
        )
        if config.keywords and not all_keywords_present(context, config.keywords):
            continue

        issue = int(issue_match.group(1))
        seen_matching_issues.add(issue)
        numbers = collect_numbers_after_in_section(lines, index, config, stop=next_issue)
        if not numbers or not valid_36_code_record(numbers):
            diagnostic_numbers, raw_tokens = collect_candidate_numbers_for_diagnostics(
                lines,
                index,
                config,
                stop=next_issue,
            )
            invalid_candidates.append(
                (issue, index, invalid_36_code_diagnostic_reason(diagnostic_numbers, raw_tokens))
            )
            continue
        candidates.append(
            candidate_evidence(
                lines,
                index,
                issue,
                numbers,
                config,
                full_range,
                anchor_line=lines[0],
            )
        )
    return candidates, invalid_candidates, seen_matching_issues


def extract_topic_content_3x12(text_or_html: str, config: SiteConfig) -> SiteResult:
    return extract_from_candidates(
        text_or_html,
        config,
        topic_content_3x12_candidates,
        no_candidates_message="专属正文三行36码栏目没有找到符合条件的数据",
        issue_range_error="no 36-number record found in issue range",
    )


def marker_after_issue_3x12_candidates(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[CandidateEvidence], list[tuple[int, int, str]], set[int]]:
    lines = html_to_lines(text_or_html)
    starts = [
        index
        for index, line in enumerate(lines)
        if all_keywords_present(line, config.section_keywords)
    ]
    if not starts:
        raise ScrapeError("未找到栏目关键词: " + "、".join(config.section_keywords))

    candidates: list[CandidateEvidence] = []
    invalid_candidates: list[tuple[int, int, str]] = []
    seen_matching_issues: set[int] = set()
    for start in starts:
        document_range = document_range_for_index(lines, start)
        stop = min(document_range.stop, start + config.search_window)
        next_heading = next(
            (
                index
                for index in range(start + 1, stop)
                if lines[index].startswith(config.name)
                and not ISSUE_RE.search(lines[index])
            ),
            None,
        )
        if next_heading is not None:
            stop = next_heading

        for index in range(start + 1, stop):
            issue_match = ISSUE_RE.search(lines[index])
            if not issue_match or candidate_foreign_strict_36_markers(lines[index], config):
                continue
            next_issue = next(
                (
                    position
                    for position in range(index + 1, stop)
                    if ISSUE_RE.search(lines[position])
                ),
                stop,
            )
            context = candidate_context(
                lines,
                index,
                config,
                block_start=start,
                block_end=next_issue,
            )
            if config.keywords and not all_keywords_present(context, config.keywords):
                continue
            issue = int(issue_match.group(1))
            seen_matching_issues.add(issue)
            numbers = collect_numbers_after_in_section(lines, index, config, stop=next_issue)
            if not numbers or not valid_36_code_record(numbers):
                diagnostic_numbers, raw_tokens = collect_candidate_numbers_for_diagnostics(
                    lines,
                    index,
                    config,
                    stop=next_issue,
                )
                invalid_candidates.append(
                    (issue, index, invalid_36_code_diagnostic_reason(diagnostic_numbers, raw_tokens))
                )
                continue
            candidates.append(
                candidate_evidence(
                    lines,
                    index,
                    issue,
                    numbers,
                    config,
                    range(start, stop),
                    anchor_line=lines[start],
                )
            )
    return candidates, invalid_candidates, seen_matching_issues


def extract_marker_after_issue_3x12(text_or_html: str, config: SiteConfig) -> SiteResult:
    return extract_from_candidates(
        text_or_html,
        config,
        marker_after_issue_3x12_candidates,
        no_candidates_message="内幕快报专属36码栏目没有找到符合条件的数据",
        issue_range_error="no 36-number record found in issue range",
    )


def section_3x12_candidates(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[CandidateEvidence], list[tuple[int, int, str]], set[int]]:
    lines = html_to_lines(text_or_html)
    starts = [
        index
        for index, line in enumerate(lines)
        if all_keywords_present(line, config.section_keywords)
    ]
    if not starts:
        raise ScrapeError("未找到栏目关键词: " + "、".join(config.section_keywords))

    candidates: list[CandidateEvidence] = []
    invalid_candidates: list[tuple[int, int, str]] = []
    seen_matching_issues: set[int] = set()
    for start in starts:
        document_range = document_range_for_index(lines, start)
        limit = min(document_range.stop, start + config.search_window)
        run_stop = limit
        previous_issue: int | None = None
        for position in range(start, limit):
            issue_match = ISSUE_RE.search(lines[position])
            if not issue_match:
                continue
            issue = int(issue_match.group(1))
            if previous_issue is not None and issue > previous_issue:
                run_stop = position
                break
            previous_issue = issue

        for index in range(start, run_stop):
            issue_match = ISSUE_RE.search(lines[index])
            if index == start or not issue_match or candidate_foreign_strict_36_markers(lines[index], config):
                continue
            next_issue = next(
                (
                    position
                    for position in range(index + 1, run_stop)
                    if ISSUE_RE.search(lines[position])
                ),
                run_stop,
            )
            context = candidate_context(
                lines,
                index,
                config,
                block_start=start,
                block_end=next_issue,
            )
            if config.keywords and not all_keywords_present(context, config.keywords):
                continue
            issue = int(issue_match.group(1))
            seen_matching_issues.add(issue)
            numbers = collect_numbers_after_in_section(lines, index, config, stop=next_issue)
            if not numbers or not valid_36_code_record(numbers):
                diagnostic_numbers, raw_tokens = collect_candidate_numbers_for_diagnostics(
                    lines,
                    index,
                    config,
                    stop=next_issue,
                )
                invalid_candidates.append(
                    (issue, index, invalid_36_code_diagnostic_reason(diagnostic_numbers, raw_tokens))
                )
                continue
            candidates.append(
                candidate_evidence(
                    lines,
                    index,
                    issue,
                    numbers,
                    config,
                    range(start, run_stop),
                    anchor_line=lines[start],
                )
            )
    return candidates, invalid_candidates, seen_matching_issues


def extract_section_3x12(text_or_html: str, config: SiteConfig) -> SiteResult:
    return extract_from_candidates(
        text_or_html,
        config,
        section_3x12_candidates,
        no_candidates_message="专属栏目三行36码没有找到符合条件的数据",
        issue_range_error="no 36-number record found in issue range",
    )


def fengkuang_zhongma_candidates(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[CandidateEvidence], list[tuple[int, int, str]], set[int]]:
    lines = html_to_lines(text_or_html)
    issue_matches = [(index, ISSUE_RE.search(line)) for index, line in enumerate(lines)]
    issue_matches = [(index, match) for index, match in issue_matches if match]
    candidates: list[CandidateEvidence] = []
    invalid_candidates: list[tuple[int, int, str]] = []
    seen_matching_issues: set[int] = set()

    for match_index, (line_index, match) in enumerate(issue_matches):
        issue = int(match.group(1))
        document_range = document_range_for_index(lines, line_index)
        next_issue = (
            issue_matches[match_index + 1][0]
            if match_index + 1 < len(issue_matches)
            else document_range.stop
        )
        block_stop = min(next_issue, document_range.stop)
        block_lines = lines[line_index:block_stop]
        block = "".join(block_lines)
        compact_block = re.sub(r"\s+", "", block)
        if "疯狂中码" not in compact_block or "三十六码" not in compact_block:
            continue

        seen_matching_issues.add(issue)
        bracket_parts = re.findall(r"【(.*?)】", compact_block)
        numbers = tuple(number for part in bracket_parts for number in line_numbers(part))
        if valid_36_code_record(numbers):
            candidates.append(
                candidate_evidence(
                    lines,
                    line_index,
                    issue,
                    numbers,
                    config,
                    range(line_index, block_stop),
                    raw_number_lines=tuple(
                        line for line in block_lines if line_numbers(line)
                    ),
                    anchor_line=lines[line_index],
                )
            )
            continue
        invalid_candidates.append((issue, line_index, invalid_36_code_diagnostic_reason(numbers, tuple(raw_digit_tokens(block)[:80]))))

    return candidates, invalid_candidates, seen_matching_issues


def extract_fengkuang_zhongma(text_or_html: str, config: SiteConfig) -> SiteResult:
    return extract_from_candidates(
        text_or_html,
        config,
        fengkuang_zhongma_candidates,
        no_candidates_message="疯狂中码专属解析没有找到符合条件的数据",
        issue_range_error=(
            f"no 36-number record found in issue range {DEFAULT_ISSUE_MIN}-{DEFAULT_ISSUE_MAX}"
        ),
    )


def yiyiba_candidates(
    text_or_html: str,
    config: SiteConfig,
) -> tuple[list[CandidateEvidence], list[tuple[int, int, str]], set[int]]:
    source_lines = first_topic_content_lines(text_or_html)
    lines: list[str] = []
    last_issue: int | None = None
    started = False
    for line in source_lines:
        issue_match = ISSUE_RE.search(line)
        if issue_match and all_keywords_present(line, config.section_keywords):
            issue = int(issue_match.group(1))
            if last_issue is not None and issue > last_issue:
                break
            last_issue = issue
            started = True
        if started:
            lines.append(line)
    if not started:
        lines = source_lines
    ranges = section_ranges(lines, config)
    if config.section_keywords and not ranges:
        raise ScrapeError("未找到栏目关键词: " + "、".join(config.section_keywords))

    candidates: list[CandidateEvidence] = []
    invalid_candidates: list[tuple[int, int, str]] = []
    seen_matching_issues: set[int] = set()
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
            numbers = collect_numbers_after_in_section(lines, index, config, stop=line_range.stop)
            if numbers and valid_36_code_record(numbers):
                candidates.append(
                    candidate_evidence(lines, index, issue, numbers, config, line_range)
                )
                continue
            diagnostic_numbers, raw_tokens = collect_candidate_numbers_for_diagnostics(
                lines, index, config, stop=line_range.stop
            )
            invalid_candidates.append(
                (issue, index, invalid_36_code_diagnostic_reason(diagnostic_numbers, raw_tokens))
            )
    return candidates, invalid_candidates, seen_matching_issues


def extract_yiyiba(text_or_html: str, config: SiteConfig) -> SiteResult:
    return extract_from_candidates(
        text_or_html,
        config,
        yiyiba_candidates,
        no_candidates_message="以已把专属精准36码栏目没有找到符合条件的数据",
        issue_range_error="no 36-number record found in issue range",
        include_invalid_candidates=False,
    )
