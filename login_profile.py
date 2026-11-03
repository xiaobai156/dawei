"""打开本项目的持久化浏览器 profile，做一次性人工登录。

背景：部分站点正文设为「回复可见 / 登录可见」，游客抓不到正文。这类站点在
`sites_36.json` 里标 `requires_login: true`，抓取时改用 `login_profile`
指向的持久化上下文（默认是本目录下的 `browser-profile/`）。
本脚本负责把登录态写进该 profile：运行后在弹出的浏览器里登录，
回到命令行按 Enter 保存。

用法：
    py -3.11 login_profile.py                 # 打开所有需要登录的站点
    py -3.11 login_profile.py --url <URL>     # 只打开指定入口
    py -3.11 login_profile.py --list          # 只列出需要登录的站点
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dawei.infrastructure.config_repository import ConfigRepository  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_SITES_CONFIG = ROOT / "sites_36.json"


def login_sites(config_path: Path) -> list[tuple[str, str, str]]:
    sites = ConfigRepository(config_path).load()
    return [
        (site.name, site.url, site.login_profile or "")
        for site in sites
        if site.requires_login
    ]


def _login(url: str, profile_dir: str) -> None:
    from playwright.sync_api import sync_playwright

    profile = Path(profile_dir)
    profile.mkdir(parents=True, exist_ok=True)
    print(f"profile 目录：{profile}")
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            headless=False,
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        print(f"已打开：{url}")
        print("请在浏览器里完成登录，确认能看到正文后回到这里按 Enter 保存并关闭。")
        input()
        context.close()
    print("登录态已保存。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="为需要登录的站点保存登录态")
    parser.add_argument("--url", help="只打开这个入口URL")
    parser.add_argument("--profile", help="覆盖持久化目录")
    parser.add_argument("--list", action="store_true", help="只列出需要登录的站点")
    parser.add_argument("--sites-config", default=str(DEFAULT_SITES_CONFIG))
    args = parser.parse_args(argv)

    sites = login_sites(Path(args.sites_config))
    if not sites:
        print("当前配置没有 requires_login 站点。")
        return 0
    if args.list:
        for name, url, profile in sites:
            print(f"{name}\t{url}\t{profile}")
        return 0

    if args.url:
        profile = args.profile or sites[0][2]
        _login(args.url, profile)
        return 0

    for name, url, profile in sites:
        print(f"=== {name} ===")
        _login(url, args.profile or profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
