"""2026-09 新增 11 站(域名 6yrtfjghvnb-0978dtuxfyh.xyz)专属 three_rows 解析测试。

页面证据:牛坛论坛 bbs_topic 模板,每期块头为一行(期号+站名+码类+开奖),后跟三行 12 码;
部分期的三行存在跨行碎片(如 '02' 单独一行、'.47』' 续行),html_to_lines 会将碎片并回上一行。
以下夹具取自 2026-09-14 真实抓取页面(html_to_lines 合并行原文)。
"""

from __future__ import annotations

import unittest

from dawei.domain.errors import ScrapeError
from dawei.domain.models import SiteConfig
from dawei.parsers.three_rows import extract_onboarded_manager_article

NANFANG_257 = (
    "257期:🔬【南方之福】🔬36码中特🔬开:？00中\n"
    "『01.05.16.27.40.49.14.46.26.38.47.28』\n"
    "『33.24.30.07.48.32.10.25.19.39.43.02』\n"
    "『04.17.18.34.03.45.44.13.29.20.36.09』\n"
    "http://6yrtfjghvnb-0978dtuxfyh.xyz\n"
)
NANFANG_256 = (
    "256期:🔬【南方之福】🔬36码中特🔬开:马01中\n"
    "『07.01.44.33.20.15.23.39.13.40.34.37』\n"
    "『26.25.24.03.42.09.35.18.05.38.41.02』\n"
    "『21.04.48.12.27.06.28.49.14.45.10.22』\n"
    "257期:🔬【南方之福】🔬36码中特🔬开:？00中\n"
    "『01.05.16.27.40.49.14.46.26.38.47.28』\n"
    "『33.24.30.07.48.32.10.25.19.39.43.02』\n"
    "『04.17.18.34.03.45.44.13.29.20.36.09』\n"
)
GONGDE_257 = (
    "257期:🧇《功德圆满》🧇36码中特🧇开:？00准\n"
    "《41.04.25.29.42.43.31.49.02.26.32.28》\n"
    "《45.13.40.20.08.48.27.18.39.17.46.16》\n"
    "《34.06.36.03.11.33.35.47.22.19.07.05》\n"
)
PIANPIAN_256 = (
    "256期:《翩翩少年》📤三十六码中特📤开:马01准\n"
    "〖16.28.34.20.11.23.46.08.41.15.38.48〗\n"
    "〖06.40.30.47.18.24.33.21.07.43.39.19〗\n"
    "〖42.05.35.29.22.32.17.49.45.25.03.01〗\n"
    "257期:《翩翩少年》📤三十六码中特📤开:？00准\n"
    "〖30.16.10.14.11.24.33.45.38.29.37.06〗\n"
    "〖40.07.02.13.26.35.34.03.15.27.21.09〗\n"
    "〖17.42.39.23.20.31.01.04.49.36.32.43〗\n"
)
CAOCAO_257 = (
    "257期:👩【嘈嘈杂杂】👩三十六码中特👩开:？00对\n"
    "〖17.19.29.04.41.49.14.03.09.37.23.33〗\n"
    "〖05.11.24.07.30.18.26.40.38.10.25.45〗\n"
    "〖13.16.01.22.21.39.02.15.06.32.46.44〗\n"
)

NANFANG_257_NUMBERS = (
    "01", "05", "16", "27", "40", "49", "14", "46", "26", "38", "47", "28",
    "33", "24", "30", "07", "48", "32", "10", "25", "19", "39", "43", "02",
    "04", "17", "18", "34", "03", "45", "44", "13", "29", "20", "36", "09",
)
NANFANG_256_NUMBERS = (
    "07", "01", "44", "33", "20", "15", "23", "39", "13", "40", "34", "37",
    "26", "25", "24", "03", "42", "09", "35", "18", "05", "38", "41", "02",
    "21", "04", "48", "12", "27", "06", "28", "49", "14", "45", "10", "22",
)
GONGDE_257_NUMBERS = (
    "41", "04", "25", "29", "42", "43", "31", "49", "02", "26", "32", "28",
    "45", "13", "40", "20", "08", "48", "27", "18", "39", "17", "46", "16",
    "34", "06", "36", "03", "11", "33", "35", "47", "22", "19", "07", "05",
)
PIANPIAN_256_NUMBERS = (
    "16", "28", "34", "20", "11", "23", "46", "08", "41", "15", "38", "48",
    "06", "40", "30", "47", "18", "24", "33", "21", "07", "43", "39", "19",
    "42", "05", "35", "29", "22", "32", "17", "49", "45", "25", "03", "01",
)
CAOCAO_257_NUMBERS = (
    "17", "19", "29", "04", "41", "49", "14", "03", "09", "37", "23", "33",
    "05", "11", "24", "07", "30", "18", "26", "40", "38", "10", "25", "45",
    "13", "16", "01", "22", "21", "39", "02", "15", "06", "32", "46", "44",
)


def site_config(name: str, url: str, keyword: str, issue: int | None = None) -> SiteConfig:
    return SiteConfig(
        name=name,
        url=url,
        keywords=(keyword,),
        section_keywords=(name,),
        search_window=520,
        fixed_issue=issue,
        region="bottom",
        position="tail",
        source_type="bbs_topic",
        parser_id="three_rows",
        site_id=f"site_test_{name}",
    )


class NewSitesThreeRowsTests(unittest.TestCase):
    def test_nafangzhifu_257_extracted_in_original_order(self) -> None:
        result = extract_onboarded_manager_article(
            NANFANG_257,
            site_config(
                "南方之福",
                "https://6yrtfjghvnb-0978dtuxfyh.xyz/read.php?tid=53267",
                "36码中特",
                issue=257,
            ),
        )
        self.assertEqual(result.issue, 257)
        self.assertEqual(result.numbers, NANFANG_257_NUMBERS)

    def test_gongdeyuanyuan_257_extracted_in_original_order(self) -> None:
        result = extract_onboarded_manager_article(
            GONGDE_257,
            site_config(
                "功德圆满",
                "https://6yrtfjghvnb-0978dtuxfyh.xyz/read.php?tid=53282",
                "36码中特",
                issue=257,
            ),
        )
        self.assertEqual(result.issue, 257)
        self.assertEqual(result.numbers, GONGDE_257_NUMBERS)

    def test_cacaozaza_keword_san_shi_liu_ma(self) -> None:
        result = extract_onboarded_manager_article(
            CAOCAO_257,
            site_config(
                "嘈嘈杂杂",
                "https://6yrtfjghvnb-0978dtuxfyh.xyz/read.php?tid=51905",
                "三十六码",
                issue=257,
            ),
        )
        self.assertEqual(result.issue, 257)
        self.assertEqual(result.numbers, CAOCAO_257_NUMBERS)

    def test_adjacent_issue_256_selected_over_257(self) -> None:
        result = extract_onboarded_manager_article(
            NANFANG_256,
            site_config(
                "南方之福",
                "https://6yrtfjghvnb-0978dtuxfyh.xyz/read.php?tid=53267",
                "36码中特",
                issue=256,
            ),
        )
        self.assertEqual(result.issue, 256)
        self.assertEqual(result.numbers, NANFANG_256_NUMBERS)

    def test_missing_issue_fails_without_guessing(self) -> None:
        with self.assertRaisesRegex(ScrapeError, "未找到指定999期"):
            extract_onboarded_manager_article(
                NANFANG_257,
                site_config(
                    "南方之福",
                    "https://6yrtfjghvnb-0978dtuxfyh.xyz/read.php?tid=53267",
                    "36码中特",
                    issue=999,
                ),
            )

    def test_pianpianshaonian_256_extracted_adjacent_to_257(self) -> None:
        result = extract_onboarded_manager_article(
            PIANPIAN_256,
            site_config(
                "翩翩少年",
                "https://6yrtfjghvnb-0978dtuxfyh.xyz/read.php?tid=53895",
                "三十六码",
                issue=256,
            ),
        )
        self.assertEqual(result.issue, 256)
        self.assertEqual(result.numbers, PIANPIAN_256_NUMBERS)

    def test_all_active_formal_entries_use_dedicated_three_rows(self) -> None:
        """本批 12 站中的每一项，要么在册且配置正确，要么已封存。

        名单不写死"在册数量"，因此后续封存本批站点不会再次让该测试失败；只要求
        每个站名都能在 sites_36.json 或 archived_sites_36.json 中找到落点。
        """
        import json
        from pathlib import Path

        from dawei.infrastructure.config_repository import ConfigRepository

        root = Path(__file__).resolve().parent
        sites = ConfigRepository(root / "sites_36.json").load()
        archived = json.loads(
            (root / "archived_sites_36.json").read_text(encoding="utf-8-sig")
        )
        archived_names = {item["name"] for item in archived["sites"]}
        batch = {
            "南方之福", "扶正祛邪", "功德圆满", "高手过招", "烽火连天",
            "嘈嘈杂杂", "树上开花", "各尽所能", "认证猛料", "家有敝帚",
            "稳赚不陪", "翩翩少年",
        }
        by_name = {site.name: site for site in sites}
        active = [by_name[name] for name in sorted(batch) if name in by_name]
        retired = batch & archived_names

        self.assertTrue(active or retired, "本批站点不应全部被移出正式配置和封存清单")
        self.assertFalse(
            batch - set(by_name) - retired,
            "本批站点必须要么在册、要么已封存，不得凭空消失",
        )
        for site in active:
            self.assertEqual(site.parser_id, "three_rows")
            self.assertEqual(site.direction, "bottom")
            self.assertEqual(site.source_type, "bbs_topic")
            self.assertEqual(site.render_policy, "never")
            self.assertIn(site.keywords, (("36码中特",), ("三十六码",)))
            self.assertEqual(site.section_keywords, (site.name,))
        for name in sorted(retired):
            self.assertNotIn(name, by_name, "已封存站点不得留在正式配置")


if __name__ == "__main__":
    unittest.main()
