from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from dawei.application import duplicate_service, onboarding_service
from dawei.application.duplicate_runner import DuplicateOptions, DuplicateRunner
from dawei.application.duplicate_service import BackupSnapshot, SiteWindow
from dawei.application.onboarding_service import (
    OnboardingService,
    candidate_identity_conflicts,
    matches_involving_sites,
    normalized_url,
    record_identities,
    site_key,
    topic_identity,
    validate_candidate_window,
)
from dawei.application.scrape_service import ScrapeService, SourceLoadResult
from dawei.domain.errors import CacheError, ConfigurationError, ScrapeError
from dawei.domain.models import (
    ArticleRecord,
    CacheSite,
    CacheSnapshot,
    CandidateEvidence,
    CandidateOrigin,
    ParsedRecord,
    ScrapeRecord,
    SiteConfig,
)
from dawei.infrastructure.cache_repository import CacheRepository
from dawei.infrastructure.config_repository import ConfigRepository, config_fingerprint
from dawei.parsers import dynamic_article, generic_36
from dawei.parsers.registry import (
    ParserRegistry,
    extract_from_candidates,
    resolve_parser_id,
)


def _numbers(offset: int = 0) -> tuple[str, ...]:
    values = [((offset + index) % 49) + 1 for index in range(36)]
    return tuple(f"{value:02d}" for value in values)


def _candidate(
    issue: int,
    page_index: int,
    *,
    document_id: str = "document-0",
    numbers: tuple[str, ...] | None = None,
) -> CandidateEvidence:
    values = numbers or _numbers(issue)
    origin = CandidateOrigin(
        raw_issue_line=f"栏目 {issue}期",
        raw_number_lines=("专属 " + " ".join(values),),
        anchor_line="目标栏目",
        document_id=document_id,
        document_url="https://example.test/topic",
        source_method="generic_html",
        block_id=f"{document_id}:0-100",
        block_start=0,
        block_end=100,
        page_index=page_index,
        block_index=page_index,
        parser_id="generic_36",
    )
    return CandidateEvidence(
        issue=issue,
        numbers=values,
        raw_issue_line=origin.raw_issue_line,
        raw_number_lines=origin.raw_number_lines,
        anchor_line=origin.anchor_line,
        document_id=origin.document_id,
        document_url=origin.document_url,
        source_method=origin.source_method,
        block_id=origin.block_id,
        block_start=origin.block_start,
        block_end=origin.block_end,
        page_index=page_index,
        block_index=page_index,
        parser_id=origin.parser_id,
        origins=(origin,),
    )


def _config(
    *,
    region: str = "top",
    fixed_issue: int | None = None,
    exception: str | None = None,
) -> SiteConfig:
    return SiteConfig(
        name="候选站",
        url="https://example.test/topic",
        keywords=("专属",),
        section_keywords=("目标栏目",),
        fixed_issue=fixed_issue,
        region=region,
        site_id="site_candidate",
        parser_id="generic_36",
        onboarding_exception=exception,
    )


def _window(config: SiteConfig, records: list[ParsedRecord]) -> SiteWindow:
    return SiteWindow(
        name=config.name,
        url=config.url,
        period=225,
        periods=10,
        records=tuple(records),
        latest_issue=225,
        site_id=config.site_id,
        parser_id=config.parser_id,
    )


def _parsed(candidate: CandidateEvidence) -> ParsedRecord:
    return ParsedRecord(
        name="候选站",
        url="https://example.test/topic",
        issue=candidate.issue,
        numbers=candidate.numbers,
        raw_position=candidate.page_index,
        evidence=candidate,
    )


def _plain_record(
    issue: int,
    *,
    name: str = "站点",
    url: str = "https://example.test/topic",
    numbers: tuple[str, ...] | None = None,
    record_id: str | None = None,
    raw_position: int | None = 0,
) -> ParsedRecord:
    return ParsedRecord(
        name,
        url,
        issue,
        numbers or _numbers(issue),
        record_id=record_id,
        raw_position=raw_position,
    )


def _plain_window(
    issues: list[int],
    *,
    name: str = "站点",
    url: str = "https://example.test/topic",
    numbers: tuple[str, ...] | None = None,
) -> SiteWindow:
    records = tuple(
        _plain_record(
            issue,
            name=name,
            url=url,
            numbers=numbers,
            raw_position=index,
        )
        for index, issue in enumerate(issues)
    )
    return SiteWindow(name, url, max(issues, default=None) or 0, len(records), records)


class DuplicateServiceCoverageTests(unittest.TestCase):
    @staticmethod
    def _bulk_generic_text(issues: tuple[int, ...], *, conflict: bool = False) -> str:
        rows: list[str] = []
        for index, issue in enumerate(issues):
            numbers = _numbers(issue)
            if conflict and index == 1:
                numbers = _numbers(issue + 1)
            rows.extend(("栏目 目标栏目 专属", f"{issue}期", " ".join(numbers)))
        return "\n".join(rows)

    def test_scrape_window_preserves_full_direction_candidates_for_onboarding(self) -> None:
        config = _config(region="bottom")
        text = self._bulk_generic_text(tuple(range(225, 194, -1)))
        window = duplicate_service.scrape_site_window(
            config,
            225,
            10,
            10,
            5,
            text_fetcher=lambda _url, _timeout: text,
        )

        self.assertEqual(10, len(window.records))
        self.assertEqual(31, len(window.direction_candidates))
        with self.assertRaisesRegex(ScrapeError, "方向候选范围"):
            validate_candidate_window(window, 225, 10, config=config)

        top_config = replace(config, region="top")
        top_window = duplicate_service.scrape_site_window(
            top_config,
            225,
            10,
            10,
            5,
            text_fetcher=lambda _url, _timeout: text,
        )
        validate_candidate_window(top_window, 225, 10, config=top_config)

    def test_scrape_window_does_not_hide_repeated_issue_conflict(self) -> None:
        config = _config(region="top")
        text = self._bulk_generic_text((225, 225, *range(224, 194, -1)), conflict=True)
        with self.assertRaisesRegex(ScrapeError, "多个高可信候选36码冲突"):
            duplicate_service.scrape_site_window(
                config,
                225,
                10,
                10,
                5,
                text_fetcher=lambda _url, _timeout: text,
            )

    def test_duplicate_runner_real_scrape_and_onboarding_keep_direction_source(self) -> None:
        configured = SiteConfig(
            "基准站",
            "https://baseline.test/topic",
            keywords=("专属",),
            section_keywords=("目标栏目",),
            region="top",
            site_id="baseline",
            parser_id="generic_36",
        )
        text = self._bulk_generic_text(tuple(range(225, 194, -1)))

        def run_candidate(region: str, root: Path):
            candidate = _config(region=region)
            candidate_path = root / f"candidate-{region}.json"
            candidate_path.write_text(
                json.dumps(
                    {
                        "name": candidate.name,
                        "url": candidate.url,
                        "site_id": candidate.site_id,
                        "source_type": candidate.source_type,
                        "parser_id": candidate.parser_id,
                        "region": region,
                        "render_policy": candidate.render_policy,
                        "section_keywords": list(candidate.section_keywords),
                        "keywords": list(candidate.keywords),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            def scrape(site, period, periods, min_records, timeout, _fetcher):
                return duplicate_service.scrape_site_window(
                    site,
                    period,
                    periods,
                    min_records,
                    timeout,
                    text_fetcher=lambda _url, _timeout: text,
                )

            return DuplicateRunner(
                window_scraper=scrape,
                progress_sink=lambda _message: None,
            ).run(
                DuplicateOptions(
                    sites_config=root / "sites.json",
                    backup_path=root / "recent_10_cache.json",
                    period=225,
                    periods=10,
                    timeout=1,
                    workers=1,
                    min_common=1,
                    duplicate_common=1,
                    candidate_sites=(str(candidate_path),),
                    use_backup=True,
                )
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ConfigRepository(root / "sites.json").save((configured,))
            CacheRepository(root / "recent_10_cache.json").update(
                [
                    ScrapeRecord(
                        site_id=configured.site_id,
                        name=configured.name,
                        url=configured.url,
                        issue=225,
                        numbers=_numbers(),
                        parser_id=configured.parser_id,
                    )
                ],
                [],
                fixed_issue=225,
                periods=10,
                config_fingerprint=config_fingerprint((configured,)),
            )

            outside = run_candidate("bottom", root)
            self.assertTrue(outside.failures)
            self.assertIn("方向候选范围", outside.failures[0])

            inside = run_candidate("top", root)
            self.assertEqual((), inside.failures)
            self.assertEqual("", inside.onboarding_error)

    def test_window_match_properties_and_same_issue_handling(self) -> None:
        first = _plain_record(225, record_id="r1", raw_position=1)
        second = _plain_record(224, record_id="r2", raw_position=2)
        window = SiteWindow("站点", "https://example.test/topic", 225, 2, (first, second))

        self.assertEqual((first.numbers, second.numbers), window.key)
        self.assertEqual((first, second), tuple(window.by_issue.values()))
        same = replace(first, raw_position=3)
        self.assertEqual(first, SiteWindow(window.name, window.url, 225, 2, (first, same)).by_issue[225])
        conflict = replace(first, numbers=_numbers(1))
        with self.assertRaisesRegex(ScrapeError, "同期36码冲突"):
            _ = SiteWindow(window.name, window.url, 225, 2, (first, conflict)).by_issue

        match = duplicate_service.DuplicateMatch(window, window, (225,))
        self.assertEqual(1, match.consecutive_count)
        self.assertEqual("重复", match.status(1))
        self.assertEqual("疑似重复", match.status(2))

    def test_load_backup_snapshot_success_and_fail_closed_errors(self) -> None:
        cached = ScrapeRecord(
            site_id="site-1",
            name="缓存站",
            url="https://example.test/cache",
            issue=225,
            numbers=_numbers(),
            record_id="record-1",
            source_path="article-1",
            raw_position=4,
            parser_id="generic_36",
        )
        snapshot = CacheSnapshot(
            period=225,
            periods=10,
            generated_at="now",
            incomplete=False,
            failures=(),
            sites=(CacheSite("site-1", "缓存站", "https://example.test/cache", (cached,)),),
        )
        with patch.object(duplicate_service.CacheRepository, "load", return_value=snapshot):
            loaded = duplicate_service.load_backup_snapshot("backup.json")
        self.assertEqual(225, loaded.period)
        self.assertEqual("record-1", loaded.sites[0].records[0].record_id)

        with patch.object(
            duplicate_service.CacheRepository,
            "load",
            side_effect=CacheError("坏缓存"),
        ), self.assertRaisesRegex(ScrapeError, "读取备份JSON失败"):
            duplicate_service.load_backup_snapshot("backup.json")
        for invalid in (
            replace(snapshot, period=None),
            replace(snapshot, sites=(CacheSite("site-1", "缓存站", "https://example.test/cache", ()),)),
        ):
            with patch.object(
                duplicate_service.CacheRepository,
                "load",
                return_value=invalid,
            ), self.assertRaises(ScrapeError):
                duplicate_service.load_backup_snapshot("backup.json")

    def test_period_selection_and_latest_issue_failures(self) -> None:
        records = tuple(_plain_record(issue, raw_position=index) for index, issue in enumerate((225, 224, 223)))
        self.assertEqual(records, duplicate_service.select_recent_records(records, 9999, 10, 1))
        self.assertEqual((records[0],), duplicate_service.select_recent_records(records, 225, 1, 1))
        with self.assertRaisesRegex(ScrapeError, "only found 0/1"):
            duplicate_service.select_recent_records(records, 100, 2, 1)
        with self.assertRaisesRegex(ScrapeError, "没有抓到任何可用记录"):
            duplicate_service.detect_latest_period(())
        result = SiteWindow("站", "url", 225, 1, (records[0],), latest_issue=None)
        self.assertEqual(225, duplicate_service.detect_latest_period((result,)))
        adjusted = duplicate_service.with_period(result, 225, 1)
        self.assertEqual(225, adjusted.records[0].issue)

    def test_site_results_and_collector_wrapper(self) -> None:
        config = _config(region="bottom")
        candidates = [
            _candidate(225, 1, numbers=_numbers(5)),
            _candidate(225, 4, numbers=_numbers(5)),
            _candidate(224, 2, numbers=_numbers(6)),
        ]
        records = duplicate_service.site_results_from_candidates(candidates, config)
        self.assertEqual((225, 224), tuple(record.issue for record in records))
        conflict = [
            _candidate(225, 1, numbers=_numbers(7)),
            _candidate(225, 4, numbers=_numbers(8)),
        ]
        with self.assertRaisesRegex(ScrapeError, "多个高可信候选36码冲突"):
            duplicate_service.site_results_from_candidates(conflict, config)

        collector = lambda _text, _config: (candidates, [], {224, 225})
        with patch.object(duplicate_service.DEFAULT_REGISTRY, "candidate_collector", return_value=collector):
            collected, direction_candidates = duplicate_service.collect_issue_records("raw", config)
        self.assertEqual((225, 224), tuple(record.issue for record in collected))
        self.assertEqual(tuple(candidates), direction_candidates)

    def test_scrape_site_window_collection_and_missing_api(self) -> None:
        config = replace(
            _config(region="bottom"),
            name="困难站",
            url="https://example.test/kunnan",
            source_type="dynamic_collection",
            api_url="https://api.example/users/3792/forums",
            parser_id="kunnan_magazine",
        )
        payload = json.dumps(
            [
                {
                    "user_id": "3792",
                    "status": "published",
                    "lottery": "macao",
                    "topic": "专属",
                    "id": f"record-{issue}",
                    "draw": str(issue),
                    "content": f"目标栏目 专属\n{issue}期\n{' '.join(f'{number:02d}' for number in range(1, 37))}",
                }
                for issue in (224, 225)
            ],
            ensure_ascii=False,
        )
        window = duplicate_service.scrape_site_window(
            config,
            225,
            2,
            1,
            5,
            text_fetcher=lambda _url, _timeout: payload,
        )
        self.assertEqual((225, 224), tuple(record.issue for record in window.records))

        with self.assertRaisesRegex(ScrapeError, "缺少专属 API"):
            duplicate_service.scrape_site_window(replace(config, api_url=None), 225, 2, 1, 5)

    def test_scrape_site_window_http_success_browser_fallback_and_article(self) -> None:
        config = _config(region="top")
        parsed = _plain_record(225, name=config.name, url=config.url, raw_position=0)
        source = SourceLoadResult("document", None, config.url, False)
        with (
            patch.object(duplicate_service, "load_source", return_value=source),
            patch.object(duplicate_service, "collect_issue_records", return_value=([parsed], ())),
        ):
            window = duplicate_service.scrape_site_window(config, 225, 1, 1, 5)
        self.assertEqual(225, window.records[0].issue)

        fallback_config = replace(config, render_policy="fallback")
        browser_source = SourceLoadResult("browser", None, config.url, True)
        with (
            patch.object(duplicate_service, "load_source", return_value=source),
            patch.object(
                duplicate_service,
                "collect_issue_records",
                side_effect=[ScrapeError("HTTP解析失败"), ([parsed], ())],
            ),
            patch.object(duplicate_service, "load_browser_source", return_value=browser_source),
        ):
            duplicate_service.scrape_site_window(fallback_config, 225, 1, 1, 5)

        paginated = replace(
            config,
            source_type="paginated_article_list",
            parser_id="paginated_article_36",
            navigation_keywords=("目标",),
        )
        with (
            patch.object(duplicate_service, "load_source", return_value=source),
            patch.object(duplicate_service, "collect_issue_records", side_effect=ScrapeError("坏详情")),
            self.assertRaisesRegex(ScrapeError, "坏详情"),
        ):
            duplicate_service.scrape_site_window(paginated, 225, 1, 1, 5)

        article = ArticleRecord("record-1", "https://example.test/article", "225期", "作者", "正文", "正文")
        article_source = SourceLoadResult("article", article, config.url, False)
        with (
            patch.object(duplicate_service, "load_source", return_value=article_source),
            patch.object(duplicate_service.dynamic_article, "validate_article_identity"),
            patch.object(duplicate_service, "collect_issue_records", return_value=([parsed], ())),
        ):
            duplicate_service.scrape_site_window(config, 225, 1, 1, 5)

    def test_paginated_window_fails_closed_without_history_detail_fetches(self) -> None:
        list_url = "https://example.test/list"
        detail_url = "https://example.test/article.aspx?id=225"
        config = SiteConfig(
            name="分页站",
            url=list_url,
            keywords=("专属",),
            section_keywords=("栏目",),
            region="top",
            site_id="site-paginated-window",
            source_type="paginated_article_list",
            parser_id="paginated_article_36",
            navigation_keywords=("目标",),
        )
        numbers = " ".join(_numbers())
        list_html = f'<a href="{detail_url}">225期 目标</a>'
        detail_html = f"栏目 专属\n225期\n{numbers}"
        calls: list[str] = []

        def fetch(url: str, _timeout: int) -> str:
            calls.append(url)
            return list_html if url == list_url else detail_html

        with self.assertRaisesRegex(ScrapeError, "only found 1/10"):
            duplicate_service.scrape_site_window(config, 225, 10, 10, 5, fetch)
        self.assertEqual([list_url, detail_url], calls)

    def test_matching_and_duplicate_group_branches(self) -> None:
        left = _plain_window([225, 224, 223, 221])
        same = _plain_window([225, 224, 223, 221], name="相同", url="https://example.test/same")
        mismatch = _plain_window([225, 224, 223, 221], name="不同", url="https://example.test/diff", numbers=_numbers(10))
        self.assertEqual((), duplicate_service.matching_consecutive_issues(left, mismatch, 5))
        self.assertEqual((225, 224, 223), duplicate_service.matching_consecutive_issues(left, same, 2))
        gap = _plain_window([225, 223, 221], name="间隔", url="https://example.test/gap")
        self.assertEqual((), duplicate_service.matching_consecutive_issues(left, gap, 2))

        groups, matches = duplicate_service.duplicate_groups_and_matches([], 2)
        self.assertEqual(([], []), (groups, matches))
        groups, matches = duplicate_service.duplicate_groups_and_matches(
            [left, same, _plain_window([225, 224, 223, 221], name="第三", url="https://example.test/third")],
            2,
            duplicate_common=3,
        )
        self.assertEqual(3, len(matches))
        self.assertEqual(1, len(groups))


class ParserRegistryCoverageTests(unittest.TestCase):
    def test_resolve_parser_identity_paths(self) -> None:
        base = replace(_config(), parser_id="legacy")
        self.assertEqual("explicit", resolve_parser_id(replace(base, parser_id="explicit")))
        self.assertEqual(
            "kunnan_magazine",
            resolve_parser_id(replace(base, source_type="dynamic_collection")),
        )
        self.assertEqual(
            "kunnan_magazine",
            resolve_parser_id(
                replace(base, api_url="https://api.example/users/3792/forums")
            ),
        )
        self.assertEqual(
            "renjianrenai",
            resolve_parser_id(replace(base, record_id="6a0445a74ea5c20141013e81")),
        )
        self.assertEqual(
            "three_rows",
            resolve_parser_id(
                replace(base, url="https://slyaoiw577.772149.shop/bbs/topic.php?id=1050")
            ),
        )
        for url, expected in (
            ("https://host/topic/547563.html", "fenfatuqiang"),
            ("https://ocnrhq.du156-vb27w-tmhsed.xyz:16677/x", "xueqiu"),
            ("https://host/topic/238933.html", "yiyiba"),
            ("https://host/topic/459501.html", "xiongchumo"),
        ):
            with self.subTest(url=url):
                self.assertEqual(expected, resolve_parser_id(replace(base, url=url)))
        self.assertEqual(
            "baoma_xuanji",
            resolve_parser_id(replace(base, section_keywords=("综合特码",))),
        )
        self.assertEqual(
            "xiaoyuer",
            resolve_parser_id(replace(base, section_keywords=("澳门精准36码",))),
        )
        lok_url = "https://lokzmlcf.0jbc9-wavec-csoybc.xyz:16677/x"
        self.assertEqual(
            "zhuchiren_weite",
            resolve_parser_id(
                replace(base, url=lok_url, keywords=("第",), section_keywords=("36码围特",))
            ),
        )
        self.assertEqual(
            "zhuchiren_baote",
            resolve_parser_id(
                replace(base, url=lok_url, keywords=("36码爆特",), section_keywords=("36码爆特",))
            ),
        )
        self.assertEqual("generic_36", resolve_parser_id(base))

    def test_registry_registration_lookup_and_collector_contracts(self) -> None:
        registry = ParserRegistry()
        with self.assertRaisesRegex(ConfigurationError, "parser_id不能为空"):
            registry.register(" ", lambda _text, site: _plain_record(126, name=site.name))

        evidence = _candidate(126, 2)

        def parser(_text: str, site: SiteConfig) -> ParsedRecord:
            return ParsedRecord(
                site.name,
                site.url,
                evidence.issue,
                evidence.numbers,
                raw_position=evidence.page_index,
                evidence=evidence,
            )

        registry.register("parser", parser)
        with self.assertRaisesRegex(ConfigurationError, "parser_id重复注册"):
            registry.register("parser", parser)
        self.assertEqual(126, registry.parse("text", replace(_config(), parser_id="parser")).issue)
        with self.assertRaisesRegex(ConfigurationError, "未注册parser_id"):
            registry.get("missing")

        def collector(_text: str, _config: SiteConfig):
            return [evidence], [], {126}

        registry.register("collector", parser, collector)
        self.assertTrue(registry.has_candidate_collector("collector"))
        self.assertFalse(registry.has_candidate_collector("parser"))
        self.assertEqual(("collector", "parser"), registry.parser_ids)
        wrapped = registry.candidate_collector("collector")
        self.assertEqual([evidence], wrapped("text", replace(_config(), parser_id="collector"))[0])
        with self.assertRaisesRegex(ConfigurationError, "未注册多期候选解析器"):
            registry.candidate_collector("missing")

        bad = ParserRegistry()
        bad.register("bad", parser, lambda _text, _config: ([("old",)], [], set()))
        with self.assertRaisesRegex(ScrapeError, "旧三元组"):
            bad.candidate_collector("bad")("text", replace(_config(), parser_id="bad"))

    def test_extract_from_candidates_all_empty_and_range_paths(self) -> None:
        config = _config(fixed_issue=126)
        invalid = [(126, 2, "数量不足")]
        with self.assertRaisesRegex(ScrapeError, "126期数据无效"):
            extract_from_candidates(
                "text",
                config,
                lambda _text, _config: ([], invalid, {126}),
                no_candidates_message="没有候选",
                issue_range_error="没有范围",
            )
        with self.assertRaisesRegex(ScrapeError, "126期没有找到完整36码"):
            extract_from_candidates(
                "text",
                config,
                lambda _text, _config: ([], [], {126}),
                no_candidates_message="没有候选",
                issue_range_error="没有范围",
            )
        with self.assertRaisesRegex(ScrapeError, "页面可命中的期数"):
            extract_from_candidates(
                "text",
                config,
                lambda _text, _config: ([], [], {125, 124}),
                no_candidates_message="没有候选",
                issue_range_error="没有范围",
            )
        with self.assertRaisesRegex(ScrapeError, "没有候选"):
            extract_from_candidates(
                "text",
                replace(config, fixed_issue=None),
                lambda _text, _config: ([], [], set()),
                no_candidates_message="没有候选",
                issue_range_error="没有范围",
                include_seen_issue_list=False,
            )
        with self.assertRaisesRegex(ScrapeError, "126期数据无效"):
            extract_from_candidates(
                "text",
                replace(config, fixed_issue=None),
                lambda _text, _config: ([], [(126, 1, "坏号码")], set()),
                no_candidates_message="没有候选",
                issue_range_error="没有范围",
            )
        with self.assertRaisesRegex(ScrapeError, "没有候选"):
            extract_from_candidates(
                "text",
                replace(config, fixed_issue=None),
                lambda _text, _config: ([], [(126, 1, "坏号码")], set()),
                no_candidates_message="没有候选",
                issue_range_error="没有范围",
                include_invalid_candidates=False,
            )

        in_range = _candidate(126, 0)
        parsed = extract_from_candidates(
            "text",
            replace(config, fixed_issue=None),
            lambda _text, _config: ([in_range], [], {126}),
            no_candidates_message="没有候选",
            issue_range_error="没有范围",
        )
        self.assertEqual(126, parsed.issue)
        with self.assertRaisesRegex(ScrapeError, "没有范围"):
            extract_from_candidates(
                "text",
                replace(config, fixed_issue=None),
                lambda _text, _config: ([_candidate(125, 0)], [], {125}),
                no_candidates_message="没有候选",
                issue_range_error="没有范围",
            )

    def test_extract_from_candidates_fixed_empty_selection_branches(self) -> None:
        candidate = _candidate(126, 0)
        config = _config(fixed_issue=126)
        with (
            patch("dawei.parsers.generic_36.exact_issue_candidates_for_selection", return_value=[]),
            self.assertRaisesRegex(ScrapeError, "可用有效期数"),
        ):
            extract_from_candidates(
                "text",
                config,
                lambda _text, _config: ([candidate], [], {126}),
                no_candidates_message="没有候选",
                issue_range_error="没有范围",
            )


class Generic36CoverageTests(unittest.TestCase):
    def test_selection_and_diagnostic_helpers_cover_direction_paths(self) -> None:
        top = replace(_config(region="top"), parser_id="generic_36")
        bottom = replace(_config(region="bottom"), parser_id="generic_36")
        self.assertEqual("top", generic_36.candidate_region(top))
        self.assertEqual("top", generic_36.candidate_region(replace(top, region="顶部")))
        self.assertEqual("bottom", generic_36.candidate_region(replace(bottom, region="tail")))
        with self.assertRaisesRegex(ScrapeError, "未知候选区域"):
            generic_36.candidate_region(replace(top, region="side"))

        values = _numbers()
        inline = f"225期 【{' '.join(values)}】"
        self.assertEqual(values, generic_36.collect_candidate_numbers_for_diagnostics([inline], 0, top)[0])
        before_lines = [
            " ".join(values[:12]),
            " ".join(values[12:24]),
            " ".join(values[24:]),
            "225期",
        ]
        before = replace(top, numbers_before_issue=True)
        self.assertEqual(values, generic_36.collect_candidate_numbers_for_diagnostics(before_lines, 3, before)[0])
        zero_lines = ["225期", "00 " + " ".join(values)]
        drop_zero = replace(top, drop_zero_numbers=True)
        self.assertEqual(values, generic_36.collect_candidate_numbers_for_diagnostics(zero_lines, 0, drop_zero)[0])
        after_lines = ["225期", " ".join(values)]
        self.assertEqual(values, generic_36.collect_candidate_numbers_for_diagnostics(after_lines, 0, top)[0])
        self.assertEqual("带号码原因", generic_36.best_invalid_candidate_reason([(1, 1, "没有收集到"), (1, 2, "带号码原因")]))
        self.assertEqual("没有收集到", generic_36.best_invalid_candidate_reason([(1, 1, "没有收集到")]))

        context_lines = ["栏目", "225期", "附加文字", "DAWEI_DOCUMENT_BOUNDARY", "下一期"]
        context = generic_36.candidate_context(
            context_lines,
            1,
            top,
            block_start=0,
            block_end=4,
        )
        self.assertIn("栏目", context)
        self.assertNotIn("下一期", context)
        self.assertIn("225期", generic_36.candidate_context(context_lines, 1))
        self.assertTrue(generic_36.common_document_boundary("DAWEI_DOCUMENT_BOUNDARY"))
        result = ParsedRecord("站", "url", 225, values)
        text = "栏目\n225期\n" + " ".join(values) + "\n225期\n" + " ".join(values)
        self.assertEqual(1, generic_36.result_raw_position(text, result, top))
        self.assertEqual(3, generic_36.result_raw_position(text, result, bottom))
        self.assertIsNone(generic_36.result_raw_position("没有期数", result, top))

        article = ArticleRecord("record-1", "article-path", "225期", "作者", "正文", "正文")
        with_evidence = _parsed(_candidate(225, 1))
        attached = generic_36.attach_article_identity(with_evidence, article, "正文", top)
        self.assertEqual("record-1", attached.record_id)
        without_evidence = ParsedRecord("站", "url", 225, values)
        attached_without = generic_36.attach_article_identity(without_evidence, article, "225期", top)
        self.assertEqual(0, attached_without.raw_position)
        self.assertTrue(generic_36.issue_in_range(126, 126, 126))
        self.assertFalse(generic_36.issue_in_range(125, 126, 126))

    def test_candidate_selection_window_and_document_contracts(self) -> None:
        values = [_candidate(225, 7), _candidate(225, 2)]
        self.assertEqual(2, generic_36.select_candidate_for_position(values, _config(region="top")).page_index)
        self.assertEqual(7, generic_36.select_candidate_for_position(values, _config(region="bottom")).page_index)
        with self.assertRaisesRegex(ScrapeError, "没有可选"):
            generic_36.select_candidate_for_position([], _config())
        with self.assertRaisesRegex(ScrapeError, "旧三元组"):
            generic_36.select_candidate_for_position([("old",)], _config())
        with self.assertRaisesRegex(ScrapeError, "未知候选区域"):
            generic_36.select_candidate_for_position(values, replace(_config(), region="side"))
        with self.assertRaisesRegex(ScrapeError, "未知候选区域"):
            generic_36.prefer_candidate_index(1, 2, replace(_config(), region="side"))
        self.assertTrue(generic_36.prefer_candidate_index(1, 2, _config(region="top")))
        self.assertTrue(generic_36.prefer_candidate_index(2, 1, _config(region="bottom")))
        parsed = generic_36.parsed_record_from_candidate(_config(), values[0])
        self.assertEqual(values[0].issue, parsed.issue)

        with self.assertRaisesRegex(ScrapeError, "多个文档"):
            generic_36.unique_candidates(
                [_candidate(225, 0, document_id="a"), _candidate(224, 1, document_id="b")],
                _config(),
            )
        merged = generic_36.unique_candidates(values, _config(region="bottom"))
        self.assertEqual(1, len(merged))
        simple = [_candidate(225 - index, index) for index in range(4)]
        self.assertEqual(4, len(generic_36.latest_candidate_window(simple, _config(region="top"))))
        strict = [_candidate(225 - index, index) for index in range(31)]
        self.assertEqual(5, len(generic_36.latest_candidate_window(strict, _config(region="top"))))
        self.assertEqual(5, len(generic_36.latest_candidate_window(strict, _config(region="bottom"))))
        self.assertFalse(generic_36._requires_strict_candidate_window(simple))
        self.assertTrue(generic_36._requires_strict_candidate_window(strict))
        self.assertTrue(generic_36._requires_strict_candidate_window([values[0], values[0]]))

        exact = generic_36.exact_issue_candidates_for_selection(
            [_candidate(225, 4, document_id="a"), _candidate(225, 1, document_id="b")],
            225,
            _config(region="top"),
        )
        self.assertEqual(1, len(exact))
        latest = generic_36.select_latest_issue_candidate(simple, _config(region="top"))
        self.assertEqual(225, latest.issue)
        generic_36.assert_no_conflicting_exact_issue_candidates(simple, 999, _config())
        with self.assertRaisesRegex(ScrapeError, "多个高可信候选"):
            generic_36.assert_no_conflicting_exact_issue_candidates(
                [_candidate(225, 0, numbers=_numbers(1)), _candidate(225, 1, numbers=_numbers(2))],
                225,
                _config(),
            )

    def test_number_line_and_candidate_evidence_boundaries(self) -> None:
        values = _numbers()
        config = replace(_config(), parser_id="generic_36")
        inline_lines = [f"225期 【{' '.join(values)}】"]
        self.assertEqual((inline_lines[0],), generic_36.candidate_number_lines(inline_lines, 0, config, block_start=0, block_end=1))
        rows = [" ".join(values[:12]), " ".join(values[12:24]), " ".join(values[24:]), "225期"]
        before = replace(config, numbers_before_issue=True)
        self.assertEqual(tuple(rows[:3]), generic_36.candidate_number_lines(rows, 3, before, block_start=0, block_end=4))
        after = ["225期", " ".join(values[:12]), " ".join(values[12:24]), " ".join(values[24:]), "广告"]
        self.assertEqual(tuple(after[1:4]), generic_36.candidate_number_lines(after, 0, config, block_start=0, block_end=5))
        drop_zero = replace(config, drop_zero_numbers=True)
        self.assertEqual(tuple(after[1:4]), generic_36.candidate_number_lines(after, 0, drop_zero, block_start=0, block_end=5))
        self.assertEqual(values, generic_36.candidate_evidence(rows, 3, 225, values, before, range(4)).numbers)
        no_section = replace(config, section_keywords=())
        self.assertEqual("225期", generic_36.candidate_evidence(["225期"], 0, 225, values, no_section, range(1), raw_number_lines=("号码",)).anchor_line)
        with self.assertRaisesRegex(ScrapeError, "跨文档"):
            generic_36.candidate_evidence(
                ["DAWEI_DOCUMENT_BOUNDARY", "225期"],
                1,
                225,
                values,
                config,
                range(2),
            )
        with self.assertRaisesRegex(ScrapeError, "区块边界"):
            generic_36.candidate_evidence(
                ["225期"],
                0,
                225,
                values,
                config,
                range(0),
            )

    def test_generic_candidates_and_extract_errors(self) -> None:
        values = _numbers()
        config = replace(
            _config(fixed_issue=225),
            parser_id="generic_36",
            keywords=("专属",),
            section_keywords=("栏目",),
        )
        text = "栏目 专属\n225期\n" + " ".join(values)
        candidates, invalid, seen = generic_36.generic_candidates(text, config)
        self.assertEqual(1, len(candidates))
        self.assertEqual([], invalid)
        self.assertEqual({225}, seen)
        self.assertEqual(225, generic_36.extract_generic_36(text, config).issue)
        with self.assertRaisesRegex(ScrapeError, "未找到栏目"):
            generic_36.generic_candidates("225期\n" + " ".join(values), config)
        with self.assertRaisesRegex(ScrapeError, "没有找到符合关键词"):
            generic_36.extract_generic_36("无关内容", replace(config, section_keywords=()))
        invalid_text = "栏目 专属\n225期\n01 02"
        with self.assertRaisesRegex(ScrapeError, "225期数据无效"):
            generic_36.extract_generic_36(invalid_text, config)
        with self.assertRaisesRegex(ScrapeError, "没有找到符合关键词"):
            generic_36.extract_generic_36("栏目 专属\n没有期数", replace(config, fixed_issue=None))
        out_of_range = replace(config, fixed_issue=None)
        with self.assertRaisesRegex(ScrapeError, "issue range"):
            generic_36.extract_generic_36("栏目 专属\n225期\n" + " ".join(values), out_of_range)

    def test_fixed_issue_crosses_decorative_separator_within_same_section(self) -> None:
        values = _numbers()
        text = "\n".join(
            (
                "237期【目标栏目】【专属】",
                "237期 专属",
                " ".join(_numbers(1)),
                "================",
                "236期 专属",
                " ".join(values),
                "================",
            )
        )

        result = generic_36.extract_generic_36(text, _config(fixed_issue=236))

        self.assertEqual(result.issue, 236)
        self.assertEqual(result.numbers, values)
        candidates, _, _ = generic_36.generic_candidates(text, _config())
        self.assertEqual([candidate.issue for candidate in candidates], [237])

    def test_generic_candidates_special_collection_modes(self) -> None:
        values = _numbers()
        base = replace(_config(), parser_id="generic_36", keywords=("专属",), section_keywords=("栏目",))
        before_text = "栏目 专属\n" + " ".join(values[:12]) + "\n" + " ".join(values[12:24]) + "\n" + " ".join(values[24:]) + "\n225期"
        before = replace(base, numbers_before_issue=True)
        candidates, _, _ = generic_36.generic_candidates(before_text, before)
        self.assertEqual(1, len(candidates))
        zero_text = "栏目 专属\n225期\n00 " + " ".join(values)
        drop_zero = replace(base, drop_zero_numbers=True)
        candidates, _, _ = generic_36.generic_candidates(zero_text, drop_zero)
        self.assertEqual(1, len(candidates))
        foreign = replace(base, keywords=("专属36码",), section_keywords=("栏目",))
        self.assertEqual([], generic_36.generic_candidates("栏目\n225期 其他36码\n" + " ".join(values), foreign)[0])

        partial = "00 " + " ".join(values[:18])
        boundary_text = (
            "栏目 专属\n225期\n"
            + partial
            + "\nDAWEI_DOCUMENT_BOUNDARY\n栏目 专属\n224期\n00 "
            + " ".join(values)
        )
        candidates, invalid, _ = generic_36.generic_candidates(boundary_text, drop_zero)
        self.assertEqual([224], [candidate.issue for candidate in candidates])
        self.assertTrue(any(issue == 225 for issue, _, _ in invalid))


class CandidateWindowRulesTests(unittest.TestCase):
    def test_non_strict_direction_can_select_target_outside_old_three_group_window(self) -> None:
        candidates = [_candidate(225 - index, index) for index in range(9)]
        config = _config(region="top", fixed_issue=217)

        selected = generic_36.exact_issue_candidates_for_selection(
            candidates,
            config.fixed_issue,
            config,
        )

        self.assertEqual([217], [candidate.issue for candidate in selected])

    def test_non_strict_missing_issue_reports_ordinary_missing_issue(self) -> None:
        candidates = [_candidate(225 - index, index) for index in range(5)]
        config = _config(region="top", fixed_issue=219)

        with self.assertRaisesRegex(ScrapeError, "未找到指定219期") as raised:
            generic_36.exact_issue_candidates_for_selection(
                candidates,
                config.fixed_issue,
                config,
            )

        self.assertNotIn("严格候选范围", str(raised.exception))

    def test_strict_window_is_five_groups_when_one_document_has_more_than_thirty(self) -> None:
        candidates = [_candidate(300 - index, index) for index in range(31)]
        config = _config(region="top", fixed_issue=270)

        with self.assertRaisesRegex(ScrapeError, "最近5组"):
            generic_36.exact_issue_candidates_for_selection(
                candidates,
                config.fixed_issue,
                config,
            )

        config = _config(region="top", fixed_issue=300)
        selected = generic_36.exact_issue_candidates_for_selection(
            candidates,
            config.fixed_issue,
            config,
        )
        self.assertEqual([300], [candidate.issue for candidate in selected])

        config = _config(region="bottom", fixed_issue=300)
        with self.assertRaisesRegex(ScrapeError, "最近5组"):
            generic_36.exact_issue_candidates_for_selection(
                candidates,
                config.fixed_issue,
                config,
            )
        config = _config(region="bottom", fixed_issue=270)
        selected = generic_36.exact_issue_candidates_for_selection(
            candidates,
            config.fixed_issue,
            config,
        )
        self.assertEqual([270], [candidate.issue for candidate in selected])

    def test_fixed_target_in_one_document_is_not_mixed_with_other_documents(self) -> None:
        candidates = [
            _candidate(225 - index, index, document_id=f"document-{index // 30}")
            for index in range(60)
        ]
        config = _config(region="top", fixed_issue=166)

        selected = generic_36.exact_issue_candidates_for_selection(
            candidates,
            config.fixed_issue,
            config,
        )
        self.assertEqual([166], [candidate.issue for candidate in selected])

    def test_same_issue_same_numbers_still_triggers_strict_window_before_deduplication(self) -> None:
        same = _numbers(7)
        candidates = [
            _candidate(219, 0, numbers=_numbers(8)),
            _candidate(225, 1, numbers=same),
            _candidate(225, 2, numbers=same),
            *[_candidate(224 - index, index + 3) for index in range(5)],
        ]
        config = _config(region="bottom", fixed_issue=219)

        with self.assertRaisesRegex(ScrapeError, "最近5组"):
            generic_36.exact_issue_candidates_for_selection(
                candidates,
                config.fixed_issue,
                config,
            )

    def test_fixed_target_in_second_document_is_not_combined_with_large_document(self) -> None:
        candidates = [_candidate(300 - index, index) for index in range(31)]
        candidates.append(_candidate(200, 31, document_id="document-1"))
        config = _config(region="bottom", fixed_issue=200)

        selected = generic_36.exact_issue_candidates_for_selection(
            candidates,
            config.fixed_issue,
            config,
        )
        self.assertEqual([200], [candidate.issue for candidate in selected])

    def test_fixed_target_absent_from_multiple_documents_fails_closed(self) -> None:
        candidates = [
            _candidate(225, 0, document_id="document-a"),
            _candidate(224, 0, document_id="document-b"),
        ]

        with self.assertRaisesRegex(ScrapeError, "多个文档.*权威"):
            generic_36.exact_issue_candidates_for_selection(
                candidates,
                223,
                _config(region="top", fixed_issue=223),
            )

    def test_generic_fixed_issue_deduplicates_same_numbers_across_documents(self) -> None:
        numbers = _numbers(11)
        candidates = [
            _candidate(205, 90, document_id="document-a", numbers=numbers),
            _candidate(205, 0, document_id="document-b", numbers=numbers),
        ]

        selected = generic_36.exact_issue_candidates_for_selection(
            candidates,
            205,
            _config(region="top", fixed_issue=205),
        )

        self.assertEqual(1, len(selected))
        self.assertEqual(205, selected[0].issue)
        self.assertEqual(numbers, selected[0].numbers)

    def test_generic_fixed_issue_rejects_different_numbers_across_documents(self) -> None:
        candidates = [
            _candidate(205, 90, document_id="document-a", numbers=_numbers(12)),
            _candidate(205, 0, document_id="document-b", numbers=_numbers(13)),
        ]

        with self.assertRaisesRegex(ScrapeError, "205期存在多个高可信候选"):
            generic_36.exact_issue_candidates_for_selection(
                candidates,
                205,
                _config(region="top", fixed_issue=205),
            )

    def test_repeated_issue_enables_strict_window_but_conflict_is_checked_before_it(self) -> None:
        same = _numbers(4)
        candidates = [_candidate(225, 0, numbers=same), _candidate(225, 1, numbers=same)]
        candidates.extend(_candidate(224 - index, index + 2) for index in range(5))
        config = _config(region="bottom", fixed_issue=219)

        with self.assertRaisesRegex(ScrapeError, "最近5组"):
            generic_36.exact_issue_candidates_for_selection(
                candidates,
                config.fixed_issue,
                config,
            )

        conflict = [
            _candidate(225, 0, numbers=_numbers(1)),
            _candidate(225, 1, numbers=_numbers(2)),
            *[_candidate(224 - index, index + 2) for index in range(30)],
        ]
        with self.assertRaisesRegex(ScrapeError, "225期存在多个高可信候选"):
            generic_36.exact_issue_candidates_for_selection(
                conflict,
                _config(region="bottom", fixed_issue=225).fixed_issue,
                _config(region="bottom", fixed_issue=225),
            )


class CandidateOnboardingRulesTests(unittest.TestCase):
    def test_url_identity_keeps_distinct_fragments_separate(self) -> None:
        configured = replace(
            _config(),
            name="已有栏目站",
            url="https://example.test/topic#xw",
            site_id="site_existing_fragment_a",
        )
        candidate = replace(
            _config(),
            name="候选用户站",
            url="https://example.test/topic#/users/3792",
            site_id="site_candidate_fragment_b",
        )

        conflicts = candidate_identity_conflicts((candidate,), (configured,), ())

        self.assertFalse(any("同URL候选站" in conflict for conflict in conflicts))

    def test_url_identity_matches_same_fragment(self) -> None:
        configured = replace(
            _config(),
            name="已有栏目站",
            url="https://example.test/topic#xw",
            site_id="site_existing_fragment",
        )
        candidate = replace(
            _config(),
            name="候选栏目站",
            url="https://example.test/topic#xw",
            site_id="site_candidate_fragment",
        )

        conflicts = candidate_identity_conflicts((candidate,), (configured,), ())

        self.assertTrue(any("同URL候选站" in conflict for conflict in conflicts))

    def test_url_identity_matches_query_order_variants(self) -> None:
        configured = replace(
            _config(),
            name="已有查询站",
            url="https://example.test/topic?a=1&b=2#xw",
            site_id="site_existing_query",
        )
        candidate = replace(
            _config(),
            name="候选查询站",
            url="https://example.test/topic?b=2&a=1#xw",
            site_id="site_candidate_query",
        )

        conflicts = candidate_identity_conflicts((candidate,), (configured,), ())

        self.assertTrue(any("同URL候选站" in conflict for conflict in conflicts))

    def test_name_identity_applies_nfkc(self) -> None:
        configured = replace(
            _config(),
            name="全角Ａ站",
            url="https://example.test/topic/a",
            site_id="site_existing_name",
        )
        candidate = replace(
            _config(),
            name="全角A站",
            url="https://example.test/topic/b",
            site_id="site_candidate_name",
        )

        conflicts = candidate_identity_conflicts((candidate,), (configured,), ())

        self.assertTrue(any("同名候选站" in conflict for conflict in conflicts))

    def test_record_id_identity_is_not_scoped_to_hostname(self) -> None:
        configured = SiteConfig(
            name="已有站",
            url="https://mirror-a.example/article/manager/abc123",
            record_id="abc123",
            site_id="site_existing",
            parser_id="generic_36",
            region="top",
        )
        candidate = SiteConfig(
            name="候选镜像",
            url="https://mirror-b.example/article/manager/abc123",
            record_id="abc123",
            site_id="site_candidate_new",
            parser_id="generic_36",
            region="top",
        )

        conflicts = candidate_identity_conflicts((candidate,), (configured,), ())

        self.assertTrue(any("record_id" in conflict for conflict in conflicts))

    def test_incomplete_backup_exception_never_allows_candidate(self) -> None:
        config = _config(exception="allow_incomplete_backup")
        backup = BackupSnapshot(
            period=225,
            periods=10,
            sites=(),
            failures=("其他站失败",),
            incomplete=True,
        )
        with self.assertRaisesRegex(ScrapeError, "缓存不完整"):
            OnboardingService().validate(
                candidates=(config,),
                configured=(),
                backup=backup,
                windows=(),
                matches=(),
            )

    def test_insufficient_history_exception_never_bypasses_ten_period_gate(self) -> None:
        config = _config(exception="allow_insufficient_history")
        records = [_parsed(_candidate(225, 0)), _parsed(_candidate(224, 1))]
        with self.assertRaisesRegex(ScrapeError, "正好10条窗口记录"):
            validate_candidate_window(
                _window(config, records),
                backup_period=225,
                periods=10,
                config=config,
            )

    def test_candidate_window_rejects_same_issue_conflict_before_filtering(self) -> None:
        config = _config(region="bottom")
        records = [_parsed(_candidate(issue, index)) for index, issue in enumerate(range(216, 226))]
        records.append(_parsed(_candidate(225, 10, numbers=_numbers(2))))

        with self.assertRaisesRegex(ScrapeError, "225期存在同期36码冲突"):
            validate_candidate_window(
                _window(config, records),
                backup_period=225,
                periods=10,
                config=config,
            )

    def test_kunnan_candidate_window_uses_explicit_issues_across_documents(self) -> None:
        config = replace(
            _config(region="bottom"),
            source_type="dynamic_collection",
            api_url="https://api.example/users/3792/forums",
            parser_id="kunnan_magazine",
        )
        records = [
            _parsed(_candidate(issue, index, document_id=f"record-{index}"))
            for index, issue in enumerate(range(216, 226))
        ]

        validate_candidate_window(
            _window(config, records),
            backup_period=225,
            periods=10,
            config=config,
        )


class OnboardingServiceCoverageTests(unittest.TestCase):
    def test_identity_helpers_cover_empty_query_route_and_window_records(self) -> None:
        record_window = SiteWindow(
            "站点",
            "https://example.test/topic",
            225,
            1,
            (_plain_record(225, record_id="Record-A"),),
        )
        self.assertEqual(("record-a",), record_identities(record_window))
        self.assertEqual("", normalized_url(None))
        self.assertEqual("host|/topic.php|1", topic_identity("https://host/topic.php?id=1"))
        self.assertEqual("host|/read.php|2", topic_identity("https://host/read.php?tid=2"))
        self.assertEqual("host|/topic/item.html|item", topic_identity("https://host/topic/item.html"))
        self.assertEqual("", topic_identity("https://host/other"))
        self.assertEqual(("候选站", "https://example.test/topic"), site_key(_config()))

    def test_identity_conflicts_cover_api_site_id_record_and_topic(self) -> None:
        existing = SiteConfig(
            name="同名站",
            url="https://host/topic.php?id=42",
            api_url="https://host/api/42",
            record_id="record-42",
            site_id="site-42",
            parser_id="generic_36",
            region="top",
        )
        candidate = replace(
            existing,
            name="候选站",
            site_id="site-42",
        )
        conflicts = candidate_identity_conflicts((candidate,), (existing,), ())
        self.assertTrue(any("同URL" in item for item in conflicts))
        self.assertTrue(any("同api_url" in item for item in conflicts))
        self.assertTrue(any("同site_id" in item for item in conflicts))
        self.assertTrue(any("同record_id" in item for item in conflicts))
        self.assertTrue(any("同topic" in item for item in conflicts))

    def test_validate_candidate_window_rejects_config_and_evidence_gates(self) -> None:
        base = _config()
        cases = (
            (replace(base, keywords=()), "非空专属数据关键词"),
            (replace(base, section_keywords=()), "非空专属栏目关键词"),
            (replace(base, parser_id="legacy"), "显式配置"),
            (replace(base, parser_id="three_rows"), "证据解析器不匹配"),
        )
        records = [_parsed(_candidate(issue, index)) for index, issue in enumerate(range(216, 226))]
        for config, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ScrapeError, message):
                validate_candidate_window(
                    _window(config, records),
                    backup_period=225,
                    periods=10,
                    config=config,
                )

        missing_window = _window(base, [_parsed(_candidate(200, 0))])
        with self.assertRaisesRegex(ScrapeError, "正好10条窗口记录"):
            validate_candidate_window(missing_window, 225, 10, config=base)

        bad_anchor = _candidate(225, 0)
        origin = replace(bad_anchor.origins[0], anchor_line="其他栏目")
        bad_anchor = replace(bad_anchor, anchor_line="其他栏目", origins=(origin,))
        bad_anchor_records = [_parsed(_candidate(issue, index)) for index, issue in enumerate(range(216, 226))]
        bad_anchor_records[-1] = _parsed(bad_anchor)
        with self.assertRaisesRegex(ScrapeError, "栏目锚点"):
            validate_candidate_window(
                _window(base, bad_anchor_records),
                225,
                10,
                config=base,
            )

        keyword_config = replace(base, keywords=("不存在关键词",))
        keyword_records = [_parsed(_candidate(issue, index)) for index, issue in enumerate(range(216, 226))]
        with self.assertRaisesRegex(ScrapeError, "未绑定专属数据关键词"):
            validate_candidate_window(
                _window(keyword_config, keyword_records),
                225,
                10,
                config=keyword_config,
            )

    def test_validate_candidate_window_direction_reference_and_data_gates(self) -> None:
        config = _config(region="top")
        records = [_parsed(_candidate(issue, index)) for index, issue in enumerate(range(216, 226))]
        direction_candidates = tuple(
            _candidate(issue, index) for index, issue in enumerate(range(195, 226))
        )
        with self.assertRaisesRegex(ScrapeError, "方向候选范围"):
            validate_candidate_window(
                replace(_window(config, records), direction_candidates=direction_candidates),
                backup_period=225,
                periods=10,
                config=config,
            )

        old_records = [_parsed(_candidate(issue, index)) for index, issue in enumerate(range(216, 224))]
        collection_config = replace(
            config,
            source_type="dynamic_collection",
            api_url="https://api.example/users/3792/forums",
            parser_id="kunnan_magazine",
        )
        with self.assertRaisesRegex(ScrapeError, "正好10条窗口记录"):
            validate_candidate_window(
                _window(collection_config, old_records),
                backup_period=225,
                periods=10,
                config=collection_config,
            )

        invalid = [_parsed(_candidate(issue, index)) for index, issue in enumerate(range(216, 226))]
        invalid[0] = replace(invalid[0], numbers=("01",) * 36)
        with patch.object(
            onboarding_service,
            "validate_candidate_evidence",
            return_value=invalid[0].evidence,
        ), self.assertRaisesRegex(ScrapeError, "不是有效36码"):
            validate_candidate_window(
                _window(config, invalid),
                225,
                10,
                config=config,
            )

    def test_onboarding_service_window_and_match_outcomes(self) -> None:
        config = _config()
        backup = BackupSnapshot(225, 1, (), (), False)
        service = OnboardingService()
        with self.assertRaisesRegex(ScrapeError, "候选新站为空"):
            service.validate(candidates=(), configured=(), backup=backup, windows=(), matches=())

        records = [_parsed(_candidate(225, 0))]
        window = _window(config, records)
        other = _plain_window([225], name="其他站", url="https://example.test/other")
        match = duplicate_service.DuplicateMatch(window, other, (225,))
        self.assertEqual([match], matches_involving_sites((match,), (config,)))
        self.assertEqual(
            [],
            matches_involving_sites((match,), (replace(config, name="不相关站"),)),
        )

        with self.assertRaisesRegex(ScrapeError, "唯一对应"):
            service.validate(
                candidates=(config,),
                configured=(),
                backup=BackupSnapshot(225, 10, ()),
                windows=(),
                matches=(),
            )
        with self.assertRaisesRegex(ScrapeError, "唯一对应"):
            service.validate(
                candidates=(config,),
                configured=(),
                backup=BackupSnapshot(225, 10, ()),
                windows=(window, window),
                matches=(),
            )

        full_records = [_parsed(_candidate(issue, index)) for index, issue in enumerate(range(216, 226))]
        valid_window = _window(config, full_records)
        with self.assertRaisesRegex(ScrapeError, "重复"):
            service.validate(
                candidates=(config,),
                configured=(),
                backup=BackupSnapshot(225, 10, ()),
                windows=(valid_window,),
                matches=(match,),
            )

    def test_onboarding_requires_exact_ten_period_cache_and_candidate_window(self) -> None:
        config = _config()
        service = OnboardingService()
        full_records = [
            _parsed(_candidate(issue, index))
            for index, issue in enumerate(range(216, 226))
        ]
        valid_window = _window(config, full_records)

        for cached_periods in (1, 9, 11):
            with self.subTest(cached_periods=cached_periods), self.assertRaisesRegex(
                ScrapeError, "正好10期完整缓存"
            ):
                service.validate(
                    candidates=(config,),
                    configured=(),
                    backup=BackupSnapshot(225, cached_periods, ()),
                    windows=(valid_window,),
                    matches=(),
                )

        with self.assertRaisesRegex(ScrapeError, "缓存基准期数无效"):
            service.validate(
                candidates=(config,),
                configured=(),
                backup=BackupSnapshot(0, 10, ()),
                windows=(valid_window,),
                matches=(),
            )

        for records in (full_records[:-1], [*full_records, _parsed(_candidate(226, 10))]):
            with self.subTest(record_count=len(records)), self.assertRaisesRegex(
                ScrapeError, "正好10"
            ):
                service.validate(
                    candidates=(config,),
                    configured=(),
                    backup=BackupSnapshot(225, 10, ()),
                    windows=(_window(config, records),),
                    matches=(),
                )

        self.assertEqual(
            (),
            service.validate(
                candidates=(config,),
                configured=(),
                backup=BackupSnapshot(225, 10, ()),
                windows=(valid_window,),
                matches=(),
            ),
        )

    def test_duplicate_runner_rejects_non_ten_candidate_periods_before_scraping(self) -> None:
        scraper = Mock()
        for requested_periods in (1, 9, 11):
            with self.subTest(requested_periods=requested_periods), self.assertRaisesRegex(
                ScrapeError, "正好10期窗口"
            ):
                DuplicateRunner(window_scraper=scraper).run(
                    DuplicateOptions(
                        sites_config=Path("missing-sites.json"),
                        backup_path=Path("missing-cache.json"),
                        periods=requested_periods,
                        min_common=1,
                        duplicate_common=1,
                        candidate_sites=("candidate.json",),
                        use_backup=True,
                    )
                )
        scraper.assert_not_called()

    def test_duplicate_runner_backup_identity_rejects_non_ten_periods(self) -> None:
        for cached_periods in (1, 9, 11):
            with self.subTest(cached_periods=cached_periods), self.assertRaisesRegex(
                ScrapeError, "备份缓存正好10期"
            ):
                DuplicateRunner._validate_backup_identity(
                    BackupSnapshot(225, cached_periods, ()),
                    (),
                    Path("unused-cache.json"),
                )

    def test_validate_candidate_window_is_fixed_ten_period_hard_gate(self) -> None:
        config = _config()
        full_records = [
            _parsed(_candidate(issue, index))
            for index, issue in enumerate(range(216, 226))
        ]
        valid_window = _window(config, full_records)

        for requested_periods in (1, 9, 11):
            with self.subTest(requested_periods=requested_periods), self.assertRaisesRegex(
                ScrapeError, "正好10期窗口"
            ):
                validate_candidate_window(
                    valid_window,
                    backup_period=225,
                    periods=requested_periods,
                    config=config,
                )

        with self.assertRaisesRegex(ScrapeError, "缓存基准期数无效"):
            validate_candidate_window(valid_window, 0, 10, config=config)

        for records in (full_records[:-1], [*full_records, _parsed(_candidate(226, 10))]):
            with self.subTest(record_count=len(records)), self.assertRaisesRegex(
                ScrapeError, "正好10条窗口记录"
            ):
                validate_candidate_window(_window(config, records), 225, 10, config=config)

        validate_candidate_window(valid_window, 225, 10, config=config)

class KunnanCollectionSelectionTests(unittest.TestCase):
    def _config(self) -> SiteConfig:
        return replace(
            _config(region="bottom"),
            source_type="dynamic_collection",
            api_url="https://api.example/users/3792/forums",
            parser_id="kunnan_magazine",
        )

    def _payload(self, issues: list[int]) -> str:
        numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        return json.dumps(
            [
                {
                    "user_id": "3792",
                    "status": "published",
                    "lottery": "macao",
                    "topic": "专属",
                    "id": f"record-{index}",
                    "draw": str(issue),
                    "content": f"目标栏目 专属\n{issue}期\n{numbers}",
                }
                for index, issue in enumerate(issues)
            ],
            ensure_ascii=False,
        )

    def test_fixed_issue_accepts_ten_collection_records_with_distinct_documents(self) -> None:
        config = self._config()
        service = ScrapeService(
            text_fetcher=lambda _url, _timeout: self._payload(list(range(200, 210)))
        )

        result = service.scrape(config, fixed_issue=205)

        self.assertEqual(205, result.issue)
        self.assertEqual("record-5", result.record_id)

    def test_collection_without_fixed_issue_uses_highest_explicit_issue(self) -> None:
        config = self._config()
        service = ScrapeService(
            text_fetcher=lambda _url, _timeout: self._payload([205, 209, 207])
        )

        result = service.scrape(config)

        self.assertEqual(209, result.issue)
        self.assertEqual("record-1", result.record_id)

    def test_collection_rejects_non_decimal_issue_types(self) -> None:
        config = self._config()
        numbers = " ".join(f"{number:02d}" for number in range(1, 37))
        for raw_issue in (236.9, True, -1, "", "236.9", " 236"):
            with self.subTest(raw_issue=raw_issue):
                payload = json.dumps(
                    [
                        {
                            "user_id": "3792",
                            "status": "published",
                            "lottery": "macao",
                            "topic": "专属",
                            "id": "record-invalid-issue",
                            "draw": raw_issue,
                            "content": f"目标栏目 专属\n236期\n{numbers}",
                        }
                    ],
                    ensure_ascii=False,
                )
                with self.assertRaisesRegex(ScrapeError, "缺少有效期号"):
                    dynamic_article.kunnan_magazine_records_from_payload(payload, config)

    def test_collection_latest_issue_conflict_is_not_silently_selected(self) -> None:
        numbers = _numbers(18)
        records = (
            _parsed(_candidate(209, 0, document_id="record-a", numbers=numbers)),
            _parsed(_candidate(209, 1, document_id="record-b", numbers=_numbers(19))),
            _parsed(_candidate(208, 2, document_id="record-c")),
        )
        config = self._config()
        service = ScrapeService(text_fetcher=lambda _url, _timeout: "payload")

        with (
            patch(
                "dawei.application.scrape_service.dynamic_article.kunnan_magazine_records_from_payload",
                return_value=records,
            ),
            self.assertRaisesRegex(ScrapeError, "209期存在多个高可信候选"),
        ):
            service.scrape(config)

    def test_collection_latest_issue_same_numbers_is_deduplicated(self) -> None:
        numbers = _numbers(20)
        records = (
            _parsed(_candidate(209, 90, document_id="record-z", numbers=numbers)),
            _parsed(_candidate(209, 0, document_id="record-a", numbers=numbers)),
            _parsed(_candidate(208, 1, document_id="record-c")),
        )
        config = self._config()
        service = ScrapeService(text_fetcher=lambda _url, _timeout: "payload")

        with patch(
            "dawei.application.scrape_service.dynamic_article.kunnan_magazine_records_from_payload",
            return_value=records,
        ):
            result = service.scrape(config)

        self.assertEqual(209, result.issue)
        self.assertEqual(numbers, result.numbers)
        self.assertIsNotNone(result.evidence)
        self.assertEqual("record-a", result.evidence.document_id)


if __name__ == "__main__":
    unittest.main()
