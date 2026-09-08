"""Validate only listed failed sites without writing production outputs or cache."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import TextIO

from dawei.application.repair_service import (
    ContentDiagnostics,
    RepairService,
    ResolvedCase,
    ValidationCase,
    ValidationReport,
    analyze_content,
    case_from_mapping,
    configure_proxy,
    dedicated_candidates,
    empty_diagnostics,
    generic_candidates,
    load_sites,
    normalize_url,
    resolve_case,
    validate_case,
)
from dawei.domain.errors import ScrapeError
from dawei.domain.models import ParsedRecord, SiteConfig

__all__ = [
    "ContentDiagnostics",
    "RepairService",
    "ResolvedCase",
    "ValidationCase",
    "ValidationReport",
    "analyze_content",
    "case_from_mapping",
    "dedicated_candidates",
    "empty_diagnostics",
    "generic_candidates",
    "normalize_url",
    "resolve_case",
    "select_validation_cases",
    "validate_case",
]


SCRIPT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SITES_PATH = SCRIPT_DIR / "sites_36.json"


def _never_write(*args, **kwargs) -> None:
    raise AssertionError("失败站验证器禁止调用生产写入器")


# 仅保留旧测试使用的数据类型兼容面，不导入正式主入口。
scraper = SimpleNamespace(
    SiteConfig=SiteConfig,
    SiteResult=ParsedRecord,
    ScrapeError=ScrapeError,
    update_recent_duplicate_backup=_never_write,
    write_results=_never_write,
    write_failures=_never_write,
)


def pass_text(value: bool) -> str:
    return "通过" if value else "失败"


def format_report(report: ValidationReport) -> str:
    diagnostics = report.diagnostics
    result = report.formal_result
    actual_numbers = result.numbers if result is not None else diagnostics.numbers
    actual_issue_found = result is not None and result.issue == report.case.issue
    return "\n".join(
        (
            f"站点: {report.case.config.name}",
            f"URL: {report.case.config.url}",
            f"指定期数: {report.case.issue}期",
            f"真实正文来源: {report.source_kind}",
            f"浏览器兜底: {'是' if report.rendered else '否'}",
            f"是否抓到指定期数: {'是' if actual_issue_found else '否'}",
            f"实际数字数量: {len(actual_numbers)}",
            f"实际数字: {','.join(actual_numbers) or '无'}",
            f"top/bottom: {diagnostics.region}，{pass_text(diagnostics.direction_pass)}",
            f"锚点: {pass_text(diagnostics.anchor_pass)}",
            f"关键词: {pass_text(diagnostics.keyword_pass)}",
            f"数字: {pass_text(diagnostics.numbers_pass)}",
            f"同期冲突: {'有' if diagnostics.same_issue_conflict else '无'}",
            f"重复数字: {','.join(diagnostics.duplicate_numbers) or '无'}",
            f"文章ID: {report.record_id or '无'}",
            f"来源路径: {report.source_path or '无'}",
            f"原始位置: {report.raw_position if report.raw_position is not None else '无'}",
            f"最终结果: {pass_text(report.passed)}",
            f"失败原因: {report.failure_reason or '无'}",
        )
    )


def select_validation_cases(
    sites: Iterable[SiteConfig],
    selectors: Iterable[str],
    issue: int,
) -> tuple[ValidationCase, ...]:
    if type(issue) is not int or issue <= 0:
        raise ScrapeError("验证期数必须为正整数")
    site_list = tuple(sites)
    selected: list[ValidationCase] = []
    selected_ids: set[str] = set()
    values = tuple(selectors)
    if not values or any(type(value) is not str or not value.strip() for value in values):
        raise ScrapeError("必须明确指定至少一个站名或URL")
    for value in values:
        selector = value.strip()
        matches = tuple(
            site
            for site in site_list
            if site.name == selector or normalize_url(site.url) == normalize_url(selector)
        )
        if not matches:
            raise ScrapeError(f"当前正式配置未找到指定站点: {selector}")
        if len(matches) != 1:
            identities = "；".join(f"{site.name} {site.url}" for site in matches)
            raise ScrapeError(f"指定站点不唯一: {selector} -> {identities}")
        site = matches[0]
        if site.site_id in selected_ids:
            continue
        selected_ids.add(site.site_id)
        selected.append(ValidationCase(issue=issue, name=site.name, url=site.url))
    if not selected:
        raise ScrapeError("指定站点选择为空")
    return tuple(selected)


def run_cases(
    cases: Iterable[ValidationCase],
    sites: Iterable[SiteConfig],
    timeout: int,
    output: TextIO = sys.stdout,
    issue_override: int | None = None,
    source_fetcher=None,
    site_scraper=None,
) -> int:
    case_list = tuple(cases)
    if not case_list:
        print("失败站点测试清单为空，未执行抓取。", file=output)
        return 1
    failures = 0
    site_list = tuple(sites)
    service = RepairService()
    for index, case in enumerate(case_list, start=1):
        if index > 1:
            print(file=output)
        print(f"===== 失败站点验证 {index}/{len(case_list)} =====", file=output)
        try:
            resolved = resolve_case(case, site_list, issue_override=issue_override)
            if source_fetcher is None and site_scraper is None:
                report = service.validate(resolved, timeout=timeout)
            else:
                report = validate_case(
                    resolved,
                    timeout=timeout,
                    source_fetcher=source_fetcher,
                    site_scraper=site_scraper,
                )
        except Exception as exc:  # noqa: BLE001 - report each isolated validation case
            failures += 1
            print(f"测试清单解析失败: {exc}", file=output)
            continue
        print(format_report(report), file=output)
        if not report.passed:
            failures += 1
    print(file=output)
    print(f"汇总: 通过 {len(case_list) - failures} / 失败 {failures} / 总计 {len(case_list)}", file=output)
    print("隔离状态: 未更新近10期缓存，未写正式成功/失败TXT。", file=output)
    return 1 if failures else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只验证清单中的失败站点，不跑全站、不写正式输出。")
    parser.add_argument("--issue", type=int, required=True, help="指定要验证的正整数期数。")
    parser.add_argument("--only", nargs="+", required=True, help="指定当前正式配置中的站名或URL。")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--sites-config", default=str(DEFAULT_SITES_PATH))
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--proxy-retries", type=int, default=1)
    args = parser.parse_args(argv)
    if args.issue <= 0:
        parser.error("--issue must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.proxy_retries <= 0:
        parser.error("--proxy-retries must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        sites = load_sites(Path(args.sites_config))
        cases = select_validation_cases(sites, args.only, args.issue)
        configure_proxy(args.proxy)
    except Exception as exc:  # noqa: BLE001 - CLI must report startup diagnostics
        print(f"验证器启动失败: {exc}", file=sys.stderr)
        return 2
    return run_cases(cases, sites, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
