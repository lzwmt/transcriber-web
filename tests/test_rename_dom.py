"""A7 的 DOM 证据：`<script>` 标题在真实浏览器里只作文本，不产生 script 节点。

服务端转义已由 tests/test_rename.py::test_a7_* 覆盖；这里把同一份服务端渲染结果
交给 Chromium 解析，直接读 DOM，排除「HTML 里转义了但浏览器仍执行」的漏口。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("playwright")

from app.jobs import JobStore  # noqa: E402
from app.main import create_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from verify.browser import chromium_page  # noqa: E402

_ISO = "%Y-%m-%dT%H:%M:%S.%fZ"
PAYLOAD = "<script>alert(1)</script>"


def _job_with_script_name(data: Path) -> str:
    store = JobStore(data / "jobs.db")
    job_id = uuid4().hex
    conn = sqlite3.connect(data / "jobs.db")
    try:
        conn.execute(
            """
            INSERT INTO jobs (
                id, status, tier, source_name, source_kind, source_path, url,
                created_at, minutes_status
            ) VALUES (?, 'done', 'fast', ?, 'upload', NULL, NULL, ?, 'done')
            """,
            (job_id, PAYLOAD, datetime.now(UTC).strftime(_ISO)),
        )
        conn.commit()
    finally:
        conn.close()
    assert store.get(job_id) is not None
    return job_id


@pytest.fixture
def pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """返回（列表页 HTML，详情页 HTML），两份都是真实服务端渲染结果。"""
    data = tmp_path / "data"
    monkeypatch.setenv("TRANSCRIBER_STUB", "1")
    monkeypatch.setenv("TRANSCRIBER_DATA_DIR", str(data))
    app = create_app(data)
    with TestClient(app) as client:
        job_id = _job_with_script_name(data)
        home = client.get("/").text
        detail = client.get(f"/jobs/{job_id}/view").text
        assert PAYLOAD in client.get(f"/jobs/{job_id}").json()["source_name"]
    return home, detail


@pytest.mark.parametrize("which", ["list", "detail"])
def test_a7_dom_has_no_script_node_and_name_is_text(
    pages: tuple[str, str], which: str
) -> None:
    """A7：列表页与详情页 DOM 里没有该 script 节点，标题按文本呈现。"""
    html = pages[0] if which == "list" else pages[1]
    with chromium_page(color_scheme="light") as page:
        page.set_content(html, wait_until="domcontentloaded")
        assert page.locator("script", has_text="alert(1)").count() == 0
        assert page.locator("script").filter(has_text="alert(1)").count() == 0

        if which == "list":
            cell = page.locator(".workbench .file-name")
            assert cell.count() == 1
            assert cell.inner_text() == PAYLOAD
        else:
            title = page.locator(".page-title")
            assert title.count() == 1
            assert title.inner_text() == PAYLOAD


def test_a7_rename_editor_markup_is_present_for_both_pages(
    pages: tuple[str, str],
) -> None:
    """A1/A2 的 DOM 前提：两页都有铅笔按钮与就地编辑框，且编辑框初始隐藏。"""
    with chromium_page(color_scheme="light") as page:
        for html in pages:
            page.set_content(html, wait_until="domcontentloaded")
            assert page.locator("[data-rename-open]").count() == 1
            editor = page.locator("[data-rename-editor]")
            assert editor.count() == 1
            assert not editor.is_visible()
            assert page.locator("[data-rename-input]").count() == 1
