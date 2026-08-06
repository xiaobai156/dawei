from __future__ import annotations

import unittest

from dawei.application.duplicate_service import BackupSnapshot, SiteWindow
from dawei.application.onboarding_service import (
    OnboardingService,
    candidate_identity_conflicts,
    validate_candidate_window,
)
from dawei.domain.errors import ScrapeError
from dawei.domain.models import ParsedRecord, SiteConfig
from tests.support.evidence_factory import record as evidence_record


def numbers(last: int = 36) -> tuple[str, ...]:
    return tuple(f"{value:02d}" for value in range(1, 36)) + (f"{last:02d}",)


def config(
    name: str,
    url: str,
    *,
    site_id: str,
    record_id: str | None = None,
) -> SiteConfig:
    return SiteConfig(
        name,
        url,
        site_id=site_id,
        parser_id="generic_36",
        region="bottom",
        record_id=record_id,
    )


class DuplicateAndOnboardingServiceTests(unittest.TestCase):
    def test_site_window_rejects_conflicting_same_issue_records(self) -> None:
        window = SiteWindow(
            "候选",
            "https://candidate.test",
            210,
            10,
            (
                ParsedRecord("候选", "https://candidate.test", 210, numbers(36)),
                ParsedRecord("候选", "https://candidate.test", 210, numbers(37)),
            ),
        )

        with self.assertRaisesRegex(ScrapeError, "210期.*冲突"):
            _ = window.by_issue

    def test_identity_conflicts_include_site_id_record_id_and_topic_id(self) -> None:
        existing = config(
            "旧站",
            "https://forum.test/bbs/topic.php?id=88&view=full",
            site_id="same-site-id",
            record_id="same-record-id",
        )
        candidate = config(
            "新站",
            "https://forum.test/bbs/topic.php?view=compact&id=88",
            site_id="same-site-id",
            record_id="same-record-id",
        )

        conflicts = candidate_identity_conflicts((candidate,), (existing,), ())

        self.assertTrue(any("site_id" in conflict for conflict in conflicts))
        self.assertTrue(any("record_id" in conflict for conflict in conflicts))
        self.assertTrue(any("topic" in conflict for conflict in conflicts))

    def test_onboarding_rejects_incomplete_backup_before_candidate_checks(self) -> None:
        candidate = config(
            "候选",
            "https://candidate.test/topic/1",
            site_id="candidate-id",
        )
        snapshot = BackupSnapshot(
            210,
            10,
            (),
            ("旧站 网络失败",),
            incomplete=True,
        )

        with self.assertRaisesRegex(ScrapeError, "缓存不完整"):
            OnboardingService().validate(
                candidates=(candidate,),
                configured=(),
                backup=snapshot,
                windows=(),
                matches=(),
            )

    def test_explicit_insufficient_history_exception_accepts_recorded_gap(self) -> None:
        candidate = SiteConfig(
            "候选",
            "https://candidate.test/topic/1",
            keywords=("36码中特",),
            section_keywords=("候选",),
            site_id="candidate-id",
            parser_id="generic_36",
            region="top",
            onboarding_exception="allow_insufficient_history",
            onboarding_valid_issues=(217, 216, 215, 214, 213, 212, 210, 209, 208),
            onboarding_missing_issues=(211,),
        )
        records = tuple(
            evidence_record(
                "候选",
                candidate.url,
                issue,
                tuple(f"{value:02d}" for value in range(1, 37)),
                page_index=index,
            )
            for index, issue in enumerate((217, 216, 215, 214, 213, 212, 210, 209, 208))
        )
        window = SiteWindow(
            candidate.name,
            candidate.url,
            217,
            10,
            records,
            latest_issue=217,
            site_id=candidate.site_id,
            parser_id=candidate.parser_id,
        )

        validate_candidate_window(window, 217, 10, config=candidate)

    def test_insufficient_history_without_exception_is_rejected(self) -> None:
        candidate = SiteConfig(
            "候选",
            "https://candidate.test/topic/1",
            keywords=("36码中特",),
            section_keywords=("候选",),
            site_id="candidate-id",
            parser_id="generic_36",
            region="top",
        )
        records = tuple(
            evidence_record(
                "候选",
                candidate.url,
                issue,
                tuple(f"{value:02d}" for value in range(1, 37)),
                page_index=index,
            )
            for index, issue in enumerate((217, 216, 215, 214, 213, 212, 210, 209, 208))
        )
        window = SiteWindow(candidate.name, candidate.url, 217, 10, records)

        with self.assertRaisesRegex(ScrapeError, "有效数据不足"):
            validate_candidate_window(window, 217, 10, config=candidate)


if __name__ == "__main__":
    unittest.main()
