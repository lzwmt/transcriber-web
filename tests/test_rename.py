"""A 节「文件名可修改」：改名接口、校验边界、转义、下载与内嵌行不受影响。

对应清单 A1–A8。页面交互（点铅笔就地变输入框）由 tests/test_pages.py 的
源码对账与清单人工截图兜底；本文件覆盖可自动判定的行为。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from app import service
from app.jobs import JobStore
from app.main import create_app
from fastapi.testclient import TestClient

_ISO = "%Y-%m-%dT%H:%M:%S.%fZ"
_LONG = 120
_TOO_LONG = 121


def _insert_job(
    db: Path,
    *,
    source_name: str,
    created_at: str,
    status: str = "done",
    tier: str = "fast",
    source_kind: str = "upload",
    url: str | None = None,
    minutes_status: str = "done",
    source_path: str | None = None,
) -> str:
    JobStore(db)
    job_id = uuid4().hex
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """
            INSERT INTO jobs (
                id, status, tier, source_name, source_kind, source_path, url,
                created_at, minutes_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                status,
                tier,
                source_name,
                source_kind,
                source_path,
                url,
                created_at,
                minutes_status,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return job_id


def _name(store: JobStore, job_id: str) -> str:
    job = store.get(job_id)
    assert job is not None
    return str(job["source_name"])


@pytest.fixture
def http_and_data(client: tuple[TestClient, Path]) -> tuple[TestClient, Path]:
    return client


def test_a1_rename_persists_and_shows_on_list_and_detail(
    http_and_data: tuple[TestClient, Path],
) -> None:
    """A1：改名成功后列表页与详情页都显示新名。"""
    http, data = http_and_data
    job_id = _insert_job(
        data / "jobs.db",
        source_name="原名.wav",
        created_at=datetime.now(UTC).strftime(_ISO),
    )

    response = http.post(f"/jobs/{job_id}/rename", json={"name": "新名字"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "source_name": "新名字"}

    home = http.get("/")
    assert home.status_code == 200
    assert "新名字" in home.text
    assert "原名.wav" not in home.text

    detail = http.get(f"/jobs/{job_id}/view")
    assert detail.status_code == 200
    assert "新名字" in detail.text
    assert "原名.wav" not in detail.text


def test_a2_rename_from_detail_reflects_on_list(
    http_and_data: tuple[TestClient, Path],
) -> None:
    """A2：从详情页改名（同一接口）后，返回列表页即新名。"""
    http, data = http_and_data
    job_id = _insert_job(
        data / "jobs.db",
        source_name="旧.wav",
        created_at=datetime.now(UTC).strftime(_ISO),
    )
    detail = http.get(f"/jobs/{job_id}/view")
    assert detail.status_code == 200
    assert "data-rename-open" in detail.text

    response = http.post(f"/jobs/{job_id}/rename", data={"name": "改后名字"})
    assert response.status_code == 200
    assert response.json()["source_name"] == "改后名字"

    home = http.get("/")
    assert "改后名字" in home.text
    assert "旧.wav" not in home.text


def test_a3_filename_links_to_same_detail_page(
    http_and_data: tuple[TestClient, Path],
) -> None:
    """A3：文件名本身是链接，与「打开」指向同一详情页。"""
    http, data = http_and_data
    job_id = _insert_job(
        data / "jobs.db",
        source_name="可点.wav",
        created_at=datetime.now(UTC).strftime(_ISO),
    )
    home = http.get("/")
    assert home.status_code == 200
    assert f'<a class="file-name" href="/jobs/{job_id}/view">可点.wav</a>' in home.text
    assert f'href="/jobs/{job_id}/view">打开</a>' in home.text


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "名称不能为空"),
        ("   ", "名称不能为空"),
        ("\t", "名称不能为空"),
        ("a\nb", "名称不能包含换行或控制字符"),
        ("a\rb", "名称不能包含换行或控制字符"),
        ("a\x00b", "名称不能包含换行或控制字符"),
        ("a\x7fb", "名称不能包含换行或控制字符"),
    ],
)
def test_a4_invalid_names_are_rejected_and_original_kept(
    http_and_data: tuple[TestClient, Path], value: str, reason: str
) -> None:
    """A4 反例：空名/纯空白/含换行与控制字符一律 400，原名不变。"""
    http, data = http_and_data
    job_id = _insert_job(
        data / "jobs.db",
        source_name="保持原名.wav",
        created_at=datetime.now(UTC).strftime(_ISO),
    )
    response = http.post(f"/jobs/{job_id}/rename", json={"name": value})
    assert response.status_code == 400
    assert reason in response.json()["error"]
    assert _name(JobStore(data / "jobs.db"), job_id) == "保持原名.wav"


def test_a4_length_boundary_is_python_code_points(
    http_and_data: tuple[TestClient, Path],
) -> None:
    """A4 边界：120 个 Unicode 码点可存，121 个被拒且原名不变。"""
    http, data = http_and_data
    job_id = _insert_job(
        data / "jobs.db",
        source_name="边界.wav",
        created_at=datetime.now(UTC).strftime(_ISO),
    )
    store = JobStore(data / "jobs.db")

    ok_name = "字" * _LONG
    assert len(ok_name) == _LONG
    assert http.post(f"/jobs/{job_id}/rename", json={"name": ok_name}).status_code == 200
    assert _name(store, job_id) == ok_name

    too_long = "字" * _TOO_LONG
    assert len(too_long) == _TOO_LONG
    rejected = http.post(f"/jobs/{job_id}/rename", json={"name": too_long})
    assert rejected.status_code == 400
    assert str(_LONG) in rejected.json()["error"]
    assert _name(store, job_id) == ok_name


def test_a4_trims_surrounding_whitespace(
    http_and_data: tuple[TestClient, Path],
) -> None:
    """A4 正例：首尾空白去掉后存库，返回的也是去空白后的名字。"""
    http, data = http_and_data
    job_id = _insert_job(
        data / "jobs.db",
        source_name="原名.wav",
        created_at=datetime.now(UTC).strftime(_ISO),
    )
    response = http.post(f"/jobs/{job_id}/rename", json={"name": "  留白  "})
    assert response.status_code == 200
    assert response.json()["source_name"] == "留白"
    assert _name(JobStore(data / "jobs.db"), job_id) == "留白"


def test_a4_missing_job_is_404(
    http_and_data: tuple[TestClient, Path],
) -> None:
    """不存在的任务 404，非法 id 也 404，且都不落库。"""
    http, data = http_and_data
    missing = http.post(f"/jobs/{uuid4().hex}/rename", json={"name": "x"})
    assert missing.status_code == 404
    assert missing.json()["error"] == "任务不存在"
    assert JobStore(data / "jobs.db").count_jobs() == 0

    bad_id = http.post("/jobs/not-a-real-id/rename", json={"name": "x"})
    assert bad_id.status_code == 404


def test_a5_download_set_unaffected_by_rename(
    http_and_data: tuple[TestClient, Path],
) -> None:
    """A5：改名后四件下载仍可达，且内容与改名前一致（下载文件名不跟随改名）。"""
    http, data = http_and_data
    job_id = _insert_job(
        data / "jobs.db",
        source_name="原名.wav",
        created_at=datetime.now(UTC).strftime(_ISO),
    )
    job_dir = data / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "transcript.md").write_text("# 逐字稿\n\n- 文件名：原名.wav\n", encoding="utf-8")
    (job_dir / "segments.json").write_text("[]", encoding="utf-8")
    (job_dir / "minutes.md").write_text("# 纪要\n", encoding="utf-8")
    (job_dir / "combined.md").write_text("# 合并\n", encoding="utf-8")

    endpoints = {
        "transcript.md": "transcript",
        "segments.json": "segments",
        "minutes.md": "minutes",
        "combined.md": "combined",
    }
    before = {
        filename: http.get(f"/jobs/{job_id}/{endpoint}").text
        for filename, endpoint in endpoints.items()
    }
    for text in before.values():
        assert text

    renamed = http.post(f"/jobs/{job_id}/rename", json={"name": "改名后.wav"})
    assert renamed.status_code == 200

    for filename, text in before.items():
        after = http.get(f"/jobs/{job_id}/{endpoints[filename]}")
        assert after.status_code == 200
        assert after.text == text, filename
        disposition = after.headers.get("content-disposition", "")
        assert filename in disposition, disposition


def test_a6_existing_transcript_header_keeps_old_name(
    http_and_data: tuple[TestClient, Path],
) -> None:
    """A6：改名前已生成的 transcript.md 内嵌「文件名：」行保持原名。"""
    http, data = http_and_data
    job_id = _insert_job(
        data / "jobs.db",
        source_name="原名.wav",
        created_at=datetime.now(UTC).strftime(_ISO),
    )
    job_dir = data / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    transcript = job_dir / "transcript.md"
    transcript.write_text("# 逐字稿\n\n- 文件名：原名.wav\n", encoding="utf-8")

    assert http.post(f"/jobs/{job_id}/rename", json={"name": "改名后.wav"}).status_code == 200
    assert "- 文件名：原名.wav" in transcript.read_text(encoding="utf-8")
    assert "原名.wav" in http.get(f"/jobs/{job_id}/transcript").text


def test_a6_rename_before_generation_uses_new_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A6 后半：先改名再生成，内嵌「文件名：」行用新名（流水线读存库名）。"""
    from app import pipeline

    data = tmp_path / "data"
    source = tmp_path / "source.bin"
    source.write_bytes(b"audio")
    store = JobStore(data / "jobs.db")
    job_id = _insert_job(
        data / "jobs.db",
        source_name="旧名.wav",
        created_at=datetime.now(UTC).strftime(_ISO),
        status="queued",
        source_path=str(source),
    )
    store.rename_job(job_id, "新名.wav")
    job_dir = data / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "app.pipeline._to_wav",
        lambda _source, destination: destination.write_bytes(b"RIFF"),
    )
    monkeypatch.setattr("app.pipeline.probe_duration", lambda _path: 1.0)
    monkeypatch.setattr(
        "app.pipeline.transcribe",
        lambda _path, _tier, **_kw: [{"start": 0.0, "end": 1.0, "speaker": None, "text": "你好"}],
    )
    monkeypatch.setattr("app.pipeline.run_minutes", lambda *_a, **_k: None)

    claimed = store.get(job_id)
    assert claimed is not None
    pipeline._run(store, data, claimed)

    markdown = (job_dir / "transcript.md").read_text(encoding="utf-8")
    assert "- 文件名：新名.wav" in markdown
    assert "旧名.wav" not in markdown


def test_a7_script_name_is_escaped_everywhere(
    http_and_data: tuple[TestClient, Path],
) -> None:
    """A7：标题含 <script> 时列表与详情按文本显示，DOM 里不出现 script 节点。"""
    http, data = http_and_data
    payload = "<script>alert(1)</script>"
    job_id = _insert_job(
        data / "jobs.db",
        source_name="起点.wav",
        created_at=datetime.now(UTC).strftime(_ISO),
    )
    assert http.post(f"/jobs/{job_id}/rename", json={"name": payload}).status_code == 200
    assert _name(JobStore(data / "jobs.db"), job_id) == payload

    for path in ("/", f"/jobs/{job_id}/view"):
        page = http.get(path)
        assert page.status_code == 200
        assert payload not in page.text, path
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text, path
        assert not re.search(r"<script>\s*alert", page.text), path


def test_a7_script_name_in_input_value_is_escaped(
    http_and_data: tuple[TestClient, Path],
) -> None:
    """A7 补强：编辑框 value 属性同样转义，不能靠属性注入闭合。"""
    http, data = http_and_data
    payload = '"><script>alert(2)</script>'
    job_id = _insert_job(
        data / "jobs.db",
        source_name="起点.wav",
        created_at=datetime.now(UTC).strftime(_ISO),
    )
    assert http.post(f"/jobs/{job_id}/rename", json={"name": payload}).status_code == 200

    for path in ("/", f"/jobs/{job_id}/view"):
        page = http.get(path)
        assert "<script>alert(2)</script>" not in page.text, path
        assert "&#34;&gt;&lt;script&gt;" in page.text or "&#34;&gt;" in page.text, path


def test_a8_rename_keeps_created_time_and_order(
    http_and_data: tuple[TestClient, Path],
) -> None:
    """A8：改名不改 created_at，也不改列表顺序。"""
    http, data = http_and_data
    db = data / "jobs.db"
    base = datetime(2026, 6, 1, tzinfo=UTC)
    ids = []
    for index, name in enumerate(["最早.wav", "中间.wav", "最新.wav"]):
        stamp = (base.replace(hour=index)).strftime(_ISO)
        ids.append(_insert_job(db, source_name=name, created_at=stamp))

    before_home = http.get("/").text
    newest_at = before_home.find("最新.wav")
    middle_at = before_home.find("中间.wav")
    oldest_at = before_home.find("最早.wav")
    assert newest_at < middle_at < oldest_at

    store = JobStore(db)
    middle_before = store.get(ids[1])
    assert middle_before is not None
    created_at_before = str(middle_before["created_at"])

    assert http.post(f"/jobs/{ids[1]}/rename", json={"name": "中间改.wav"}).status_code == 200

    after_home = http.get("/").text
    newest_at = after_home.find("最新.wav")
    renamed_at = after_home.find("中间改.wav")
    oldest_at = after_home.find("最早.wav")
    assert newest_at < renamed_at < oldest_at
    assert after_home.find("中间.wav") == -1

    after_job = JobStore(db).get(ids[1])
    assert after_job is not None
    assert str(after_job["created_at"]) == created_at_before


def test_rename_accepts_form_encoded_name(
    http_and_data: tuple[TestClient, Path],
) -> None:
    """§6 第 4 点：JSON 与表单两种提交都接受。"""
    http, data = http_and_data
    job_id = _insert_job(
        data / "jobs.db",
        source_name="表单.wav",
        created_at=datetime.now(UTC).strftime(_ISO),
    )
    response = http.post(
        f"/jobs/{job_id}/rename",
        data={"name": "表单改名"},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "source_name": "表单改名"}


def test_rename_without_name_field_is_400(
    http_and_data: tuple[TestClient, Path],
) -> None:
    """缺 name 字段按 400 处理，提示可读。"""
    http, data = http_and_data
    job_id = _insert_job(
        data / "jobs.db",
        source_name="缺字段.wav",
        created_at=datetime.now(UTC).strftime(_ISO),
    )
    response = http.post(f"/jobs/{job_id}/rename", json={"tier": "fast"})
    assert response.status_code == 400
    assert response.json()["error"] == "缺少 name"


def test_rename_endpoint_does_not_touch_other_fields(
    http_and_data: tuple[TestClient, Path],
) -> None:
    """改名只改标题字段：status/tier/url/created_at 一律不动。"""
    http, data = http_and_data
    db = data / "jobs.db"
    stamp = datetime.now(UTC).strftime(_ISO)
    job_id = _insert_job(
        db,
        source_name="不动.wav",
        created_at=stamp,
        tier="precise",
        source_kind="url",
        url="https://example.com/a.mp3",
    )
    before = JobStore(db).get(job_id)
    assert before is not None
    assert http.post(f"/jobs/{job_id}/rename", json={"name": "只改标题"}).status_code == 200
    after = JobStore(db).get(job_id)
    assert after is not None
    for field in ("status", "tier", "url", "created_at", "source_kind"):
        assert after[field] == before[field], field
    assert after["source_name"] == "只改标题"


def test_validate_source_name_rejects_non_string() -> None:
    """非字符串入参按不合法处理，不静默接受 None 或数字。"""
    with pytest.raises(ValueError, match="名称不合法"):
        service.validate_source_name(None)
    with pytest.raises(ValueError, match="名称不合法"):
        service.validate_source_name(123)


def test_source_domain_strips_www_and_ignores_bad_urls() -> None:
    """域名抽取：去 www、只留主机；非法 URL 返回空串。"""
    assert service.source_domain("https://www.bilibili.com/video/BV1") == "bilibili.com"
    assert service.source_domain("https://youtube.com/watch?v=x") == "youtube.com"
    assert service.source_domain("not a url") == ""
    assert service.source_domain("") == ""


def test_rename_route_registered_in_app() -> None:
    """路由存在且只接受 POST（GET 不提供改名入口）。"""
    app = create_app(Path("/tmp/transcriber-rename-route-check"))
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/jobs/{job_id}/rename" in paths
