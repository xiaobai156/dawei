import unittest
from pathlib import Path

from dawei.infrastructure.cache_repository import CacheRepository


class RepositoryCleanupTests(unittest.TestCase):
    def test_dead_name_tools_are_removed(self) -> None:
        for filename in ("generate_daily_txt.py", "sync_names.py", "open_names.bat"):
            with self.subTest(filename=filename):
                self.assertFalse(Path(__file__).with_name(filename).exists())

    def test_one_off_cache_migrations_are_not_part_of_active_repository(self) -> None:
        for method_name in ("migrate", "remap_parser_ids", "migrate_site_urls"):
            with self.subTest(method_name=method_name):
                self.assertFalse(hasattr(CacheRepository, method_name))

if __name__ == "__main__":
    unittest.main()
