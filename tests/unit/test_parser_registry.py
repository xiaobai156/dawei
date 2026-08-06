from __future__ import annotations

import unittest

from dawei.domain.errors import ConfigurationError
from dawei.domain.models import ParsedRecord, SiteConfig
from dawei.parsers import DEFAULT_REGISTRY
from dawei.parsers.registry import ParserRegistry
from tests.support.evidence_factory import record as evidence_record


def config(parser_id: str) -> SiteConfig:
    return SiteConfig(
        name="测试站",
        url="https://example.test",
        region="bottom",
        site_id="site-test",
        parser_id=parser_id,
    )


class ParserRegistryTests(unittest.TestCase):
    def test_dispatches_only_by_persisted_parser_id(self) -> None:
        registry = ParserRegistry()
        calls: list[str] = []

        def parser(text: str, site: SiteConfig) -> ParsedRecord:
            calls.append(text)
            return evidence_record(
                site.name,
                site.url,
                210,
                tuple(f"{value:02d}" for value in range(1, 37)),
            )

        registry.register("generic_36", parser)
        result = registry.parse("document", config("generic_36"))
        self.assertEqual(result.issue, 210)
        self.assertEqual(calls, ["document"])

    def test_rejects_duplicate_and_unknown_parser_ids(self) -> None:
        registry = ParserRegistry()

        def parser(text: str, site: SiteConfig) -> ParsedRecord:
            return ParsedRecord(site.name, site.url, 210, ())

        registry.register("one", parser)
        with self.assertRaisesRegex(ConfigurationError, "重复注册"):
            registry.register("one", parser)
        with self.assertRaisesRegex(ConfigurationError, "未注册"):
            registry.parse("document", config("missing"))

    def test_every_text_parser_has_registered_history_collector(self) -> None:
        network_only = {"image_bb48kk", "image_tuku2135", "kunnan_magazine"}
        missing = [
            parser_id
            for parser_id in DEFAULT_REGISTRY.parser_ids
            if parser_id not in network_only
            and not DEFAULT_REGISTRY.has_candidate_collector(parser_id)
        ]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
