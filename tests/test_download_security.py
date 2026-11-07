"""A3 下载安全测试：异常日志仅记类型与摘要（无堆栈）、下载文件名常量白名单静态断言与清洗测试。"""

from __future__ import annotations

import ast
import logging
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from app.jobs import JobStore
from app.main import (
    ALLOWED_DOWNLOAD_FILENAMES,
    _download,
    create_app,
    sanitize_download_filename,
)
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def test_sanitize_download_filename_crlf_and_path_traversal() -> None:
    # 正常常量文件名保留
    assert sanitize_download_filename("transcript.md") == "transcript.md"
    assert sanitize_download_filename("segments.json") == "segments.json"
    assert sanitize_download_filename("minutes.md") == "minutes.md"
    assert sanitize_download_filename("combined.md") == "combined.md"

    # CRLF 换行注入清洗
    assert (
        sanitize_download_filename("transcript.md\r\nSet-Cookie: evil=1")
        == "transcript.mdSet-Cookie: evil=1"
    )
    assert sanitize_download_filename("test\r\n.md") == "test.md"
    assert sanitize_download_filename("\n\rsegments.json\r\n") == "segments.json"

    # 路径穿越清洗（仅取文件名 base）
    assert sanitize_download_filename("../../etc/passwd") == "passwd"
    assert sanitize_download_filename("/root/secret.key") == "secret.key"


def test_download_endpoints_filenames_are_constant_whitelist() -> None:
    """静态断言：app/main.py 下载端点中使用的文件名只许取常量集合。"""
    main_path = ROOT / "app" / "main.py"
    tree = ast.parse(main_path.read_text(encoding="utf-8"))

    # 收集在 main.py 路由定义中传递给 _download 或 FileResponse 的文件名常量字面量
    collected_filenames: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # 1. 检查 _download(..., "filename", ...)
        if isinstance(node.func, ast.Name) and node.func.id == "_download":
            if len(node.args) >= 4 and isinstance(node.args[3], ast.Constant):
                val = str(node.args[3].value)
                collected_filenames.add(val)
        # 2. 检查 FileResponse(..., filename="filename")
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "FileResponse"
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "FileResponse")
        ):
            for kw in node.keywords:
                if kw.arg == "filename" and isinstance(kw.value, ast.Constant):
                    collected_filenames.add(str(kw.value.value))
        # 3. 检查 headers 里的 filename="combined.md"
        if isinstance(node.func, ast.Name) and node.func.id == "Response":
            for kw in node.keywords:
                if kw.arg == "headers" and isinstance(kw.value, ast.Dict):
                    for v in kw.value.values:
                        if isinstance(v, ast.Constant) and "filename=" in str(v.value):
                            raw = str(v.value)
                            start = raw.find('filename="') + len('filename="')
                            end = raw.find('"', start)
                            if end != -1:
                                collected_filenames.add(raw[start:end])

    assert collected_filenames == {"transcript.md", "segments.json", "minutes.md", "combined.md"}
    assert collected_filenames <= ALLOWED_DOWNLOAD_FILENAMES

    # 运行时断言：非法文件名被拒绝且返回 404
    dummy_store = MagicMock(spec=JobStore)
    resp = _download(
        dummy_store,
        ROOT,
        "0123456789abcdef0123456789abcdef",
        "evil.exe",
        "application/octet-stream",
    )
    assert resp.status_code == 404

    # S8: 不在白名单集合内一律拒（如 ../transcript.md 不再清洗放行，直接 404）
    resp_traversal = _download(
        dummy_store,
        ROOT,
        "0123456789abcdef0123456789abcdef",
        "../transcript.md",
        "text/markdown; charset=utf-8",
    )
    assert resp_traversal.status_code == 404


def test_download_exception_logs_type_and_summary_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """下载异常时：logger.error 仅记类型与摘要，无堆栈入日志，保留 404 状态码。"""
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    job_id = "0123456789abcdef0123456789abcdef"

    # 模拟 store.get 抛出异常
    def boom(_id: str) -> dict[str, str]:
        raise RuntimeError("模拟底层存储损坏")

    monkeypatch.setattr(store, "get", boom)

    with caplog.at_level(logging.ERROR):
        resp = _download(
            store,
            data,
            job_id,
            "transcript.md",
            "text/markdown; charset=utf-8",
        )

    # 1. 状态码与响应体保留既有 404 口径
    assert resp.status_code == 404

    # 2. 日志断言：只记类型与摘要，无堆栈
    matching_records = [
        r for r in caplog.records if r.levelname == "ERROR" and "download failed" in r.message
    ]
    assert len(matching_records) == 1
    record = matching_records[0]
    assert "download failed: RuntimeError: 模拟底层存储损坏" == record.message
    assert record.exc_info is None

    # 日志全文中不包含 Traceback
    assert "Traceback (most recent call last)" not in caplog.text

    # S7: 异常信息先过 redact_sensitive 再记入日志
    def boom_with_secret(_id: str) -> dict[str, str]:
        raise RuntimeError("读取失败：http://example.com/api?token=SUPER_SECRET_TOKEN")

    monkeypatch.setattr(store, "get", boom_with_secret)
    with caplog.at_level(logging.ERROR):
        caplog.clear()
        resp2 = _download(
            store,
            data,
            job_id,
            "transcript.md",
            "text/markdown; charset=utf-8",
        )
    assert resp2.status_code == 404
    sensitive_records = [
        r for r in caplog.records if r.levelname == "ERROR" and "download failed" in r.message
    ]
    assert len(sensitive_records) == 1
    assert "SUPER_SECRET_TOKEN" not in sensitive_records[0].message
    assert "<url>" in sensitive_records[0].message


def _mark_done(db_path: Path, job_id: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (job_id,))
        conn.commit()
    finally:
        conn.close()


def test_download_cross_job_symlink_is_404(tmp_path: Path) -> None:
    """别的任务目录里的文件，经符号链接也不能从本任务下载。"""
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    victim = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    attacker = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    for job_id in (victim, attacker):
        store.create_job(
            job_id=job_id,
            tier="fast",
            source_name="a.wav",
            source_kind="upload",
            source_path=None,
            url=None,
        )
        _mark_done(data / "jobs.db", job_id)
    victim_dir = data / "jobs" / victim
    attacker_dir = data / "jobs" / attacker
    victim_dir.mkdir(parents=True)
    attacker_dir.mkdir(parents=True)
    secret = "VICTIM_JOB_TRANSCRIPT"
    (victim_dir / "transcript.md").write_text(secret, encoding="utf-8")
    (attacker_dir / "transcript.md").symlink_to(victim_dir / "transcript.md")

    resp = _download(store, data, attacker, "transcript.md", "text/markdown; charset=utf-8")
    assert resp.status_code == 404
    assert secret not in resp.body.decode()


def test_download_jobs_root_traversal_symlink_is_404(tmp_path: Path) -> None:
    """解析后落到本任务目录之外（仍在 jobs 根下）一律 404。"""
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    job_id = "cccccccccccccccccccccccccccccccc"
    store.create_job(
        job_id=job_id,
        tier="fast",
        source_name="a.wav",
        source_kind="upload",
        source_path=None,
        url=None,
    )
    _mark_done(data / "jobs.db", job_id)
    planted = data / "jobs" / "planted.txt"
    planted.parent.mkdir(parents=True, exist_ok=True)
    marker = "JOBS_ROOT_ESCAPE_MARKER"
    planted.write_text(marker, encoding="utf-8")
    job_dir = data / "jobs" / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "segments.json").symlink_to(Path("..") / "planted.txt")

    resp = _download(store, data, job_id, "segments.json", "application/json; charset=utf-8")
    assert resp.status_code == 404
    assert marker not in resp.body.decode()


def test_download_file_inside_job_dir_still_ok(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    job_id = "dddddddddddddddddddddddddddddddd"
    store.create_job(
        job_id=job_id,
        tier="fast",
        source_name="a.wav",
        source_kind="upload",
        source_path=None,
        url=None,
    )
    _mark_done(data / "jobs.db", job_id)
    job_dir = data / "jobs" / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "transcript.md").write_text("正常逐字稿", encoding="utf-8")

    resp = _download(store, data, job_id, "transcript.md", "text/markdown; charset=utf-8")
    assert isinstance(resp, FileResponse)
    assert resp.status_code == 200
    assert Path(resp.path).read_text(encoding="utf-8") == "正常逐字稿"


def test_download_endpoint_via_testclient_error_logging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """通过 HTTP 端点 GET /jobs/{id}/transcript 触发异常验证日志无堆栈。"""
    data = tmp_path / "data"
    app = create_app(data)
    store = app.state.store

    def explode(_id: str) -> None:
        raise OSError("磁盘读错误")

    monkeypatch.setattr(store, "get", explode)

    with caplog.at_level(logging.ERROR):
        with TestClient(app) as client:
            resp = client.get("/jobs/0123456789abcdef0123456789abcdef/transcript")
            assert resp.status_code == 404
            assert resp.json() == {"error": "结果暂不可用"}

    matching = [
        r for r in caplog.records if r.levelname == "ERROR" and "download failed" in r.message
    ]
    assert len(matching) >= 1
    for rec in matching:
        assert rec.exc_info is None
        assert "download failed: OSError: 磁盘读错误" in rec.message
    assert "Traceback" not in caplog.text
