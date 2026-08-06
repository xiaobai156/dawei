"""Shared, side-effect-free text and 36-number parsing primitives."""

from __future__ import annotations

from collections.abc import Iterable
from html.parser import HTMLParser
import re

from dawei.domain.models import SiteConfig


ISSUE_RE = re.compile(r"(?<!\d)(\d{3})\s*期")
STANDALONE_ISSUE_RE = re.compile(r"^\s*第\s*(\d{3})\s*期\s*$")
NUMBER_RE = re.compile(r"(?<!\d)(0[1-9]|[1-4]\d)(?!\d)")
STRICT_36_SIGNAL_RE = re.compile(r"36|三十六|六码|碼")
STRICT_36_MARKER_SIGNALS = (
    "36码", "36碼", "三十六码", "三十六碼", "三十六", "六码",
)
NUMBER_BLOCK_STOP_KEYWORDS = (
    "已解锁", "广告", "相关推荐", "免责", "版权所有", "注册",
    "充值", "规则", "客服", "返回顶部",
)
DOCUMENT_BOUNDARY = "DAWEI_DOCUMENT_BOUNDARY"


class VisibleTextParser(HTMLParser):
    block_tags = {
        "br",
        "p",
        "div",
        "tr",
        "td",
        "th",
        "li",
        "ul",
        "ol",
        "table",
        "tbody",
        "section",
        "article",
        "header",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.skip_depth += 1
        if tag in self.block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.skip_depth:
            self.skip_depth -= 1
        if tag in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if data.strip():
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


class FirstTopicContentTextParser(HTMLParser):
    block_tags = VisibleTextParser.block_tags

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.content_depth: int | None = None
        self.skip_depth = 0
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.content_depth is None and not self.found and tag == "div":
            attributes = dict(attrs)
            classes = set((attributes.get("class") or "").split())
            if "topic-content" in classes:
                self.content_depth = 1
                self.found = True
                return
        if self.content_depth is None:
            return
        if tag == "div":
            self.content_depth += 1
        if tag in {"script", "style"}:
            self.skip_depth += 1
        if tag in self.block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.content_depth is None:
            return
        if tag in {"script", "style"} and self.skip_depth:
            self.skip_depth -= 1
        if tag in self.block_tags:
            self.parts.append("\n")
        if tag == "div":
            self.content_depth -= 1
            if self.content_depth == 0:
                self.content_depth = None

    def handle_data(self, data: str) -> None:
        if self.content_depth is not None and not self.skip_depth and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def first_topic_content_lines(text_or_html: str) -> list[str]:
    parser = FirstTopicContentTextParser()
    parser.feed(text_or_html)
    if parser.found:
        text = parser.text()
        return [
            line
            for line in (
                re.sub(r"\s+", " ", raw_line.replace("\xa0", " ")).strip()
                for raw_line in text.splitlines()
            )
            if line
        ]
    return html_to_lines(text_or_html)


def html_to_lines(html: str) -> list[str]:
    parser = VisibleTextParser()
    parser.feed(html)
    raw_text = parser.text()
    lines = []
    for raw_line in raw_text.splitlines():
        line = re.sub(r"\s+", " ", raw_line.replace("\xa0", " ")).strip()
        if line:
            lines.append(line)
    return lines


def keyword_present(text: str, keywords: Iterable[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def all_keywords_present(text: str, keywords: Iterable[str]) -> bool:
    values = tuple(keyword for keyword in keywords if keyword)
    return all(keyword in text for keyword in values)


def strict_36_keywords(config: SiteConfig) -> tuple[str, ...]:
    keywords = tuple(config.keywords) + tuple(config.section_keywords)
    return tuple(keyword for keyword in keywords if STRICT_36_SIGNAL_RE.search(keyword))


def strict_36_marker_snippets(text: str) -> tuple[str, ...]:
    compact = re.sub(r"\s+", "", text)
    snippets: list[str] = []
    for signal in STRICT_36_MARKER_SIGNALS:
        start = 0
        while True:
            index = compact.find(signal, start)
            if index < 0:
                break
            snippet = compact[max(0, index - 10) : min(len(compact), index + len(signal) + 10)]
            if snippet not in snippets:
                snippets.append(snippet)
            start = index + len(signal)
    return tuple(snippets)


def candidate_foreign_strict_36_markers(line: str, config: SiteConfig) -> tuple[str, ...]:
    markers = strict_36_marker_snippets(line)
    if not markers:
        return ()

    keywords = tuple(re.sub(r"\s+", "", keyword) for keyword in strict_36_keywords(config))
    if not keywords:
        return ()
    if any(any(keyword in marker for keyword in keywords) for marker in markers):
        return ()
    return markers


def line_has_foreign_strict_36_marker(line: str, config: SiteConfig) -> bool:
    return bool(candidate_foreign_strict_36_markers(line, config))


def has_strict_36_signal(text: str, config: SiteConfig) -> bool:
    keywords = strict_36_keywords(config)
    if keywords:
        return keyword_present(text, keywords)
    return bool(STRICT_36_SIGNAL_RE.search(text))


def section_anchor_is_strict_enough(lines: list[str], index: int, config: SiteConfig) -> bool:
    line = lines[index]
    return all_keywords_present(line, config.section_keywords)


def _keyword_only_heading(line: str, config: SiteConfig) -> bool:
    compact = re.sub(r"[\W_]+", "", line)
    return any(compact == re.sub(r"[\W_]+", "", keyword) for keyword in config.keywords)


def explicit_section_boundary(line: str, config: SiteConfig) -> bool:
    if DOCUMENT_BOUNDARY in line:
        return True
    if any(keyword in line for keyword in NUMBER_BLOCK_STOP_KEYWORDS):
        return True
    if re.search(r"(?:作者|楼主|上一篇|下一篇)\s*[:：]?", line) and not all_keywords_present(
        line,
        config.section_keywords,
    ):
        return True
    return (
        not ISSUE_RE.search(line)
        and any(signal in re.sub(r"\s+", "", line) for signal in STRICT_36_MARKER_SIGNALS)
        and not all_keywords_present(line, config.section_keywords)
        and not _keyword_only_heading(line, config)
    )


def plain_section_heading_boundary(
    lines: list[str],
    index: int,
    config: SiteConfig,
    *,
    stop: int | None = None,
) -> bool:
    line = lines[index].strip()
    compact = re.sub(r"\s+", "", line)
    if not compact:
        return False
    if ISSUE_RE.search(line) or raw_digit_tokens(line):
        return False
    if "http://" in line.lower() or "https://" in line.lower():
        return False
    if all_keywords_present(line, config.section_keywords):
        return False
    if any(keyword and keyword in line for keyword in (*config.section_keywords, *config.keywords)):
        return False
    search_stop = min(len(lines), stop if stop is not None else len(lines))
    return any(ISSUE_RE.search(lines[position]) for position in range(index + 1, search_stop))


def document_range_for_index(lines: list[str], index: int) -> range:
    if not 0 <= index < len(lines):
        raise ValueError(f"document index out of range: {index}")
    start = 0
    for position in range(index - 1, -1, -1):
        if DOCUMENT_BOUNDARY in lines[position]:
            start = position + 1
            break
    stop = len(lines)
    for position in range(index + 1, len(lines)):
        if DOCUMENT_BOUNDARY in lines[position]:
            stop = position
            break
    return range(start, stop)


def section_boundary_at(
    lines: list[str],
    index: int,
    config: SiteConfig,
    *,
    stop: int | None = None,
) -> bool:
    return explicit_section_boundary(lines[index], config) or plain_section_heading_boundary(
        lines,
        index,
        config,
        stop=stop,
    )


def section_stop_index(
    lines: list[str],
    start: int,
    config: SiteConfig,
    *,
    stop: int | None = None,
    include_plain_heading: bool = True,
) -> int:
    limit = min(len(lines), stop if stop is not None else len(lines))
    saw_issue = False
    for index in range(start + 1, limit):
        if explicit_section_boundary(lines[index], config):
            return index
        if ISSUE_RE.search(lines[index]):
            saw_issue = True
            continue
        if (
            include_plain_heading
            and saw_issue
            and plain_section_heading_boundary(lines, index, config, stop=limit)
        ):
            return index
    return limit


def section_ranges(lines: list[str], config: SiteConfig) -> list[range]:
    if not config.section_keywords:
        boundaries = [
            index for index, line in enumerate(lines) if DOCUMENT_BOUNDARY in line
        ]
        starts = [0, *(index + 1 for index in boundaries)]
        stops = [*boundaries, len(lines)]
        return [range(start, stop) for start, stop in zip(starts, stops) if start < stop]

    starts = [
        index
        for index, line in enumerate(lines)
        if all_keywords_present(line, config.section_keywords)
        and section_anchor_is_strict_enough(lines, index, config)
    ]
    if not starts:
        return []

    ranges: list[range] = []
    for start_index, start in enumerate(starts):
        stop = min(len(lines), start + config.search_window)
        if start_index + 1 < len(starts):
            stop = min(stop, starts[start_index + 1])
        boundary = section_stop_index(lines, start, config, stop=stop)
        stop = min(stop, boundary)
        ranges.append(range(start, stop))
    return ranges


def normalize_merged_highlight_numbers(text: str) -> str:
    return re.sub(r"(?<!\d)((?:0[1-9])|(?:[1-4]\d))\1(?!\d)", r"\1", text)


def line_numbers(line: str) -> list[str]:
    return NUMBER_RE.findall(normalize_merged_highlight_numbers(line))


def line_numbers_with_zero(line: str) -> list[str]:
    return re.findall(r"(?<!\d)(00|0[1-9]|[1-4]\d)(?!\d)", normalize_merged_highlight_numbers(line))


def raw_digit_tokens(line: str) -> list[str]:
    return re.findall(r"(?<!\d)\d+(?!\d)", normalize_merged_highlight_numbers(line))


def merged_digit_tokens(tokens: Iterable[str]) -> list[str]:
    return [token for token in tokens if token.isdigit() and len(token) > 2]


def is_number_block_stop_line(line: str, config: SiteConfig | None = None) -> bool:
    if DOCUMENT_BOUNDARY in line:
        return True
    if ISSUE_RE.search(line):
        return True
    if config is not None and line_has_foreign_strict_36_marker(line, config):
        return True
    return any(keyword in line for keyword in NUMBER_BLOCK_STOP_KEYWORDS)


def collect_numbers_inline(line: str) -> tuple[str, ...] | None:
    bracket_parts = re.findall(r"[【\[\(（]([^】\]\)）]{60,})[】\]\)）]", line)
    for part in bracket_parts:
        part = normalize_merged_highlight_numbers(part)
        digits = re.sub(r"\D+", "", part)
        if len(digits) == 72:
            numbers = tuple(digits[index : index + 2] for index in range(0, len(digits), 2))
            if len(numbers) == 36 and all(1 <= int(number) <= 49 for number in numbers):
                return numbers
    return None


def collect_numbers_after(
    lines: list[str],
    start: int,
    min_numbers_per_line: int = 2,
) -> tuple[str, ...] | None:
    numbers: list[str] = []
    started = False
    for index in range(start + 1, len(lines)):
        if is_number_block_stop_line(lines[index]):
            break
        raw_tokens = raw_digit_tokens(lines[index])
        if merged_digit_tokens(raw_tokens):
            return None
        current = line_numbers(lines[index])
        if len(current) >= min_numbers_per_line:
            started = True
            numbers.extend(current)
            if len(numbers) >= 36:
                return tuple(numbers)
        elif started:
            break
    return None


def collect_numbers_after_in_section(
    lines: list[str],
    start: int,
    config: SiteConfig,
    stop: int | None = None,
) -> tuple[str, ...] | None:
    numbers: list[str] = []
    started = False
    section_stop = min(len(lines), stop) if stop is not None else len(lines)
    for index in range(start + 1, section_stop):
        if is_number_block_stop_line(lines[index], config):
            break
        raw_tokens = raw_digit_tokens(lines[index])
        if merged_digit_tokens(raw_tokens):
            return None
        current = line_numbers(lines[index])
        if len(current) >= config.min_numbers_per_line:
            started = True
            numbers.extend(current)
            if len(numbers) >= 36:
                return tuple(numbers)
        elif started:
            break
    return None


def collect_numbers_after_for_diagnostics(
    lines: list[str],
    start: int,
    min_numbers_per_line: int = 2,
    config: SiteConfig | None = None,
    stop: int | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    numbers: list[str] = []
    raw_tokens: list[str] = []
    started = False
    section_stop = min(len(lines), stop) if stop is not None else len(lines)
    for index in range(start + 1, section_stop):
        if is_number_block_stop_line(lines[index], config):
            break
        current_raw = raw_digit_tokens(lines[index])
        current = line_numbers(lines[index])
        if current_raw:
            raw_tokens.extend(current_raw)
        if merged_digit_tokens(current_raw):
            break
        if len(current_raw) >= min_numbers_per_line or len(current) >= min_numbers_per_line:
            started = True
        if len(current) >= min_numbers_per_line:
            numbers.extend(current)
        if len(numbers) >= 36:
            break
        if started and len(current) < min_numbers_per_line:
            break
    return tuple(numbers), tuple(raw_tokens[:80])


def collect_non_zero_numbers_after(
    lines: list[str],
    start: int,
    min_numbers_per_line: int = 1,
) -> tuple[str, ...] | None:
    numbers: list[str] = []
    started = False
    for index in range(start + 1, len(lines)):
        if is_number_block_stop_line(lines[index]):
            break
        raw_tokens = raw_digit_tokens(lines[index])
        if merged_digit_tokens(raw_tokens):
            return None
        current = [number for number in line_numbers_with_zero(lines[index]) if number != "00"]
        if len(current) >= min_numbers_per_line:
            started = True
            numbers.extend(current)
        elif started:
            break
    numbers_tuple = tuple(numbers)
    return numbers_tuple if valid_36_code_record(numbers_tuple) else None


def collect_non_zero_numbers_after_in_section(
    lines: list[str],
    start: int,
    config: SiteConfig,
    stop: int | None = None,
) -> tuple[str, ...] | None:
    numbers: list[str] = []
    started = False
    section_stop = min(len(lines), stop) if stop is not None else len(lines)
    for index in range(start + 1, section_stop):
        if is_number_block_stop_line(lines[index], config):
            break
        raw_tokens = raw_digit_tokens(lines[index])
        if merged_digit_tokens(raw_tokens):
            return None
        current = [number for number in line_numbers_with_zero(lines[index]) if number != "00"]
        if len(current) >= config.min_numbers_per_line:
            started = True
            numbers.extend(current)
        elif started:
            break
    numbers_tuple = tuple(numbers)
    return numbers_tuple if valid_36_code_record(numbers_tuple) else None


def collect_non_zero_numbers_after_for_diagnostics(
    lines: list[str],
    start: int,
    min_numbers_per_line: int = 1,
    config: SiteConfig | None = None,
    stop: int | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    numbers: list[str] = []
    raw_tokens: list[str] = []
    started = False
    section_stop = min(len(lines), stop) if stop is not None else len(lines)
    for index in range(start + 1, section_stop):
        if is_number_block_stop_line(lines[index], config):
            break
        current_raw = raw_digit_tokens(lines[index])
        current = [number for number in line_numbers_with_zero(lines[index]) if number != "00"]
        if current_raw:
            raw_tokens.extend(current_raw)
        if merged_digit_tokens(current_raw):
            break
        if len(current_raw) >= min_numbers_per_line or len(current) >= min_numbers_per_line:
            started = True
        if len(current) >= min_numbers_per_line:
            numbers.extend(current)
        elif started:
            break
    return tuple(numbers), tuple(raw_tokens[:80])


def collect_numbers_before(
    lines: list[str],
    start: int,
    min_numbers_per_line: int = 2,
    block_start: int = 0,
) -> tuple[str, ...] | None:
    rows: list[list[str]] = []
    total = 0
    started = False
    for index in range(start - 1, block_start - 1, -1):
        if is_number_block_stop_line(lines[index]):
            break
        current_raw = raw_digit_tokens(lines[index])
        if merged_digit_tokens(current_raw):
            return None
        current = line_numbers(lines[index])
        if len(current) >= min_numbers_per_line:
            started = True
            rows.append(current)
            total += len(current)
        if total >= 36:
            numbers = [number for row in reversed(rows) for number in row]
            return tuple(numbers)
        if started and len(current) < min_numbers_per_line:
            break
    return None


def collect_numbers_before_for_diagnostics(
    lines: list[str],
    start: int,
    min_numbers_per_line: int = 2,
    block_start: int = 0,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rows: list[list[str]] = []
    raw_rows: list[list[str]] = []
    total = 0
    started = False
    for index in range(start - 1, block_start - 1, -1):
        if is_number_block_stop_line(lines[index]):
            break
        current = line_numbers(lines[index])
        current_raw = raw_digit_tokens(lines[index])
        if current_raw:
            raw_rows.append(current_raw)
        if merged_digit_tokens(current_raw):
            break
        if len(current_raw) >= min_numbers_per_line or len(current) >= min_numbers_per_line:
            started = True
        if len(current) >= min_numbers_per_line:
            rows.append(current)
            total += len(current)
        if total >= 36:
            numbers = [number for row in reversed(rows) for number in row]
            raw_tokens = [number for row in reversed(raw_rows) for number in row]
            return tuple(numbers), tuple(raw_tokens[:80])
        if started and len(current) < min_numbers_per_line:
            break
    raw_tokens = [number for row in reversed(raw_rows) for number in row]
    numbers = [number for row in reversed(rows) for number in row]
    return tuple(numbers), tuple(raw_tokens[:80])


def valid_36_code_record(numbers: tuple[str, ...]) -> bool:
    return (
        len(numbers) == 36
        and len(set(numbers)) == 36
        and all(1 <= int(number) <= 49 for number in numbers)
    )


def duplicate_numbers(numbers: tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for number in numbers:
        if number in seen:
            duplicates.add(number)
        seen.add(number)
    return sorted(duplicates, key=int)


def invalid_36_code_reason(numbers: tuple[str, ...] | None) -> str:
    if not numbers:
        return "没有收集到号码"
    if len(numbers) != 36:
        return f"号码数量不是36个: 实际{len(numbers)}个"

    out_of_range = [number for number in numbers if not (1 <= int(number) <= 49)]
    if out_of_range:
        return "号码超出01-49范围: " + ",".join(out_of_range)

    duplicates = duplicate_numbers(numbers)
    if duplicates:
        return "重复数字: " + ",".join(duplicates)

    return "未知无效原因"


def invalid_36_code_diagnostic_reason(
    numbers: tuple[str, ...] | None,
    raw_tokens: tuple[str, ...] = (),
) -> str:
    details: list[str] = []
    merged_tokens = merged_digit_tokens(raw_tokens)
    if merged_tokens:
        details.append("疑似连写数字: " + ",".join(merged_tokens[:20]))
    if not numbers:
        if not merged_tokens:
            details.append("没有收集到有效号码")
    else:
        if len(numbers) != 36 and not merged_tokens:
            details.append(f"有效号码数量不是36个: 实际{len(numbers)}个")
        out_of_range = [number for number in numbers if number.isdigit() and not (1 <= int(number) <= 49)]
        if out_of_range and not merged_tokens:
            details.append("有效号码超出01-49范围: " + ",".join(out_of_range))
        duplicates = duplicate_numbers(numbers)
        if duplicates and not merged_tokens:
            details.append("有效号码重复: " + ",".join(duplicates))

    if raw_tokens:
        raw_out_of_range = [token for token in raw_tokens if token.isdigit() and not (1 <= int(token) <= 49)]
        if raw_out_of_range:
            details.append("原始数字超出01-49范围: " + ",".join(raw_out_of_range[:20]))
        raw_duplicates = duplicate_numbers(tuple(token.zfill(2) for token in raw_tokens if token.isdigit() and 1 <= int(token) <= 49))
        if raw_duplicates and not merged_tokens:
            details.append("原始数字重复: " + ",".join(raw_duplicates))
        details.append("原始数字片段: " + ",".join(raw_tokens[:40]))

    return "；".join(details) if details else invalid_36_code_reason(numbers)
