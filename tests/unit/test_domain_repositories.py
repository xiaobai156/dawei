from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from dawei.domain.errors import (
    CacheConflictError,
    CacheRollbackError,
    ConfigurationError,
    ValidationError,
)
from dawei.domain.models import ScrapeRecord
from dawei.domain.validation import validate_36_numbers
from dawei.infrastructure.cache_repository import CacheRepository, atomic_write_text
from dawei.infrastructure.config_repository import ConfigRepository


def numbers(offset: int = 0) -> tuple[str, ...]:
    values = list(range(1, 37))
    offset %= len(values)
    values = values[offset:] + values[:offset]
    return tuple(f"{value:02d}" for value in values)


def record(site_id: str, name: str, issue: int, offset: int = 0) -> ScrapeRecord:
    return ScrapeRecord(
        site_id=site_id,
        name=name,
        url=f"https://example.test/{site_id}",
        issue=issue,
        numbers=numbers(offset),
        record_id=f"article-{site_id}",
        source_path=f"$.items[{site_id}]",
        raw_position=120,
        parser_id="generic_36",
        source_hash="source-hash",
        fetched_at="2026-07-30T00:00:00+08:00",
    )


class DomainValidationTests(unittest.TestCase):
    def test_36_numbers_preserve_original_order(self) -> None:
        original = numbers(7)
        self.assertEqual(validate_36_numbers(original), original)

    def test_36_numbers_reject_duplicates_and_out_of_range(self) -> None:
        with self.assertRaisesRegex(ValidationError, "36"):
            validate_36_numbers(("01",) * 36)
        with self.assertRaisesRegex(ValidationError, "01-49"):
            validate_36_numbers(tuple(f"{value:02d}" for value in range(2, 38))[:-1] + ("50",))


class ConfigRepositoryTests(unittest.TestCase):
    def test_migrates_v1_config_and_persists_stable_site_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sites_36.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "name": "测试站",
                            "url": "https://host.test/article/manager/6a1234?url=x",
                            "keywords": ["36码中特"],
                            "section_keywords": ["测试站"],
                            "region": "bottom",
                            "render_browser": True,
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8-sig",
            )

            repository = ConfigRepository(path)
            first = repository.load()[0]
            self.assertTrue(first.site_id.startswith("site_"))
            self.assertEqual(first.source_type, "dynamic_article")
            self.assertEqual(first.record_id, "6a1234")
            self.assertEqual(first.render_policy, "always")
            self.assertEqual(first.parser_id, "legacy")

            repository.save((first,))
            persisted = json.loads(path.read_text(encoding="utf-8-sig"))[0]
            self.assertEqual(persisted["site_id"], first.site_id)

            changed = replace(first, name="改名后", url="https://new.test/detail")
            repository.save((changed,))
            reloaded = repository.load()[0]
            self.assertEqual(reloaded.site_id, first.site_id)

    def test_rejects_duplicate_site_ids_and_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sites_36.json"
            path.write_text(
                json.dumps(
                    [
                        {"site_id": "same", "name": "甲", "url": "https://a.test"},
                        {"site_id": "same", "name": "乙", "url": "https://b.test"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8-sig",
            )
            with self.assertRaisesRegex(ConfigurationError, "site_id"):
                ConfigRepository(path).load()

            path.write_text(
                json.dumps([{"name": "甲", "url": "https://a.test", "unexpected": 1}], ensure_ascii=False),
                encoding="utf-8-sig",
            )
            with self.assertRaisesRegex(ConfigurationError, "unexpected"):
                ConfigRepository(path).load()


class CacheRepositoryTests(unittest.TestCase):
    def test_same_record_position_drift_updates_without_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "recent.json"
            repository = CacheRepository(path)
            expected = record("alpha", "甲", 210)
            repository.update((expected,), (), fixed_issue=210)

            shifted = replace(expected, raw_position=124)
            repository.update((shifted,), (), fixed_issue=210)

            actual = repository.load().sites[0].records[0]
            self.assertEqual(actual.numbers, expected.numbers)
            self.assertEqual(actual.record_id, expected.record_id)
            self.assertEqual(actual.source_path, expected.source_path)
            self.assertEqual(actual.raw_position, 124)

    def test_existing_record_evidence_cannot_be_downgraded_to_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "recent.json"
            repository = CacheRepository(path)
            expected = record("alpha", "甲", 210)
            repository.update((expected,), (), fixed_issue=210)
            before = path.read_bytes()

            with self.assertRaises(CacheConflictError):
                repository.update(
                    (replace(expected, record_id=None, source_path=None),),
                    (),
                    fixed_issue=210,
                )

            self.assertEqual(path.read_bytes(), before)

    def test_existing_site_id_identity_cannot_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "recent.json"
            repository = CacheRepository(path)
            expected = record("alpha", "甲", 210)
            repository.update((expected,), (), fixed_issue=210)
            before = path.read_bytes()

            with self.assertRaises(CacheConflictError):
                repository.update(
                    (
                        replace(
                            expected,
                            name="乙",
                            url="https://changed.test/alpha",
                        ),
                    ),
                    (),
                    fixed_issue=210,
                )

            self.assertEqual(path.read_bytes(), before)

    def test_same_numbers_with_different_record_id_still_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "recent.json"
            repository = CacheRepository(path)
            expected = record("alpha", "甲", 210)
            repository.update((expected,), (), fixed_issue=210)

            with self.assertRaises(CacheConflictError):
                repository.update(
                    (replace(expected, record_id="article-other", source_path="$"),),
                    (),
                    fixed_issue=210,
                )

    def test_replace_window_keeps_v2_contract_and_rejects_existing_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "recent.json"
            repository = CacheRepository(path)
            expected = record("alpha", "甲", 210)
            repository.replace_window((expected,), (), fixed_issue=210, periods=10)
            before = path.read_bytes()

            payload = json.loads(before.decode("utf-8-sig"))
            self.assertEqual(payload["version"], 2)
            self.assertEqual(payload["sites"][0]["site_id"], "alpha")

            with self.assertRaises(CacheConflictError):
                repository.replace_window(
                    (record("alpha", "甲", 210, 1),),
                    (),
                    fixed_issue=210,
                    periods=10,
                )
            self.assertEqual(path.read_bytes(), before)

    def test_round_trip_preserves_order_and_v1_compatibility_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "recent.json"
            repository = CacheRepository(path)
            expected = record("alpha", "甲", 210, 9)

            repository.update((expected,), (), fixed_issue=210, periods=10)
            snapshot = repository.load()
            actual = snapshot.sites[0].records[0]
            self.assertEqual(actual.numbers, expected.numbers)
            self.assertEqual(actual.source_path, expected.source_path)
            self.assertEqual(actual.record_id, expected.record_id)

            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            stored = payload["sites"][0]["records"][0]
            self.assertEqual(payload["version"], 2)
            self.assertEqual(stored["numbers"], list(expected.numbers))
            self.assertEqual(stored["article_id"], expected.record_id)
            self.assertEqual(stored["source_path"], expected.source_path)

    def test_rejects_period_rollback_without_changing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "recent.json"
            repository = CacheRepository(path)
            repository.update((record("alpha", "甲", 210),), (), fixed_issue=210)
            before = path.read_bytes()

            with self.assertRaises(CacheRollbackError):
                repository.update((record("alpha", "甲", 209),), (), fixed_issue=209)

            self.assertEqual(path.read_bytes(), before)

    def test_rejects_same_issue_conflict_without_changing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "recent.json"
            repository = CacheRepository(path)
            repository.update((record("alpha", "甲", 210),), (), fixed_issue=210)
            before = path.read_bytes()

            with self.assertRaises(CacheConflictError):
                repository.update((record("alpha", "甲", 210, 1),), (), fixed_issue=210)

            self.assertEqual(path.read_bytes(), before)

    def test_atomic_replace_failure_keeps_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data.json"
            path.write_text("old", encoding="utf-8")

            with mock.patch("dawei.infrastructure.cache_repository.os.replace", side_effect=OSError("stop")):
                with self.assertRaisesRegex(OSError, "stop"):
                    atomic_write_text(path, "new", encoding="utf-8")

            self.assertEqual(path.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_two_processes_do_not_lose_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "recent.json"
            project_root = Path(__file__).resolve().parents[2]
            worker = """
import sys
from dawei.domain.models import ScrapeRecord
from dawei.infrastructure.cache_repository import CacheRepository
site_id, name, target = sys.argv[1:]
values = tuple(f'{value:02d}' for value in range(1, 37))
result = ScrapeRecord(
    site_id=site_id,
    name=name,
    url=f'https://example.test/{site_id}',
    issue=210,
    numbers=values,
    parser_id='generic_36',
    source_hash='hash',
    fetched_at='2026-07-30T00:00:00+08:00',
)
CacheRepository(target).update((result,), (), fixed_issue=210)
"""
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(project_root)
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", worker, site_id, name, str(path)],
                    cwd=project_root,
                    env=environment,
                )
                for site_id, name in (("alpha", "甲"), ("beta", "乙"))
            ]
            return_codes = [process.wait(timeout=20) for process in processes]
            self.assertEqual(return_codes, [0, 0])

            snapshot = CacheRepository(path).load()
            self.assertEqual({site.site_id for site in snapshot.sites}, {"alpha", "beta"})

    def test_parser_id_metadata_migration_preserves_source_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "recent.json"
            repository = CacheRepository(path)
            expected = record("alpha", "甲", 210, 5)
            repository.update((replace(expected, parser_id="legacy"),), (), fixed_issue=210)

            migrated = repository.remap_parser_ids({"alpha": "three_rows"})
            actual = migrated.sites[0].records[0]

            self.assertEqual(actual.parser_id, "three_rows")
            self.assertEqual(actual.numbers, expected.numbers)
            self.assertEqual(actual.record_id, expected.record_id)
            self.assertEqual(actual.source_path, expected.source_path)
            self.assertEqual(actual.raw_position, expected.raw_position)

    def test_site_url_migration_is_explicit_and_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "recent.json"
            repository = CacheRepository(path)
            expected = record("alpha", "甲", 210, 5)
            repository.update((expected,), (), fixed_issue=210)

            migrated = repository.migrate_site_urls(
                {"alpha": (expected.url, "https://new.example/alpha")}
            )
            actual = migrated.sites[0].records[0]

            self.assertEqual(migrated.sites[0].url, "https://new.example/alpha")
            self.assertEqual(actual.url, "https://new.example/alpha")
            self.assertEqual(actual.numbers, expected.numbers)
            self.assertEqual(actual.source_path, expected.source_path)
            with self.assertRaises(CacheConflictError):
                repository.migrate_site_urls(
                    {"alpha": (expected.url, "https://other.example/alpha")}
                )

    def test_selective_success_clears_only_its_preserved_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "recent.json"
            repository = CacheRepository(path)
            expected = record("alpha", "甲", 210)
            repository.update(
                (expected,),
                (f"甲 {expected.url} [解析失败] old", "乙 https://example.test/beta [HTTP失败] old"),
                fixed_issue=210,
            )

            snapshot = repository.update(
                (expected,),
                (),
                fixed_issue=210,
                preserve_existing_failures=True,
            )

            self.assertEqual(snapshot.failures, ("乙 https://example.test/beta [HTTP失败] old",))

    def test_archive_sites_removes_identity_history_and_its_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "recent.json"
            repository = CacheRepository(path)
            alpha = record("alpha", "甲", 210)
            beta = record("beta", "乙", 210)
            repository.update(
                (alpha, beta),
                (f"甲 {alpha.url} [解析失败] old", f"乙 {beta.url} [HTTP失败] old"),
                fixed_issue=210,
            )

            snapshot = repository.archive_sites({"alpha"})

            self.assertEqual([site.site_id for site in snapshot.sites], ["beta"])
            self.assertEqual(snapshot.failures, (f"乙 {beta.url} [HTTP失败] old",))
            self.assertTrue(snapshot.incomplete)


if __name__ == "__main__":
    unittest.main()
