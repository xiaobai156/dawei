"""Offline tests for in-flight same-URL fetch attempt merging."""

from __future__ import annotations

import threading
import unittest

from dawei.domain.errors import ScrapeError
from dawei.infrastructure import http_client


class ConcurrentFailureMergeTests(unittest.TestCase):
    def test_concurrent_failure_merges_single_attempt_and_is_not_cached(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls: list[int] = []
        errors: list[str] = []

        def fetcher(_url: str, _timeout: int) -> str:
            calls.append(1)
            entered.set()
            release.wait(2)
            raise ScrapeError("boom")

        cache = http_client.TextFetchCache(fetcher)

        def worker() -> None:
            try:
                cache.fetch("https://shared.test", 1)
            except ScrapeError as exc:
                errors.append(str(exc))

        first = threading.Thread(target=worker)
        second = threading.Thread(target=worker)
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        release.set()
        first.join(2)
        second.join(2)

        self.assertEqual(len(calls), 1)
        self.assertEqual(errors, ["boom", "boom"])

    def test_failure_is_not_cached_so_later_call_retries(self) -> None:
        calls: list[int] = []

        def fetcher(_url: str, _timeout: int) -> str:
            calls.append(1)
            if len(calls) == 1:
                raise ScrapeError("first")
            return "recovered"

        cache = http_client.TextFetchCache(fetcher)
        with self.assertRaises(ScrapeError):
            cache.fetch("https://recover.test", 1)
        self.assertEqual(cache.fetch("https://recover.test", 1), "recovered")
        self.assertEqual(len(calls), 2)

    def test_concurrent_success_still_deduplicates(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls: list[int] = []
        results: list[str] = []

        def fetcher(_url: str, _timeout: int) -> str:
            calls.append(1)
            entered.set()
            release.wait(2)
            return "shared"

        cache = http_client.TextFetchCache(fetcher)

        def worker() -> None:
            results.append(cache.fetch("https://shared-ok.test", 1))

        first = threading.Thread(target=worker)
        second = threading.Thread(target=worker)
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        release.set()
        first.join(2)
        second.join(2)

        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(results), ["shared", "shared"])


if __name__ == "__main__":
    unittest.main()
