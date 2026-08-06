from __future__ import annotations

from dawei.domain.models import CandidateEvidence, CandidateOrigin, ParsedRecord


def evidence(
    name: str,
    url: str,
    issue: int,
    numbers: tuple[str, ...],
    *,
    page_index: int = 0,
    parser_id: str = "generic_36",
) -> CandidateEvidence:
    raw_issue_line = f"{issue}期 {name} 36码中特"
    raw_number_lines = (",".join(numbers),)
    document_id = f"test-document-{name}"
    block_id = f"{document_id}:{page_index}-{page_index + 1}"
    origin = CandidateOrigin(
        raw_issue_line,
        raw_number_lines,
        name,
        document_id,
        url,
        "test_fixture",
        block_id,
        page_index,
        page_index + 1,
        page_index,
        0,
        parser_id,
    )
    return CandidateEvidence(
        issue,
        numbers,
        raw_issue_line,
        raw_number_lines,
        name,
        document_id,
        url,
        "test_fixture",
        block_id,
        page_index,
        page_index + 1,
        page_index,
        0,
        parser_id,
        (origin,),
    )


def record(
    name: str,
    url: str,
    issue: int,
    numbers: tuple[str, ...],
    *,
    page_index: int | None = None,
    parser_id: str = "generic_36",
) -> ParsedRecord:
    position = issue if page_index is None else page_index
    candidate = evidence(
        name,
        url,
        issue,
        numbers,
        page_index=position,
        parser_id=parser_id,
    )
    return ParsedRecord(
        name,
        url,
        issue,
        numbers,
        raw_position=position,
        evidence=candidate,
    )
