"""Image conversion and OCR adapters used by image-backed sites."""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import parse_qs, urlsplit

from dawei.domain.errors import ScrapeError, ValidationError
from dawei.domain.models import SiteConfig
from dawei.domain.validation import validate_36_numbers

from . import http_client


PostJson = Callable[[str, dict[str, object], int], object]


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def jpeg_to_bmp_bytes(jpeg: bytes) -> bytes:
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    if not powershell:
        raise ScrapeError("PowerShell is required to decode image JPEG data")
    with tempfile.TemporaryDirectory(prefix="scrape36_") as temp_dir:
        input_path = Path(temp_dir) / "input.jpg"
        output_path = Path(temp_dir) / "output.bmp"
        input_path.write_bytes(jpeg)
        command = (
            "$ErrorActionPreference='Stop';"
            "Add-Type -AssemblyName System.Drawing;"
            f"$img=[System.Drawing.Image]::FromFile({_powershell_quote(str(input_path))});"
            "try {"
            f"$img.Save({_powershell_quote(str(output_path))}, "
            "[System.Drawing.Imaging.ImageFormat]::Bmp)"
            "} finally { $img.Dispose() }"
        )
        completed = subprocess.run(
            [powershell, "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()
            raise ScrapeError(f"failed to decode JPEG image: {detail}")
        return output_path.read_bytes()


def _tesseract_cmd() -> str:
    candidates = (
        shutil.which("tesseract"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    )
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise ScrapeError("Tesseract OCR is not installed or not in PATH")


def _validated_ocr_numbers(values: list[str], prefix: str) -> tuple[str, ...]:
    try:
        return validate_36_numbers(values)
    except ValidationError as exc:
        raise ScrapeError(f"{prefix}: {exc}") from exc


def ocr_bb48kk_image_numbers(image_bytes: bytes) -> tuple[str, ...]:
    try:
        from PIL import Image
        import pytesseract
    except ImportError as exc:
        raise ScrapeError("Pillow and pytesseract are required for OCR image sites") from exc
    pytesseract.pytesseract.tesseract_cmd = _tesseract_cmd()
    image = Image.open(BytesIO(image_bytes)).convert("RGB")
    image = image.resize((image.width * 2, image.height * 2))
    text = pytesseract.image_to_string(image, lang="eng", config="--psm 6")
    numeric_rows: list[list[str]] = []
    for line in text.splitlines():
        numbers = [
            number
            for number in re.findall(r"\d{2}", line)
            if 1 <= int(number) <= 49
        ]
        if len(numbers) >= 10:
            numeric_rows.append(numbers)
    if len(numeric_rows) < 2:
        raise ScrapeError("OCR image did not contain enough numeric rows")
    return _validated_ocr_numbers(
        (numeric_rows[-2] + numeric_rows[-1])[:36],
        "OCR图片号码无效",
    )


def ocr_2135_image_numbers(image_bytes: bytes) -> tuple[str, ...]:
    try:
        from PIL import Image
        import pytesseract
    except ImportError as exc:
        raise ScrapeError("Pillow and pytesseract are required for OCR image sites") from exc
    pytesseract.pytesseract.tesseract_cmd = _tesseract_cmd()
    image = Image.open(BytesIO(image_bytes)).convert("RGB")
    scale_x = image.width / 1000
    scale_y = image.height / 1018
    row_boxes = (
        (120, 300, 860, 348),
        (120, 426, 860, 474),
        (120, 552, 860, 600),
        (120, 678, 860, 726),
    )
    numbers: list[str] = []
    for left, top, right, bottom in row_boxes:
        row = image.crop(
            (
                round(left * scale_x),
                round(top * scale_y),
                round(right * scale_x),
                round(bottom * scale_y),
            )
        )
        row = row.resize((row.width * 3, row.height * 3))
        row = row.convert("L").point(lambda pixel: 0 if pixel > 180 else 255, "1")
        text = pytesseract.image_to_string(
            row,
            config="--psm 7 -c tessedit_char_whitelist=0123456789,",
        )
        numbers.extend(re.findall(r"\d{2}", text))
    return _validated_ocr_numbers(numbers, "OCR图片号码无效")


def tuku2135_issue_records(
    config: SiteConfig,
    timeout: int = 20,
    post_jsoner: PostJson | None = None,
) -> list[tuple[int, int, str]]:
    origin = f"{urlsplit(config.url).scheme}://{urlsplit(config.url).netloc}"
    post = post_jsoner or http_client.post_json
    payload = post(f"{origin}/api/qishu", {"type": "am"}, timeout)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ScrapeError("2135 issue API returned invalid data")
    records: list[tuple[int, int, str]] = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        try:
            issue = int(str(item["qi"]))
            year = int(str(item["year"]))
        except (KeyError, TypeError, ValueError):
            continue
        records.append((issue, year, str(item["qi"])))
    if not records:
        raise ScrapeError("2135 issue API returned no issues")
    return records


def tuku2135_image_url(config: SiteConfig, issue: int, year: int) -> str:
    query = {
        key: values[0]
        for key, values in parse_qs(urlsplit(config.url).query).items()
        if values
    }
    image_name = query.get("name")
    if not image_name:
        raise ScrapeError("2135 image URL missing name")
    color_index = int(query.get("color", "0") or "0")
    color_dir = "black" if color_index == 1 else "col"
    return (
        "https://amtk.tuku99988.com/galleryfiles/system/big-pic/"
        f"{color_dir}/{year}/{issue}/{image_name}"
    )
