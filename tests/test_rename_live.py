"""A1/A2/A4 的端到端证据：真 uvicorn + 真 Chromium，点铅笔改名走完整条链路。

服务端契约在 tests/test_rename.py 已覆盖；这里补「页面上的铅笔确实能改名」，
排除「HTML 里有按钮但 JS 没接上」这类只在浏览器里才暴露的漏口。
"""

from __future__ import annotations

import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import uvicorn

pytest.importorskip("playwright")

from app.jobs import JobStore  # noqa: E402
from app.main import create_app  # noqa: E402
from verify.browser import chromium_page  # noqa: E402

_ISO = "%Y-%m-%dT%H:%M:%S.%fZ"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _insert_job(data: Path, name: str) -> str:
    JobStore(data / "jobs.db")
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
            (job_id, name, datetime.now(UTC).strftime(_ISO)),
        )
        conn.commit()
    finally:
        conn.close()
    return job_id


@pytest.fixture
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Path]]:
    """起一个真 uvicorn（127.0.0.1 随机端口），产出（base_url，数据目录）。"""
    monkeypatch.setenv("TRANSCRIBER_STUB", "1")
    monkeypatch.setenv("TRANSCRIBER_DATA_DIR", str(tmp_path / "data"))
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    app = create_app(data)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline:
            raise AssertionError("uvicorn 没起来")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}", data
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "uvicorn 没停干净"


def test_a1_pencil_rename_in_browser_persists_after_reload(
    live: tuple[str, Path],
) -> None:
    """A1：列表页点铅笔 → 输新名 → 回车 → 页面显示新名，刷新后仍在。"""
    base, data = live
    job_id = _insert_job(data, "浏览器原名.wav")

    with chromium_page(color_scheme="light") as page:
        page.goto(f"{base}/", wait_until="domcontentloaded")
        name_cell = page.locator(".workbench .file-name")
        assert name_cell.count() == 1
        assert name_cell.inner_text() == "浏览器原名.wav"

        page.locator("[data-rename-open]").click()
        editor = page.locator("[data-rename-editor]")
        assert editor.is_visible()
        field = page.locator("[data-rename-input]")
        assert field.input_value() == "浏览器原名.wav"

        field.fill("浏览器新名")
        field.press("Enter")
        page.wait_for_function(
            "() => document.querySelector('.workbench .file-name').textContent === '浏览器新名'"
        )
        assert not editor.is_visible()
        assert page.locator("[data-rename-error]").inner_text() == ""

        page.reload(wait_until="domcontentloaded")
        assert page.locator(".workbench .file-name").inner_text() == "浏览器新名"

        page.goto(f"{base}/jobs/{job_id}/view", wait_until="domcontentloaded")
        assert page.locator(".page-title").inner_text() == "浏览器新名"


def test_a2_detail_pencil_rename_shows_on_list(live: tuple[str, Path]) -> None:
    """A2：详情页标题旁铅笔改名，回列表页即新名。"""
    base, data = live
    job_id = _insert_job(data, "详情原名.wav")

    with chromium_page(color_scheme="light") as page:
        page.goto(f"{base}/jobs/{job_id}/view", wait_until="domcontentloaded")
        assert page.locator(".page-title").inner_text() == "详情原名.wav"

        page.locator("[data-rename-open]").click()
        field = page.locator("[data-rename-input]")
        field.fill("详情新名")
        page.locator("[data-rename-save]").click()
        page.wait_for_function(
            "() => document.querySelector('.page-title').textContent === '详情新名'"
        )

        page.goto(f"{base}/", wait_until="domcontentloaded")
        assert page.locator(".workbench .file-name").inner_text() == "详情新名"


def test_a4_empty_name_stays_in_edit_and_does_not_persist(
    live: tuple[str, Path],
) -> None:
    """A4：纯空白名被拦在本行提示里，编辑框不收起，刷新后仍是原名。"""
    base, data = live
    job_id = _insert_job(data, "空名原名.wav")

    with chromium_page(color_scheme="light") as page:
        page.goto(f"{base}/", wait_until="domcontentloaded")
        page.locator("[data-rename-open]").click()
        field = page.locator("[data-rename-input]")
        field.fill("   ")
        field.press("Enter")

        editor = page.locator("[data-rename-editor]")
        assert editor.is_visible(), "校验失败应留在编辑态"
        error = page.locator("[data-rename-error]")
        assert error.inner_text() == "名称不能为空"
        assert error.is_visible()

        page.reload(wait_until="domcontentloaded")
        assert page.locator(".workbench .file-name").inner_text() == "空名原名.wav"

    store = JobStore(data / "jobs.db")
    job = store.get(job_id)
    assert job is not None
    assert str(job["source_name"]) == "空名原名.wav"


def test_a4_input_caps_at_120_code_points(live: tuple[str, Path]) -> None:
    """A4 边界：输入框 maxlength=120 让 121 字输不进去，存下的正好 120 字。"""
    base, data = live
    job_id = _insert_job(data, "边界原名.wav")

    with chromium_page(color_scheme="light") as page:
        page.goto(f"{base}/", wait_until="domcontentloaded")
        page.locator("[data-rename-open]").click()
        field = page.locator("[data-rename-input]")
        assert field.get_attribute("maxlength") == "120"
        field.fill("字" * 121)
        assert len(field.input_value()) == 120
        field.press("Enter")
        page.wait_for_function(
            "() => document.querySelector('.workbench .file-name')"
            ".textContent.length === 120"
        )
        assert not page.locator("[data-rename-editor]").is_visible()

    store = JobStore(data / "jobs.db")
    job = store.get(job_id)
    assert job is not None
    assert str(job["source_name"]) == "字" * 120


def test_a4_escape_cancels_without_writing(live: tuple[str, Path]) -> None:
    """Esc 取消：编辑框收起、名字不变、库里也不变。"""
    base, data = live
    job_id = _insert_job(data, "取消原名.wav")

    with chromium_page(color_scheme="light") as page:
        page.goto(f"{base}/", wait_until="domcontentloaded")
        page.locator("[data-rename-open]").click()
        field = page.locator("[data-rename-input]")
        field.fill("不该保存")
        field.press("Escape")

        assert not page.locator("[data-rename-editor]").is_visible()
        assert page.locator(".workbench .file-name").inner_text() == "取消原名.wav"

    store = JobStore(data / "jobs.db")
    job = store.get(job_id)
    assert job is not None
    assert str(job["source_name"]) == "取消原名.wav"


def test_a1_rename_does_not_break_polling_after_reload(
    live: tuple[str, Path],
) -> None:
    """改名后页面仍可用：轮询不报错、用量横幅与列表都还在（回归护栏）。"""
    base, data = live
    _insert_job(data, "轮询原名.wav")
    errors: list[str] = []

    with chromium_page(color_scheme="light") as page:
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"{base}/", wait_until="domcontentloaded")
        page.locator("[data-rename-open]").click()
        page.locator("[data-rename-input]").fill("轮询新名")
        page.locator("[data-rename-input]").press("Enter")
        page.wait_for_function(
            "() => document.querySelector('.workbench .file-name').textContent === '轮询新名'"
        )
        page.wait_for_timeout(1200)
        assert errors == [], errors
        assert page.locator("#live").count() == 1
        assert page.locator("table.jobs").count() == 1


@pytest.mark.parametrize("name", ["<script>alert(1)</script>", "&<b>粗体</b>"])
def test_a7_hostile_name_renders_as_text_in_browser(
    live: tuple[str, Path], name: str
) -> None:
    """A7 端到端：恶意名存进库后，列表与详情都按文本显示，不执行、不建节点。"""
    base, data = live
    job_id = _insert_job(data, name)

    with chromium_page(color_scheme="light") as page:
        alerts: list[str] = []

        def _dismiss(dialog: object) -> None:
            message = getattr(dialog, "message", "")
            alerts.append(str(message))
            dismiss = getattr(dialog, "dismiss", None)
            if callable(dismiss):
                dismiss()

        page.on("dialog", _dismiss)
        page.goto(f"{base}/", wait_until="domcontentloaded")
        assert page.locator(".workbench .file-name").inner_text() == name
        assert page.locator("script", has_text="alert(1)").count() == 0
        assert page.locator("b").count() == 0

        page.goto(f"{base}/jobs/{job_id}/view", wait_until="domcontentloaded")
        assert page.locator(".page-title").inner_text() == name
        assert page.locator("script", has_text="alert(1)").count() == 0
        assert page.locator("b").count() == 0
        assert alerts == [], alerts
