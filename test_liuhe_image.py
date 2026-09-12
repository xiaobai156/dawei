import copy
import gzip
import hashlib
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from dawei.application.scrape_service import ScrapeService
from dawei.domain.errors import ScrapeError
from dawei.domain.models import SiteConfig
from dawei.infrastructure import http_client, image_client
from dawei.parsers import DEFAULT_REGISTRY
from dawei.parsers.image_36 import resolve_image_url

SITE = SiteConfig(
    name="六合王", site_id="site_a890f7ad000724880a84",
    url="https://aa.2135a.cc:1888/picart?title=六合王36码特围&color=0&name=6hwtw.jpg&id=171",
    parser_id="image_tuku2135", image_decoder="tuku2135_ocr", position="none",
    section_keywords=("六合王",), keywords=("36码特围",), fixed_issue=255,
)
YEAR = datetime.now(timezone(timedelta(hours=8))).year
IMAGE_URL = f"https://amtk.tuku99988.com/galleryfiles/system/big-pic/col/{YEAR}/255/6hwtw.jpg"
ROWS = [
    "01,02,03,04,06,07,08,10,13", "14,15,16,20,21,22,23,24,26",
    "28,29,30,31,32,33,34,35,37", "38,39,40,41,45,46,47,48,49",
]


def document():
    return {
        "image_url": IMAGE_URL, "image_sha256": hashlib.sha256(b"image").hexdigest(),
        "rec_texts": ["六合王36碼特圍", "王", *ROWS, "第255期", "254期开奖结果26-44-35-06-16-10特02蛇"],
        "rec_scores": [0.99] * 8,
        "rec_boxes": [[100, 100 + i * 100, 900, 150 + i * 100] for i in range(8)],
    }


def parse(data, config=SITE):
    return DEFAULT_REGISTRY.parse(json.dumps(data, ensure_ascii=False), config)


class LiuheImageTests(unittest.TestCase):
    def test_ocr_adapter_reuses_cpu_engine_and_cleans_temp_images(self):
        factory = Mock()
        paths = []

        def predict(path):
            self.assertEqual(Path(path).read_bytes(), b"image")
            paths.append(Path(path))
            return [document()]

        factory.return_value.predict.side_effect = predict
        image_client._engine.cache_clear()
        try:
            with patch.dict("sys.modules", {"paddleocr": SimpleNamespace(PaddleOCR=factory)}):
                for _ in range(2):
                    self.assertEqual(image_client.ocr_image(b"image")["rec_texts"], document()["rec_texts"])
            factory.assert_called_once()
            self.assertEqual(factory.call_args.kwargs["device"], "cpu")
            self.assertTrue(all(not path.exists() for path in paths))
        finally:
            image_client._engine.cache_clear()
        with patch.object(image_client, "_engine", side_effect=ImportError("missing")), \
                self.assertRaisesRegex(ScrapeError, "PaddleOCR CPU"):
            image_client.ocr_image(b"image")

    def test_source_transport_decodes_compressed_data_and_reports_errors(self):
        with patch.object(http_client, "fetch_raw", return_value=(gzip.compress(b"image"), "", "gzip")):
            self.assertEqual(http_client.fetch_bytes(IMAGE_URL), b"image")
        with patch.object(http_client, "fetch_raw", return_value=(b"bad", "", "gzip")), \
                self.assertRaises(ScrapeError):
            http_client.fetch_bytes(IMAGE_URL)
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = Message()
        response.headers["Content-Encoding"] = "gzip"
        response.read.return_value = gzip.compress(b'{"data": []}')
        with patch.object(http_client, "open_url_with_retries", return_value=response) as open_url:
            self.assertEqual(http_client.post_json(SITE.url, {"type": "am"}), {"data": []})
            self.assertEqual(json.loads(open_url.call_args.args[0].data), {"type": "am"})
            response.read.return_value = b"bad"
            with self.assertRaises(ScrapeError):
                http_client.post_json(SITE.url, {})

    def test_source_resolution_guards_identity_and_preserves_padded_issue(self):
        payload = {"data": [{"year": YEAR, "qi": "001", "type": "am"}]}
        self.assertIn("/001/", resolve_image_url(payload, replace(SITE, fixed_issue=1)))
        payload["data"].append({"year": YEAR, "qi": "1", "type": "am"})
        with self.assertRaises(ScrapeError):
            resolve_image_url(payload, replace(SITE, fixed_issue=1))
        for config in (replace(SITE, fixed_issue=None), replace(SITE, url=SITE.url.replace("id=171", "id=172"))):
            with self.assertRaises(ScrapeError):
                resolve_image_url({"data": [{"year": YEAR, "qi": "255", "type": "am"}]}, config)
        with patch.object(http_client, "post_json") as fetch:
            self.assertIsNone(ScrapeService().execute(replace(SITE, fixed_issue=None)).result)
            fetch.assert_not_called()

    def test_real_layout_keeps_raw_evidence_and_order(self):
        result = parse(document())
        self.assertEqual(result.issue, 255)
        self.assertEqual(result.numbers, tuple(",".join(ROWS).split(",")))
        self.assertEqual(result.evidence.anchor_line, "六合王36碼特圍")
        self.assertEqual(result.evidence.raw_issue_line, "第255期")
        self.assertEqual(result.evidence.raw_number_lines, tuple(ROWS))
        self.assertIn(hashlib.sha256(b"image").hexdigest(), result.evidence.document_id)
        self.assertEqual(result.record_path, IMAGE_URL)

    def test_ocr_order_is_reconstructed_from_coordinates(self):
        data = document()
        for key in ("rec_texts", "rec_boxes", "rec_scores"):
            data[key].reverse()
        self.assertEqual(parse(data).numbers, parse(document()).numbers)

    def test_wrong_missing_or_conflicting_issue_and_title_fail(self):
        changes = [(0, "全网36碼特圍"), (0, "六合王12碼特圍"), (0, ""),
                   (6, "第254期"), (6, "第256期"), (6, ""), (7, "第254期"),
                   (7, "第255期"), (7, "六合王36碼特圍")]
        for index, value in changes:
            with self.subTest(value=value):
                data = document()
                data["rec_texts"][index] = value
                with self.assertRaises(ScrapeError):
                    parse(data)

    def test_wrong_count_values_and_outside_rows_fail(self):
        for value in ("", "01,02,03", ROWS[2], ROWS[2].replace("28", "50"),
                      ROWS[2].replace("28", "00"), ROWS[2] + ",42"):
            data = document()
            data["rec_texts"][3] = value
            with self.subTest(value=value), self.assertRaises(ScrapeError):
                parse(data)
        data = document()
        data["rec_texts"][7] = ROWS[0]
        with self.assertRaises(ScrapeError):
            parse(data)

    def test_uncertain_or_malformed_ocr_fails(self):
        changes = [("rec_scores", 2, 0.5), ("rec_scores", 0, float("nan")),
                   ("rec_boxes", 3, [100, 310, 900, 350]),
                   ("rec_boxes", 2, [100, 300, 90, 350]), ("rec_boxes", 0, [])]
        for key, index, value in changes:
            data = document()
            data[key][index] = value
            with self.subTest(key=key, value=value), self.assertRaises(ScrapeError):
                parse(data)
        data = document()
        data["rec_scores"].pop()
        with self.assertRaises(ScrapeError):
            parse(data)

    def test_wrong_source_direction_or_missing_requested_issue_fails(self):
        for key, value in (("image_url", IMAGE_URL.replace("6hwtw", "other")),
                           ("image_url", IMAGE_URL.replace("/255/", "/254/")),
                           ("image_sha256", "")):
            data = document()
            data[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ScrapeError):
                parse(data)
        for config in (replace(SITE, position="top"), replace(SITE, fixed_issue=None)):
            with self.assertRaises(ScrapeError):
                parse(document(), config)

    def test_real_service_route_downloads_only_requested_image(self):
        payload = {"data": [
            {"year": YEAR - 1, "qi": "255", "type": "am"},
            {"year": YEAR, "qi": "256", "type": "am"},
            {"year": YEAR, "qi": "255", "type": "am"},
        ]}
        with patch("dawei.infrastructure.http_client.post_json", return_value=payload), \
                patch("dawei.infrastructure.http_client.fetch_bytes", return_value=b"image") as fetch, \
                patch("dawei.infrastructure.image_client.ocr_image", return_value=document()), \
                patch("dawei.infrastructure.browser_client.fetch_rendered_text") as browser:
            result = ScrapeService().execute(SITE)
        self.assertIsNotNone(result.result, result.error)
        self.assertEqual(result.result.issue, 255)
        self.assertEqual(result.source_kind, "图片OCR")
        self.assertIn("rec_texts", result.document)
        fetch.assert_called_once_with(IMAGE_URL, timeout=20, extra_headers={"Referer": SITE.url})
        browser.assert_not_called()

    def test_missing_current_year_period_never_downloads_another_image(self):
        payloads = [{}, {"data": []}, {"data": [None]}, {"data": [
            {"year": YEAR - 1, "qi": "255", "type": "am"},
            {"year": YEAR, "qi": "254", "type": "am"},
        ]}]
        for payload in payloads:
            with self.subTest(payload=payload), \
                    patch("dawei.infrastructure.http_client.post_json", return_value=payload), \
                    patch("dawei.infrastructure.http_client.fetch_bytes") as fetch:
                result = ScrapeService().execute(SITE)
                self.assertIsNone(result.result)
                fetch.assert_not_called()

    def test_bad_image_does_not_fall_back_to_browser(self):
        data = copy.deepcopy(document())
        data["rec_texts"][6] = "第254期"
        with patch("dawei.infrastructure.http_client.post_json", return_value={"data": [
            {"year": YEAR, "qi": "255", "type": "am"},
        ]}), patch("dawei.infrastructure.http_client.fetch_bytes", return_value=b"image"), \
                patch("dawei.infrastructure.image_client.ocr_image", return_value=data):
            browser = Mock()
            result = ScrapeService(rendered_text_fetcher=browser).execute(SITE)
        self.assertIsNone(result.result)
        browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
