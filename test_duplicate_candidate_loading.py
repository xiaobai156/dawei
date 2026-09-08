from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dawei.application.duplicate_runner import load_candidate_sites
from dawei.domain.errors import ScrapeError


def candidate_mapping(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "name": "候选站",
        "url": "https://example.test/article/manager/abc123",
        "keywords": ["专属36码"],
        "section_keywords": ["候选栏目"],
        "region": "bottom",
        "site_id": "site_candidate",
        "source_type": "dynamic_article",
        "parser_id": "dynamic_article",
        "render_policy": "never",
        "api_url": "https://example.test/api/articles/abc123",
        "record_id": "abc123",
    }
    record.update(overrides)
    return record


def load_mapping(record: dict[str, object]) -> tuple:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "candidate.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return load_candidate_sites((str(path),))


class CandidateLoadingTests(unittest.TestCase):
    def test_candidate_requires_explicit_identity_fields(self) -> None:
        required = (
            "name",
            "url",
            "site_id",
            "source_type",
            "parser_id",
            "region",
            "render_policy",
            "section_keywords",
            "keywords",
        )
        for field in required:
            record = candidate_mapping()
            del record[field]
            with self.subTest(field=field), self.assertRaisesRegex(ScrapeError, "必须显式配置"):
                load_mapping(record)

    def test_dynamic_candidate_requires_source_contract(self) -> None:
        for field in ("api_url", "record_id"):
            record = candidate_mapping()
            del record[field]
            with self.subTest(field=field), self.assertRaisesRegex(ScrapeError, "必须显式配置"):
                load_mapping(record)
        record = candidate_mapping(source_type="dynamic_collection", parser_id="kunnan_magazine")
        del record["api_url"]
        with self.assertRaisesRegex(ScrapeError, "必须显式配置"):
            load_mapping(record)

    def test_candidate_identity_fields_are_validated_before_inference(self) -> None:
        for field, value in (
            ("name", ""),
            ("url", ""),
            ("site_id", ""),
            ("source_type", ""),
            ("parser_id", ""),
            ("region", "tail"),
            ("render_policy", "guess"),
            ("keywords", "专属36码"),
            ("section_keywords", [""]),
        ):
            record = candidate_mapping(**{field: value})
            with self.subTest(field=field), self.assertRaises(ScrapeError):
                load_mapping(record)

    def test_candidate_optional_sequences_reject_coercible_values(self) -> None:
        for field, value in (
            ("navigation_keywords", [1]),
            ("onboarding_valid_issues", ["236"]),
            ("onboarding_missing_issues", ["235"]),
        ):
            record = candidate_mapping(**{field: value})
            with self.subTest(field=field), self.assertRaises(ScrapeError):
                load_mapping(record)

    def test_paginated_candidate_cannot_enable_browser_rendering(self) -> None:
        base = candidate_mapping(
            source_type="paginated_article_list",
            parser_id="paginated_article_36",
            navigation_keywords=["下一页"],
        )
        for changes in (
            {"render_policy": "fallback"},
            {"render_policy": "always"},
            {"render_browser": True},
        ):
            record = dict(base)
            record.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ScrapeError):
                load_mapping(record)

    def test_candidate_requires_existing_regular_file(self) -> None:
        with TemporaryDirectory() as directory, self.assertRaisesRegex(ScrapeError, "普通文件"):
            load_candidate_sites((directory,))
        with self.assertRaisesRegex(ScrapeError, "不存在"):
            load_candidate_sites(("missing-candidate.json",))

    def test_explicit_candidate_mapping_loads(self) -> None:
        sites = load_mapping(candidate_mapping())

        self.assertEqual(("site_candidate",), tuple(site.site_id for site in sites))


if __name__ == "__main__":
    unittest.main()
