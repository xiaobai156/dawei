from dataclasses import replace
import io
import unittest
from unittest import mock

from dawei.cli import validate_failed as validator
from dawei.parsers import DEFAULT_REGISTRY
from tests.support.evidence_factory import record as evidence_record


def number_rows(numbers: tuple[str, ...]) -> str:
    return "\n".join(
        f"<div>{','.join(numbers[index:index + 12])}</div>"
        for index in range(0, len(numbers), 12)
    )


def section(issue: int, numbers: tuple[str, ...], name: str = "测试站") -> str:
    return (
        f"<h2>{name}</h2>"
        f"<div>{issue}期 三十六码</div>"
        f"{number_rows(numbers)}"
    )


class FailedSiteValidatorTests(unittest.TestCase):
    def setUp(self):
        self.config = validator.scraper.SiteConfig(
            "测试站",
            "https://example.test/topic.html",
            ("三十六码",),
            ("测试站",),
            region="bottom",
        )
        self.numbers = tuple(f"{number:02d}" for number in range(1, 37))

    def test_cross_line_record_passes_all_content_checks(self):
        diagnostics = validator.analyze_content(section(196, self.numbers), self.config, 196)

        self.assertTrue(diagnostics.target_issue_found)
        self.assertTrue(diagnostics.anchor_pass)
        self.assertTrue(diagnostics.keyword_pass)
        self.assertTrue(diagnostics.numbers_pass)
        self.assertTrue(diagnostics.direction_pass)
        self.assertFalse(diagnostics.same_issue_conflict)
        self.assertEqual(diagnostics.numbers, self.numbers)

    def test_same_issue_conflicting_records_are_rejected(self):
        conflicting_numbers = tuple(f"{number:02d}" for number in range(2, 38))
        html = section(196, self.numbers) + section(196, conflicting_numbers)

        diagnostics = validator.analyze_content(html, self.config, 196)

        self.assertTrue(diagnostics.same_issue_conflict)
        self.assertFalse(diagnostics.passed)
        self.assertIn("同期候选冲突", diagnostics.failure_reason)

    def test_duplicate_numbers_are_reported(self):
        numbers = tuple(f"{number:02d}" for number in range(1, 36)) + ("35",)

        diagnostics = validator.analyze_content(section(196, numbers), self.config, 196)

        self.assertFalse(diagnostics.numbers_pass)
        self.assertEqual(diagnostics.number_count, 36)
        self.assertEqual(diagnostics.duplicate_numbers, ("35",))
        self.assertIn("重复数字", diagnostics.failure_reason)

    def test_missing_anchor_is_reported(self):
        html = section(196, self.numbers, name="其他栏目")

        diagnostics = validator.analyze_content(html, self.config, 196)

        self.assertFalse(diagnostics.anchor_pass)
        self.assertFalse(diagnostics.passed)
        self.assertIn("锚点", diagnostics.failure_reason)

    def test_bottom_direction_rejects_target_outside_recent_window(self):
        html = "".join(section(issue, self.numbers) for issue in range(160, 196))

        diagnostics = validator.analyze_content(html, self.config, 160)

        self.assertTrue(diagnostics.target_issue_found)
        self.assertFalse(diagnostics.direction_pass)
        self.assertIn("严格候选范围", diagnostics.failure_reason)

    def test_fenfatuqiang_dedicated_parser_reaches_target_after_long_history(self):
        config = validator.scraper.SiteConfig(
            "奋发图强",
            "https://ymkakun.dwgml-7jbcy-ohrbrq.xyz:16677/topic/547563.html",
            ("36码中特",),
            ("奋发图强", "36码中特"),
            region="bottom",
            search_window=320,
        )
        records = []
        for issue in range(132, 196):
            records.append(
                f"<div>{issue}期:『【36码中特】』开：准</div>{number_rows(self.numbers)}<div>====</div>"
            )
        html = (
            "<h1>196期:奋发图强【36码中特】</h1>"
            + "".join(records)
            + f"<div>196期:『【36码中特】』开：0000准</div>{number_rows(self.numbers)}"
            + "<div>上一篇：</div>"
        )

        candidates, invalid, seen = validator.dedicated_candidates(html, config)

        self.assertIn(196, seen)
        self.assertFalse(invalid)
        exact = [candidate for candidate in candidates if candidate.issue == 196]
        self.assertEqual(len(exact), 1)
        self.assertEqual(exact[0].page_index, 321)
        self.assertEqual(exact[0].numbers, self.numbers)

    def test_xueqiu_dedicated_parser_accepts_only_spaced_dedicated_marker(self):
        config = validator.scraper.SiteConfig(
            "雪球",
            "https://ocnrhq.du156-vb27w-tmhsed.xyz:16677/",
            ("36码特围",),
            ("雪球36码",),
            region="top",
        )
        numbers = (
            "05", "37", "39", "27", "04", "02", "36", "15", "25", "43", "06", "07",
            "16", "31", "24", "32", "12", "03", "28", "08", "48", "44", "42", "29",
            "01", "38", "26", "30", "40", "19", "41", "17", "13", "18", "49", "14",
        )
        html = (
            "<h2>（雪球36码）</h2>"
            "<div>雪球模式正式启动</div>"
            f"<div>196期【3 6码特围】开000准</div>{number_rows(numbers)}"
        )

        candidates, invalid, seen = validator.dedicated_candidates(html, config)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].issue, 196)
        self.assertEqual(candidates[0].page_index, 2)
        self.assertEqual(candidates[0].numbers, numbers)
        self.assertFalse(invalid)
        self.assertEqual(seen, {196})

    def test_xueqiu_dedicated_parser_rejects_spaced_marker_without_site_anchor(self):
        config = validator.scraper.SiteConfig(
            "雪球",
            "https://ocnrhq.du156-vb27w-tmhsed.xyz:16677/",
            ("36码特围",),
            ("雪球36码",),
            region="top",
        )
        html = f"<div>196期【3 6码特围】开000准</div>{number_rows(self.numbers)}"

        with self.assertRaisesRegex(validator.scraper.ScrapeError, "未找到栏目关键词"):
            validator.dedicated_candidates(html, config)

    def test_topic_content_3x12_parser_keeps_article_body_boundary(self):
        config = validator.scraper.SiteConfig(
            "正文三行站",
            "https://example.test/topic/1.html",
            ("精准36码",),
            ("正文三行站",),
            fixed_issue=196,
            region="top",
            parser_id="topic_content_3x12",
        )
        html = (
            "<div class='topic-content'>"
            "<div>196期【精准36码】开000准</div>"
            f"{number_rows(self.numbers)}"
            "</div>"
        )

        candidates, invalid, seen = validator.dedicated_candidates(html, config)

        self.assertEqual([candidate.issue for candidate in candidates], [196])
        self.assertEqual(candidates[0].numbers, self.numbers)
        self.assertFalse(invalid)
        self.assertEqual(seen, {196})

    def test_section_3x12_parser_skips_author_line_without_crossing_issue(self):
        config = validator.scraper.SiteConfig(
            "特围区站",
            "https://example.test/topic/2.html",
            ("36码特围",),
            ("特围区",),
            fixed_issue=196,
            region="top",
            parser_id="section_3x12",
        )
        html = (
            "<h2>特围区196期:标题「36码特围」</h2>"
            "<div>作者:测试作者</div>"
            "<div>196期「36码特围」开000准</div>"
            f"{number_rows(self.numbers)}"
            "<div>195期「36码特围」开准</div>"
            f"{number_rows(self.numbers)}"
        )

        candidates, invalid, seen = validator.dedicated_candidates(html, config)

        exact = [candidate for candidate in candidates if candidate.issue == 196]
        self.assertEqual(len(exact), 1)
        self.assertEqual(exact[0].numbers, self.numbers)
        self.assertFalse(invalid)
        self.assertEqual(seen, {195, 196})

    def test_meirenyu_parser_requires_identity_and_exact_section_heading(self):
        config = validator.scraper.SiteConfig(
            "美人鱼",
            "https://example.test/topic/185453.html",
            ("无错36码",),
            ("美人鱼",),
            fixed_issue=215,
            region="top",
            parser_id="meirenyu",
        )
        html = (
            "<title>美人鱼-澳门</title><br><h2>36码</h2>"
            f"<div>215期围特【无错36码】00准</div>{number_rows(self.numbers)}"
            f"<div>214期围特【无错36码】04错</div>{number_rows(self.numbers)}"
        )

        candidates, invalid, seen = validator.dedicated_candidates(html, config)

        self.assertEqual([candidate.issue for candidate in candidates], [215, 214])
        self.assertFalse(invalid)
        self.assertEqual(seen, {214, 215})
        with self.assertRaisesRegex(validator.scraper.ScrapeError, "页面身份缺失"):
            validator.dedicated_candidates(html.replace("美人鱼-澳门", "其他站-澳门"), config)
        with self.assertRaisesRegex(validator.scraper.ScrapeError, "区块边界缺失"):
            validator.dedicated_candidates(html.replace(">36码<", ">其他栏目<"), config)
        self.assertEqual(DEFAULT_REGISTRY.parse(html, config).issue, 215)
        self.assertEqual(DEFAULT_REGISTRY.parse(html, replace(config, fixed_issue=214)).issue, 214)
        historical_html = html.replace("215期", "126期").replace("214期", "125期")
        self.assertEqual(
            DEFAULT_REGISTRY.parse(historical_html, replace(config, fixed_issue=None)).issue,
            126,
        )
        with self.assertRaises(validator.scraper.ScrapeError):
            DEFAULT_REGISTRY.parse(html, replace(config, fixed_issue=216))
        with self.assertRaisesRegex(validator.scraper.ScrapeError, "未找到栏目关键词"):
            DEFAULT_REGISTRY.parse(
                "<title>美人鱼-澳门</title><br><h2>36码</h2>",
                replace(config, fixed_issue=216),
            )

    def test_xiaoyuer_parser_stops_before_archived_grid(self):
        config = validator.scraper.SiteConfig(
            "小鱼儿",
            "https://example.test/",
            ("开奖结果",),
            ("澳门精准36码",),
            fixed_issue=215,
            region="bottom",
            parser_id="xiaoyuer",
            drop_zero_numbers=True,
            min_numbers_per_line=1,
        )
        rows = number_rows(self.numbers)
        html = (
            "<h2>澳门精准36码</h2>"
            f"<div>215期:开奖结果</div>{rows}"
            "<div>小鱼儿官方网址155573c.com</div>"
            f"<div>084期:开奖结果</div>{rows}"
        )

        candidates, invalid, seen = validator.dedicated_candidates(html, config)

        self.assertEqual([candidate.issue for candidate in candidates], [215])
        self.assertFalse(invalid)
        self.assertEqual(seen, {215})
        self.assertEqual(DEFAULT_REGISTRY.parse(html, config).issue, 215)
        with self.assertRaises(validator.scraper.ScrapeError):
            DEFAULT_REGISTRY.parse(html, replace(config, fixed_issue=216))
        with self.assertRaisesRegex(validator.scraper.ScrapeError, "结束边界缺失"):
            validator.dedicated_candidates(html.replace("小鱼儿官方网址", "广告"), config)

    def test_marker_after_issue_3x12_parser_requires_same_bounded_section(self):
        config = validator.scraper.SiteConfig(
            "内幕快报",
            "https://example.test/",
            ("特围36码",),
            ("内幕36码",),
            fixed_issue=196,
            region="top",
            parser_id="marker_after_issue_3x12",
        )
        html = (
            "<h2>内幕快报【内幕36码】</h2>"
            "<div>收藏</div><div>196期开000准</div><div>【特围36码】</div>"
            f"{number_rows(self.numbers)}"
            "<h2>内幕快报【其他栏目】</h2>"
            "<div>196期【特围36码】</div>"
            f"{number_rows(tuple(f'{number:02d}' for number in range(2, 38)))}"
        )

        candidates, invalid, seen = validator.dedicated_candidates(html, config)

        self.assertEqual([candidate.issue for candidate in candidates], [196])
        self.assertEqual(candidates[0].numbers, self.numbers)
        self.assertFalse(invalid)
        self.assertEqual(seen, {196})

    def test_dedicated_site_still_calls_formal_scraper(self):
        config = validator.scraper.SiteConfig(
            "雪球",
            "https://ocnrhq.du156-vb27w-tmhsed.xyz:16677/",
            ("36码特围",),
            ("雪球36码",),
            region="top",
        )
        numbers = tuple(f"{number:02d}" for number in range(1, 37))
        html = f"<h2>雪球36码</h2><div>196期【3 6码特围】开000准</div>{number_rows(numbers)}"
        calls = []

        def formal_scraper(site_config, **_kwargs):
            calls.append(site_config.name)
            return evidence_record(site_config.name, site_config.url, 196, numbers)

        report = validator.validate_case(
            validator.ResolvedCase(config, 196),
            timeout=10,
            source_fetcher=lambda *_args, **_kwargs: (html, "测试正文"),
            site_scraper=formal_scraper,
        )

        self.assertTrue(report.passed)
        self.assertEqual(calls, ["雪球"])

    def test_formal_result_must_match_diagnostic_numbers(self):
        case = validator.ResolvedCase(self.config, 196)
        formal_numbers = tuple(f"{number:02d}" for number in range(2, 38))

        report = validator.validate_case(
            case,
            timeout=10,
            source_fetcher=lambda *_args, **_kwargs: (section(196, self.numbers), "测试正文"),
            site_scraper=lambda *_args, **_kwargs: evidence_record(
                self.config.name,
                self.config.url,
                196,
                formal_numbers,
            ),
        )

        self.assertFalse(report.passed)
        self.assertIn("正式抓取数字与诊断候选不一致", report.failure_reason)

    def test_empty_case_list_does_not_write_project_outputs(self):
        output = io.StringIO()

        exit_code = validator.run_cases((), (), timeout=10, output=output)

        self.assertEqual(exit_code, 0)
        self.assertIn("测试清单为空", output.getvalue())

    def test_run_cases_only_runs_listed_site_and_never_calls_production_writers(self):
        output = io.StringIO()
        other_site = validator.scraper.SiteConfig(
            "其他站",
            "https://other.test/topic.html",
            ("三十六码",),
            ("其他站",),
            region="bottom",
        )
        calls: list[str] = []

        def formal_scraper(config, **_kwargs):
            calls.append(config.name)
            return evidence_record(config.name, config.url, 196, self.numbers)

        with (
            mock.patch.object(
                validator.scraper,
                "update_recent_duplicate_backup",
                side_effect=AssertionError("cache writer called"),
            ),
            mock.patch.object(
                validator.scraper,
                "write_results",
                side_effect=AssertionError("success writer called"),
            ),
            mock.patch.object(
                validator.scraper,
                "write_failures",
                side_effect=AssertionError("failure writer called"),
            ),
        ):
            exit_code = validator.run_cases(
                (validator.ValidationCase(name="测试站", issue=196),),
                (self.config, other_site),
                timeout=10,
                output=output,
                source_fetcher=lambda *_args, **_kwargs: (section(196, self.numbers), "测试正文"),
                site_scraper=formal_scraper,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, ["测试站"])
        self.assertIn("是否抓到指定期数: 是", output.getvalue())
        self.assertIn("实际数字数量: 36", output.getvalue())
        self.assertIn("top/bottom: bottom，通过", output.getvalue())
        self.assertIn("隔离状态: 未更新近10期缓存，未写正式成功/失败TXT", output.getvalue())


if __name__ == "__main__":
    unittest.main()
