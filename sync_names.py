#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SITES_FILE = SCRIPT_DIR / "sites_36.json"
STATE_FILE = SCRIPT_DIR / ".sync_names_state.json"
HTML_CANDIDATES = (
    SCRIPT_DIR / "大围名称.html",
    SCRIPT_DIR / "围子名称添加.html",
)


def html_file() -> Path:
    for path in HTML_CANDIDATES:
        if path.exists():
            return path
    raise FileNotFoundError("找不到 大围名称.html 或 围子名称添加.html")


def scraper_names() -> list[str]:
    payload = json.loads(SITES_FILE.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list):
        raise RuntimeError("sites_36.json 必须是站点列表")
    names: list[str] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        value = item.get("name")
        if not isinstance(value, str):
            continue
        name = value.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def parse_default_directories(html: str) -> tuple[re.Match[str], list[str]]:
    match = re.search(
        r"(const\s+DEFAULT_DIRECTORIES\s*=\s*)\[(.*?)\](\s*;)",
        html,
        flags=re.DOTALL,
    )
    if not match:
        raise RuntimeError("HTML 里找不到 DEFAULT_DIRECTORIES")

    raw_items = re.findall(r'"((?:\\.|[^"\\])*)"', match.group(2))
    names = [json.loads(f'"{item}"') for item in raw_items]
    return match, names


def parse_deleted_directories(html: str) -> set[str]:
    match = re.search(
        r"const\s+DEFAULT_DELETED_DIRECTORIES\s*=\s*(\[(?:.|\n|\r)*?\])\s*;",
        html,
    )
    if not match:
        return set()
    try:
        names = json.loads(match.group(1))
    except json.JSONDecodeError:
        return set()
    return {name for name in names if isinstance(name, str)}


def load_previous_scraper_names() -> set[str] | None:
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    names = data.get("scraper_names")
    if not isinstance(names, list):
        return None
    return {name for name in names if isinstance(name, str)}


def save_previous_scraper_names(names: list[str]) -> None:
    STATE_FILE.write_text(
        json.dumps({"scraper_names": names}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def update_default_deleted_directories(html: str, names: set[str]) -> str:
    if not names:
        return html

    match = re.search(
        r"(const\s+DEFAULT_DELETED_DIRECTORIES\s*=\s*)(\[(?:.|\n|\r)*?\])(\s*;)",
        html,
    )
    if not match:
        return html

    try:
        existing = json.loads(match.group(2))
    except json.JSONDecodeError:
        existing = []

    updated: list[str] = []
    seen: set[str] = set()
    for name in existing:
        if isinstance(name, str) and name not in seen:
            updated.append(name)
            seen.add(name)
    for name in sorted(names):
        if name not in seen:
            updated.append(name)
            seen.add(name)

    array_text = json.dumps(updated, ensure_ascii=False)
    return html[: match.start()] + match.group(1) + array_text + match.group(3) + html[match.end() :]


def install_local_cache_merge(html: str) -> str:
    if "function normalizeRootDirectories" in html:
        return html

    marker = "// sync_names.py: append missing default dirs"
    if marker in html:
        return html

    needle = "directories = JSON.parse(savedDirs);"
    replacement = (
        "directories = JSON.parse(savedDirs);\n"
        f"            {marker}\n"
        "            const beforeSyncCount = directories.length;\n"
        "            const deletedDirs = getDeletedDirs();\n"
        "            DEFAULT_DIRECTORIES.forEach(name => {\n"
        "                if (!deletedDirs.includes(name) && !directories.includes(name)) directories.push(name);\n"
        "            });\n"
        "            if (directories.length !== beforeSyncCount) {\n"
        "                localStorage.setItem('v3_dirs', JSON.stringify(directories));\n"
        "            }"
    )
    if needle not in html:
        raise RuntimeError("HTML 里找不到本地目录加载位置")
    return html.replace(needle, replacement, 1)


def sync() -> tuple[Path, list[str]]:
    target = html_file()
    html = target.read_text(encoding="utf-8")
    match, existing = parse_default_directories(html)
    current_scraper_names = scraper_names()
    previous_scraper_names = load_previous_scraper_names()
    if previous_scraper_names is None:
        previous_scraper_names = set(current_scraper_names)

    existing_set = set(existing)
    new_names = set(current_scraper_names) - previous_scraper_names
    missing = [
        name
        for name in current_scraper_names
        if name in new_names and name not in existing_set
    ]

    if missing:
        updated = existing + missing
        array_text = json.dumps(updated, ensure_ascii=False)
        html = html[: match.start()] + match.group(1) + array_text + match.group(3) + html[match.end() :]

    html = install_local_cache_merge(html)
    target.write_text(html, encoding="utf-8", newline="")
    save_previous_scraper_names(current_scraper_names)
    return target, missing


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync scraper site names into the name HTML.")
    parser.add_argument("--open", action="store_true", help="Open the HTML after syncing.")
    args = parser.parse_args()

    target, missing = sync()
    print(f"已同步: {target.name}")
    if missing:
        print(f"新增名称 {len(missing)} 个:")
        for name in missing:
            print(f"  {name}")
    else:
        print("没有缺少的名称")
    if args.open:
        os.startfile(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
