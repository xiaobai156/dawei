"""Lazy CPU OCR adapter. Returns raw recognition, without business decisions."""

from __future__ import annotations

import tempfile
import threading
from functools import lru_cache
from pathlib import Path

from dawei.domain.errors import ScrapeError

_OCR_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _engine():
    from paddleocr import PaddleOCR

    return PaddleOCR(
        lang="en", device="cpu",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )


def ocr_image(image_bytes: bytes) -> dict:
    try:
        with tempfile.TemporaryDirectory(prefix="dawei-ocr-") as directory:
            path = Path(directory) / "image.jpg"
            path.write_bytes(image_bytes)
            # One shared CPU engine; concurrent calls must not enter Paddle together.
            with _OCR_LOCK:
                result = next(iter(_engine().predict(str(path))))
                return {
                    "rec_texts": list(result["rec_texts"]),
                    "rec_scores": [float(score) for score in result["rec_scores"]],
                    "rec_boxes": [[int(value) for value in box] for box in result["rec_boxes"]],
                }
    except Exception as exc:
        raise ScrapeError(f"PaddleOCR CPU识别失败: {exc}") from exc
