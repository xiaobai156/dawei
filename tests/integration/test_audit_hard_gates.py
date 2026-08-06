from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dawei.application.batch_service import BatchOptions, SingleIssueBatchService
from dawei.application.duplicate_service import SiteWindow, scrape_site_window
from dawei.application.onboarding_service import validate_candidate_window
from dawei.application.scrape_service import ScrapeService, scrape_bb48kk_image_site
from dawei.domain.errors import ScrapeError
from dawei.domain.models import (
    ArticleRecord,
    CandidateEvidence,
    CandidateOrigin,
    ParsedRecord,
    SiteConfig,
)
from dawei.parsers import DEFAULT_REGISTRY
from dawei.parsers import dynamic_article
from dawei.parsers.common import DOCUMENT_BOUNDARY
from dawei.parsers.registry import ParserRegistry
from tests.support.evidence_factory import record as evidence_record


NUMBERS = tuple(f"{value:02d}" for value in range(1, 37))
ALT_NUMBERS = tuple(f"{value:02d}" for value in range(2, 38))
NUMBER_LINE = " ".join(NUMBERS)


def generic_config(*, fixed_issue: int = 211, region: str = "bottom") -> SiteConfig:
    return SiteConfig(
        "测试站",
        "https://example.test",
        section_keywords=("目标栏目",),
        keywords=("36码中特",),
        fixed_issue=fixed_issue,
        region=region,
        parser_id="generic_36",
        site_id="site-test",
    )


def blank_evidence(issue: int = 211) -> CandidateEvidence:
    origin = CandidateOrigin("", (), "", "", "", "", "", 0, 0, 0, 0, "")
    return CandidateEvidence(issue, NUMBERS, "", (), "", "", "", "", "", 0, 0, 0, 0, "", (origin,))


class AuditHardGateTests(unittest.TestCase):
    def test_previous_issue_body_keyword_cannot_be_borrowed(self) -> None:
        document = "\n".join(
            [
                "目标栏目",
                "210期",
                "36码中特",
                NUMBER_LINE,
                "211期",
                NUMBER_LINE,
            ]
        )

        with self.assertRaises(ScrapeError):
            DEFAULT_REGISTRY.parse(document, generic_config())

    def test_previous_issue_title_keyword_cannot_be_borrowed(self) -> None:
        document = "\n".join(
            [
                "210期 目标栏目 36码中特",
                NUMBER_LINE,
                "211期 无关内容",
                NUMBER_LINE,
            ]
        )

        with self.assertRaises(ScrapeError):
            DEFAULT_REGISTRY.parse(document, generic_config())

    def test_different_issue_heading_cannot_anchor_candidate(self) -> None:
        document = "\n".join(
            [
                "212期 目标栏目 36码中特",
                "212期 开奖结果",
                NUMBER_LINE,
                "211期 开奖结果",
                NUMBER_LINE,
            ]
        )

        with self.assertRaises(ScrapeError):
            DEFAULT_REGISTRY.parse(document, generic_config())

    def test_duplicate_previous_issue_records_do_not_become_section_heading(self) -> None:
        document = "\n".join(
            [
                "210期 目标栏目 36码中特",
                NUMBER_LINE,
                "210期 36码中特",
                NUMBER_LINE,
                "211期 无关内容",
                NUMBER_LINE,
            ]
        )

        with self.assertRaises(ScrapeError):
            DEFAULT_REGISTRY.parse(document, generic_config())

    def test_plain_foreign_heading_ends_target_section(self) -> None:
        document = "\n".join(
            [
                "目标栏目",
                "210期 36码中特",
                NUMBER_LINE,
                "其他栏目",
                "211期 36码中特",
                NUMBER_LINE,
            ]
        )

        with self.assertRaises(ScrapeError):
            DEFAULT_REGISTRY.parse(document, generic_config())

    def test_delayed_foreign_heading_still_ends_target_section(self) -> None:
        document = "\n".join(
            [
                "目标栏目",
                "210期 36码中特",
                NUMBER_LINE,
                "其他栏目",
                "说明 1",
                "说明 2",
                "说明 3",
                "说明 4",
                "211期 36码中特",
                NUMBER_LINE,
            ]
        )

        with self.assertRaises(ScrapeError):
            DEFAULT_REGISTRY.parse(document, generic_config())

    def test_long_foreign_heading_still_ends_target_section(self) -> None:
        document = "\n".join(
            [
                "目标栏目",
                "210期 36码中特",
                NUMBER_LINE,
                "其他栏目这是一个长度明显超过四十个字符并且属于完全不同数据区域的正式栏目标题用于验证严格边界",
                "211期 36码中特",
                NUMBER_LINE,
            ]
        )

        with self.assertRaises(ScrapeError):
            DEFAULT_REGISTRY.parse(document, generic_config())

    def test_taxue_dedicated_parser_keeps_issue_and_rows_together(self) -> None:
        rows = (
            "[A] " + " ".join(f"{value:02d}" for value in range(1, 10)),
            "[B] " + " ".join(f"{value:02d}" for value in range(10, 19)),
            "[C] " + " ".join(f"{value:02d}" for value in range(19, 28)),
            "[D] " + " ".join(f"{value:02d}" for value in range(28, 37)),
        )
        document = "\n".join(
            [
                "212期 踏雪无痕 内幕36码",
                "212期【】开000中",
                *rows,
                "211期【】开马01错",
                *rows,
            ]
        )
        config = SiteConfig(
            "踏雪",
            "https://example.test",
            keywords=("内幕36码",),
            section_keywords=("踏雪无痕", "内幕36码"),
            fixed_issue=211,
            region="top",
            parser_id="taxue_four_rows",
            site_id="site-taxue",
        )

        result = DEFAULT_REGISTRY.parse(document, config)

        self.assertEqual(result.issue, 211)
        self.assertEqual(result.numbers, NUMBERS)
        self.assertEqual(result.evidence.anchor_line, "211期【】开马01错")

    def test_dedicated_parser_cannot_borrow_anchor_across_documents(self) -> None:
        document = "\n".join(
            [
                "澳门精准36码",
                DOCUMENT_BOUNDARY,
                "211期:开奖结果:00-00-00-00-00-00特:0000",
                NUMBER_LINE,
            ]
        )
        config = SiteConfig(
            "小鱼儿",
            "https://example.test",
            keywords=("开奖结果",),
            section_keywords=("澳门精准36码",),
            fixed_issue=211,
            min_numbers_per_line=1,
            drop_zero_numbers=True,
            region="bottom",
            parser_id="xiaoyuer",
            site_id="site-xiaoyuer",
        )

        with self.assertRaises(ScrapeError):
            DEFAULT_REGISTRY.parse(document, config)

    def test_tabular_parser_stops_at_document_boundary(self) -> None:
        document = "\n".join(
            [
                "澳门精准36码",
                DOCUMENT_BOUNDARY,
                "211期:开奖结果:00-00-00-00-00-00特:0000",
                NUMBER_LINE,
            ]
        )
        config = SiteConfig(
            "小鱼儿",
            "https://example.test",
            keywords=("开奖结果",),
            section_keywords=("澳门精准36码",),
            fixed_issue=211,
            min_numbers_per_line=1,
            drop_zero_numbers=True,
            region="bottom",
            parser_id="xiaoyuer",
            site_id="site-xiaoyuer",
        )

        with self.assertRaisesRegex(ScrapeError, "没有找到符合条件的数据"):
            DEFAULT_REGISTRY.parse(document, config)

    def test_fenfatuqiang_article_end_cannot_come_from_next_document(self) -> None:
        document = "\n".join(
            [
                "奋发图强 36码中特",
                "211期:『【36码中特】』开",
                NUMBER_LINE,
                DOCUMENT_BOUNDARY,
                "上一篇：",
            ]
        )
        config = SiteConfig(
            "奋发图强",
            "https://example.test",
            keywords=("36码中特",),
            section_keywords=("奋发图强", "36码中特"),
            fixed_issue=211,
            region="bottom",
            parser_id="fenfatuqiang",
            site_id="site-fenfatuqiang",
        )

        with self.assertRaisesRegex(ScrapeError, "正文边界缺失"):
            DEFAULT_REGISTRY.parse(document, config)

    def test_dedicated_block_cannot_cross_document_boundary(self) -> None:
        document = "\n".join(
            [
                "211期",
                "疯狂中码 三十六码",
                DOCUMENT_BOUNDARY,
                f"【{NUMBER_LINE}】",
            ]
        )
        config = SiteConfig(
            "疯狂中码",
            "https://example.test",
            keywords=("三十六码",),
            section_keywords=("疯狂中码",),
            fixed_issue=211,
            region="bottom",
            parser_id="fengkuang_zhongma",
            site_id="site-fengkuang",
        )

        with self.assertRaises(ScrapeError):
            DEFAULT_REGISTRY.parse(document, config)

    def test_fixed_issue_rejects_conflict_outside_direction_window(self) -> None:
        document = "\n".join(
            [
                "目标栏目",
                "211期 36码中特",
                " ".join(ALT_NUMBERS),
                "208期 36码中特",
                NUMBER_LINE,
                "209期 36码中特",
                NUMBER_LINE,
                "210期 36码中特",
                NUMBER_LINE,
                "211期 36码中特",
                NUMBER_LINE,
            ]
        )

        with self.assertRaisesRegex(ScrapeError, "多个高可信候选.*冲突"):
            DEFAULT_REGISTRY.parse(document, generic_config())

    def test_candidate_onboarding_requires_reference_issue_in_direction_window(self) -> None:
        lines = ["目标栏目"]
        for issue in range(211, 201, -1):
            lines.extend((f"{issue}期 36码中特", NUMBER_LINE))
        config = generic_config(region="bottom")
        window = scrape_site_window(
            config,
            211,
            10,
            10,
            20,
            text_fetcher=lambda *_: "\n".join(lines),
        )

        with self.assertRaisesRegex(ScrapeError, "方向最近3组"):
            validate_candidate_window(window, 211, 10, config=config)

    def test_candidate_onboarding_rejects_empty_dedicated_keywords(self) -> None:
        config = SiteConfig(
            "候选站",
            "https://candidate.test",
            keywords=(),
            section_keywords=(),
            region="bottom",
            parser_id="generic_36",
            site_id="candidate",
        )
        records = tuple(
            evidence_record(
                config.name,
                config.url,
                issue,
                NUMBERS,
                page_index=issue,
            )
            for issue in range(202, 212)
        )
        window = SiteWindow(
            config.name,
            config.url,
            211,
            10,
            records,
            latest_issue=211,
            site_id=config.site_id,
            parser_id=config.parser_id,
        )

        with self.assertRaisesRegex(ScrapeError, "专属.*关键词|栏目"):
            validate_candidate_window(window, 211, 10, config=config)

    def test_formal_batch_rejects_missing_candidate_evidence(self) -> None:
        site = generic_config()

        def scrape(config: SiteConfig, *, timeout: int, fixed_issue: int | None):
            return ParsedRecord(config.name, config.url, fixed_issue or 0, NUMBERS)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = SingleIssueBatchService(site_scraper=scrape).run(
                (site,),
                BatchOptions(
                    fixed_issue=211,
                    output_path=root / "success.txt",
                    error_output_path=root / "failure.txt",
                    update_recent_cache=False,
                ),
            )

            self.assertEqual(result.exit_code, 1)
            self.assertFalse((root / "success.txt").read_text(encoding="utf-8-sig"))
            self.assertIn("候选证据", (root / "failure.txt").read_text(encoding="utf-8-sig"))

    def test_formal_batch_rejects_blank_candidate_evidence(self) -> None:
        site = generic_config()

        def scrape(config: SiteConfig, *, timeout: int, fixed_issue: int | None):
            return ParsedRecord(
                config.name,
                config.url,
                fixed_issue or 0,
                NUMBERS,
                evidence=blank_evidence(fixed_issue or 0),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = SingleIssueBatchService(site_scraper=scrape).run(
                (site,),
                BatchOptions(
                    fixed_issue=211,
                    output_path=root / "success.txt",
                    error_output_path=root / "failure.txt",
                    update_recent_cache=False,
                ),
            )

            self.assertEqual(result.exit_code, 1)
            self.assertIn("候选证据", (root / "failure.txt").read_text(encoding="utf-8-sig"))

    def test_registry_rejects_legacy_tuple_candidates(self) -> None:
        registry = ParserRegistry()
        registry.register(
            "legacy",
            lambda text, config: ParsedRecord(config.name, config.url, 211, NUMBERS),
            lambda text, config: ([(211, 0, NUMBERS)], [], {211}),
        )
        config = SiteConfig(
            "测试站",
            "https://example.test",
            fixed_issue=211,
            region="bottom",
            parser_id="legacy",
        )

        with self.assertRaisesRegex(ScrapeError, "CandidateEvidence"):
            registry.parse("211期", config)

    def test_image_auto_latest_rejects_same_issue_conflict(self) -> None:
        config = SiteConfig(
            "图片站",
            "https://image.test",
            site_id="image-test",
            parser_id="image_bb48kk",
            region="top",
        )
        fetched: list[str] = []

        def fetch_bytes(url: str, timeout: int):
            fetched.append(url)
            return url.encode()

        with (
            mock.patch(
                "dawei.application.scrape_service.image_36.bb48kk_images_from",
                return_value=[
                    (126, "https://img.test/a.jpg", True),
                    (126, "https://img.test/b.jpg", True),
                ],
            ),
            mock.patch(
                "dawei.application.scrape_service.image_36.decode_keyed_jpeg",
                side_effect=lambda value: value,
            ),
            mock.patch(
                "dawei.application.scrape_service.image_client.jpeg_to_bmp_bytes",
                side_effect=lambda value: value,
            ),
            mock.patch(
                "dawei.application.scrape_service.image_36.extract_fixed_image_numbers",
                side_effect=lambda value: NUMBERS if b"a.jpg" in value else ALT_NUMBERS,
            ),
        ):
            with self.assertRaisesRegex(ScrapeError, "图片候选.*冲突"):
                scrape_bb48kk_image_site(
                    config,
                    20,
                    None,
                    text_fetcher=lambda *_: "page",
                    byte_fetcher=fetch_bytes,
                )

        self.assertEqual(fetched, ["https://img.test/a.jpg", "https://img.test/b.jpg"])

    def test_collection_parser_respects_top_three_records(self) -> None:
        records = []
        for issue in range(211, 205, -1):
            records.append(
                {
                    "id": f"record-{issue}",
                    "user_id": 3792,
                    "status": "published",
                    "lottery": "macao",
                    "topic": "困难杂志",
                    "draw": issue,
                    "content": f"{issue}期 困难杂志\n{NUMBER_LINE}",
                }
            )
        config = SiteConfig(
            "困难杂志",
            "https://collection.test/#/users/3792",
            api_url="https://collection.test/users/3792/forums",
            keywords=("困难杂志",),
            region="top",
            source_type="dynamic_collection",
            parser_id="kunnan_magazine",
            site_id="site-collection",
        )

        with self.assertRaisesRegex(ScrapeError, "最近3组"):
            ScrapeService(text_fetcher=lambda *_: json.dumps(records)).scrape(
                config,
                fixed_issue=208,
            )

    def test_dynamic_api_id_mismatch_cannot_be_hidden_by_browser_success(self) -> None:
        config = SiteConfig(
            "目标站",
            "https://example.test/article/manager/target-id?url=x",
            keywords=("36码中特",),
            section_keywords=("目标站",),
            api_url="https://example.test/api/target-id",
            region="bottom",
            parser_id="generic_36",
            source_type="dynamic_article",
            record_id="target-id",
            site_id="target",
        )
        payload = json.dumps(
            {
                "data": [
                    {
                        "id": "decoy-id",
                        "authorNickname": "目标站",
                        "title": "211期 36码中特",
                        "content": "目标站\n211期 36码中特\n" + NUMBER_LINE,
                    }
                ]
            },
            ensure_ascii=False,
        )
        rendered_calls: list[str] = []

        def render(*args, **kwargs):
            rendered_calls.append("rendered")
            body = "目标站\n211期 36码中特\n" + NUMBER_LINE
            return ArticleRecord(
                "target-id",
                config.url + "::rendered",
                "211期 36码中特",
                config.name,
                body,
                body,
            )

        with self.assertRaisesRegex(ScrapeError, "API ID不匹配"):
            ScrapeService(
                text_fetcher=lambda *_: payload,
                rendered_article_fetcher=render,
            ).scrape(config, fixed_issue=211)

        self.assertEqual(rendered_calls, [])

    def test_dynamic_api_candidate_conflict_cannot_be_hidden_by_browser_success(self) -> None:
        config = SiteConfig(
            "目标站",
            "https://example.test/article/manager/target-id?url=x",
            keywords=("36码中特",),
            section_keywords=("目标站",),
            api_url="https://example.test/api/target-id",
            region="bottom",
            parser_id="generic_36",
            source_type="dynamic_article",
            record_id="target-id",
            site_id="target",
        )
        conflicting_body = "\n".join(
            (
                "目标站",
                "211期 36码中特",
                NUMBER_LINE,
                "211期 36码中特",
                " ".join(ALT_NUMBERS),
            )
        )
        payload = json.dumps(
            {
                "data": [
                    {
                        "id": "target-id",
                        "authorNickname": "目标站",
                        "title": "211期 36码中特",
                        "content": conflicting_body,
                    }
                ]
            },
            ensure_ascii=False,
        )
        rendered_calls: list[str] = []

        def render(*args, **kwargs):
            rendered_calls.append("rendered")
            body = "目标站\n211期 36码中特\n" + NUMBER_LINE
            return ArticleRecord(
                "target-id",
                config.url + "::rendered",
                "211期 36码中特",
                config.name,
                body,
                body,
            )

        with self.assertRaisesRegex(ScrapeError, "多个高可信候选.*冲突"):
            ScrapeService(
                text_fetcher=lambda *_: payload,
                rendered_article_fetcher=render,
            ).scrape(config, fixed_issue=211)

        self.assertEqual(rendered_calls, [])

    def test_collection_wrapped_payload_preserves_real_json_root_path(self) -> None:
        config = SiteConfig(
            "困难杂志",
            "https://collection.test/#/users/3792",
            api_url="https://collection.test/users/3792/forums",
            keywords=("困难杂志",),
            region="top",
            source_type="dynamic_collection",
            parser_id="kunnan_magazine",
            site_id="site-collection",
        )
        payload = json.dumps(
            {
                "data": [
                    {
                        "id": "record-211",
                        "user_id": 3792,
                        "status": "published",
                        "lottery": "macao",
                        "topic": "困难杂志",
                        "draw": 211,
                        "content": f"211期 困难杂志\n{NUMBER_LINE}",
                    }
                ]
            },
            ensure_ascii=False,
        )

        result = dynamic_article.kunnan_magazine_records_from_payload(payload, config)[0]

        self.assertEqual(
            result.record_path,
            config.api_url + "::$.data[0]",
        )


if __name__ == "__main__":
    unittest.main()
