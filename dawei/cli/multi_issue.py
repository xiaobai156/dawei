"""CLI for multiple explicitly listed issues without recent-cache updates."""

from __future__ import annotations

import argparse

from dawei.application.batch_service import (
    DEFAULT_OUTPUT_DIR,
    write_multi_failure_summary,
)
from dawei.cli import single_issue


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape multiple exact issues without updating cache.")
    parser.add_argument("issues", nargs="*", type=int)
    parser.add_argument(
        "--prompt-issues",
        action="store_true",
        help="在Python进程内安全读取空格分隔的正整数期数（供BAT入口使用）",
    )
    args = parser.parse_args(argv)
    if args.prompt_issues:
        if args.issues:
            parser.error("--prompt-issues cannot be combined with positional issues")
        try:
            raw_issues = input("Input issue numbers, separated by spaces: ").split()
        except EOFError:
            parser.error("issue input is required")
        if not raw_issues or any(not value.isascii() or not value.isdecimal() for value in raw_issues):
            parser.error("all issues must contain only decimal digits")
        args.issues = [int(value) for value in raw_issues]
    if not args.issues:
        parser.error("at least one issue is required unless --prompt-issues is used")
    if any(issue <= 0 for issue in args.issues):
        parser.error("all issues must be positive")
    return args


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
