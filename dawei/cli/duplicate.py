"""CLI for recent-window duplicate detection and candidate checks."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dawei.application.duplicate_runner import DuplicateOptions, DuplicateRunner
from dawei.application.duplicate_service import DuplicateMatch, SiteWindow
from dawei.domain.errors import ScrapeError
from dawei.infrastructure.cache_repository import atomic_write_text

SCRIPT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = Path(
    r"C:\Users\Administrator\Desktop\每天工具\爬虫合集\大围杀号生肖数据统一归纳"
)


def issue_range(issues: tuple[int, ...]) -> str:
    if not issues:
        return "无"
    return f"{issues[0]}期" if len(issues) == 1 else f"{issues[0]}~{issues[-1]}期"


def duplicate_report(
    windows: tuple[SiteWindow, ...],
    groups: tuple[tuple[SiteWindow, ...], ...],
    matches: tuple[DuplicateMatch, ...],
    min_common: int,
    duplicate_common: int,
) -> str:
    duplicate_matches = [m for m in matches if m.consecutive_count >= duplicate_common]
    review_matches = [m for m in matches if min_common <= m.consecutive_count < duplicate_common]
    lines: list[str] = []
    if groups:
        lines.append("重复网站（连续6期或以上一致，直接拒收）")
    for group_index, members in enumerate(groups, start=1):
        lines.extend((f"重复组{group_index}", "站点："))
        lines.extend(f"- {member.name} {member.url}" for member in members)
        member_keys = {(member.site_id, member.name, member.url) for member in members}
        lines.append("重复明细：")
        for match in duplicate_matches:
            left_key = (match.left.site_id, match.left.name, match.left.url)
            right_key = (match.right.site_id, match.right.name, match.right.url)
            if left_key not in member_keys or right_key not in member_keys:
                continue
            lines.append(
                f"- {match.left.name} <=> {match.right.name}: {issue_range(match.issues)}，"
                f"连续{match.consecutive_count}期，每期36码位置+数值完全一致"
            )
            lines.extend(
                f"  {issue}期: {','.join(match.left.by_issue[issue].numbers)}"
                for issue in match.issues
            )
        lines.append("")
    if review_matches:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("疑似重复（连续3-5期一致，人工审核）")
        for index, match in enumerate(review_matches, start=1):
            lines.extend(
                (
                    f"疑似组{index}",
                    f"- {match.left.name} {match.left.url}",
                    f"- {match.right.name} {match.right.url}",
                    (
                        f"重复明细：{issue_range(match.issues)}，连续{match.consecutive_count}期，"
                        "每期36码位置+数值完全一致"
                    ),
                )
            )
            lines.extend(
                f"  {issue}期: {','.join(match.left.by_issue[issue].numbers)}"
                for issue in match.issues
            )
            lines.append("")
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def write_text(path: Path, text: str) -> None:
    atomic_write_text(path, text, encoding="utf-8-sig")


def write_failures(path: Path, failures: tuple[str, ...]) -> None:
    if not failures:
        path.unlink(missing_ok=True)
        return
    write_text(path, "\n\n".join(failures) + "\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect duplicate sites from exact issue-aligned windows.")
    parser.add_argument("--period", type=int, default=None)
    parser.add_argument(
        "--prompt-period",
        action="store_true",
        help="在Python进程内安全读取期数；空输入表示自动识别最新期（供BAT入口使用）",
    )
    parser.add_argument("--periods", type=int, default=10)
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("--error-output", default=None)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--min-common", type=int, default=3)
    parser.add_argument("--duplicate-common", type=int, default=6)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--sites-config", default=str(SCRIPT_DIR / "sites_36.json"))
    parser.add_argument("--backup-json", default=str(SCRIPT_DIR / "recent_10_cache.json"))
    parser.add_argument("--update-backup", action="store_true")
    parser.add_argument("--use-backup", action="store_true")
    parser.add_argument("--candidate-site", action="append")
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--proxy-retries", type=int, default=1)
    args = parser.parse_args(argv)
    if args.prompt_period:
        if args.period is not None:
            parser.error("--prompt-period cannot be combined with --period")
        try:
            raw_period = input("Input issue number, press Enter for auto latest: ").strip()
        except EOFError:
            parser.error("period input is required or press Enter for auto latest")
        if raw_period:
            if not raw_period.isascii() or not raw_period.isdecimal():
                parser.error("period must contain only decimal digits")
            args.period = int(raw_period)
    if args.period is not None and args.period <= 0:
        parser.error("--period must be positive")
    if args.periods < 1:
        parser.error("--periods must be at least 1")
    if args.timeout <= 0 or args.workers <= 0:
        parser.error("--timeout and --workers must be positive")
    if args.min_common < 1 or args.duplicate_common < 1:
        parser.error("--min-common and --duplicate-common must be at least 1")
    if not args.min_common <= args.duplicate_common <= args.periods:
        parser.error("require --min-common <= --duplicate-common <= --periods")
    if args.proxy_retries <= 0:
        parser.error("--proxy-retries must be positive")
    if args.candidate_site and not args.use_backup:
        parser.error("新增候选站必须使用 --use-backup 读取近10期备份JSON，禁止实时全站抓取作为判重依据")
    return args


def main(
    argv: list[str] | None = None,
    runner: DuplicateRunner | None = None,
) -> int:
    args = parse_args(argv)
    try:
        result = (runner or DuplicateRunner()).run(
            DuplicateOptions(
                sites_config=Path(args.sites_config),
                backup_path=Path(args.backup_json),
                period=args.period,
                periods=args.periods,
                timeout=args.timeout,
                workers=args.workers,
                min_common=args.min_common,
                duplicate_common=args.duplicate_common,
                only=tuple(args.only or ()),
                candidate_sites=tuple(args.candidate_site or ()),
                use_backup=args.use_backup,
                update_backup=args.update_backup,
                proxy=args.proxy,
                proxy_retries=args.proxy_retries,
            )
        )
    except ScrapeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if result.auto_period:
        print(f"自动识别最新期数: {result.period}")
    output = Path(args.output) if args.output else DEFAULT_OUTPUT_DIR / f"{result.period}期重复网站.txt"
    errors = Path(args.error_output) if args.error_output else DEFAULT_OUTPUT_DIR / f"{result.period}期重复检测失败.txt"
    write_text(
        output,
        duplicate_report(
            result.windows,
            result.groups,
            result.matches,
            args.min_common,
            args.duplicate_common,
        ),
    )
    failures = list(result.failures)
    if result.onboarding_error:
        failures.append(f"新增站点验证失败: {result.onboarding_error}")
    write_failures(errors, tuple(failures))
    for failure in result.failures:
        print(f"Error: {failure}", file=sys.stderr)
    if result.onboarding_error and not result.blocking_matches:
        print(f"Error: {result.onboarding_error}", file=sys.stderr)
    for match in result.blocking_matches:
        print(
            f"Error: {match.status(args.duplicate_common)}：{match.left.name} <=> "
            f"{match.right.name} {issue_range(match.issues)}，连续{match.consecutive_count}期",
            file=sys.stderr,
        )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
