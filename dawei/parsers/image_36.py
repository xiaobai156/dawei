"""Pure parsing of the authorized, directionless 六合王 image source."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from urllib.parse import parse_qs, urlsplit

from dawei.domain.errors import ScrapeError
from dawei.domain.models import ParsedRecord, SiteConfig
from dawei.parsers.generic_36 import candidate_evidence, parsed_record_from_candidate


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).translate(
        str.maketrans("碼圍", "码围")
    )


def image_url(config: SiteConfig, year: int, issue_text: str) -> str:
    query = parse_qs(urlsplit(config.url).query)
    if (query.get("name") != ["6hwtw.jpg"] or query.get("id") != ["171"]
            or query.get("color") != ["0"]):
        raise ScrapeError("六合王图片来源身份不符")
    return f"https://amtk.tuku99988.com/galleryfiles/system/big-pic/col/{year}/{issue_text}/6hwtw.jpg"


def resolve_image_url(payload: object, config: SiteConfig) -> str:
    if config.fixed_issue is None:
        raise ScrapeError("六合王图片抓取必须指定期数，不扫描历史图片")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ScrapeError("六合王期数API结构无效")
    year = datetime.now(timezone(timedelta(hours=8))).year
    matches = set()
    for row in payload["data"]:
        if not isinstance(row, dict) or row.get("type") != "am":
            continue
        if str(row.get("year")) != str(year):
            continue
        issue_text = str(row.get("qi", ""))
        if re.fullmatch(r"[0-9]{1,3}", issue_text) and int(issue_text) == config.fixed_issue:
            matches.add(image_url(config, year, issue_text))
    if len(matches) != 1:
        raise ScrapeError(f"六合王当前年份指定{config.fixed_issue}期图片缺失或来源冲突")
    return matches.pop()


def extract_tuku2135(document: str, config: SiteConfig) -> ParsedRecord:
    if config.fixed_issue is None or config.direction != "none":
        raise ScrapeError("六合王只按指定期识别，不使用TOP/BOTTOM方向")
    if len(config.section_keywords) != 1 or len(config.keywords) != 1:
        raise ScrapeError("六合王必须配置唯一图片栏目和字段锚点")
    try:
        data = json.loads(document)
        texts, scores, boxes = (data[key] for key in ("rec_texts", "rec_scores", "rec_boxes"))
        if not texts or not len(texts) == len(scores) == len(boxes):
            raise ValueError("OCR文本、分数、坐标数量不一致")
        for text, score, box in zip(texts, scores, boxes):
            if not isinstance(text, str) or not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("OCR文本或置信度无效")
            if (len(box) != 4 or any(not math.isfinite(value) or value < 0 for value in box)
                    or box[0] >= box[2] or box[1] >= box[3]):
                raise ValueError("OCR坐标无效")
        source_url = data["image_url"]
        parts = urlsplit(source_url).path.split("/")
        year, issue_text = int(parts[-3]), parts[-2]
        if (year != datetime.now(timezone(timedelta(hours=8))).year or int(issue_text) != config.fixed_issue
                or source_url != image_url(config, year, issue_text)
                or not re.fullmatch(r"[0-9a-f]{64}", data["image_sha256"])):
            raise ValueError("图片来源或内容哈希无效")
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise ScrapeError(f"六合王OCR原始证据无效: {exc}") from exc

    ordered = sorted(zip(texts, scores, boxes), key=lambda row: (row[2][1], row[2][0]))
    lines = [row[0] for row in ordered]
    normalized = [_compact(line) for line in lines]
    title = _compact(config.section_keywords[0] + config.keywords[0])
    anchors = [i for i, line in enumerate(normalized) if line == title]
    issues = [(i, re.fullmatch(r"第([0-9]{1,3})期", line)) for i, line in enumerate(normalized)]
    issues = [(i, match) for i, match in issues if match]
    if len(anchors) != 1 or len(issues) != 1:
        raise ScrapeError("六合王图片标题或目标期号缺失/冲突")
    start, (end, issue_match) = anchors[0], issues[0]
    issue = int(issue_match.group(1))
    if issue != config.fixed_issue or start >= end:
        raise ScrapeError(f"六合王图片实际{issue}期与指定{config.fixed_issue}期或区块位置不符")
    numeric = [i for i in range(start + 1, end) if re.search(r"[0-9]", normalized[i])]
    row_pattern = r"[0-9]{2}(?:,[0-9]{2}){8}"
    all_rows = [i for i, line in enumerate(normalized) if re.fullmatch(row_pattern, line)]
    if len(numeric) != 4 or numeric != all_rows:
        raise ScrapeError("六合王标题与期号间必须恰好四行九码，禁止跨区块补数")
    selected = [start, *numeric, end]
    if any(ordered[i][1] < 0.9 for i in selected):
        raise ScrapeError("六合王标题、期数或号码OCR置信度不足")
    if any(ordered[left][2][3] >= ordered[right][2][1] for left, right in pairwise(selected)):
        raise ScrapeError("六合王图片行位置重叠，不能确定原始顺序")
    numbers = tuple(number for i in numeric for number in normalized[i].split(","))
    evidence = candidate_evidence(
        lines, end, issue, numbers, replace(config, url=source_url), range(start, end + 1),
        raw_number_lines=tuple(lines[i] for i in numeric), anchor_line=lines[start],
    )
    identity = "sha256:" + data["image_sha256"]
    origin = replace(evidence.origins[0], document_id=identity, source_method="image_ocr")
    evidence = replace(evidence, document_id=identity, source_method="image_ocr", origins=(origin,))
    return replace(parsed_record_from_candidate(config, evidence), record_path=source_url)
