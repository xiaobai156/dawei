from __future__ import annotations

import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dawei.application import duplicate_runner as duplicate_runner_module
from dawei.application import duplicate_service
from dawei.application.batch_service import (
    DEFAULT_OUTPUT_DIR,
    BatchOptions,
    SingleIssueBatchService,
    classify_failure,
    default_error_output_path,
    default_output_path,
    failure_site_names,
    read_issue_failures,
    selected_sites,
    write_failures,
    write_multi_failure_summary,
)
from dawei.application.batch_service import format_failure as format_batch_failure
from dawei.application.duplicate_runner import (
    DuplicateOptions,
    DuplicateRunner,
    DuplicateRunResult,
)
from dawei.application.duplicate_service import load_backup_snapshot
from dawei.cli import duplicate as duplicate_cli
from dawei.cli import multi_issue as multi_issue_cli
from dawei.cli import single_issue as single_issue_cli
from dawei.cli import validate_failed as validate_failed_cli
from dawei.domain.errors import (
    CacheConflictError,
    CacheFormatError,
    ConfigurationError,
    ScrapeError,
)
from dawei.domain.models import (
    CandidateEvidence,
    CandidateOrigin,
    ParsedRecord,
    ScrapeRecord,
    SiteConfig,
)
from dawei.infrastructure import config_repository as config_repository_module
from dawei.infrastructure.cache_repository import CacheRepository
from dawei.infrastructure.config_repository import (
    ConfigRepository,
    config_fingerprint,
    infer_record_id,
    infer_source_type,
    migrate_site_mapping,
    normalize_url_identity,
)

NUMBERS = tuple(f"{number:02d}" for number in range(1, 37))


def evidence(issue: int = 236) -> CandidateEvidence:
    origin = CandidateOrigin(
        raw_issue_line=f"{issue}期",
        raw_number_lines=(" ".join(NUMBERS),),
        anchor_line="36码",
        document_id="doc-1",
        document_url="https://example.test",
        source_method="http",
        block_id="block-1",
        block_start=0,
        block_end=10,
        page_index=1,
        block_index=0,
        parser_id="generic_36",
    )
    return CandidateEvidence(
        issue=issue,
        numbers=NUMBERS,
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


def record(
    *,
    issue: int = 236,
    record_id: str | None = "record-1",
    source_path: str | None = "/article/record-1",
    raw_position: int | None = 1,
    source_hash: str = "",
) -> ScrapeRecord:
    return ScrapeRecord(
        site_id="site-1",
        name="测试站",
        url="https://example.test/topic",
        issue=issue,
        numbers=NUMBERS,
        record_id=record_id,
        source_path=source_path,
        raw_position=raw_position,
        parser_id="generic_36",
        source_hash=source_hash,
    )


class CacheIdentityTests(unittest.TestCase):
    @staticmethod
    def _cache_payload(*, period: object = 236, periods: object = 10, records=None) -> dict:
        return {
            "version": 2,
            "period": period,
            "periods": periods,
            "generated_at": "now",
            "incomplete": False,
            "failures": [],
            "sites": [
                {
                    "site_id": "site-1",
                    "name": "测试站",
                    "url": "https://example.test/topic",
                    "records": records if records is not None else [
                        {"issue": 236, "numbers": list(NUMBERS), "parser_id": "generic_36"}
                    ],
                }
            ],
        }

    def test_same_record_id_allows_source_metadata_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            repository = CacheRepository(path)
            repository.update([record()], [], fixed_issue=236)
            updated = record(source_path="/article/record-1?view=full", raw_position=99)
            snapshot = repository.update([updated], [], fixed_issue=236)
            self.assertEqual(snapshot.sites[0].records[0].source_path, updated.source_path)
            self.assertEqual(snapshot.sites[0].records[0].raw_position, 99)

    def test_different_record_id_still_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = CacheRepository(Path(directory) / "cache.json")
            repository.update([record()], [], fixed_issue=236)
            with self.assertRaises(CacheConflictError):
                repository.update([record(record_id="other-record")], [], fixed_issue=236)

    def test_missing_record_id_requires_same_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = CacheRepository(Path(directory) / "cache.json")
            repository.update([record(record_id=None, source_path="/same")], [], fixed_issue=236)
            repository.update([record(record_id=None, source_path="/same", raw_position=77)], [], fixed_issue=236)
            with self.assertRaises(CacheConflictError):
                repository.update([record(record_id=None, source_path="/other")], [], fixed_issue=236)

    def test_one_missing_record_id_can_refresh_same_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = CacheRepository(Path(directory) / "cache.json")
            repository.update([record(source_path="/same")], [], fixed_issue=236)
            repository.update(
                [record(record_id=None, source_path="/same", raw_position=77)],
                [],
                fixed_issue=236,
            )

    def test_cache_persists_configuration_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            repository = CacheRepository(path)
            repository.update([record()], [], fixed_issue=236, config_fingerprint="fingerprint-1")
            self.assertEqual(repository.load_config_fingerprint(), "fingerprint-1")
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            self.assertEqual(payload["config_fingerprint"], "fingerprint-1")

    def test_update_rejects_different_fingerprint_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            repository = CacheRepository(path)
            repository.update([record()], [], fixed_issue=236, config_fingerprint="fingerprint-1")
            before = path.read_bytes()
            with self.assertRaises(CacheConflictError):
                repository.update([record()], [], fixed_issue=236, config_fingerprint="fingerprint-2")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(repository.load_config_fingerprint(), "fingerprint-1")

    def test_missing_fingerprint_binds_only_after_full_identity_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            repository = CacheRepository(path)
            repository.update([record()], [], fixed_issue=236)
            repository.update(
                [record()],
                [],
                fixed_issue=236,
                config_fingerprint="fingerprint-2",
                expected_site_identities={
                    "site-1": ("测试站", "HTTPS://EXAMPLE.TEST/topic/", "generic_36", None),
                },
                allow_missing_fingerprint_binding=True,
            )
            self.assertEqual(repository.load_config_fingerprint(), "fingerprint-2")

    def test_missing_fingerprint_subset_binding_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            repository = CacheRepository(path)
            repository.update([record()], [], fixed_issue=236)
            before = path.read_bytes()
            with self.assertRaises(CacheConflictError):
                repository.update(
                    [record()],
                    [],
                    fixed_issue=236,
                    config_fingerprint="fingerprint-2",
                    expected_site_identities={
                        "site-1": ("测试站", "https://example.test/topic", "generic_36", None),
                    },
                )
            self.assertEqual(path.read_bytes(), before)

    def test_missing_fingerprint_rejects_configuration_external_site(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            repository = CacheRepository(path)
            old_site = ScrapeRecord(
                site_id="old-site",
                name="旧站",
                url="https://old.example/topic",
                issue=236,
                numbers=NUMBERS,
                record_id="old-record",
                source_path="/old-record",
                parser_id="generic_36",
            )
            repository.update([old_site], [], fixed_issue=236)
            before = path.read_bytes()
            current_site = record()
            with self.assertRaises(CacheConflictError):
                repository.update(
                    [current_site],
                    [],
                    fixed_issue=236,
                    config_fingerprint="fingerprint-2",
                    expected_site_identities={
                        "site-1": ("测试站", "https://example.test/topic", "generic_36", None),
                    },
                    allow_missing_fingerprint_binding=True,
                )
            self.assertEqual(path.read_bytes(), before)

    def test_cache_site_url_identity_uses_normalized_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = CacheRepository(Path(directory) / "cache.json")
            repository.update(
                [ScrapeRecord(
                    site_id="site-1",
                    name="测试站",
                    url="HTTPS://Example.test/topic/?b=2&a=1#ignored",
                    issue=236,
                    numbers=NUMBERS,
                    record_id="record-1",
                    source_path="/article/record-1",
                    raw_position=1,
                    parser_id="generic_36",
                )],
                [],
                fixed_issue=236,
            )
            with self.assertRaises(CacheConflictError):
                repository.update(
                    [ScrapeRecord(
                        site_id="site-1",
                        name="测试站",
                        url="https://example.test/topic?a=1&b=2#/different",
                        issue=236,
                        numbers=NUMBERS,
                        record_id="record-1",
                        source_path="/article/record-1",
                        raw_position=2,
                        parser_id="generic_36",
                    )],
                    [],
                    fixed_issue=236,
                )
            repository.update(
                [ScrapeRecord(
                    site_id="site-1",
                    name="测试站",
                    url="https://example.test/topic?a=1&b=2#ignored",
                    issue=236,
                    numbers=NUMBERS,
                    record_id="record-1",
                    source_path="/article/record-1",
                    raw_position=2,
                    parser_id="generic_36",
                )],
                [],
                fixed_issue=236,
            )

    def test_archiving_sites_clears_configuration_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            repository = CacheRepository(path)
            repository.update([record()], [], fixed_issue=236, config_fingerprint="fingerprint-1")
            repository.archive_sites({"site-1"})
            self.assertIsNone(repository.load_config_fingerprint())
            self.assertNotIn("config_fingerprint", json.loads(path.read_text(encoding="utf-8-sig")))

    def test_repository_cleanup_removes_dead_replace_window_api(self) -> None:
        self.assertFalse(hasattr(CacheRepository, "replace_window"))

    def test_same_url_failure_cleanup_targets_name_too(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            results = [
                ScrapeRecord(
                    site_id="site-a",
                    name="站A",
                    url="https://shared.test/topic",
                    issue=236,
                    numbers=NUMBERS,
                    record_id="a",
                    source_path="/a",
                ),
                ScrapeRecord(
                    site_id="site-b",
                    name="站B",
                    url="https://shared.test/topic",
                    issue=236,
                    numbers=NUMBERS,
                    record_id="b",
                    source_path="/b",
                ),
            ]
            failures = (
                "站A https://shared.test/topic [解析失败] old",
                "站B https://shared.test/topic [解析失败] old",
            )
            archive_repository = CacheRepository(Path(directory) / "archive.json")
            archive_repository.update(results, failures, fixed_issue=236)
            archive_repository.archive_sites({"site-a"})
            self.assertEqual(archive_repository.load().failures, (failures[1],))

            repository = CacheRepository(Path(directory) / "cache.json")
            repository.update(results, failures, fixed_issue=236)
            repository.update([results[0]], [], fixed_issue=236, preserve_existing_failures=True)
            self.assertEqual(repository.load().failures, (failures[1],))

    def test_cache_rejects_bool_and_non_positive_period_fields(self) -> None:
        cases = (("period", True), ("period", 0), ("periods", True), ("periods", 0))
        for field, value in cases:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cache.json"
                payload = self._cache_payload(**{field: value})
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(CacheFormatError):
                    CacheRepository(path).load()

    def test_cache_rejects_bad_record_shape_and_source_hash(self) -> None:
        cases = (
            {"issue": True, "numbers": list(NUMBERS), "parser_id": "generic_36"},
            {"issue": 0, "numbers": list(NUMBERS), "parser_id": "generic_36"},
            {
                "issue": 236,
                "numbers": list(NUMBERS),
                "parser_id": "generic_36",
                "raw_position": True,
            },
            {
                "issue": 236,
                "numbers": list(NUMBERS),
                "parser_id": "generic_36",
                "source_hash": "wrong",
            },
        )
        for record_payload in cases:
            with self.subTest(record=record_payload), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cache.json"
                path.write_text(
                    json.dumps(self._cache_payload(records=[record_payload])),
                    encoding="utf-8",
                )
                with self.assertRaises(CacheFormatError):
                    CacheRepository(path).load()

    def test_cache_normalizes_blank_optional_identity_and_rejects_duplicate_issue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blank_path = root / "blank.json"
            blank_path.write_text(
                json.dumps(self._cache_payload(records=[
                    {
                        "issue": 236,
                        "numbers": list(NUMBERS),
                        "parser_id": "generic_36",
                        "record_id": " \t",
                        "source_path": "\n ",
                    }
                ])),
                encoding="utf-8",
            )
            loaded = CacheRepository(blank_path).load()
            loaded_record = loaded.sites[0].records[0]
            self.assertIsNone(loaded_record.record_id)
            self.assertIsNone(loaded_record.source_path)

            duplicate_path = root / "duplicate.json"
            duplicate_path.write_text(
                json.dumps(self._cache_payload(records=[
                    {"issue": 236, "numbers": list(NUMBERS), "parser_id": "generic_36"},
                    {"issue": 236, "numbers": list(NUMBERS), "parser_id": "generic_36"},
                ])),
                encoding="utf-8",
            )
            with self.assertRaises(CacheFormatError):
                CacheRepository(duplicate_path).load()

    def test_cache_rejects_loose_root_and_site_fields(self) -> None:
        root_cases = (
            ("incomplete", 1),
            ("incomplete", "false"),
            ("version", True),
            ("version", 0),
            ("version", -1),
        )
        for field, value in root_cases:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cache.json"
                payload = self._cache_payload()
                payload[field] = value
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(CacheFormatError):
                    CacheRepository(path).load()

        for incomplete, failures in ((False, ["站点 https://example.test [解析失败] x"]), (True, [])):
            with self.subTest(incomplete=incomplete), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cache.json"
                payload = self._cache_payload()
                payload["incomplete"] = incomplete
                payload["failures"] = failures
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(CacheFormatError):
                    CacheRepository(path).load()

        site_cases = (
            {"name": ""},
            {"name": " \t"},
            {"url": ""},
            {"site_id": ""},
            {"site_id": " \t"},
        )
        for changes in site_cases:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cache.json"
                payload = self._cache_payload()
                payload["sites"][0].update(changes)
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(CacheFormatError):
                    CacheRepository(path).load()

        for failures in ([""], [" \t"], [1]):
            with self.subTest(failures=failures), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cache.json"
                payload = self._cache_payload()
                payload["failures"] = failures
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(CacheFormatError):
                    CacheRepository(path).load()

    def test_cache_requires_nonempty_record_parser_and_nonnegative_position(self) -> None:
        cases = (
            {"issue": 236, "numbers": list(NUMBERS)},
            {"issue": 236, "numbers": list(NUMBERS), "parser_id": ""},
            {"issue": 236, "numbers": list(NUMBERS), "parser_id": " \t"},
            {"issue": 236, "numbers": list(NUMBERS), "parser_id": 1},
            {
                "issue": 236,
                "numbers": list(NUMBERS),
                "parser_id": "generic_36",
                "raw_position": -1,
            },
        )
        for record_payload in cases:
            with self.subTest(record=record_payload), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cache.json"
                path.write_text(
                    json.dumps(self._cache_payload(records=[record_payload])),
                    encoding="utf-8",
                )
                with self.assertRaises(CacheFormatError):
                    CacheRepository(path).load()

    def test_cache_rejects_empty_oversized_and_out_of_window_sites(self) -> None:
        cases = (
            [],
            [
                {"issue": 236, "numbers": list(NUMBERS), "parser_id": "generic_36"}
            ] * 11,
            [{"issue": 225, "numbers": list(NUMBERS), "parser_id": "generic_36"}],
        )
        for records in cases:
            with self.subTest(record_count=len(records)), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cache.json"
                path.write_text(
                    json.dumps(self._cache_payload(records=records)),
                    encoding="utf-8",
                )
                with self.assertRaises(CacheFormatError):
                    CacheRepository(path).load()

    def test_cache_rejects_non_string_time_fields(self) -> None:
        for field in ("generated_at", "fetched_at"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cache.json"
                payload = self._cache_payload()
                if field == "generated_at":
                    payload[field] = 1
                else:
                    payload["sites"][0]["records"][0][field] = False
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(CacheFormatError):
                    CacheRepository(path).load()

    def test_cache_writes_reject_invalid_window_and_source_hash(self) -> None:
        for field, value in (
            ("fixed_issue", True),
            ("fixed_issue", "236"),
            ("fixed_issue", 0),
            ("periods", True),
            ("periods", "10"),
            ("periods", 0),
        ):
            with (
                self.subTest(field=field, value=value),
                tempfile.TemporaryDirectory() as directory,
                self.assertRaises(CacheFormatError),
            ):
                repository = CacheRepository(Path(directory) / "cache.json")
                repository.update(
                    [record()],
                    [],
                    fixed_issue=236 if field == "periods" else value,
                    periods=value if field == "periods" else 10,
                )
        with (
            self.subTest(field="source_hash"),
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(CacheFormatError),
        ):
            repository = CacheRepository(Path(directory) / "cache.json")
            repository.update(
                [record(source_hash="wrong")],
                [],
                fixed_issue=236,
            )

    def test_update_rejects_invalid_incoming_identity_and_metadata(self) -> None:
        cases = (
            ("site_id", ""),
            ("site_id", None),
            ("name", " 	"),
            ("name", None),
            ("url", ""),
            ("url", None),
            ("parser_id", " 	"),
            ("parser_id", None),
            ("record_id", 123),
            ("source_path", 123),
            ("fetched_at", 123),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                repository = CacheRepository(Path(directory) / "cache.json")
                with self.assertRaises(CacheFormatError):
                    repository.update(
                        [replace(record(), **{field: value})],
                        [],
                        fixed_issue=236,
                    )

    def test_update_normalizes_blank_optional_identity_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = CacheRepository(Path(directory) / "cache.json").update(
                [replace(record(), record_id=" 	", source_path="\n ")],
                [],
                fixed_issue=236,
            )
            incoming = snapshot.sites[0].records[0]
            self.assertIsNone(incoming.record_id)
            self.assertIsNone(incoming.source_path)

    def test_update_rejects_incoming_record_outside_target_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = CacheRepository(Path(directory) / "cache.json")
            with self.assertRaises(CacheFormatError):
                repository.update([record(issue=225)], [], fixed_issue=236, periods=10)

    def test_update_validates_each_incoming_record_against_expected_identity(self) -> None:
        expected = {
            "site-1": ("测试站", "https://example.test/topic", "generic_36", None),
        }
        invalid_results = (
            replace(record(), site_id="unknown-site"),
            replace(record(), name="错误站"),
            replace(record(), url="https://other.test/topic"),
            replace(record(), parser_id="wrong-parser"),
        )
        for invalid in invalid_results:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cache.json"
                with self.assertRaises(CacheConflictError):
                    CacheRepository(path).update(
                        [invalid],
                        [],
                        fixed_issue=236,
                        expected_site_identities=expected,
                    )
                self.assertFalse(path.exists())

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "normalized.json"
            snapshot = CacheRepository(path).update(
                [replace(record(), url="HTTPS://EXAMPLE.TEST/topic/")],
                [],
                fixed_issue=236,
                expected_site_identities=expected,
            )
            self.assertEqual(snapshot.sites[0].records[0].url, "HTTPS://EXAMPLE.TEST/topic/")

        dynamic_expected = {
            "site-1": (
                "测试站",
                "https://example.test/topic",
                "generic_36",
                "expected-record",
            ),
        }
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(CacheConflictError):
            CacheRepository(Path(directory) / "dynamic.json").update(
                [record(record_id="wrong-record")],
                [],
                fixed_issue=236,
                expected_site_identities=dynamic_expected,
            )


class BatchOutputAndCacheTests(unittest.TestCase):
    def test_formal_output_directory_is_project_contract(self) -> None:
        expected = Path(r"C:\Users\Administrator\Desktop\每天工具\爬虫合集\大围杀号生肖数据统一归纳")
        self.assertEqual(DEFAULT_OUTPUT_DIR, expected)
        self.assertEqual(default_output_path(236).parent, expected)
        self.assertEqual(default_error_output_path(236).parent, expected)

    def test_failure_report_keeps_prefix_and_adds_context(self) -> None:
        site = SiteConfig("格式站", "https://format.test/topic", region="bottom")
        cases = (
            (RuntimeError("TLS handshake failed"), "TLS失败"),
            (ScrapeError("no valid record"), "解析失败"),
            (ScrapeError("校验失败：结果身份错误"), "校验失败"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failures = []
            for error, category in cases:
                with self.subTest(category=category):
                    failure = format_batch_failure(site, error, 236)
                    self.assertTrue(
                        failure.startswith("格式站 https://format.test/topic [")
                    )
                    self.assertIn(f"[{category}]", failure)
                    self.assertIn("方向:bottom；指定期数:236期；原因:", failure)
                    failures.append(failure)
            output = root / "236期-大围-失败.txt"
            write_failures((failures[0],), output)
            self.assertEqual(
                set(read_issue_failures(236, root)),
                {("格式站", "https://format.test/topic")},
            )
            self.assertEqual(failure_site_names(output), ["格式站"])

    def test_batch_helper_error_and_summary_branches(self) -> None:
        cases = (
            ("unexpected error: boom", "程序异常"),
            ("198.18.0.80 dns", "DNS/线路失败"),
            ("health check failed", "健康检测失败"),
            ("校验失败：身份", "校验失败"),
            ("TLS handshake failed", "TLS失败"),
            ("http 500", "HTTP失败"),
            ("request timeout", "超时"),
            ("未找到栏目关键词", "栏目/关键词解析失败"),
            ("多个高可信候选冲突", "候选冲突失败"),
            ("不是完整36码", "36码数量失败"),
            ("未找到指定236期", "指定期数缺失"),
            ("no valid record", "解析失败"),
            ("network unavailable", "网络失败"),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(classify_failure(ScrapeError(message)), expected)
        self.assertTrue(classify_failure(RuntimeError("unclassified")).startswith("未分类失败/"))

        sites = (
            SiteConfig("甲站", "https://a.test", site_id="a", region="top"),
            SiteConfig("乙站", "https://b.test", site_id="b", region="bottom"),
        )
        self.assertEqual(selected_sites((), sites), sites)
        self.assertEqual(selected_sites(("a.test",), sites), (sites[0],))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failure_path = root / "236期-大围-失败.txt"
            self.assertEqual(read_issue_failures(236, root), {})
            write_failures(
                ("甲站 https://a.test [解析失败] old", "乙站 https://b.test [解析失败] old"),
                failure_path,
            )
            write_failures(("甲站 https://a.test [解析失败] old",), root / "235期-大围-失败.txt")
            summary = write_multi_failure_summary((236, 235, 236), root)
            summary_text = summary.read_text(encoding="utf-8-sig")
            self.assertIn("甲站 https://a.test", summary_text)
            self.assertNotIn("乙站 https://b.test", summary_text)
            write_failures((), failure_path)
            with self.assertRaises(ScrapeError):
                failure_site_names(root)
            with self.assertRaises(ScrapeError):
                write_multi_failure_summary((), root)

    def test_partial_run_writes_success_and_failure_to_cache(self) -> None:
        sites = (
            SiteConfig(name="成功站", url="https://success.test", site_id="success", region="top"),
            SiteConfig(name="失败站", url="https://failure.test", site_id="failure", region="top"),
        )

        def scrape(site: SiteConfig, *, timeout: int, fixed_issue: int | None) -> ParsedRecord:
            if site.site_id == "failure":
                raise RuntimeError("target unavailable")
            return ParsedRecord(
                site.name,
                site.url,
                236,
                NUMBERS,
                record_id="same",
                raw_position=1,
                evidence=evidence(),
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options = BatchOptions(
                fixed_issue=236,
                output_path=root / "success.txt",
                error_output_path=root / "failure.txt",
                recent_cache_path=root / "recent_10_cache.json",
                workers=1,
            )
            result = SingleIssueBatchService(site_scraper=scrape, progress_sink=lambda _: None).run(
                sites, options
            )
            self.assertTrue(result.cache_updated)
            snapshot = CacheRepository(options.recent_cache_path).load()
            self.assertTrue(snapshot.incomplete)
            self.assertEqual([site.name for site in snapshot.sites], ["成功站"])
            self.assertTrue((root / "success.txt").exists())
            self.assertTrue((root / "failure.txt").exists())

    def test_all_failure_run_still_persists_failure_state(self) -> None:
        sites = (
            SiteConfig(name="失败一", url="https://failure-1.test", site_id="failure-1", region="top"),
            SiteConfig(name="失败二", url="https://failure-2.test", site_id="failure-2", region="top"),
        )

        def scrape(site: SiteConfig, *, timeout: int, fixed_issue: int | None) -> ParsedRecord:
            raise RuntimeError("target unavailable")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options = BatchOptions(
                fixed_issue=236,
                output_path=root / "success.txt",
                error_output_path=root / "failure.txt",
                recent_cache_path=root / "recent_10_cache.json",
                workers=1,
            )
            result = SingleIssueBatchService(site_scraper=scrape, progress_sink=lambda _: None).run(
                sites, options
            )
            self.assertFalse(result.results)
            self.assertTrue(result.cache_updated)
            snapshot = CacheRepository(options.recent_cache_path).load()
            self.assertTrue(snapshot.incomplete)
            self.assertEqual(len(snapshot.failures), 2)

    def test_cache_failure_is_reported_without_erasing_realtime_results(self) -> None:
        site = SiteConfig(name="成功站", url="https://success.test", site_id="success", region="top")

        def scrape(site: SiteConfig, *, timeout: int, fixed_issue: int | None) -> ParsedRecord:
            return ParsedRecord(
                site.name,
                site.url,
                236,
                NUMBERS,
                record_id="same",
                raw_position=1,
                evidence=evidence(),
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options = BatchOptions(
                fixed_issue=236,
                output_path=root / "success.txt",
                error_output_path=root / "failure.txt",
                recent_cache_path=root / "recent_10_cache.json",
            )
            with patch.object(
                SingleIssueBatchService,
                "_update_cache",
                side_effect=CacheConflictError("cache write failed"),
            ):
                result = SingleIssueBatchService(site_scraper=scrape, progress_sink=lambda _: None).run(
                    (site,), options
                )
            self.assertEqual(len(result.results), 1)
            self.assertFalse(result.cache_updated)
            self.assertIn("cache write failed", result.cache_error)
            self.assertEqual(result.exit_code, 1)
            self.assertTrue((root / "success.txt").exists())

    def test_batch_rejects_scraper_result_from_another_site(self) -> None:
        site = SiteConfig(name="配置站", url="https://configured.test", site_id="configured", region="top")

        def scrape(site: SiteConfig, *, timeout: int, fixed_issue: int | None) -> ParsedRecord:
            return ParsedRecord(
                "别的站",
                "https://other.test",
                236,
                NUMBERS,
                raw_position=1,
                evidence=evidence(),
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = SingleIssueBatchService(
                site_scraper=scrape,
                progress_sink=lambda _: None,
            ).run(
                (site,),
                BatchOptions(
                    fixed_issue=236,
                    output_path=root / "success.txt",
                    error_output_path=root / "failure.txt",
                    recent_cache_path=root / "recent_10_cache.json",
                ),
            )
            self.assertFalse(result.results)
            self.assertTrue(any("身份错误" in failure for failure in result.failures))
            self.assertFalse((root / "success.txt").read_text(encoding="utf-8-sig").strip())

    def test_validation_errors_are_one_formatted_failure_per_site(self) -> None:
        site = SiteConfig(name="配置站", url="https://configured.test", site_id="configured", region="top")

        def scrape(site: SiteConfig, *, timeout: int, fixed_issue: int | None) -> ParsedRecord:
            return ParsedRecord(
                site.name,
                site.url,
                235,
                ("01",) * 36,
                raw_position=1,
                evidence=evidence(235),
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options = BatchOptions(
                fixed_issue=236,
                output_path=root / "success.txt",
                error_output_path=root / "failure.txt",
                recent_cache_path=root / "recent_10_cache.json",
            )
            result = SingleIssueBatchService(site_scraper=scrape, progress_sink=lambda _: None).run(
                (site,), options
            )
            self.assertEqual(len(result.failures), 1)
            self.assertRegex(
                result.failures[0],
                r"^配置站 https://configured\.test \[校验失败\] 方向:top；指定期数:236期；原因:校验失败：",
            )
            snapshot = CacheRepository(options.recent_cache_path).load()
            self.assertEqual(snapshot.failures, result.failures)

    def test_batch_core_api_rejects_invalid_runtime_options(self) -> None:
        site = SiteConfig(name="配置站", url="https://configured.test", site_id="configured", region="top")

        def scrape(site: SiteConfig, *, timeout: int, fixed_issue: int | None) -> ParsedRecord:
            return ParsedRecord(
                site.name,
                site.url,
                236,
                NUMBERS,
                raw_position=1,
                evidence=evidence(),
            )

        base = BatchOptions(
            fixed_issue=236,
            output_path=Path("success.txt"),
            error_output_path=Path("failure.txt"),
            update_recent_cache=False,
        )
        service = SingleIssueBatchService(site_scraper=scrape, progress_sink=lambda _: None)
        with self.assertRaises(ScrapeError):
            service.run((), base)
        for field in ("fixed_issue", "timeout", "workers", "proxy_retries"):
            with self.subTest(field=field), self.assertRaises(ScrapeError):
                service.run((site,), replace(base, **{field: 0}))

    def test_subset_run_cannot_advance_missing_or_old_cache(self) -> None:
        sites = (
            SiteConfig("站A", "https://a.test", site_id="a", region="top", parser_id="generic_36"),
            SiteConfig("站B", "https://b.test", site_id="b", region="top", parser_id="generic_36"),
        )

        def scrape(site: SiteConfig, *, timeout: int, fixed_issue: int | None) -> ParsedRecord:
            return ParsedRecord(
                site.name,
                site.url,
                236,
                NUMBERS,
                record_id="record-a",
                raw_position=1,
                evidence=evidence(),
            )

        for existing_period in (None, 235):
            with self.subTest(existing_period=existing_period), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                ConfigRepository(root / "sites.json").save(sites)
                cache_path = root / "recent_10_cache.json"
                if existing_period is not None:
                    CacheRepository(cache_path).update(
                        [ScrapeRecord(
                            site_id="a",
                            name="站A",
                            url="https://a.test",
                            issue=existing_period,
                            numbers=NUMBERS,
                            record_id="record-a",
                            source_path="/a",
                            parser_id="generic_36",
                        )],
                        [],
                        fixed_issue=existing_period,
                    )
                result = SingleIssueBatchService(
                    site_scraper=scrape,
                    progress_sink=lambda _: None,
                ).run_configured(
                    root / "sites.json",
                    BatchOptions(
                        fixed_issue=236,
                        output_path=root / "success.txt",
                        error_output_path=root / "failure.txt",
                        recent_cache_path=cache_path,
                        workers=1,
                    ),
                    only=("站A",),
                )
                self.assertFalse(result.cache_updated)
                self.assertIn("子集运行", result.cache_error)
                self.assertEqual(result.exit_code, 1)
                self.assertTrue((root / "success.txt").exists())

    def test_same_period_subset_run_repairs_only_selected_failures(self) -> None:
        sites = (
            SiteConfig("站A", "https://a.test", site_id="a", region="top", parser_id="generic_36"),
            SiteConfig("站B", "https://b.test", site_id="b", region="top", parser_id="generic_36"),
        )

        def scrape(site: SiteConfig, *, timeout: int, fixed_issue: int | None) -> ParsedRecord:
            return ParsedRecord(
                site.name,
                site.url,
                236,
                NUMBERS,
                record_id="record-a",
                raw_position=1,
                evidence=evidence(),
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            success_path = root / "success.txt"
            success_path.write_text("原有成功行\n", encoding="utf-8-sig")
            original_success = success_path.read_bytes()
            config_path = root / "sites.json"
            ConfigRepository(config_path).save(sites)
            cache_path = root / "recent_10_cache.json"
            CacheRepository(cache_path).update(
                [ScrapeRecord(
                    site_id="a",
                    name="站A",
                    url="https://a.test",
                    issue=236,
                    numbers=NUMBERS,
                    record_id="record-a",
                    source_path="/a",
                    parser_id="generic_36",
                )],
                ["站A https://a.test [解析失败] old", "站B https://b.test [解析失败] old"],
                fixed_issue=236,
                config_fingerprint=config_fingerprint(sites),
            )
            result = SingleIssueBatchService(
                site_scraper=scrape,
                progress_sink=lambda _: None,
            ).run_configured(
                config_path,
                BatchOptions(
                    fixed_issue=236,
                    output_path=success_path,
                    error_output_path=root / "failure.txt",
                    recent_cache_path=cache_path,
                    workers=1,
                ),
                only=("站A",),
            )
            self.assertTrue(result.cache_updated)
            snapshot = CacheRepository(cache_path).load()
            self.assertEqual(snapshot.period, 236)
            self.assertTrue(snapshot.incomplete)
            self.assertEqual(snapshot.failures, ("站B https://b.test [解析失败] old",))
            updated_success = success_path.read_bytes()
            self.assertTrue(updated_success.startswith(original_success))
            self.assertEqual(
                success_path.read_text(encoding="utf-8-sig").splitlines(),
                ["原有成功行", f"{','.join(NUMBERS)} 站A"],
            )

    def test_only_run_appends_when_selected_site_is_entire_config(self) -> None:
        site = SiteConfig(
            "站A",
            "https://a.test",
            site_id="a",
            region="top",
            parser_id="generic_36",
        )

        def scrape(config: SiteConfig, *, timeout: int, fixed_issue: int | None) -> ParsedRecord:
            return ParsedRecord(
                config.name,
                config.url,
                236,
                NUMBERS,
                record_id="record-a",
                raw_position=1,
                evidence=evidence(),
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            success_path = root / "success.txt"
            success_path.write_text("原有成功行\n", encoding="utf-8-sig")
            original_success = success_path.read_bytes()
            config_path = root / "sites.json"
            ConfigRepository(config_path).save((site,))

            SingleIssueBatchService(
                site_scraper=scrape,
                progress_sink=lambda _: None,
            ).run_configured(
                config_path,
                BatchOptions(
                    fixed_issue=236,
                    output_path=success_path,
                    error_output_path=root / "failure.txt",
                    update_recent_cache=False,
                    workers=1,
                ),
                only=("站A",),
            )

            updated_success = success_path.read_bytes()
            self.assertTrue(updated_success.startswith(original_success))
            self.assertEqual(
                success_path.read_text(encoding="utf-8-sig").splitlines(),
                ["原有成功行", f"{','.join(NUMBERS)} 站A"],
            )

    def test_same_period_failed_subset_retry_replaces_selected_old_failure(self) -> None:
        sites = (
            SiteConfig("测试站", "https://example.test/topic", site_id="site-1", region="top", parser_id="generic_36"),
            SiteConfig("另站", "https://b.test", site_id="site-2", region="top", parser_id="generic_36"),
        )

        def scrape(site: SiteConfig, *, timeout: int, fixed_issue: int | None) -> ParsedRecord:
            raise RuntimeError("retry failed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "sites.json"
            ConfigRepository(config_path).save(sites)
            cache_path = root / "recent_10_cache.json"
            CacheRepository(cache_path).update(
                [record()],
                ["测试站 https://example.test/topic [解析失败] old"],
                fixed_issue=236,
                config_fingerprint=config_fingerprint(sites),
            )
            result = SingleIssueBatchService(
                site_scraper=scrape,
                progress_sink=lambda _: None,
            ).run_configured(
                config_path,
                BatchOptions(
                    fixed_issue=236,
                    output_path=root / "success.txt",
                    error_output_path=root / "failure.txt",
                    recent_cache_path=cache_path,
                    workers=1,
                ),
                only=("测试站",),
            )
            self.assertTrue(result.cache_updated)
            snapshot = CacheRepository(cache_path).load()
            self.assertEqual(len(snapshot.failures), 1)
            self.assertNotIn("old", snapshot.failures[0])
            self.assertIn("retry failed", snapshot.failures[0])

    def test_failed_subset_retry_clears_failure_without_cached_site_record(self) -> None:
        site = SiteConfig(
            "测试站",
            "https://example.test/topic",
            site_id="site-1",
            region="top",
        )

        def scrape(site: SiteConfig, *, timeout: int, fixed_issue: int | None) -> ParsedRecord:
            raise RuntimeError("retry failed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "sites.json"
            ConfigRepository(config_path).save((site,))
            cache_path = root / "recent_10_cache.json"
            CacheRepository(cache_path).update(
                [],
                ["测试站 https://example.test/topic [解析失败] old"],
                fixed_issue=236,
                config_fingerprint=config_fingerprint((site,)),
            )
            result = SingleIssueBatchService(
                site_scraper=scrape,
                progress_sink=lambda _: None,
            ).run_configured(
                config_path,
                BatchOptions(
                    fixed_issue=236,
                    output_path=root / "success.txt",
                    error_output_path=root / "failure.txt",
                    recent_cache_path=cache_path,
                ),
                only=("测试站",),
            )
            self.assertTrue(result.cache_updated)
            failures = CacheRepository(cache_path).load().failures
            self.assertEqual(len(failures), 1)
            self.assertNotIn("old", failures[0])


class DuplicateAndConfigTests(unittest.TestCase):
    @staticmethod
    def _window(site: SiteConfig, issue: int = 236) -> duplicate_service.SiteWindow:
        record_value = ParsedRecord(site.name, site.url, issue, NUMBERS)
        return duplicate_service.SiteWindow(
            site.name,
            site.url,
            236,
            1,
            (record_value,),
            latest_issue=issue,
            site_id=site.site_id,
            parser_id=site.parser_id,
        )

    @staticmethod
    def _candidate_mapping(**changes: object) -> dict[str, object]:
        mapping: dict[str, object] = {
            "name": "候选站",
            "url": "https://candidate.test/topic",
            "site_id": "candidate",
            "source_type": "generic_html",
            "parser_id": "generic_36",
            "region": "top",
            "render_policy": "never",
            "section_keywords": ["专属栏目"],
            "keywords": ["36码"],
        }
        mapping.update(changes)
        return mapping

    def test_duplicate_helper_and_candidate_loader_branches(self) -> None:
        reasons = (
            ("", "程序异常"),
            ("only found 1/3 comparable issues near 236: none", "实际找到1期"),
            ("only found 1/3 comparable issues near 236: 235,234", "235、234"),
            ("unexpected error:", "无详细异常消息"),
            ("unexpected error: boom", "程序异常：boom"),
            ("network error: down", "网络请求失败：down"),
            ("IncompleteRead: short", "网络读取不完整"),
            ("plain reason", "plain reason"),
        )
        for reason, expected in reasons:
            with self.subTest(reason=reason):
                self.assertIn(expected, duplicate_runner_module.describe_failure_reason(reason))
        self.assertEqual(DuplicateRunner._empty_result(236, (), False).exit_code, 0)
        self.assertEqual(DuplicateRunner._empty_result(236, ("bad",), False).exit_code, 1)

        valid = self._candidate_mapping()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid_path = root / "candidate.json"
            valid_path.write_text(json.dumps([valid, self._candidate_mapping(
                name="候选站2", site_id="candidate-2", url="https://candidate2.test/topic"
            )]), encoding="utf-8")
            loaded = duplicate_runner_module.load_candidate_sites((str(valid_path),))
            self.assertEqual(len(loaded), 2)
            with self.assertRaises(ScrapeError):
                duplicate_runner_module.load_candidate_sites((str(root / "missing.json"),))
            with self.assertRaises(ScrapeError):
                duplicate_runner_module.load_candidate_sites((str(root),))
            bad_json = root / "bad.json"
            bad_json.write_text("{", encoding="utf-8")
            with self.assertRaises(ScrapeError):
                duplicate_runner_module.load_candidate_sites((str(bad_json),))
            scalar = root / "scalar.json"
            scalar.write_text(json.dumps([1]), encoding="utf-8")
            with self.assertRaises(ScrapeError):
                duplicate_runner_module.load_candidate_sites((str(scalar),))

        invalid_mappings = (
            ({"name": "x"},),
            (self._candidate_mapping(name=""),),
            (self._candidate_mapping(region="side"),),
            (self._candidate_mapping(render_policy="bad"),),
            (self._candidate_mapping(source_type="paginated_article_list", navigation_keywords=["下一页"], render_policy="always"),),
            (self._candidate_mapping(keywords=[]),),
            (self._candidate_mapping(navigation_keywords=[""]),),
            (self._candidate_mapping(onboarding_valid_issues=[0]),),
            (self._candidate_mapping(source_type="dynamic_article"),),
            (self._candidate_mapping(source_type="dynamic_article", api_url="https://candidate.test/api/abc", record_id=""),),
            (self._candidate_mapping(source_type="dynamic_collection"),),
        )
        for (mapping,) in invalid_mappings:
            with self.subTest(mapping=mapping), self.assertRaises(ScrapeError):
                duplicate_runner_module._validate_candidate_mapping(mapping, 1)

    def test_duplicate_runner_basic_success_failures_and_selection(self) -> None:
        site = SiteConfig("判重站", "https://duplicate.test/topic", site_id="duplicate", region="top")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "sites.json"
            ConfigRepository(config_path).save((site,))
            base = DuplicateOptions(
                sites_config=config_path,
                backup_path=root / "recent_10_cache.json",
                period=236,
                periods=1,
                timeout=1,
                workers=1,
                min_common=1,
                duplicate_common=1,
            )
            runner = DuplicateRunner(
                window_scraper=lambda current, *args: self._window(current),
                progress_sink=lambda _: None,
            )
            result = runner.run(base)
            self.assertEqual(result.period, 236)
            self.assertEqual(len(result.windows), 1)
            self.assertEqual(result.exit_code, 0)

            failed = DuplicateRunner(
                window_scraper=lambda current, *args: (_ for _ in ()).throw(
                    ScrapeError("network error: down")
                ),
                progress_sink=lambda _: None,
            ).run(base)
            self.assertEqual(len(failed.failures), 1)
            self.assertIn("网络请求失败", failed.failures[0])

            outside = DuplicateRunner(
                window_scraper=lambda current, *args: self._window(current, 225),
                progress_sink=lambda _: None,
            ).run(base)
            self.assertEqual(len(outside.failures), 1)
            self.assertIn("周期筛选", outside.failures[0])

            auto = DuplicateRunner(
                window_scraper=lambda current, *args: self._window(current),
                progress_sink=lambda _: None,
            ).run(replace(base, period=None))
            self.assertTrue(auto.auto_period)
            with self.assertRaises(ScrapeError):
                runner.run(replace(base, only=("missing",)))
            with self.assertRaises(ScrapeError):
                runner.run(replace(base, candidate_sites=("candidate.json",)))

    def test_duplicate_runner_backup_and_candidate_guard_branches(self) -> None:
        site = SiteConfig("已有站", "https://existing.test/topic", site_id="existing", region="top")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "sites.json"
            ConfigRepository(config_path).save((site,))
            backup_path = root / "recent_10_cache.json"
            CacheRepository(backup_path).update(
                [ScrapeRecord(
                    site_id="existing",
                    name=site.name,
                    url=site.url,
                    issue=236,
                    numbers=NUMBERS,
                    record_id="existing-record",
                    source_path="/existing",
                    parser_id=site.parser_id,
                )],
                [],
                fixed_issue=236,
                config_fingerprint=config_fingerprint((site,)),
            )
            base = DuplicateOptions(
                sites_config=config_path,
                backup_path=backup_path,
                period=236,
                periods=10,
                timeout=1,
                workers=1,
                min_common=1,
                duplicate_common=1,
                use_backup=True,
            )
            result = DuplicateRunner(
                window_scraper=lambda current, *args: self._window(current),
                progress_sink=lambda _: None,
            ).run(base)
            self.assertEqual(len(result.windows), 1)

            empty_candidate = root / "empty-candidate.json"
            empty_candidate.write_text("[]", encoding="utf-8")
            with self.assertRaises(ScrapeError):
                DuplicateRunner(window_scraper=lambda *args: None).run(
                    replace(base, candidate_sites=(str(empty_candidate),))
                )

            conflict_candidate = root / "conflict-candidate.json"
            conflict_candidate.write_text(
                json.dumps(self._candidate_mapping(name=site.name, url=site.url, site_id="candidate")),
                encoding="utf-8",
            )
            conflict_result = DuplicateRunner(window_scraper=lambda *args: None).run(
                replace(base, candidate_sites=(str(conflict_candidate),))
            )
            self.assertTrue(conflict_result.failures)

            incomplete_path = root / "incomplete.json"
            CacheRepository(incomplete_path).update(
                [ScrapeRecord(
                    site_id="existing", name=site.name, url=site.url, issue=236,
                    numbers=NUMBERS, record_id="existing-record", source_path="/existing",
                    parser_id=site.parser_id,
                )],
                ["已有站 https://existing.test/topic [解析失败] old"],
                fixed_issue=236,
                config_fingerprint=config_fingerprint((site,)),
            )
            distinct_candidate = root / "distinct-candidate.json"
            distinct_candidate.write_text(json.dumps(self._candidate_mapping()), encoding="utf-8")
            incomplete_result = DuplicateRunner(window_scraper=lambda *args: None).run(
                replace(base, backup_path=incomplete_path, candidate_sites=(str(distinct_candidate),))
            )
            self.assertTrue(incomplete_result.failures)

            with self.assertRaises(ScrapeError):
                DuplicateRunner(window_scraper=lambda *args: None).run(
                    replace(base, candidate_sites=(str(distinct_candidate),), use_backup=False)
                )

    def test_duplicate_rejects_incomplete_backup_without_candidates(self) -> None:
        site = SiteConfig("不完整站", "https://incomplete.test/topic", site_id="incomplete", region="top")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "sites.json"
            backup_path = root / "recent_10_cache.json"
            ConfigRepository(config_path).save((site,))
            CacheRepository(backup_path).update(
                [ScrapeRecord(
                    site_id=site.site_id,
                    name=site.name,
                    url=site.url,
                    issue=236,
                    numbers=NUMBERS,
                    parser_id=site.parser_id,
                )],
                [f"{site.name} {site.url} [解析失败] incomplete"],
                fixed_issue=236,
                config_fingerprint=config_fingerprint((site,)),
            )
            result = DuplicateRunner(
                window_scraper=lambda current, *args: self._window(current),
                progress_sink=lambda _: None,
            ).run(
                DuplicateOptions(
                    sites_config=config_path,
                    backup_path=backup_path,
                    period=236,
                    periods=1,
                    timeout=1,
                    workers=1,
                    min_common=1,
                    duplicate_common=1,
                    use_backup=True,
                )
            )
            self.assertEqual(result.windows, ())
            self.assertEqual(result.exit_code, 1)
            self.assertIn("缓存不完整，禁止判重", result.failures[0])

    def test_duplicate_text_fetcher_and_backup_identity_branches(self) -> None:
        with patch("dawei.application.duplicate_runner.http_client.fetch_text") as fetch_text, patch(
            "dawei.application.duplicate_runner.http_client.fetch_raw",
            return_value=(b"<html></html>", "utf-8", ""),
        ) as fetch_raw:
            fetch_text.side_effect = lambda url, timeout, raw_fetcher: (
                raw_fetcher(url, timeout, None) and "text"
            )
            fetch = DuplicateRunner._text_fetcher(2)
            self.assertEqual(fetch("https://example.test", 3), "text")
            fetch_raw.assert_called_once_with("https://example.test", 3, None, proxy_retries=2)

        site = SiteConfig("站点", "https://backup.test/topic", site_id="backup", region="top")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_path = root / "cache.json"
            ConfigRepository(root / "sites.json").save((site,))
            CacheRepository(cache_path).update(
                [ScrapeRecord(
                    site_id="backup", name=site.name, url=site.url, issue=236,
                    numbers=NUMBERS, record_id="record", source_path="/record", parser_id="legacy",
                )],
                [], fixed_issue=236, config_fingerprint=config_fingerprint((site,))
            )
            backup = load_backup_snapshot(cache_path)
            DuplicateRunner._validate_backup_identity(backup, (site,), cache_path)
            with self.assertRaises(ScrapeError):
                DuplicateRunner._validate_backup_identity(
                    backup, (replace(site, site_id="other"),), cache_path
                )
    def test_backup_site_latest_issue_comes_from_records(self) -> None:
        payload = {
            "version": 2,
            "period": 236,
            "periods": 10,
            "generated_at": "now",
            "incomplete": False,
            "failures": [],
            "sites": [
                {
                    "site_id": "site-1",
                    "name": "测试站",
                    "url": "https://example.test",
                    "records": [
                        {"issue": 234, "numbers": list(NUMBERS), "parser_id": "generic_36"},
                        {"issue": 235, "numbers": list(NUMBERS), "parser_id": "generic_36"},
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            snapshot = load_backup_snapshot(path)
            self.assertEqual(snapshot.sites[0].latest_issue, 235)

    def test_formal_duplicate_run_rejects_backup_update_flag(self) -> None:
        options = DuplicateOptions(
            sites_config=Path("sites_36.json"),
            backup_path=Path("recent_10_cache.json"),
            update_backup=True,
        )
        with self.assertRaises(ScrapeError):
            DuplicateRunner(window_scraper=lambda *args: None).run(options)

    def test_duplicate_rejects_cache_without_matching_config_fingerprint(self) -> None:
        site_payload = [
            {
                "name": "缓存站",
                "url": "https://example.test/topic",
                "site_id": "site-1",
                "region": "top",
                "parser_id": "generic_36",
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "sites.json"
            cache_path = root / "recent_10_cache.json"
            config_path.write_text(json.dumps(site_payload), encoding="utf-8")
            configured = ConfigRepository(config_path).load()
            CacheRepository(cache_path).update(
                [record()], [], fixed_issue=236, config_fingerprint=config_fingerprint(configured)
            )
            cache_path.write_text(
                cache_path.read_text(encoding="utf-8-sig").replace("\"config_fingerprint\":", "\"old_fingerprint\":"),
                encoding="utf-8-sig",
            )
            with self.assertRaises(ScrapeError):
                DuplicateRunner(window_scraper=lambda *args: None).run(
                    DuplicateOptions(
                        sites_config=config_path,
                        backup_path=cache_path,
                        use_backup=True,
                    )
                )

    def test_duplicate_rejects_dynamic_article_record_id_mismatch(self) -> None:
        site = SiteConfig(
            "动态站",
            "https://example.test/article/admin/abc?url=x",
            site_id="site-1",
            source_type="dynamic_article",
            parser_id="generic_36",
            record_id="abc",
            api_url="https://example.test/api/proxy/admin-articles/abc",
            region="top",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "sites.json"
            cache_path = root / "recent_10_cache.json"
            ConfigRepository(config_path).save((site,))
            configured = ConfigRepository(config_path).load()
            CacheRepository(cache_path).update(
                [ScrapeRecord(
                    site_id="site-1",
                    name="动态站",
                    url=site.url,
                    issue=236,
                    numbers=NUMBERS,
                    record_id="wrong",
                    source_path="/article/wrong",
                    parser_id="generic_36",
                )],
                [],
                fixed_issue=236,
                config_fingerprint=config_fingerprint(configured),
            )
            backup = load_backup_snapshot(cache_path)
            with self.assertRaises(ScrapeError):
                DuplicateRunner._validate_backup_identity(backup, configured, cache_path)

    def test_config_fingerprint_uses_normalized_url_identity(self) -> None:
        first = SiteConfig(
            name="缓存站",
            url="HTTPS://Example.test/topic/?b=2&a=1#ignored",
            site_id="site-1",
            region="top",
        )
        second = SiteConfig(
            name="缓存站",
            url="https://example.test/topic?a=1&b=2#ignored",
            site_id="site-1",
            region="top",
        )
        self.assertEqual(normalize_url_identity(first.url), normalize_url_identity(second.url))
        self.assertEqual(config_fingerprint((first,)), config_fingerprint((second,)))

    def test_url_fragment_remains_part_of_identity(self) -> None:
        self.assertNotEqual(
            normalize_url_identity("https://example.test/#xw"),
            normalize_url_identity("https://example.test/#/users/3792"),
        )

    def test_duplicate_core_api_rejects_invalid_runtime_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "sites.json"
            ConfigRepository(config_path).save(
                (SiteConfig("配置站", "https://configured.test", site_id="configured", region="top"),)
            )
            base = DuplicateOptions(
                sites_config=config_path,
                backup_path=root / "recent_10_cache.json",
                period=236,
            )
            runner = DuplicateRunner(window_scraper=lambda *args: None)
            for field in (
                "period",
                "periods",
                "timeout",
                "workers",
                "min_common",
                "duplicate_common",
                "proxy_retries",
            ):
                with self.subTest(field=field), self.assertRaises(ScrapeError):
                    runner.run(replace(base, **{field: 0}))

    def test_same_url_different_parser_source_and_direction_is_distinct_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sites.json"
            sites = (
                SiteConfig(
                    "栏目甲",
                    "https://shared.test/topic",
                    site_id="site-a",
                    parser_id="generic_36",
                    source_type="generic_html",
                    region="top",
                ),
                SiteConfig(
                    "栏目乙",
                    "https://shared.test/topic",
                    site_id="site-b",
                    parser_id="three_rows",
                    source_type="topic_page",
                    region="bottom",
                ),
            )
            ConfigRepository(path).save(sites)
            self.assertEqual(len(ConfigRepository(path).load()), 2)

    def test_config_rejects_same_identity_url_and_record(self) -> None:
        payload = [
            {
                "name": "站A",
                "url": "https://Example.test/article/admin/abc?url=a",
                "site_id": "site-a",
                "source_type": "dynamic_article",
                "parser_id": "generic_36",
                "record_id": "abc",
                "api_url": "https://example.test/api/abc",
                "region": "top",
            },
            {
                "name": "站B",
                "url": "https://example.test/article/admin/abc?url=a",
                "site_id": "site-b",
                "source_type": "dynamic_article",
                "parser_id": "generic_36",
                "record_id": "abc",
                "api_url": "https://example.test/api/abc",
                "region": "top",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sites.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                ConfigRepository(path).load()

    def test_config_rejects_same_dynamic_record_id_across_hosts(self) -> None:
        payload = [
            {
                "name": "站A",
                "url": "https://a.example/article/admin/abc?url=a",
                "site_id": "site-a",
                "source_type": "dynamic_article",
                "parser_id": "generic_36",
                "record_id": "abc",
                "api_url": "https://a.example/api/proxy/admin-articles/abc",
                "region": "top",
            },
            {
                "name": "站B",
                "url": "https://b.example/article/admin/abc?url=b",
                "site_id": "site-b",
                "source_type": "dynamic_article",
                "parser_id": "generic_36",
                "record_id": "abc",
                "api_url": "https://b.example/api/proxy/admin-articles/abc",
                "region": "top",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sites.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                ConfigRepository(path).load()

    def test_config_rejects_non_http_url_and_dynamic_id_mismatch(self) -> None:
        cases = (
            {
                "name": "坏协议",
                "url": "ftp://example.test/topic",
                "site_id": "bad-protocol",
                "region": "top",
            },
            {
                "name": "错文章",
                "url": "https://example.test/article/admin/abc?url=a",
                "site_id": "bad-record",
                "source_type": "dynamic_article",
                "record_id": "different",
                "api_url": "https://example.test/api/abc",
                "region": "top",
            },
            {
                "name": "无专属接口",
                "url": "https://example.test/article/admin/abc?url=a",
                "site_id": "missing-api",
                "source_type": "dynamic_article",
                "record_id": "abc",
                "api_url": "https://example.test/api/proxy/articles",
                "region": "top",
            },
        )
        for item in cases:
            with self.subTest(item=item), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "sites.json"
                path.write_text(json.dumps([item]), encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    ConfigRepository(path).load()

    def test_config_rejects_dynamic_collection_without_api_or_explicit_parser(self) -> None:
        cases = (
            {
                "name": "无集合接口",
                "url": "https://example.test/#/users/3792",
                "site_id": "missing-collection-api",
                "source_type": "dynamic_collection",
                "parser_id": "kunnan_magazine",
                "region": "top",
            },
            {
                "name": "无集合解析器",
                "url": "https://example.test/#/users/3792",
                "site_id": "missing-collection-parser",
                "source_type": "dynamic_collection",
                "api_url": "https://example.test/api/v1/users/3792/forums",
                "region": "top",
            },
        )
        for item in cases:
            with self.subTest(item=item), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "sites.json"
                path.write_text(json.dumps([item]), encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    ConfigRepository(path).load()

    def test_config_helpers_and_safety_boundaries(self) -> None:
        self.assertEqual(normalize_url_identity("not a URL"), "not a url")
        self.assertEqual(
            normalize_url_identity("https://[invalid"),
            "https://[invalid".casefold(),
        )
        inferred = (
            ("https://example.test/article/admin/ABC?url=x", "dynamic_article"),
            ("https://example.test/#/users/3792", "dynamic_collection"),
            ("https://example.test/bbs/topic.php?id=1", "bbs_topic"),
            ("https://example.test/topic/1", "topic_page"),
            ("https://example.test/read.php?tid=1", "forum_thread"),
            ("https://example.test/other", "generic_html"),
        )
        for url, source_type in inferred:
            with self.subTest(url=url):
                self.assertEqual(infer_source_type(url), source_type)
        self.assertEqual(
            infer_record_id("https://example.test/article/admin/ABC?url=x"),
            "ABC",
        )
        self.assertEqual(infer_record_id("https://example.test/other"), None)

        safe_site = SiteConfig(
            "安全站",
            "https://safe.test/topic",
            site_id="safe",
            parser_id="generic_36",
            region="top",
        )
        safety_cases = (
            replace(safe_site, source_type="unsupported"),
            replace(safe_site, record_id=123),
            replace(safe_site, url="https://[invalid"),
            replace(safe_site, fixed_issue=0),
            replace(safe_site, source_type="dynamic_collection", api_url=None),
            replace(
                safe_site,
                source_type="dynamic_collection",
                parser_id="legacy",
                api_url="https://safe.test/api/users",
            ),
        )
        for invalid in safety_cases:
            with self.subTest(invalid=invalid), self.assertRaises(ConfigurationError):
                config_repository_module._validate_site_safety(invalid, 1)

        with self.assertRaises(ConfigurationError):
            config_repository_module._validate_identity_collisions(
                (
                    safe_site,
                    replace(safe_site, url="https://other.test/topic"),
                )
            )
        with self.assertRaises(ConfigurationError):
            config_repository_module._validate_identity_collisions(
                (
                    safe_site,
                    replace(safe_site, name="另一名称"),
                )
            )
        with self.assertRaises(ConfigurationError):
            config_repository_module._validate_identity_collisions(
                (
                    SiteConfig(
                        "动态无接口",
                        "https://dynamic.test/article/admin/abc?url=x",
                        site_id="dynamic",
                        source_type="dynamic_article",
                        parser_id="generic_36",
                        record_id="abc",
                        region="top",
                    ),
                )
            )

    def test_config_migration_and_repository_error_boundaries(self) -> None:
        with self.assertRaises(ConfigurationError):
            migrate_site_mapping(1, 1)
        invalid_mappings = (
            {"name": "未知字段", "url": "https://example.test", "unknown": True},
            {"name": "关键词错误", "url": "https://example.test", "keywords": "36码"},
            {
                "name": "栏目错误",
                "url": "https://example.test",
                "section_keywords": "栏目",
            },
            {
                "name": "关键词元素错误",
                "url": "https://example.test",
                "keywords": [36],
            },
            {
                "name": "栏目元素错误",
                "url": "https://example.test",
                "section_keywords": [1],
            },
            {
                "name": "导航元素错误",
                "url": "https://example.test",
                "navigation_keywords": [1],
            },
            {
                "name": "翻页错误",
                "url": "https://example.test",
                "navigation_keywords": "下一页",
            },
            {
                "name": "入门元素错误",
                "url": "https://example.test",
                "onboarding_valid_issues": ["236"],
            },
            {
                "name": "入门布尔错误",
                "url": "https://example.test",
                "onboarding_valid_issues": [True],
            },
            {
                "name": "入门错误",
                "url": "https://example.test",
                "onboarding_valid_issues": "236",
            },
            {
                "name": "期数错误",
                "url": "https://example.test",
                "onboarding_valid_issues": ["bad"],
            },
            {"name": 1, "url": "https://example.test"},
            {
                "name": "校验错误",
                "url": "https://example.test",
                "render_policy": "bad",
            },
            {
                "name": "期数不合法",
                "url": "https://example.test",
                "onboarding_valid_issues": [0],
            },
            {"name": "站点身份类型错误", "url": "https://example.test", "site_id": 1},
            {"name": "解析器类型错误", "url": "https://example.test", "parser_id": 1},
            {"name": "来源类型错误", "url": "https://example.test", "source_type": 0},
            {"name": "文章身份类型错误", "url": "https://example.test", "record_id": 0},
        )
        for mapping in invalid_mappings:
            with self.subTest(mapping=mapping), self.assertRaises(ConfigurationError):
                migrate_site_mapping(mapping, 1)

        image_site = migrate_site_mapping(
            {
                "name": "图像站",
                "url": "https://image.test/image",
                "site_id": "image",
                "parser_id": "image_tuku2135",
                "image_decoder": "tuku2135_ocr",
                "position": "none",
            },
            1,
        )
        self.assertIsNone(image_site.region)
        browser_site = migrate_site_mapping(
            {
                "name": "浏览器站",
                "url": "https://browser.test/topic/1",
                "site_id": "browser",
                "position": "top",
                "render_browser": True,
            },
            1,
        )
        self.assertEqual(browser_site.render_policy, "always")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing.json"
            with self.assertRaises(ConfigurationError):
                ConfigRepository(missing).load()
            bad_json = root / "bad.json"
            bad_json.write_text("{", encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                ConfigRepository(bad_json).load()
            directory_path = root / "directory.json"
            directory_path.mkdir()
            with self.assertRaises(ConfigurationError):
                ConfigRepository(directory_path).load()
            scalar = root / "scalar.json"
            scalar.write_text(json.dumps({"name": "not a list"}), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                ConfigRepository(scalar).load()
            empty = root / "empty.json"
            empty.write_text("[]", encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                ConfigRepository(empty).load()
            duplicate_id = root / "duplicate-id.json"
            duplicate_id.write_text(
                json.dumps(
                    [
                        {"name": "甲", "url": "https://a.test", "site_id": "same"},
                        {"name": "乙", "url": "https://b.test", "site_id": "same"},
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                ConfigRepository(duplicate_id).load()

            repository = ConfigRepository(root / "sites.json")
            with self.assertRaises(ConfigurationError):
                repository.save(())
            duplicate_sites = (
                SiteConfig("甲", "https://a.test", site_id="same", region="top"),
                SiteConfig("乙", "https://b.test", site_id="same", region="top"),
            )
            with self.assertRaises(ConfigurationError):
                repository.save(duplicate_sites)
            initial = SiteConfig("可替换", "https://replace.test", site_id="replace", region="top")
            repository.save((initial,))
            migrated = repository.migrate()
            self.assertEqual(migrated[0].site_id, "replace")
            updated = repository.replace("replace", region="bottom")
            self.assertEqual(updated.region, "bottom")
            with self.assertRaises(ConfigurationError):
                repository.replace("missing")

    def test_config_migration_distinguishes_missing_and_explicit_region_policy(self) -> None:
        missing = migrate_site_mapping(
            {"name": "缺省站", "url": "https://missing.test/topic"},
            1,
        )
        self.assertEqual(missing.region, "bottom")
        self.assertEqual(missing.render_policy, "never")

        explicit = migrate_site_mapping(
            {
                "name": "显式站",
                "url": "https://explicit.test/topic",
                "region": "top",
                "render_policy": "always",
            },
            1,
        )
        self.assertEqual(explicit.region, "top")
        self.assertEqual(explicit.render_policy, "always")

        for index, value in enumerate(
            ("tail", "顶部", "尾部", "TOP", "Bottom", " bottom", "bottom ", " top ")
        ):
            with self.subTest(region=value), self.assertRaises(ConfigurationError):
                migrate_site_mapping(
                    {
                        "name": f"别名区域{index}",
                        "url": f"https://bad-region-{index}.test/topic",
                        "region": value,
                    },
                    1,
                )

        for field in ("region", "render_policy"):
            for value in (False, None, 0, ""):
                with self.subTest(field=field, value=value), self.assertRaises(ConfigurationError):
                    migrate_site_mapping(
                        {
                            "name": f"坏{field}{value!r}",
                            "url": f"https://bad-{field}-{value!r}.test/topic",
                            field: value,
                        },
                        1,
                    )

    def test_duplicate_cli_writes_text_through_atomic_writer(self) -> None:
        with patch("dawei.cli.duplicate.atomic_write_text") as writer:
            duplicate_cli.write_text(Path("out.txt"), "text")
        writer.assert_called_once_with(Path("out.txt"), "text", encoding="utf-8-sig")

    def test_duplicate_cli_writes_onboarding_error_to_failure_text(self) -> None:
        result = DuplicateRunResult(
            period=236,
            windows=(),
            groups=(),
            matches=(),
            blocking_matches=(),
            failures=(),
            onboarding_error="新增站点近10期校验未通过",
        )

        class Runner:
            def run(self, options: DuplicateOptions) -> DuplicateRunResult:
                return result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            error_path = root / "failure.txt"
            exit_code = duplicate_cli.main(
                [
                    "--period",
                    "236",
                    "--output",
                    str(root / "output.txt"),
                    "--error-output",
                    str(error_path),
                ],
                runner=Runner(),
            )
            self.assertEqual(exit_code, 1)
            self.assertIn(
                "新增站点验证失败: 新增站点近10期校验未通过",
                error_path.read_text(encoding="utf-8-sig"),
            )

    def test_validate_failed_requires_explicit_current_selection(self) -> None:
        site = SiteConfig(
            "当前站",
            "https://current.test/topic",
            site_id="current",
            region="top",
        )
        cases = validate_failed_cli.select_validation_cases(
            (site,),
            ("当前站", "https://current.test/topic/"),
            236,
        )
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].issue, 236)
        self.assertEqual(cases[0].name, site.name)
        self.assertEqual(cases[0].url, site.url)
        for selectors in ((), ("",), ("不存在",)):
            with self.subTest(selectors=selectors), self.assertRaises(ScrapeError):
                validate_failed_cli.select_validation_cases((site,), selectors, 236)
        with self.assertRaises(ScrapeError):
            validate_failed_cli.select_validation_cases((site,), (site.name,), 0)

        output = io.StringIO()
        self.assertEqual(validate_failed_cli.run_cases((), (site,), timeout=1, output=output), 1)
        self.assertIn("测试清单为空", output.getvalue())

    def test_validate_failed_main_uses_loaded_config_and_rejects_unknown_only(self) -> None:
        site = SiteConfig(
            "当前站",
            "https://current.test/topic",
            site_id="current",
            region="top",
        )
        with patch.object(validate_failed_cli, "load_sites", return_value=(site,)), patch.object(
            validate_failed_cli, "configure_proxy"
        ) as configure, patch.object(validate_failed_cli, "run_cases", return_value=0) as run:
            self.assertEqual(
                validate_failed_cli.main(
                    ["--issue", "236", "--only", "当前站", "--sites-config", "ignored.json"]
                ),
                0,
            )
        configure.assert_called_once_with(None)
        selected = run.call_args.args[0]
        self.assertEqual(selected, (validate_failed_cli.ValidationCase(
            issue=236,
            name=site.name,
            url=site.url,
        ),))

        with patch.object(validate_failed_cli, "load_sites", return_value=(site,)), patch.object(
            validate_failed_cli, "configure_proxy"
        ) as configure, patch.object(validate_failed_cli, "run_cases") as run:
            self.assertEqual(
                validate_failed_cli.main(
                    ["--issue", "236", "--only", "旧212站", "--sites-config", "ignored.json"]
                ),
                2,
            )
        configure.assert_not_called()
        run.assert_not_called()

    def test_cli_rejects_non_positive_issue_and_runtime_limits(self) -> None:
        with self.assertRaises(SystemExit):
            single_issue_cli.parse_args(["--fixed-issue", "0"])
        with self.assertRaises(SystemExit):
            single_issue_cli.parse_args(["--fixed-issue", "236", "--workers", "0"])
        with self.assertRaises(SystemExit):
            duplicate_cli.parse_args(["--period", "0"])
        with self.assertRaises(SystemExit):
            duplicate_cli.parse_args(["--workers", "0"])
        with self.assertRaises(SystemExit):
            multi_issue_cli.parse_args(["0"])
        with self.assertRaises(SystemExit):
            validate_failed_cli.parse_args([])
        with self.assertRaises(SystemExit):
            validate_failed_cli.parse_args(["--issue", "236"])

    def test_prompt_modes_validate_input_inside_python(self) -> None:
        with patch("builtins.input", return_value="236"):
            self.assertEqual(single_issue_cli.parse_args(["--prompt-issue"]).fixed_issue, 236)
        with patch("builtins.input", return_value="236 237"):
            self.assertEqual(multi_issue_cli.parse_args(["--prompt-issues"]).issues, [236, 237])
        with patch("builtins.input", return_value=""):
            self.assertIsNone(duplicate_cli.parse_args(["--prompt-period"]).period)
        with patch("builtins.input", return_value="236&whoami"), self.assertRaises(SystemExit):
            single_issue_cli.parse_args(["--prompt-issue"])

    def test_bat_entries_do_not_expand_raw_prompt_input(self) -> None:
        root = Path(__file__).resolve().parent
        scripts = (
            root / "爬虫-每天大围网站数字.bat",
            root / "爬虫-大围多期抓取不刷新缓存.bat",
            root / "爬虫-每天大围网站数字重复.bat",
        )
        for script in scripts:
            text = script.read_text(encoding="utf-8-sig")
            self.assertNotIn("set /p", text.lower())
            self.assertNotIn("%ISSUE%", text)
            self.assertIn("py -3.11", text)


class CliCoverageTests(unittest.TestCase):
    @staticmethod
    def _window(name: str, site_id: str, issues: tuple[int, ...]) -> duplicate_service.SiteWindow:
        url = f"https://{site_id}.test/topic"
        return duplicate_service.SiteWindow(
            name,
            url,
            max(issues),
            len(issues),
            tuple(ParsedRecord(name, url, issue, NUMBERS) for issue in issues),
            latest_issue=max(issues),
            site_id=site_id,
            parser_id="generic_36",
        )

    @staticmethod
    def _validation_report(
        site: SiteConfig,
        *,
        passed: bool,
        formal_result: ParsedRecord | None,
    ) -> validate_failed_cli.ValidationReport:
        case = validate_failed_cli.ResolvedCase(site, 236)
        diagnostics = validate_failed_cli.ContentDiagnostics(
            target_issue_found=formal_result is not None,
            numbers=formal_result.numbers if formal_result is not None else (),
            number_count=len(formal_result.numbers) if formal_result is not None else 0,
            region="top",
            direction_pass=passed,
            anchor_pass=passed,
            keyword_pass=passed,
            numbers_pass=passed,
            same_issue_conflict=not passed,
            duplicate_numbers=(),
            passed=passed,
            failure_reason="" if passed else "注入验证失败",
        )
        return validate_failed_cli.ValidationReport(
            case=case,
            source_kind="HTTP正文",
            diagnostics=diagnostics,
            formal_result=formal_result,
            formal_error="" if passed else "formal error",
            passed=passed,
            failure_reason="" if passed else "注入验证失败",
            rendered=not passed,
            record_id=formal_result.record_id if formal_result else None,
            source_path=formal_result.record_path if formal_result else None,
            raw_position=formal_result.raw_position if formal_result else None,
        )

    def test_duplicate_report_helpers_and_failure_file_paths(self) -> None:
        self.assertEqual(duplicate_cli.issue_range(()), "无")
        self.assertEqual(duplicate_cli.issue_range((236,)), "236期")
        self.assertEqual(duplicate_cli.issue_range((236, 235)), "236~235期")
        left = self._window("左站", "left", (236, 235, 234, 233, 232, 231))
        right = self._window("右站", "right", (236, 235, 234, 233, 232, 231))
        outsider = self._window("外站", "outsider", (236, 235, 234, 233, 232, 231))
        duplicate = duplicate_service.DuplicateMatch(left, right, (236, 235, 234, 233, 232, 231))
        unrelated = duplicate_service.DuplicateMatch(left, outsider, (236, 235, 234, 233, 232, 231))
        report = duplicate_cli.duplicate_report(
            (left, right, outsider),
            ((left, right),),
            (duplicate, unrelated),
            3,
            6,
        )
        self.assertIn("重复组1", report)
        self.assertIn("左站 <=> 右站", report)
        self.assertNotIn("左站 <=> 外站", report)
        review = duplicate_service.DuplicateMatch(left, right, (236, 235, 234))
        review_report = duplicate_cli.duplicate_report((), (), (review,), 3, 6)
        self.assertIn("疑似重复", review_report)
        self.assertIn("236~234期", review_report)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failure.txt"
            path.write_text("old", encoding="utf-8")
            duplicate_cli.write_failures(path, ())
            self.assertFalse(path.exists())
            duplicate_cli.write_failures(path, ("站点 URL [失败]", "第二条"))
            self.assertEqual(
                path.read_text(encoding="utf-8-sig"),
                "站点 URL [失败]\n\n第二条\n",
            )

    def test_duplicate_cli_argument_and_main_success_failure_paths(self) -> None:
        invalid_args = (
            ["--periods", "0"],
            ["--timeout", "0"],
            ["--workers", "0"],
            ["--min-common", "0"],
            ["--duplicate-common", "0"],
            ["--periods", "3", "--min-common", "1", "--duplicate-common", "4"],
            ["--proxy-retries", "0"],
            ["--candidate-site", "candidate.json"],
            ["--prompt-period", "--period", "236"],
        )
        for argv in invalid_args:
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                duplicate_cli.parse_args(argv)
        with patch("builtins.input", side_effect=EOFError), self.assertRaises(SystemExit):
            duplicate_cli.parse_args(["--prompt-period"])
        with patch("builtins.input", return_value="not-a-period"), self.assertRaises(SystemExit):
            duplicate_cli.parse_args(["--prompt-period"])
        with patch("builtins.input", return_value="236"):
            self.assertEqual(duplicate_cli.parse_args(["--prompt-period"]).period, 236)
        with patch("builtins.input", return_value=""):
            self.assertIsNone(duplicate_cli.parse_args(["--prompt-period"]).period)
        self.assertEqual(duplicate_cli.parse_args(["--candidate-site", "candidate.json", "--use-backup"]).candidate_site, ["candidate.json"])

        empty_result = DuplicateRunResult(
            period=236,
            windows=(),
            groups=(),
            matches=(),
            blocking_matches=(),
            failures=(),
        )

        class SuccessRunner:
            def run(self, options: DuplicateOptions) -> DuplicateRunResult:
                return empty_result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale_error = root / "errors.txt"
            stale_error.write_text("stale", encoding="utf-8")
            self.assertEqual(
                duplicate_cli.main(
                    ["--period", "236", "--output", str(root / "output.txt"), "--error-output", str(stale_error)],
                    runner=SuccessRunner(),
                ),
                0,
            )
            self.assertTrue((root / "output.txt").exists())
            self.assertFalse(stale_error.exists())

        left = self._window("左站", "left", (236,))
        right = self._window("右站", "right", (236,))
        blocking = duplicate_service.DuplicateMatch(left, right, (236,))
        failed_result = DuplicateRunResult(
            period=236,
            windows=(left, right),
            groups=((left, right),),
            matches=(blocking,),
            blocking_matches=(blocking,),
            failures=("抓取失败",),
            onboarding_error="新增校验失败",
            auto_period=True,
        )

        class FailedRunner:
            def run(self, options: DuplicateOptions) -> DuplicateRunResult:
                return failed_result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                exit_code = duplicate_cli.main(
                    ["--output", str(root / "output.txt"), "--error-output", str(root / "errors.txt")],
                    runner=FailedRunner(),
                )
            self.assertEqual(exit_code, 1)
            error_text = (root / "errors.txt").read_text(encoding="utf-8-sig")
            self.assertIn("抓取失败", error_text)
            self.assertIn("新增站点验证失败: 新增校验失败", error_text)
            self.assertIn("自动识别最新期数", stdout.getvalue())
            self.assertNotIn("新增校验失败", stderr.getvalue())
            self.assertIn("重复：左站 <=> 右站", stderr.getvalue())

        class ErrorRunner:
            def run(self, options: DuplicateOptions) -> DuplicateRunResult:
                raise ScrapeError("runner failed")

        with patch("sys.stderr", io.StringIO()) as stderr:
            self.assertEqual(duplicate_cli.main(["--period", "236"], runner=ErrorRunner()), 2)
            self.assertIn("runner failed", stderr.getvalue())

    def test_validate_failed_format_and_run_cases_paths(self) -> None:
        site = SiteConfig("验证站", "https://validate.test/topic", site_id="validate", region="top")
        passed_result = ParsedRecord(
            site.name,
            site.url,
            236,
            NUMBERS,
            record_id="record",
            record_path="/record",
            raw_position=1,
        )
        passed_report = self._validation_report(site, passed=True, formal_result=passed_result)
        failed_report = self._validation_report(site, passed=False, formal_result=None)
        self.assertEqual(validate_failed_cli.pass_text(True), "通过")
        self.assertEqual(validate_failed_cli.pass_text(False), "失败")
        self.assertIn("最终结果: 通过", validate_failed_cli.format_report(passed_report))
        self.assertIn("最终结果: 失败", validate_failed_cli.format_report(failed_report))

        cases = (
            validate_failed_cli.ValidationCase(issue=236, name=site.name, url=site.url),
            validate_failed_cli.ValidationCase(issue=236, name=site.name, url=site.url),
        )
        output = io.StringIO()
        with patch.object(validate_failed_cli, "validate_case", side_effect=(passed_report, failed_report)) as validate:
            exit_code = validate_failed_cli.run_cases(
                cases,
                (site,),
                timeout=1,
                output=output,
                source_fetcher=object(),
                site_scraper=object(),
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(validate.call_count, 2)
        self.assertIn("验证 2/2", output.getvalue())
        self.assertIn("通过 1 / 失败 1 / 总计 2", output.getvalue())

        error_output = io.StringIO()
        with patch.object(validate_failed_cli, "validate_case", side_effect=ScrapeError("注入失败")):
            self.assertEqual(
                validate_failed_cli.run_cases(
                    (cases[0],),
                    (site,),
                    timeout=1,
                    output=error_output,
                    source_fetcher=object(),
                    site_scraper=object(),
                ),
                1,
            )
        self.assertIn("测试清单解析失败: 注入失败", error_output.getvalue())

        service_output = io.StringIO()
        with patch.object(validate_failed_cli, "RepairService") as service:
            service.return_value.validate.return_value = passed_report
            self.assertEqual(
                validate_failed_cli.run_cases(
                    (cases[0],),
                    (site,),
                    timeout=1,
                    output=service_output,
                ),
                0,
            )
        service.return_value.validate.assert_called_once()

    def test_validate_failed_selection_ambiguity_and_argument_failures(self) -> None:
        first = SiteConfig("甲站", "https://shared.test/topic", site_id="a", region="top")
        second = SiteConfig(
            "乙站",
            "https://shared.test/topic/",
            site_id="b",
            parser_id="three_rows",
            region="bottom",
        )
        with self.assertRaises(ScrapeError):
            validate_failed_cli.select_validation_cases((first, second), ("https://shared.test/topic",), 236)
        for argv in (
            ["--issue", "0", "--only", "甲站"],
            ["--issue", "236", "--only", "甲站", "--timeout", "0"],
            ["--issue", "236", "--only", "甲站", "--proxy-retries", "0"],
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                validate_failed_cli.parse_args(argv)
        with patch.object(validate_failed_cli, "load_sites", side_effect=ScrapeError("配置读取失败")), patch(
            "sys.stderr", io.StringIO()
        ) as stderr:
            self.assertEqual(
                validate_failed_cli.main(
                    ["--issue", "236", "--only", "甲站", "--sites-config", "offline.json"]
                ),
                2,
            )
        self.assertIn("配置读取失败", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
