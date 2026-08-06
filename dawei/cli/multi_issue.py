"""CLI for multiple explicitly listed issues without recent-cache updates."""

from __future__ import annotations

import argparse

from dawei.application.batch_service import DEFAULT_OUTPUT_DIR, write_multi_failure_summary
from dawei.cli import single_issue


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape multiple exact issues without updating cache.")
    parser.add_argument("issues", nargs="+", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    issues = tuple(dict.fromkeys(args.issues))
    failed = False
    for issue in issues:
        print(f"\n===== Issue {issue} =====")
        if single_issue.main(["--fixed-issue", str(issue), "--no-update-recent-cache"]):
            failed = True
    path = write_multi_failure_summary(issues, DEFAULT_OUTPUT_DIR)
    print(f"Multi-issue all-failed summary written: {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
