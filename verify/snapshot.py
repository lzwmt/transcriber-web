"""snapshot：对隔离实例取页面可访问性快照 + 截图，落到 `verify-out/`。

默认页面 `/`，`--page <路径>` 可指定；只读页面、不点不改（本批子命令无破坏性操作）。
"""

from __future__ import annotations

import datetime
import json
import urllib.error
from pathlib import Path

from verify import OUT_DIR
from verify.browser import chromium_page
from verify.instance import IsolatedInstance, isolated_instance, request_bytes


def snapshot_command(page_path: str = "/") -> int:
    """取一次快照，返回 0 成功 / 1 页面不可用。"""
    path = _normalize(page_path)
    with isolated_instance() as inst:
        try:
            status, raw = request_bytes(inst.base_url, path, timeout=15)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"页面 {path} 不可达：{type(exc).__name__}: {exc}")
            return 1
        html = raw.decode(errors="replace")
        with chromium_page(color_scheme="light") as browser_page:
            response = browser_page.goto(inst.base_url + path, wait_until="load")
            http_status = None if response is None else response.status
            if http_status is None or http_status >= 400:
                print(f"页面 {path} HTTP {http_status}（HTTP 探测为 {status}）")
                return 1
            browser_page.wait_for_load_state("load")
            aria = browser_page.locator("body").aria_snapshot()
            title = browser_page.title()
            shot = browser_page.screenshot(full_page=True)
            final_url = browser_page.url
        if not shot.startswith(b"\x89PNG") or len(shot) < 1000:
            print(f"页面 {path} 截图不是有效 PNG（{len(shot)} 字节）")
            return 1
        stamp = _stamp()
        target = _new_dir(stamp)
        (target / "page.txt").write_text(html, encoding="utf-8")
        (target / "aria.txt").write_text(aria + "\n", encoding="utf-8")
        (target / "shot.png").write_bytes(shot)
        meta = {
            "page": path,
            "url": final_url,
            "http_status": http_status,
            "title": title,
            "html_bytes": len(html),
            "screenshot_bytes": len(shot),
            "port": inst.port,
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        }
        (target / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"页面={path} HTTP={http_status} title={title}")
        print(
            f"html_bytes={len(html)} screenshot_bytes={len(shot)} "
            f"aria_lines={len(aria.splitlines())}"
        )
        print(f"快照目录={target.relative_to(OUT_DIR.parent)}")
    return 0


def _normalize(page_path: str) -> str:
    path = page_path.strip() or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


def _stamp() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


def _new_dir(stamp: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / f"snapshot-{stamp}"
    n = 2
    while target.exists():
        target = OUT_DIR / f"snapshot-{stamp}-{n}"
        n += 1
    target.mkdir(parents=True)
    return target


__all__ = ["snapshot_command", "IsolatedInstance"]
