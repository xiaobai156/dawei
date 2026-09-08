from __future__ import annotations

import unittest
from dataclasses import replace

from dawei.domain.errors import ValidationError
from dawei.domain.models import (
    CandidateEvidence,
    CandidateOrigin,
    ParsedRecord,
    SiteConfig,
)
from dawei.domain.validation import (
    validate_36_numbers,
    validate_candidate_evidence,
    validate_site_config,
)

VALID_NUMBERS = tuple(f"{number:02d}" for number in range(1, 37))


def valid_site_config() -> SiteConfig:
    return SiteConfig(
        name="测试站",
        url="https://example.test/topic",
        keywords=("专属",),
        section_keywords=("栏目",),
        region="bottom",
        site_id="site_test",
        parser_id="generic_36",
    )


def valid_origin() -> CandidateOrigin:
    return CandidateOrigin(
        raw_issue_line="225期",
        raw_number_lines=(" ".join(VALID_NUMBERS),),
        anchor_line="栏目",
        document_id="document-1",
        document_url="https://example.test/topic",
        source_method="generic_html",
        block_id="document-1:0-10",
        block_start=0,
        block_end=10,
        page_index=0,
        block_index=0,
        parser_id="generic_36",
    )


def valid_evidence(origin: CandidateOrigin | None = None) -> CandidateEvidence:
    origin = origin or valid_origin()
    return CandidateEvidence(
        issue=225,
        numbers=VALID_NUMBERS,
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


def valid_record(evidence: CandidateEvidence | None = None) -> ParsedRecord:
    evidence = evidence or valid_evidence()
    return ParsedRecord(
        name="测试站",
        url="https://example.test/topic",
        issue=evidence.issue,
        numbers=evidence.numbers,
        raw_position=evidence.page_index,
        evidence=evidence,
    )


class Validate36NumbersTests(unittest.TestCase):
    def test_accepts_exact_two_digit_strings_in_range(self) -> None:
        self.assertEqual(VALID_NUMBERS, validate_36_numbers(VALID_NUMBERS))

    def test_rejects_non_string_and_non_exact_two_digit_values(self) -> None:
        for invalid in (True, 1, "00", "50", "001", "0001"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validate_36_numbers((invalid, *VALID_NUMBERS[1:]))


class SiteConfigValidationTests(unittest.TestCase):
    def test_required_fields_must_be_non_empty_strings(self) -> None:
        for field in ("name", "url", "site_id", "parser_id", "source_type", "position"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_site_config(replace(valid_site_config(), **{field: None}))

        with self.assertRaises(ValidationError):
            validate_site_config(replace(valid_site_config(), render_policy=None))

    def test_optional_strings_reject_non_strings(self) -> None:
        for field in ("region", "api_url", "image_decoder", "record_id", "onboarding_exception"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_site_config(replace(valid_site_config(), **{field: 1}))

    def test_numeric_flags_and_sequences_have_strict_runtime_types(self) -> None:
        for field in ("search_window", "min_numbers_per_line"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_site_config(replace(valid_site_config(), **{field: True}))
        for field in ("numbers_before_issue", "drop_zero_numbers", "render_browser"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_site_config(replace(valid_site_config(), **{field: 1}))
        for field in ("keywords", "section_keywords", "navigation_keywords"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_site_config(replace(valid_site_config(), **{field: ["栏目"]}))
        with self.assertRaises(ValidationError):
            validate_site_config(replace(valid_site_config(), onboarding_valid_issues=(True,)))

    def test_paginated_article_list_is_fixed_to_http(self) -> None:
        base = replace(
            valid_site_config(),
            source_type="paginated_article_list",
            parser_id="paginated_article_36",
            navigation_keywords=("下一页",),
        )
        validate_site_config(base)
        for changes in (
            {"render_policy": "fallback"},
            {"render_policy": "always"},
            {"render_browser": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                validate_site_config(replace(base, **changes))


class CandidateEvidenceValidationTests(unittest.TestCase):
    def test_record_identity_and_position_are_required(self) -> None:
        for field, value in (
            ("name", ""),
            ("url", None),
            ("raw_position", True),
            ("raw_position", -1),
            ("raw_position", 1),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                validate_candidate_evidence(replace(valid_record(), **{field: value}))

    def test_candidate_evidence_requires_true_issue_int(self) -> None:
        evidence = replace(valid_evidence(), issue=True)
        record = valid_record(evidence)
        with self.assertRaises(ValidationError):
            validate_candidate_evidence(record)

    def test_origin_requires_non_negative_true_positions_and_identity_text(self) -> None:
        for field, value in (
            ("block_start", True),
            ("block_end", "10"),
            ("page_index", -1),
            ("block_index", True),
            ("document_id", ""),
            ("parser_id", 1),
        ):
            with self.subTest(field=field):
                origin = replace(valid_origin(), **{field: value})
                evidence = valid_evidence(origin)
                with self.assertRaises(ValidationError):
                    validate_candidate_evidence(valid_record(evidence))

    def test_record_and_evidence_numbers_are_each_strictly_validated(self) -> None:
        evidence = valid_evidence()
        with self.assertRaises(ValidationError):
            validate_candidate_evidence(
                replace(valid_record(evidence), numbers=("1", *VALID_NUMBERS[1:]))
            )
        invalid_evidence = replace(evidence, numbers=("001", *VALID_NUMBERS[1:]))
        with self.assertRaises(ValidationError):
            validate_candidate_evidence(
                replace(valid_record(invalid_evidence), numbers=VALID_NUMBERS)
            )


if __name__ == "__main__":
    unittest.main()
