import json
import tempfile
import unittest
from pathlib import Path


from tests.support import duplicate_regression_api as detector
from tests.support.evidence_factory import record as evidence_record
from tests.support import scrape_regression_api as base


def result(name: str, issue: int, marker: str) -> base.SiteResult:
    numbers = tuple(f"{number:02d}" for number in range(1, 36)) + (marker,)
    return evidence_record(name, "https://example.test", issue, numbers)


def candidate_config_json(name: str = "candidate", url: str = "https://candidate.test", **extra) -> str:
    payload = {
        "name": name,
        "url": url,
        "keywords": ["36码中特"],
        "section_keywords": [name],
        "parser_id": "generic_36",
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def existing_sites_config_json() -> str:
    return json.dumps(
        [
            {
                "name": "existing",
                "url": "https://existing.test",
                "keywords": ["三十六码"],
            }
        ],
        ensure_ascii=False,
    )


class DuplicateDetectionTests(unittest.TestCase):
    def test_default_periods_is_ten(self):
        self.assertEqual(detector.DEFAULT_PERIODS, 10)

    def test_default_requires_multiple_matching_issues(self):
        self.assertGreaterEqual(detector.DEFAULT_MIN_COMMON, 3)
        self.assertEqual(detector.DEFAULT_DUPLICATE_COMMON, 6)

    def test_yiyiba_duplicate_collection_ignores_second_archive_sequence(self):
        first_rows = [
            "25.09.26.38.12.20.33.47.11.01.03.39",
            "15.21.44.37.02.35.22.36.04.14.24.34",
            "10.49.13.46.32.16.23.28.45.08.40.48",
        ]
        second_rows = [
            "34.27.23.11.28.14.48.35.44.24.04.20",
            "02.13.49.38.08.15.03.45.36.40.33.26",
            "39.10.37.46.47.09.22.16.01.25.32.21",
        ]
        html = (
            '<div class="topic-content"><p>200期:『精准36码』开：0000准<br>'
            + "<br>".join(first_rows)
            + "</p><p>199期:『精准36码』开：羊36准<br>"
            + "<br>".join(first_rows)
            + "</p><p>365期:『精准36码』开：00准<br>"
            + "<br>".join(second_rows)
            + "</p><p>200期:『精准36码』开：羊47准<br>"
            + "<br>".join(second_rows)
            + "</p></div>"
        )
        config = base.SiteConfig(
            "以已把",
            "https://xosdbkls.9ca0p-tk7i0-yczwdd.work:16622/topic/238933.html",
            ("精准36码",),
            ("精准36码",),
            region="top",
        )

        records = detector.collect_issue_records(html, config)

        self.assertEqual([record.issue for record in records], [200, 199])

    def test_site_window_uses_supplied_text_fetcher(self):
        calls = []
        html = "150期 测试 三十六码 开00准\n" + " ".join(f"{number:02d}" for number in range(1, 37))
        config = base.SiteConfig(
            "测试",
            "https://example.test/a",
            ("三十六码",),
            ("测试",),
            fixed_issue=150,
        )

        def fake_fetch(url, timeout):
            calls.append((url, timeout))
            return html

        window = detector.scrape_site_window(
            config,
            period=150,
            periods=1,
            min_records=1,
            timeout=20,
            text_fetcher=fake_fetch,
        )

        self.assertEqual(calls, [("https://example.test/a", 20)])
        self.assertEqual([record.issue for record in window.records], [150])

    def test_dynamic_site_window_keeps_exact_article_identity(self):
        numbers = ".".join(f"{number:02d}" for number in range(1, 37))
        config = base.SiteConfig(
            "目标站",
            "https://example.test/article/admin/target-id?url=test",
            ("三十六码",),
            ("目标站",),
            api_url="https://example.test/api/target-id",
            region="top",
        )
        payload = json.dumps(
            {
                "data": [
                    {
                        "id": "decoy-id",
                        "authorNickname": "诱饵站",
                        "title": "190期：三十六码",
                        "html": "诱饵站 190期 三十六码\n" + numbers,
                    },
                    {
                        "id": "target-id",
                        "authorNickname": "目标站",
                        "title": "190期：三十六码",
                        "html": "目标站 三十六码\n190期 目标站 三十六码\n" + numbers,
                    },
                ]
            },
            ensure_ascii=False,
        )

        window = detector.scrape_site_window(
            config,
            period=190,
            periods=1,
            min_records=1,
            timeout=1,
            text_fetcher=lambda _url, _timeout: payload,
        )

        self.assertEqual(window.records[0].record_id, "target-id")
        self.assertEqual(
            window.records[0].record_path,
            "https://example.test/api/target-id::$.data[1]",
        )

    def test_kunnan_magazine_window_uses_dedicated_api_records(self):
        config = base.SiteConfig(
            "困难杂志",
            "https://qvuuqqs.example/#/users/3792",
            ("三十六码特",),
            api_url="https://qvuuqqs.example/api/v1/users/3792/forums?per_page=20",
            region="top",
        )

        def record(record_id, issue, start):
            numbers = tuple(f"{number:02d}" for number in range(start, start + 36))
            content = "\n".join(
                [f"{issue}期【三十六码特】开00对"]
                + [" ".join(numbers[index:index + 12]) for index in range(0, 36, 12)]
            )
            return {
                "id": record_id,
                "status": "published",
                "user_id": 3792,
                "lottery": "macao",
                "draw": issue,
                "topic": "【三十六码特】",
                "content": content,
            }

        payload = json.dumps(
            [record("one", 201, 1), record("two", 200, 13)],
            ensure_ascii=False,
        )
        window = detector.scrape_site_window(
            config,
            period=201,
            periods=2,
            min_records=1,
            timeout=1,
            text_fetcher=lambda _url, _timeout: payload,
        )
        self.assertEqual([record.issue for record in window.records], [201, 200])
        self.assertEqual([record.record_id for record in window.records], ["one", "two"])

    def test_same_issue_conflicting_candidates_fail(self):
        first_numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        second_numbers = " ".join(tuple(f"{number:02d}" for number in range(1, 36)) + ("37",))
        html = (
            "测试\n"
            f"157期 测试 三十六码 开00准\n{first_numbers}\n"
            f"157期 测试 三十六码 开00准\n{second_numbers}\n"
        )
        config = base.SiteConfig(
            "测试",
            "https://example.test/a",
            ("三十六码",),
            ("测试",),
        )

        with self.assertRaises(base.ScrapeError):
            detector.collect_issue_records(html, config)

    def test_auto_latest_period_uses_region_position_not_max_issue(self):
        old_numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        rogue_numbers = " ".join(tuple(f"{number:02d}" for number in range(1, 36)) + ("37",))
        html = (
            "测试\n"
            f"150期 测试 三十六码 开00准\n{old_numbers}\n"
            f"999期 测试 三十六码 开00准\n{rogue_numbers}\n"
        )
        config = base.SiteConfig(
            "测试",
            "https://example.test/a",
            ("三十六码",),
            ("测试",),
            region="top",
        )

        window = detector.scrape_site_window(
            config,
            period=9999,
            periods=2,
            min_records=1,
            timeout=20,
            text_fetcher=lambda _url, _timeout: html,
        )

        self.assertEqual(detector.detect_latest_period((window,)), 150)

    def test_one_matching_issue_is_not_duplicate(self):
        left = detector.SiteWindow(
            "left",
            "https://left.test",
            130,
            6,
            (
                result("left", 130, "36"),
                result("left", 129, "37"),
            ),
        )
        right = detector.SiteWindow(
            "right",
            "https://right.test",
            130,
            6,
            (
                result("right", 130, "36"),
                result("right", 129, "38"),
            ),
        )

        self.assertFalse(detector.are_duplicate(left, right))

    def test_short_site_requires_minimum_common_issues(self):
        left = detector.SiteWindow(
            "left",
            "https://left.test",
            139,
            10,
            (
                result("left", 139, "36"),
                result("left", 138, "37"),
            ),
        )
        right = detector.SiteWindow(
            "right",
            "https://right.test",
            139,
            10,
            (
                result("right", 139, "36"),
                result("right", 138, "37"),
                result("right", 137, "38"),
            ),
        )

        self.assertFalse(detector.are_duplicate(left, right))

    def test_short_site_common_issues_must_all_match(self):
        left = detector.SiteWindow(
            "left",
            "https://left.test",
            139,
            10,
            (
                result("left", 139, "36"),
                result("left", 138, "37"),
            ),
        )
        right = detector.SiteWindow(
            "right",
            "https://right.test",
            139,
            10,
            (
                result("right", 139, "36"),
                result("right", 138, "38"),
                result("right", 137, "39"),
            ),
        )

        self.assertFalse(detector.are_duplicate(left, right))

    def test_three_matching_common_issues_is_suspicious_not_duplicate(self):
        left = detector.SiteWindow(
            "left",
            "https://left.test",
            139,
            10,
            (
                result("left", 139, "36"),
                result("left", 138, "37"),
                result("left", 137, "38"),
            ),
        )
        right = detector.SiteWindow(
            "right",
            "https://right.test",
            139,
            10,
            (
                result("right", 139, "36"),
                result("right", 138, "37"),
                result("right", 137, "38"),
            ),
        )

        self.assertEqual(
            detector.matching_consecutive_issues(left, right, detector.DEFAULT_MIN_COMMON),
            (139, 138, 137),
        )
        self.assertFalse(detector.are_duplicate(left, right))

    def test_six_matching_common_issues_is_duplicate(self):
        left = detector.SiteWindow(
            "left",
            "https://left.test",
            139,
            10,
            tuple(result("left", issue, str(200 - issue)) for issue in range(139, 133, -1)),
        )
        right = detector.SiteWindow(
            "right",
            "https://right.test",
            139,
            10,
            tuple(result("right", issue, str(200 - issue)) for issue in range(139, 133, -1)),
        )

        self.assertTrue(detector.are_duplicate(left, right))

    def test_recent_records_allows_fewer_than_requested_periods(self):
        records = (
            result("short", 139, "36"),
            result("short", 138, "37"),
        )

        selected = detector.select_recent_records(records, period=139, periods=10, min_records=1)

        self.assertEqual(tuple(record.issue for record in selected), (139, 138))

    def test_recent_records_rejects_issues_above_period(self):
        records = (
            result("rogue", 365, "36"),
            result("rogue", 364, "37"),
        )

        with self.assertRaises(base.ScrapeError):
            detector.select_recent_records(records, period=154, periods=10, min_records=1)

    def test_backup_json_round_trips_recent_windows(self):
        window = detector.SiteWindow(
            "left",
            "https://left.test",
            157,
            10,
            tuple(result("left", issue, f"{36 + (157 - issue) % 14:02d}") for issue in range(157, 146, -1)),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / "backup.json"
            detector.write_backup_json((window,), backup_path, period=157, periods=10)
            loaded = detector.load_backup_json(backup_path)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].name, "left")
        self.assertEqual(loaded[0].url, "https://left.test")
        self.assertEqual(tuple(record.issue for record in loaded[0].records), tuple(range(157, 147, -1)))
        self.assertEqual(loaded[0].records[0].numbers[-1], "36")

    def test_backup_json_can_be_compared_with_candidate_window(self):
        old = detector.SiteWindow(
            "old",
            "https://old.test",
            157,
            10,
            tuple(result("old", issue, str(200 - issue)) for issue in range(157, 151, -1)),
        )
        candidate = detector.SiteWindow(
            "candidate",
            "https://candidate.test",
            157,
            10,
            tuple(result("candidate", issue, str(200 - issue)) for issue in range(157, 151, -1)),
        )

        self.assertTrue(detector.are_duplicate(old, candidate))

    def test_candidate_matches_are_found_by_name_and_url(self):
        old = detector.SiteWindow(
            "old",
            "https://old.test",
            157,
            10,
            tuple(result("old", issue, str(200 - issue)) for issue in range(157, 154, -1)),
        )
        candidate = detector.SiteWindow(
            "candidate",
            "https://candidate.test",
            157,
            10,
            tuple(result("candidate", issue, str(200 - issue)) for issue in range(157, 154, -1)),
        )
        candidate_site = base.SiteConfig("candidate", "https://candidate.test", ())

        _, matches = detector.duplicate_groups_and_matches(
            [old, candidate],
            detector.DEFAULT_MIN_COMMON,
            detector.DEFAULT_DUPLICATE_COMMON,
        )

        self.assertEqual(detector.matches_involving_sites(matches, (candidate_site,)), matches)

    def test_unmatched_candidate_has_no_candidate_matches(self):
        old = detector.SiteWindow(
            "old",
            "https://old.test",
            157,
            10,
            tuple(result("old", issue, str(200 - issue)) for issue in range(157, 154, -1)),
        )
        candidate = detector.SiteWindow(
            "candidate",
            "https://candidate.test",
            157,
            10,
            tuple(result("candidate", issue, str(180 - issue)) for issue in range(157, 154, -1)),
        )
        candidate_site = base.SiteConfig("candidate", "https://candidate.test", ())

        _, matches = detector.duplicate_groups_and_matches(
            [old, candidate],
            detector.DEFAULT_MIN_COMMON,
            detector.DEFAULT_DUPLICATE_COMMON,
        )

        self.assertEqual(detector.matches_involving_sites(matches, (candidate_site,)), [])

    def test_candidate_site_requires_backup_mode(self):
        with self.assertRaises(SystemExit) as context:
            detector.main(["--candidate-site", "{}"])

        self.assertEqual(context.exception.code, 2)

    def test_candidate_with_only_one_recent_issue_is_rejected(self):
        old = detector.SiteWindow(
            "old",
            "https://old.test",
            157,
            10,
            tuple(result("old", issue, f"{36 + (157 - issue):02d}") for issue in range(157, 147, -1)),
        )
        original_scrape_site_window = detector.scrape_site_window

        def fake_scrape_site_window(config, period, periods, min_records, timeout, text_fetcher):
            return detector.SiteWindow(
                config.name,
                config.url,
                period,
                periods,
                (result(config.name, 157, "36"),),
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / "backup.json"
            output_path = Path(tmpdir) / "duplicates.txt"
            error_path = Path(tmpdir) / "errors.txt"
            sites_path = Path(tmpdir) / "sites.json"
            detector.write_backup_json((old,), backup_path, period=157, periods=10)
            sites_path.write_text(existing_sites_config_json(), encoding="utf-8")
            detector.scrape_site_window = fake_scrape_site_window
            try:
                exit_code = detector.main(
                    [
                        "--period",
                        "157",
                        "--use-backup",
                        "--backup-json",
                        str(backup_path),
                        "--sites-config",
                        str(sites_path),
                        "--candidate-site",
                        candidate_config_json(),
                        "--output",
                        str(output_path),
                        "--error-output",
                        str(error_path),
                    ]
                )
            finally:
                detector.scrape_site_window = original_scrape_site_window

        self.assertNotEqual(exit_code, 0)

    def test_candidate_without_backup_latest_or_previous_issue_is_rejected(self):
        old = detector.SiteWindow(
            "old",
            "https://old.test",
            157,
            10,
            tuple(result("old", issue, f"{36 + (157 - issue):02d}") for issue in range(157, 147, -1)),
        )
        stale_records = tuple(
            result("candidate", issue, f"{36 + (155 - issue):02d}")
            for issue in range(155, 145, -1)
        )
        original_scrape_site_window = detector.scrape_site_window

        def fake_scrape_site_window(config, period, periods, min_records, timeout, text_fetcher):
            return detector.SiteWindow(config.name, config.url, period, periods, stale_records)

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / "backup.json"
            output_path = Path(tmpdir) / "duplicates.txt"
            error_path = Path(tmpdir) / "errors.txt"
            sites_path = Path(tmpdir) / "sites.json"
            detector.write_backup_json((old,), backup_path, period=157, periods=10)
            sites_path.write_text(existing_sites_config_json(), encoding="utf-8")
            detector.scrape_site_window = fake_scrape_site_window
            try:
                exit_code = detector.main(
                    [
                        "--period",
                        "157",
                        "--use-backup",
                        "--backup-json",
                        str(backup_path),
                        "--sites-config",
                        str(sites_path),
                        "--candidate-site",
                        candidate_config_json(),
                        "--output",
                        str(output_path),
                        "--error-output",
                        str(error_path),
                    ]
                )
            finally:
                detector.scrape_site_window = original_scrape_site_window

        self.assertNotEqual(exit_code, 0)

    def test_candidate_duplicate_name_is_reported_before_scrape(self):
        old = detector.SiteWindow(
            "candidate",
            "https://old.test",
            157,
            10,
            tuple(result("old", issue, f"{36 + (157 - issue):02d}") for issue in range(157, 147, -1)),
        )
        original_scrape_site_window = detector.scrape_site_window
        called = False

        def fake_scrape_site_window(config, period, periods, min_records, timeout, text_fetcher):
            nonlocal called
            called = True
            return detector.SiteWindow(config.name, config.url, period, periods, ())

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / "backup.json"
            output_path = Path(tmpdir) / "duplicates.txt"
            error_path = Path(tmpdir) / "errors.txt"
            sites_path = Path(tmpdir) / "sites.json"
            detector.write_backup_json((old,), backup_path, period=157, periods=10)
            sites_path.write_text(existing_sites_config_json(), encoding="utf-8")
            detector.scrape_site_window = fake_scrape_site_window
            try:
                exit_code = detector.main(
                    [
                        "--period",
                        "157",
                        "--use-backup",
                        "--backup-json",
                        str(backup_path),
                        "--sites-config",
                        str(sites_path),
                        "--candidate-site",
                        candidate_config_json(name="candidate", url="https://candidate.test"),
                        "--output",
                        str(output_path),
                        "--error-output",
                        str(error_path),
                    ]
                )
            finally:
                detector.scrape_site_window = original_scrape_site_window

        self.assertFalse(called)
        self.assertNotEqual(exit_code, 0)

    def test_candidate_duplicate_api_url_is_reported_before_scrape(self):
        original_scrape_site_window = detector.scrape_site_window
        called = False

        def fake_scrape_site_window(config, period, periods, min_records, timeout, text_fetcher):
            nonlocal called
            called = True
            return detector.SiteWindow(config.name, config.url, period, periods, ())

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / "backup.json"
            output_path = Path(tmpdir) / "duplicates.txt"
            error_path = Path(tmpdir) / "errors.txt"
            sites_path = Path(tmpdir) / "sites.json"
            old = detector.SiteWindow(
                "old",
                "https://old.test",
                157,
                10,
                tuple(result("old", issue, f"{36 + (157 - issue):02d}") for issue in range(157, 147, -1)),
            )
            detector.write_backup_json((old,), backup_path, period=157, periods=10)
            sites_path.write_text(
                json.dumps(
                    [
                        {
                            "name": "old",
                            "url": "https://old.test",
                            "api_url": "https://api.test/topic",
                            "keywords": ["三十六码"],
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            detector.scrape_site_window = fake_scrape_site_window
            try:
                exit_code = detector.main(
                    [
                        "--period",
                        "157",
                        "--use-backup",
                        "--backup-json",
                        str(backup_path),
                        "--sites-config",
                        str(sites_path),
                        "--candidate-site",
                        candidate_config_json(api_url="https://api.test/topic"),
                        "--output",
                        str(output_path),
                        "--error-output",
                        str(error_path),
                    ]
                )
            finally:
                detector.scrape_site_window = original_scrape_site_window

        self.assertFalse(called)
        self.assertNotEqual(exit_code, 0)

    def test_candidate_suspicious_duplicate_returns_nonzero(self):
        old = detector.SiteWindow(
            "old",
            "https://old.test",
            157,
            10,
            tuple(result("old", issue, f"{36 + (157 - issue):02d}") for issue in range(157, 154, -1)),
        )
        candidate_records = tuple(
            result("candidate", issue, f"{36 + (157 - issue):02d}")
            for issue in range(157, 147, -1)
        )
        original_scrape_site_window = detector.scrape_site_window

        def fake_scrape_site_window(config, period, periods, min_records, timeout, text_fetcher):
            return detector.SiteWindow(config.name, config.url, period, periods, candidate_records)

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / "backup.json"
            output_path = Path(tmpdir) / "duplicates.txt"
            error_path = Path(tmpdir) / "errors.txt"
            sites_path = Path(tmpdir) / "sites.json"
            detector.write_backup_json((old,), backup_path, period=157, periods=10)
            sites_path.write_text(existing_sites_config_json(), encoding="utf-8")
            detector.scrape_site_window = fake_scrape_site_window
            try:
                exit_code = detector.main(
                    [
                        "--period",
                        "157",
                        "--use-backup",
                        "--backup-json",
                        str(backup_path),
                        "--sites-config",
                        str(sites_path),
                        "--candidate-site",
                        candidate_config_json(),
                        "--output",
                        str(output_path),
                        "--error-output",
                        str(error_path),
                    ]
                )
            finally:
                detector.scrape_site_window = original_scrape_site_window

        self.assertNotEqual(exit_code, 0)

    def test_candidate_ignores_unrelated_existing_duplicate_matches(self):
        shared_records = tuple(
            result("old", issue, f"{36 + (157 - issue):02d}")
            for issue in range(157, 147, -1)
        )
        old_left = detector.SiteWindow("old-left", "https://old-left.test", 157, 10, shared_records)
        old_right = detector.SiteWindow("old-right", "https://old-right.test", 157, 10, shared_records)
        candidate_records = tuple(
            result("candidate", issue, f"{49 - (157 - issue):02d}")
            for issue in range(157, 147, -1)
        )
        original_scrape_site_window = detector.scrape_site_window

        def fake_scrape_site_window(config, period, periods, min_records, timeout, text_fetcher):
            return detector.SiteWindow(config.name, config.url, period, periods, candidate_records)

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / "backup.json"
            output_path = Path(tmpdir) / "duplicates.txt"
            error_path = Path(tmpdir) / "errors.txt"
            sites_path = Path(tmpdir) / "sites.json"
            detector.write_backup_json((old_left, old_right), backup_path, period=157, periods=10)
            sites_path.write_text(existing_sites_config_json(), encoding="utf-8")
            detector.scrape_site_window = fake_scrape_site_window
            try:
                exit_code = detector.main(
                    [
                        "--period",
                        "157",
                        "--use-backup",
                        "--backup-json",
                        str(backup_path),
                        "--sites-config",
                        str(sites_path),
                        "--candidate-site",
                        candidate_config_json(),
                        "--output",
                        str(output_path),
                        "--error-output",
                        str(error_path),
                    ]
                )
            finally:
                detector.scrape_site_window = original_scrape_site_window

        self.assertEqual(exit_code, 0)

    def test_candidate_mode_rejects_incomplete_backup(self):
        old_records = tuple(
            result("old", issue, f"{36 + (157 - issue):02d}")
            for issue in range(157, 147, -1)
        )
        old = detector.SiteWindow("old", "https://old.test", 157, 10, old_records)
        candidate_records = tuple(
            result("candidate", issue, f"{49 - (157 - issue):02d}")
            for issue in range(157, 147, -1)
        )
        original_scrape_site_window = detector.scrape_site_window

        def fake_scrape_site_window(config, period, periods, min_records, timeout, text_fetcher):
            return detector.SiteWindow(config.name, config.url, period, periods, candidate_records)

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / "backup.json"
            output_path = Path(tmpdir) / "duplicates.txt"
            error_path = Path(tmpdir) / "errors.txt"
            sites_path = Path(tmpdir) / "sites.json"
            detector.write_backup_json((old,), backup_path, period=157, periods=10, failures=("old failure",))
            sites_path.write_text(existing_sites_config_json(), encoding="utf-8")
            detector.scrape_site_window = fake_scrape_site_window
            try:
                exit_code = detector.main(
                    [
                        "--period",
                        "157",
                        "--use-backup",
                        "--backup-json",
                        str(backup_path),
                        "--sites-config",
                        str(sites_path),
                        "--candidate-site",
                        candidate_config_json(),
                        "--update-backup",
                        "--output",
                        str(output_path),
                        "--error-output",
                        str(error_path),
                    ]
                )
            finally:
                detector.scrape_site_window = original_scrape_site_window
            payload = json.loads(backup_path.read_text(encoding="utf-8-sig"))

        self.assertEqual(exit_code, 1)
        self.assertTrue(payload["incomplete"])
        self.assertEqual(payload["failures"], ["old failure"])

    def test_backup_json_records_failures_without_fake_records(self):
        window = detector.SiteWindow(
            "left",
            "https://left.test",
            157,
            10,
            (result("left", 157, "36"),),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / "backup.json"
            detector.write_backup_json((window,), backup_path, period=157, periods=10, failures=("bad failed",))
            payload = detector.json.loads(backup_path.read_text(encoding="utf-8-sig"))

        self.assertTrue(payload["incomplete"])
        self.assertEqual(payload["version"], 2)
        self.assertTrue(payload["sites"][0]["site_id"])
        self.assertTrue(payload["sites"][0]["records"][0]["source_hash"])
        self.assertEqual(payload["failures"], ["bad failed"])
        self.assertEqual(len(payload["sites"]), 1)

    def test_detect_latest_period_uses_most_common_latest_issue(self):
        windows = (
            detector.SiteWindow("left", "https://left.test", 9999, 10, (result("left", 151, "36"),)),
            detector.SiteWindow("right", "https://right.test", 9999, 10, (result("right", 154, "36"),)),
            detector.SiteWindow("center", "https://center.test", 9999, 10, (result("center", 154, "36"),)),
            detector.SiteWindow("rogue", "https://rogue.test", 9999, 10, (result("rogue", 365, "36"),)),
        )

        self.assertEqual(detector.detect_latest_period(windows), 154)

    def test_collect_issue_records_rejects_same_issue_conflicting_candidates(self):
        first_numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        second_numbers = " ".join(f"{number:02d}" for number in range(14, 50))
        html = "\n".join(
            [
                "测试站 三十六码",
                "190期 测试站 三十六码 开00准",
                first_numbers,
                "190期 测试站 三十六码 开00准",
                second_numbers,
            ]
        )
        config = base.SiteConfig(
            "测试站",
            "https://example.test",
            ("三十六码",),
            ("测试站",),
            region="bottom",
        )

        with self.assertRaisesRegex(base.ScrapeError, "多个高可信候选.*冲突"):
            detector.collect_issue_records(html, config)

    def test_candidate_mode_rejects_candidate_without_cache_latest_two_issues(self):
        backup_window = detector.SiteWindow(
            "旧站",
            "https://old.test",
            190,
            10,
            (
                result("旧站", 190, "36"),
                result("旧站", 189, "37"),
            ),
        )
        candidate_site = base.SiteConfig("候选站", "https://candidate.test", ("三十六码",), ("候选站",))
        candidate_window = detector.SiteWindow(
            "候选站",
            "https://candidate.test",
            190,
            10,
            (result("候选站", 150, "38"),),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / "backup.json"
            candidate_path = Path(tmpdir) / "candidate.json"
            output_path = Path(tmpdir) / "duplicates.txt"
            error_path = Path(tmpdir) / "failures.txt"
            detector.write_backup_json((backup_window,), backup_path, period=190, periods=10)
            candidate_path.write_text(
                base.json.dumps(
                    {
                        "name": candidate_site.name,
                        "url": candidate_site.url,
                        "keywords": list(candidate_site.keywords),
                        "section_keywords": list(candidate_site.section_keywords),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            original_scrape = detector.scrape_site_window
            try:
                detector.scrape_site_window = lambda *args, **kwargs: candidate_window
                code = detector.main(
                    [
                        "--period",
                        "190",
                        "--use-backup",
                        "--backup-json",
                        str(backup_path),
                        "--candidate-site",
                        str(candidate_path),
                        "--output",
                        str(output_path),
                        "--error-output",
                        str(error_path),
                    ]
                )
            finally:
                detector.scrape_site_window = original_scrape

        self.assertEqual(code, 1)

    def test_candidate_mode_rejects_same_url_before_scraping(self):
        backup_window = detector.SiteWindow(
            "旧站",
            "https://same.test/topic",
            190,
            10,
            (result("旧站", 190, "36"),),
        )
        candidate_site = base.SiteConfig("新站", "https://same.test/topic", ("三十六码",), ("新站",))

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / "backup.json"
            candidate_path = Path(tmpdir) / "candidate.json"
            detector.write_backup_json((backup_window,), backup_path, period=190, periods=10)
            candidate_path.write_text(
                base.json.dumps(
                    {
                        "name": candidate_site.name,
                        "url": candidate_site.url,
                        "keywords": list(candidate_site.keywords),
                        "section_keywords": list(candidate_site.section_keywords),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            code = detector.main(
                [
                    "--period",
                    "190",
                    "--use-backup",
                    "--backup-json",
                    str(backup_path),
                    "--candidate-site",
                    str(candidate_path),
                    "--output",
                    str(Path(tmpdir) / "duplicates.txt"),
                    "--error-output",
                    str(Path(tmpdir) / "failures.txt"),
                ]
            )

        self.assertEqual(code, 1)

    def test_write_duplicate_groups_includes_details(self):
        left = detector.SiteWindow(
            "left",
            "https://left.test",
            153,
            10,
            (
                result("left", 153, "36"),
                result("left", 152, "37"),
                result("left", 151, "38"),
            ),
        )
        right = detector.SiteWindow(
            "right",
            "https://right.test",
            153,
            10,
            (
                result("right", 153, "36"),
                result("right", 152, "37"),
                result("right", 151, "38"),
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "duplicates.txt"
            detector.write_duplicate_groups((left, right), output_path, detector.DEFAULT_MIN_COMMON)
            text = output_path.read_text(encoding="utf-8-sig")

        self.assertIn("left https://left.test", text)
        self.assertIn("right https://right.test", text)
        self.assertIn("疑似重复", text)
        self.assertIn("153~151期，连续3期", text)
        self.assertIn("每期36码位置+数值完全一致", text)
        self.assertIn("153期: 01,02,03", text)

    def test_failure_line_includes_name_url_and_reason(self):
        site = base.SiteConfig("测试站", "https://example.test/topic.html", ())

        line = detector.format_failure(site, "没有完整期数")

        self.assertEqual(line, "测试站 https://example.test/topic.html 没有完整期数")

    def test_failure_line_translates_common_english_reason(self):
        site = base.SiteConfig("测试站", "https://example.test/topic.html", ())

        line = detector.format_failure(site, "only found 2/3 comparable issues near 126: 130,129")

        self.assertEqual(
            line,
            "测试站 https://example.test/topic.html "
            "可对比期数不足：需要至少3期，实际找到2期；指定期数126附近找到：130、129",
        )

    def test_failure_line_translates_unexpected_error_as_program_error(self):
        site = base.SiteConfig("测试站", "https://example.test/topic.html", ())

        line = detector.format_failure(site, "unexpected error: boom")

        self.assertEqual(line, "测试站 https://example.test/topic.html 程序异常：boom")

    def test_failure_txt_separates_each_site_with_one_blank_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "failures.txt"

            detector.write_failures(("站点A 失败原因A", "站点B 失败原因B"), output_path)

            self.assertEqual(
                output_path.read_text(encoding="utf-8-sig"),
                "站点A 失败原因A\n\n站点B 失败原因B\n",
            )


if __name__ == "__main__":
    unittest.main()
