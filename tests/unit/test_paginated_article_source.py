from __future__ import annotations

import unittest

from dawei.application.scrape_service import ScrapeService
from dawei.domain.errors import ScrapeError
from dawei.domain.models import ParsedRecord, SiteConfig
from dawei.infrastructure.source_adapters import fetch_paginated_list_documents
from dawei.parsers.paginated_article import resolve_paginated_article
from tests.support.evidence_factory import record as evidence_record


START_URL = "https://example.test/list.aspx?id=79&page=1"
ARTICLE_URL = "https://example.test/article.aspx?id=972355"
NUMBERS = tuple(f"{value:02d}" for value in range(1, 37))


def site() -> SiteConfig:
    return SiteConfig(
        name="稳赚致胜",
        url=START_URL,
        keywords=("稳赚致胜",),
        section_keywords=("稳赚致胜",),
        navigation_keywords=("稳赚致胜【精准36码】",),
        region="top",
        site_id="site-paginated",
        source_type="paginated_article_list",
        parser_id="paginated_article_36",
    )


def page(next_href: str | None, links: str) -> str:
    next_link = f'<a href="{next_href}">下一页</a>' if next_href else ""
    return f"<html><body>{links}{next_link}</body></html>"


class PaginatedArticleSourceTests(unittest.TestCase):
    def test_fetcher_follows_only_same_origin_next_page_links(self) -> None:
        pages = {
            START_URL: page(
                "/list.aspx?id=79&page=2",
                '<a href="/article.aspx?id=1">218期: 其他栏目</a>',
            ),
            "https://example.test/list.aspx?id=79&page=2": page(
                None,
                '<a href="/article.aspx?id=2">218期: 稳赚致胜【精准36码】</a>',
            ),
        }

        documents = fetch_paginated_list_documents(
            START_URL,
            timeout=2,
            fetcher=lambda url, _timeout: pages[url],
        )

        self.assertEqual([document.url for document in documents], list(pages))
        self.assertEqual(documents[1].links[0].href, ARTICLE_URL.replace("972355", "2"))

    def test_fetcher_rejects_cross_origin_pagination(self) -> None:
        pages = {START_URL: page("https://other.example/list.aspx?page=2", "")}

        with self.assertRaisesRegex(ScrapeError, "分页链接跨域"):
            fetch_paginated_list_documents(
                START_URL,
                timeout=2,
                fetcher=lambda url, _timeout: pages[url],
            )

    def test_resolver_requires_exact_issue_and_navigation_keyword(self) -> None:
        pages = {
            START_URL: page(
                None,
                '<a href="/article.aspx?id=1">218期: 其他栏目【精准36码】</a>'
                '<a href="/article.aspx?id=2">218期: 稳赚致胜【精准36码】已免费公开</a>',
            )
        }
        documents = fetch_paginated_list_documents(
            START_URL,
            timeout=2,
            fetcher=lambda url, _timeout: pages[url],
        )

        resolved = resolve_paginated_article(documents, site(), fixed_issue=218)

        self.assertEqual(resolved.issue, 218)
        self.assertEqual(resolved.title, "218期: 稳赚致胜【精准36码】已免费公开")
        self.assertEqual(resolved.url, "https://example.test/article.aspx?id=2")

    def test_resolver_rejects_duplicate_same_issue_target_links(self) -> None:
        pages = {
            START_URL: page(
                None,
                '<a href="/article.aspx?id=1">218期: 稳赚致胜【精准36码】</a>'
                '<a href="/article.aspx?id=2">218期: 稳赚致胜【精准36码】</a>',
            )
        }
        documents = fetch_paginated_list_documents(
            START_URL,
            timeout=2,
            fetcher=lambda url, _timeout: pages[url],
        )

        with self.assertRaisesRegex(ScrapeError, "同期目标文章冲突"):
            resolve_paginated_article(documents, site(), fixed_issue=218)

    def test_scrape_service_fetches_resolved_article_then_parses_top_issue(self) -> None:
        article_html = "target article body"
        pages = {
            START_URL: page(
                None,
                '<a href="/article.aspx?id=2">218期: 稳赚致胜【精准36码】</a>',
            ),
            "https://example.test/article.aspx?id=2": article_html,
        }
        calls: list[str] = []

        def fetcher(url: str, _timeout: int) -> str:
            calls.append(url)
            return pages[url]

        def parser(document: str, config: SiteConfig) -> ParsedRecord:
            self.assertEqual(document, article_html)
            self.assertEqual(config.url, "https://example.test/article.aspx?id=2")
            return evidence_record(config.name, config.url, 218, NUMBERS)

        result = ScrapeService(text_fetcher=fetcher, parser=parser).scrape(
            site(),
            fixed_issue=218,
            timeout=2,
        )

        self.assertEqual(calls, [START_URL, "https://example.test/article.aspx?id=2"])
        self.assertEqual(result.issue, 218)
        self.assertEqual(result.url, START_URL)
        self.assertEqual(result.evidence.document_url, "https://example.test/article.aspx?id=2")


if __name__ == "__main__":
    unittest.main()
