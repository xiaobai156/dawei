"""Test-only facade composed from the active V2 duplicate modules."""

from __future__ import annotations

import json
from pathlib import Path

from dawei.application import duplicate_service, onboarding_service
from dawei.application.duplicate_runner import DuplicateRunner, format_failure
from dawei.cli import duplicate as duplicate_cli


__all__ = ["format_failure", "json"]


DEFAULT_PERIODS = 10
DEFAULT_MIN_COMMON = 3
DEFAULT_DUPLICATE_COMMON = 6
SiteWindow = duplicate_service.SiteWindow
BackupSnapshot = duplicate_service.BackupSnapshot
DuplicateMatch = duplicate_service.DuplicateMatch
detect_latest_period = duplicate_service.detect_latest_period
with_period = duplicate_service.with_period
site_results_from_candidates = duplicate_service.site_results_from_candidates
collect_issue_records = duplicate_service.collect_issue_records
select_recent_records = duplicate_service.select_recent_records
matching_consecutive_issues = duplicate_service.matching_consecutive_issues
are_duplicate = duplicate_service.are_duplicate
duplicate_groups_and_matches = duplicate_service.duplicate_groups_and_matches
duplicate_groups = duplicate_service.duplicate_groups
site_key = onboarding_service.site_key
matches_involving_sites = onboarding_service.matches_involving_sites
validate_candidate_window = onboarding_service.validate_candidate_window
write_backup_json = duplicate_service.write_backup_json
load_backup_snapshot = duplicate_service.load_backup_snapshot
load_backup_json = duplicate_service.load_backup_json
scrape_site_window = duplicate_service.scrape_site_window


def write_failures(failures, output_path: Path) -> None:
    duplicate_cli.write_failures(output_path, tuple(failures))


def write_duplicate_groups(
    results,
    output_path: Path,
    min_common: int,
    duplicate_common: int = DEFAULT_DUPLICATE_COMMON,
) -> None:
    windows = tuple(results)
    groups, matches = duplicate_service.duplicate_groups_and_matches(
        list(windows),
        min_common,
        duplicate_common,
    )
    duplicate_cli.write_text(
        output_path,
        duplicate_cli.duplicate_report(
            windows,
            tuple(tuple(group) for group in groups),
            tuple(matches),
            min_common,
            duplicate_common,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    def delegated_scrape(*args, **kwargs):
        return scrape_site_window(*args, **kwargs)

    return duplicate_cli.main(
        argv,
        runner=DuplicateRunner(window_scraper=delegated_scrape),
    )
