"""Validate only listed failed sites without writing production outputs or cache."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable, Mapping, TextIO

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
    "validate_case",
]


SCRIPT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SITES_PATH = SCRIPT_DIR / "sites_36.json"

# 212期失败TXT的逐站修复清单；正式验证只执行这32个站点。
FAILED_SITE_CASES: tuple[Mapping[str, object], ...] = (
    {"name": "智能", "url": "https://jjlachsn.4fk3e-i2rpf-vgknlh.work:16677/topic/655262.html", "issue": 212},
    {"name": "美人鱼", "url": "https://ryfbdty.p4a53-f1hew-diwcrg.xyz:16677/topic/185453.html", "issue": 212},
    {"name": "生财有道", "url": "https://ymfrudo.b6vcc-ns4ex-othxov.xyz:16677/", "issue": 212},
    {"name": "彩民书籍", "url": "https://ymqmvj.7md3q-rm157-wcvhte.work:16677/topic/567129.html", "issue": 212},
    {"name": "幽默玄机", "url": "https://eqdfqyah.jigmo-9e498-sxucok.xyz:16677/topic/221876.html", "issue": 212},
    {"name": "白虎五脏六腑", "url": "https://mtubiej.hnb7z-u00nl-xfysct.work:16622/topic/362725.html", "issue": 212},
    {"name": "天线宝宝", "url": "https://ivwdxnm.p7tcr-yus9b-vzzgop.xyz:16677/", "issue": 212},
    {"name": "灵蛇", "url": "https://zsshnok.iism3-e5t4z-jlykmo.work:16644/topic/625998.html", "issue": 212},
    {"name": "抓码王", "url": "https://wdpqveqo.efkdrr-ctvtm-kkrehx.work:16644/", "issue": 212},
    {"name": "抓码口是心非", "url": "https://bgisusu.jeh3qq-suqvlp-vjdok.work:16644/topic/439181.html", "issue": 212},
    {"name": "澳门神算子", "url": "https://bugimbaf.6fp8f-knubw-yhjilp.xyz:16677/", "issue": 212},
    {"name": "安之若素", "url": "https://cpvqejp.avdja-la48l-xnsdva.xyz:16677/topic/336912.html", "issue": 212},
    {"name": "黑沙", "url": "https://ugtzszfp.ldpiz-8xhrd-wpkjkn.xyz:16677/topic/495299.html", "issue": 212},
    {"name": "本草纲目", "url": "https://tcwsqrno.ril3o-7ghui-hiepuc.work:16633/topic/160805.html", "issue": 212},
    {"name": "澳门黄金屋", "url": "https://lonctpwj.fgvio-7zcgn-oxdqxr.xyz:16677/", "issue": 212},
    {"name": "爆中", "url": "https://ugtzszfp.ldpiz-8xhrd-wpkjkn.xyz:16677/", "issue": 212},
    {"name": "正版管家婆", "url": "https://cykiiyt.4hxms-k65ek-jsvqzm.xyz:16677/", "issue": 212},
    {"name": "周公神算", "url": "https://xxn08n.uf6h9-z8vxq-smsfdt.work/", "issue": 212},
    {"name": "内幕快报", "url": "https://piyaptz.vl4bk-ubmwm-qoznah.xyz:16677/", "issue": 212},
    {"name": "码仙专区", "url": "https://ueemjyd.lsudm-vriij-ctsqwh.work:29466/article/admin/6a12c96a6cb8d34478078a6e?url=hcf", "issue": 212},
    {"name": "小鱼儿", "url": "https://ebhxngwb.2ljj5-vdh8s-gbvkgl.xyz:16677/", "issue": 212},
    {"name": "葡京专区", "url": "https://pgyzulb.iwnn7-gyyip-pnpfqv.work:29477/article/admin/6a153b1c8be59b17287c6ce3?url=bflc", "issue": 212},
    {"name": "澳彩家园", "url": "https://sheuzjss.tgpcj-9w0vl-mwyhly.work:29422/article/admin/6a10cc5030a2246c4fe15acb?url=zfw", "issue": 212},
    {"name": "好料名流", "url": "https://aszmkf.c3z3l-qrlqm-mwgccr.work:29411/article/admin/6a08367708adb5ed7357efae?url=lhw", "issue": 212},
    {"name": "知无不言", "url": "https://yzpbdkow.6m1ba-7p7u4-ppolcy.work:29433/article/admin/6a02d4be8bad0a3579e9f761?url=tdg", "issue": 212},
    {"name": "天马行空", "url": "https://fymxwnyg.ttmzc-muvns-udlfln.work:29455/article/admin/6a2c2a179d979617a85f1551?url=lhbd", "issue": 212},
    {"name": "横财就手", "url": "https://cahgjib.5blx9-z8506-ekiwxc.work:29488/article/admin/6a3ab9cd018539c611cbe30e?url=lqz", "issue": 212},
    {"name": "状元红", "url": "https://3.www112291a.com:2053/gengxin/58.html", "issue": 212},
    {"name": "雪球", "url": "https://ocnrhq.du156-vb27w-tmhsed.xyz:16677/", "issue": 212},
    {"name": "战无不胜", "url": "https://utklnwvn.xt8d7-kd9kc-csprqr.work:29488/article/admin/6a32070fc9b8712c823a5fe8?url=qdz", "issue": 212},
    {"name": "澳彩公益", "url": "https://zqrtppa.9swju-6haxn-dlkint.work:29455/article/admin/6a33a0b2dfa16552b923cacd?url=yjs", "issue": 212},
    {"name": "四海晏然", "url": "https://nlafoq9v.dh5565656.xyz/bbs/topic.php?id=1065", "issue": 212},
)


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
        return 0
    failures = 0
    site_list = tuple(sites)
    service = RepairService()
    for index, case in enumerate(case_list, start=1):
        if index > 1:
            print("", file=output)
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
        except Exception as exc:
            failures += 1
            print(f"测试清单解析失败: {exc}", file=output)
            continue
        print(format_report(report), file=output)
        if not report.passed:
            failures += 1
    print("", file=output)
    print(f"汇总: 通过 {len(case_list) - failures} / 失败 {failures} / 总计 {len(case_list)}", file=output)
    print("隔离状态: 未更新近10期缓存，未写正式成功/失败TXT。", file=output)
    return 1 if failures else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只验证清单中的失败站点，不跑全站、不写正式输出。")
    parser.add_argument("--issue", type=int, default=None, help="临时覆盖清单内所有站点的指定期数。")
    parser.add_argument("--only", nargs="*", help="只运行指定站名或URL。")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--sites-config", default=str(DEFAULT_SITES_PATH))
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--proxy-retries", type=int, default=1)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cases = tuple(case_from_mapping(item) for item in FAILED_SITE_CASES)
        if args.only:
            wanted = set(args.only)
            cases = tuple(
                case
                for case in cases
                if (case.name and case.name in wanted) or (case.url and case.url in wanted)
            )
        sites = load_sites(Path(args.sites_config))
        configure_proxy(args.proxy)
    except Exception as exc:
        print(f"验证器启动失败: {exc}", file=sys.stderr)
        return 2
    return run_cases(cases, sites, timeout=args.timeout, issue_override=args.issue)


if __name__ == "__main__":
    raise SystemExit(main())
