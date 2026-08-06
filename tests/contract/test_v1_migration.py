from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from dawei.infrastructure.cache_repository import CacheRepository
from dawei.infrastructure.config_repository import ConfigRepository
from dawei.infrastructure.source_adapters import article_detail_id
from dawei.parsers import DEFAULT_REGISTRY


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class V1MigrationContractTests(unittest.TestCase):
    def test_archived_sites_are_not_active_or_cached(self) -> None:
        archived = json.loads(
            (PROJECT_ROOT / "archived_sites_36.json").read_text(encoding="utf-8-sig")
        )["sites"]
        sites = ConfigRepository(PROJECT_ROOT / "sites_36.json").load()
        snapshot = CacheRepository(PROJECT_ROOT / "近10期重复检测备份.json").load()
        archived_ids = {site["site_id"] for site in archived}

        self.assertEqual(
            {site["name"] for site in archived},
            {"澳彩军师", "两广兄弟", "富国一方"},
        )
        self.assertTrue(archived_ids.isdisjoint(site.site_id for site in sites))
        self.assertTrue(archived_ids.isdisjoint(site.site_id for site in snapshot.sites))

    def test_all_current_sites_migrate_with_unique_stable_identity(self) -> None:
        source = PROJECT_ROOT / "sites_36.json"
        sites = ConfigRepository(source).load()
        self.assertEqual(len(sites), 170)
        self.assertEqual(len({site.site_id for site in sites}), len(sites))
        self.assertTrue(all(site.parser_id for site in sites))
        self.assertTrue(all(site.source_type for site in sites))
        self.assertTrue(all(site.direction in {"top", "bottom"} for site in sites))

        with tempfile.TemporaryDirectory() as temp_dir:
            migrated_path = Path(temp_dir) / "sites_36.json"
            repository = ConfigRepository(migrated_path)
            repository.save(sites)
            reloaded = repository.load()

        self.assertEqual(
            [(site.site_id, site.name, site.url) for site in reloaded],
            [(site.site_id, site.name, site.url) for site in sites],
        )

    def test_current_v1_cache_maps_to_all_configured_site_ids(self) -> None:
        sites = ConfigRepository(PROJECT_ROOT / "sites_36.json").load()
        snapshot = CacheRepository(PROJECT_ROOT / "近10期重复检测备份.json").load()

        cached_periods = {
            record.issue
            for site in snapshot.sites
            for record in site.records
        }
        self.assertEqual(snapshot.period, max(cached_periods))
        self.assertEqual(snapshot.periods, 10)
        self.assertEqual(snapshot.incomplete, bool(snapshot.failures))
        self.assertEqual(len(snapshot.sites), len(sites))
        self.assertEqual(
            {site.site_id for site in snapshot.sites},
            {site.site_id for site in sites},
        )
        self.assertTrue(
            all(len(record.numbers) == 36 for site in snapshot.sites for record in site.records)
        )

    def test_every_dynamic_article_has_matching_url_record_id(self) -> None:
        sites = ConfigRepository(PROJECT_ROOT / "sites_36.json").load()
        dynamic_sites = tuple(site for site in sites if site.source_type == "dynamic_article")
        self.assertEqual(len(dynamic_sites), 53)
        self.assertTrue(all(article_detail_id(site) == site.record_id for site in dynamic_sites))

    def test_verified_admin_pages_use_direct_manager_record_api(self) -> None:
        expected_names = {
            "世俗散仙",
            "添枝增叶",
            "精准秘籍",
            "大开眼界",
            "公式救世",
            "平心静气",
            "十指紧扣",
            "一表人才",
            "赌坛彩经",
            "辉煌特码",
            "兴致勃勃",
            "绿草如茵",
        }
        sites = ConfigRepository(PROJECT_ROOT / "sites_36.json").load()
        selected = {site.name: site for site in sites if site.name in expected_names}

        self.assertEqual(set(selected), expected_names)
        for name, site in selected.items():
            with self.subTest(name=name):
                self.assertEqual(
                    site.api_url,
                    site.url.split("/article/", 1)[0]
                    + f"/api/proxy/manager-articles/{site.record_id}",
                )

    def test_every_site_has_an_explicit_registered_parser_id(self) -> None:
        sites = ConfigRepository(PROJECT_ROOT / "sites_36.json").load()
        parser_ids = {site.parser_id for site in sites}
        self.assertNotIn("legacy", parser_ids)
        self.assertTrue(parser_ids <= set(DEFAULT_REGISTRY.parser_ids))

    def test_migrated_json_contains_permanent_v2_fields(self) -> None:
        sites = ConfigRepository(PROJECT_ROOT / "sites_36.json").load()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sites.json"
            ConfigRepository(path).save(sites[:1])
            item = json.loads(path.read_text(encoding="utf-8-sig"))[0]

        required = {
            "site_id",
            "name",
            "url",
            "source_type",
            "parser_id",
            "region",
            "keywords",
            "section_keywords",
            "api_url",
            "record_id",
            "render_policy",
        }
        self.assertTrue(required <= set(item))

    def test_cache_migration_preserves_every_issue_number_and_position(self) -> None:
        source = PROJECT_ROOT / "近10期重复检测备份.json"
        before = CacheRepository(source).load()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / source.name
            shutil.copy2(source, path)
            repository = CacheRepository(path)
            migrated = repository.migrate()
            after = repository.load()
            payload = json.loads(path.read_text(encoding="utf-8-sig"))

        def records(snapshot):
            return {
                (site.site_id, record.issue): (
                    record.numbers,
                    record.record_id,
                    record.source_path,
                    record.raw_position,
                )
                for site in snapshot.sites
                for record in site.records
            }

        self.assertEqual(migrated.version, 2)
        self.assertEqual(payload["version"], 2)
        self.assertEqual(records(after), records(before))


if __name__ == "__main__":
    unittest.main()
