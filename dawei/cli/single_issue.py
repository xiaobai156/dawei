"""CLI for one explicitly specified issue."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dawei.application.batch_service import (
    DEFAULT_BACKUP_PATH,
    BatchOptions,
    SingleIssueBatchService,
    default_error_output_path,
    default_output_path,
)
from dawei.domain.errors import ScrapeError

SCRIPT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SITES_PATH = SCRIPT_DIR / "sites_36.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape one exact issue and write compatible txt files.")
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("--error-output", default=None)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--fixed-issue", type=int, default=None)
    parser.add_argument(
        "--prompt-issue",
        action="store_true",
        help="在Python进程内安全读取一个正整数期数（供BAT入口使用）",
    )
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--sites-config", default=str(DEFAULT_SITES_PATH))
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--retry-failed", default=None)
    parser.add_argument("--health-check", action="store_true")
    parser.add_argument("--cache-dir", default="cache")
    parser.add_argument("--merge-with", default=None)
    parser.add_argument("--validation-output", default=None)
    parser.add_argument("--proxy-retries", type=int, default=1)
    parser.add_argument("--no-update-recent-cache", action="store_true")
    args = parser.parse_args(argv)
    if args.prompt_issue:
        if args.fixed_issue is not None:
            parser.error("--prompt-issue cannot be combined with --fixed-issue")
        try:
            raw_issue = input("Input required issue number: ").strip()
        except EOFError:
            parser.error("issue input is required")
        if not raw_issue.isascii() or not raw_issue.isdecimal():
            parser.error("issue must contain only decimal digits")
        args.fixed_issue = int(raw_issue)
    if args.fixed_issue is None:
        parser.error("--fixed-issue is required unless --prompt-issue is used")
    if args.fixed_issue <= 0:
        parser.error("--fixed-issue must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.workers <= 0:
        parser.error("--workers must be positive")
    if args.proxy_retries <= 0:
        parser.error("--proxy-retries must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    options = BatchOptions(
        fixed_issue=args.fixed_issue,
        output_path=Path(args.output or default_output_path(args.fixed_issue)),
        error_output_path=Path(args.error_output or default_error_output_path(args.fixed_issue)),
        timeout=args.timeout,
        workers=args.workers,
        health_check=args.health_check,
        cache_dir=Path(args.cache_dir) if args.cache_dir else None,
        update_recent_cache=not args.no_update_recent_cache,
        recent_cache_path=DEFAULT_BACKUP_PATH,
        merge_with=Path(args.merge_with) if args.merge_with else None,
        proxy_retries=args.proxy_retries,
    )
    try:
        result = SingleIssueBatchService().run_configured(
            Path(args.sites_config),
            options,
            only=args.only or (),
            retry_failed=Path(args.retry_failed) if args.retry_failed else None,
            proxy=args.proxy,
        )
    except ScrapeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if result.timings:
        print("\n慢站耗时统计（前10名）:")
        for index, (elapsed, name, status) in enumerate(sorted(result.timings, reverse=True)[:10], 1):
            print(f"{index}. {name} {status} {elapsed:.1f}秒")
    for failure in result.failures:
        print(f"Error: {failure}", file=sys.stderr)
    if result.cache_error:
        print(f"Error: {result.cache_error}", file=sys.stderr)
    print(
        f"Summary: success {len(result.results)} / failed {len(result.failures)} / "
        f"total {result.total_sites}"
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
