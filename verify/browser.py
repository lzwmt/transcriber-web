"""Playwright 只读已缓存的 Chromium，不下载、不装到系统。

解析顺序：仓库根 .verify-browsers/ 优先，宿主 ~/.cache/ms-playwright 兜底。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from playwright.sync_api import Page, sync_playwright

from verify import DESKTOP_HEIGHT, DESKTOP_WIDTH

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT_BROWSERS = REPO_ROOT / ".verify-browsers"
HOST_CACHE = Path.home() / ".cache" / "ms-playwright"
Scheme = Literal["light", "dark"]


def chromium_revision() -> str:
    import playwright

    root = Path(playwright.__file__).resolve().parent
    matches = sorted(root.rglob("browsers.json"))
    if not matches:
        raise RuntimeError("安装的 Playwright 里没有 browsers.json")
    data = json.loads(matches[0].read_text(encoding="utf-8"))
    for browser in data.get("browsers", []):
        if isinstance(browser, dict) and browser.get("name") == "chromium":
            revision = browser.get("revision")
            if isinstance(revision, str) and revision:
                return revision
    raise RuntimeError("browsers.json 没有 chromium revision")


def _browser_roots() -> tuple[Path, Path]:
    return (PROJECT_BROWSERS, HOST_CACHE)


def require_cached_chromium() -> str:
    """构建号必须已在项目目录或宿主缓存。缺了就停下，不触发浏览器下载。"""
    revision = chromium_revision()
    names = (f"chromium-{revision}", f"chromium_headless_shell-{revision}")
    for root in _browser_roots():
        if all((root / name / "INSTALLATION_COMPLETE").is_file() for name in names):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root)
            return revision
    missing = [
        str(root / name / "INSTALLATION_COMPLETE")
        for root in _browser_roots()
        for name in names
        if not (root / name / "INSTALLATION_COMPLETE").is_file()
    ]
    raise RuntimeError(
        f"Playwright 需要的 chromium {revision} 在项目目录 {PROJECT_BROWSERS} "
        f"与宿主缓存 {HOST_CACHE} 都不齐。"
        "禁止系统级安装，Python 不下载浏览器。"
        "补法：在仓库根执行 "
        'PLAYWRIGHT_BROWSERS_PATH="$PWD/.verify-browsers" '
        "uv run --group verify playwright install chromium"
        "；若没有 chromium_headless_shell，再执行 "
        "uv run --group verify playwright install chromium-headless-shell"
        "（同一 PLAYWRIGHT_BROWSERS_PATH）。"
        f" 缺少：{missing}"
    )


@contextmanager
def chromium_page(
    *,
    color_scheme: Scheme,
    width: int = DESKTOP_WIDTH,
    height: int = DESKTOP_HEIGHT,
    args: Sequence[str] = (),
) -> Iterator[Page]:
    require_cached_chromium()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=list(args))
        context = browser.new_context(
            viewport={"width": width, "height": height},
            color_scheme=color_scheme,
            locale="zh-CN",
            device_scale_factor=1,
        )
        page = context.new_page()
        page.set_default_timeout(15_000)
        try:
            yield page
        finally:
            context.close()
            browser.close()
