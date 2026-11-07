from __future__ import annotations

import ipaddress
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from app import service
from app.config import (
    MAX_DURATION_SECONDS_DEFAULT,
    MAX_UPLOAD_BYTES_DEFAULT,
    max_duration_seconds,
    max_upload_bytes,
)
from app.jobs import JobStore
from app.main import create_app
from fastapi.testclient import TestClient
from tests.support import post_file, probe_audio, synth_wav, wait_job


def test_b1_health(client: tuple[TestClient, Path]) -> None:
    http, _data = client
    response = http.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_b2_upload_reaches_done_and_lists_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSCRIBER_STUB", "1")
    monkeypatch.setenv("TRANSCRIBER_STUB_SLEEP", "0.4")
    data = tmp_path / "data"
    wav = tmp_path / "talk.wav"
    synth_wav(wav, seconds=0.3)
    seen: set[str] = set()
    with TestClient(create_app(data)) as http:
        created = post_file(http, wav, tier="fast")
        assert created.status_code == 200
        job_id = created.json()["job_id"]
        deadline = time.time() + 20
        body: dict[str, object] = {}
        while time.time() < deadline:
            response = http.get(f"/jobs/{job_id}")
            assert response.status_code == 200
            body = response.json()
            seen.add(str(body["status"]))
            if body["status"] in {"done", "error"}:
                break
            time.sleep(0.05)
    assert body["status"] == "done"
    assert seen <= {"queued", "running", "done"}
    assert seen & {"queued", "running"}
    assert set(body) == {
        "id",
        "status",
        "tier",
        "source_name",
        "created_at",
        "finished_at",
        "error",
        "minutes_status",
        "minutes_error",
    }
    assert body["id"] == job_id
    assert body["tier"] == "fast"
    assert body["source_name"] == "talk.wav"
    assert body["error"] is None
    assert body["finished_at"]
    assert str(body["created_at"]).startswith("20")
    assert body["minutes_status"] in {"pending", "running", "done"}


def test_b2_default_tier_is_fast(client: tuple[TestClient, Path], tmp_path: Path) -> None:
    http, _data = client
    wav = tmp_path / "plain.wav"
    synth_wav(wav, seconds=0.2)
    created = post_file(http, wav)
    assert created.status_code == 200
    job_id = created.json()["job_id"]
    body = wait_job(http, job_id)
    assert body["status"] == "done"
    assert body["tier"] == "fast"


def test_b3_url_failure_becomes_error(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, _data = client
    monkeypatch.setattr(
        "app.service.default_resolver", lambda _h: [ipaddress.ip_address("8.8.8.8")]
    )
    response = http.post("/jobs", json={"url": "http://127.0.0.1:9/missing-audio", "tier": "fast"})
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    body = wait_job(http, job_id, timeout=40)
    assert body["status"] == "error"
    assert "抓取" in str(body["error"])
    again = http.get(f"/jobs/{job_id}")
    assert again.json()["status"] == "error"


def test_b4_over_duration_does_not_call_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "engine-called"
    monkeypatch.setenv("TRANSCRIBER_STUB", "1")
    monkeypatch.setenv("TRANSCRIBER_STUB_SLEEP", "0")
    monkeypatch.setenv("TRANSCRIBER_MAX_DURATION_SECONDS", "0.05")
    monkeypatch.setenv("TRANSCRIBER_ENGINE_MARKER", str(marker))
    data = tmp_path / "data"
    wav = tmp_path / "long-enough.wav"
    synth_wav(wav, seconds=0.4)
    with TestClient(create_app(data)) as http:
        created = post_file(http, wav, tier="fast")
        job_id = created.json()["job_id"]
        body = wait_job(http, job_id)
    assert body["status"] == "error"
    assert "时长" in str(body["error"])
    assert "超过上限" in str(body["error"])
    assert not marker.exists()


def test_b5_oversize_upload_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIBER_STUB", "1")
    monkeypatch.setenv("TRANSCRIBER_MAX_UPLOAD_BYTES", "32")
    data = tmp_path / "data"
    wav = tmp_path / "big.wav"
    wav.write_bytes(b"x" * 64)
    with TestClient(create_app(data)) as http:
        response = post_file(http, wav, tier="fast")
    assert response.status_code == 413
    assert "大小上限" in response.json()["error"]
    jobs = data / "jobs"
    leftover = [path for path in jobs.glob("*") if path.exists()] if jobs.exists() else []
    assert leftover == []


def test_b5_and_b4_default_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSCRIBER_MAX_DURATION_SECONDS", raising=False)
    monkeypatch.delenv("TRANSCRIBER_MAX_UPLOAD_BYTES", raising=False)
    assert MAX_DURATION_SECONDS_DEFAULT == 3600
    assert MAX_UPLOAD_BYTES_DEFAULT == 2 * 1024 * 1024 * 1024
    assert max_duration_seconds() == 3600
    assert max_upload_bytes() == 2 * 1024 * 1024 * 1024


def _read_stub_trace(path: Path) -> list[dict[str, object]]:
    """读桩引擎区间记录（JSONL，一行一事件）。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _stub_run_intervals(
    records: list[dict[str, object]],
) -> dict[str, dict[str, float]]:
    """把 trace 事件折叠成每个 job 的运行区间：单调钟区间 + 墙钟端点。"""
    runs: dict[str, dict[str, float]] = {}
    for record in records:
        event = record.get("event")
        job_id = record.get("job_id")
        if not isinstance(job_id, str) or event not in {"start", "end"}:
            continue
        mono = float(record["mono"])  # type: ignore[arg-type]
        wall = float(record["wall"])  # type: ignore[arg-type]
        slot = runs.setdefault(job_id, {})
        if event == "start":
            slot["start_mono"] = mono
            slot["start_wall"] = wall
        else:
            slot["end_mono"] = mono
            slot["end_wall"] = wall
    return runs


def _assert_trace_complete(records: list[dict[str, object]], job_ids: tuple[str, str]) -> None:
    """M2：trace 必须恰好 4 条事件——每个 job 各 1 条 start、1 条 end。

    `_stub_run_intervals` 折叠时是「后写覆盖前写」：同一个 job 若写了多条 start/end，
    会被静默折成一个区间，「每个 job 只跑了一次」这个前提就失去保护。先在此堵住。
    """
    expected = len(job_ids) * 2
    assert len(records) == expected, (
        f"trace 事件数应为 {expected} 条（每 job 1 start + 1 end），"
        f"实际 {len(records)} 条：{records}"
    )
    for job_id in job_ids:
        starts = [r for r in records if r.get("job_id") == job_id and r.get("event") == "start"]
        ends = [r for r in records if r.get("job_id") == job_id and r.get("event") == "end"]
        assert len(starts) == 1, f"{job_id} 应有 1 条 start，实际 {len(starts)} 条：{starts}"
        assert len(ends) == 1, f"{job_id} 应有 1 条 end，实际 {len(ends)} 条：{ends}"


def _assert_runs_are_serial(
    records: list[dict[str, object]],
    first_id: str,
    second_id: str,
    second_created_wall: float,
) -> None:
    """T2 判据（事后完整区间，不看轮询瞬时状态）。三条各自独立、互不替代：

    ① 不重叠（顺序无关）：两段区间按 start_mono 排序后，早段的 end_mono ≤ 晚段的
       start_mono。不写死「job1 先跑」——job2 先跑且完全串行时也判过。
    ② FIFO（单独断言，依据取队顺序）：先创建的 job 先开始。依据 app/jobs.py:257
       `claim_next` 在 `BEGIN IMMEDIATE`（261 行）下取 `ORDER BY created_at, rowid`
       （266 行）的第一条 queued，故先创建者先被领取。
    ③ 串行前提：job2 创建于 job1 结束之前（原 saw_second_queued_while_first_running
       的等价承接）。此处用测试进程墙钟比 worker 的 end_wall，M1 已把余量提到 3s。
    """
    runs = _stub_run_intervals(records)
    assert first_id in runs and second_id in runs, f"trace 缺少 job 区间：{sorted(runs)}"
    first = runs[first_id]
    second = runs[second_id]
    assert {"start_mono", "end_mono", "end_wall"} <= set(first), f"job1 区间不完整：{first}"
    assert {"start_mono", "end_mono"} <= set(second), f"job2 区间不完整：{second}"

    # ① 不重叠：按 start_mono 排序，不假定谁先跑
    earlier, later = sorted((first, second), key=lambda run: run["start_mono"])
    assert earlier["end_mono"] <= later["start_mono"], (
        f"两个桩转写区间重叠：先开始段={earlier} 后开始段={later}"
    )

    # ② FIFO：先创建者先开始
    assert first["start_mono"] <= second["start_mono"], (
        "FIFO 被破坏：先创建的 job 没有先开始："
        f"job1 开始={first['start_mono']} job2 开始={second['start_mono']}"
    )

    # ③ 串行前提：job2 确实是在 job1 运行期间提交的
    assert second_created_wall < first["end_wall"], (
        "job2 不是在 job1 运行期间创建的，串行前提没被测到："
        f"job2 创建={second_created_wall} job1 结束={first['end_wall']}"
    )


def test_b6_jobs_are_serial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIBER_STUB", "1")
    monkeypatch.setenv("TRANSCRIBER_STUB_SLEEP", "3.0")
    trace = tmp_path / "stub-trace.jsonl"
    monkeypatch.setenv("TRANSCRIBER_STUB_TRACE", str(trace))
    data = tmp_path / "data"
    first = tmp_path / "a.wav"
    second = tmp_path / "b.wav"
    synth_wav(first, seconds=0.2)
    synth_wav(second, seconds=0.2)
    with TestClient(create_app(data)) as http:
        id1 = post_file(http, first, tier="fast").json()["job_id"]
        id2 = post_file(http, second, tier="fast").json()["job_id"]
        second_created_wall = time.time()
        job1 = wait_job(http, id1)
        job2 = wait_job(http, id2)
    assert job1["status"] == "done"
    assert job2["status"] == "done"
    records = _read_stub_trace(trace)
    _assert_trace_complete(records, (id1, id2))
    _assert_runs_are_serial(records, id1, id2, second_created_wall)


def test_b7_restart_marks_running_as_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSCRIBER_STUB", "1")
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    job_id = store.create_job(
        tier="fast",
        source_name="http://127.0.0.1/restart-me",
        source_kind="url",
        source_path=None,
        url="http://127.0.0.1/restart-me",
    )
    claimed = store.claim_next()
    assert claimed is not None
    assert claimed["id"] == job_id
    with TestClient(create_app(data)) as http:
        body = http.get(f"/jobs/{job_id}").json()
    assert body["status"] == "error"
    assert "重启" in str(body["error"])


def test_b8_wav_is_16k_mono(client: tuple[TestClient, Path], tmp_path: Path) -> None:
    http, data = client
    wav = tmp_path / "stereo.wav"
    synth_wav(wav, seconds=0.3, rate=44100, channels=2)
    job_id = post_file(http, wav, tier="fast").json()["job_id"]
    body = wait_job(http, job_id)
    assert body["status"] == "done"
    rate, channels = probe_audio(data / "jobs" / job_id / "audio.wav")
    assert rate == 16000
    assert channels == 1


def test_b9_segments_schema(client: tuple[TestClient, Path], tmp_path: Path) -> None:
    http, _data = client
    wav = tmp_path / "seg.wav"
    synth_wav(wav, seconds=0.2)
    job_id = post_file(http, wav, tier="fast").json()["job_id"]
    assert wait_job(http, job_id)["status"] == "done"
    response = http.get(f"/jobs/{job_id}/segments")
    assert response.status_code == 200
    segments = response.json()
    assert isinstance(segments, list)
    assert segments
    previous_end = -1.0
    for segment in segments:
        assert set(segment) == {"start", "end", "speaker", "text"}
        assert segment["start"] < segment["end"]
        assert segment["start"] >= previous_end
        previous_end = segment["end"]
        assert segment["text"]
    assert segments[0]["speaker"] == "说话人1"


def test_b14_transcript_header_and_speaker_lines(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    http, _data = client
    wav = tmp_path / "talk.wav"
    synth_wav(wav, seconds=0.2)
    job_id = post_file(http, wav, tier="fast").json()["job_id"]
    assert wait_job(http, job_id)["status"] == "done"
    text = http.get(f"/jobs/{job_id}/transcript").text
    assert "文件名：talk.wav" in text
    assert "时长：" in text
    assert "秒" in text
    assert "档位：fast" in text
    assert "生成时间：" in text
    assert "[00:00] 说话人1：你好" in text
    assert "[00:01] 说话人2：大家好" in text


def test_b14_precise_omits_speaker(client: tuple[TestClient, Path], tmp_path: Path) -> None:
    http, _data = client
    wav = tmp_path / "precise.wav"
    synth_wav(wav, seconds=0.2)
    job_id = post_file(http, wav, tier="precise").json()["job_id"]
    body = wait_job(http, job_id)
    assert body["status"] == "done"
    assert body["tier"] == "precise"
    text = http.get(f"/jobs/{job_id}/transcript").text
    assert "档位：precise" in text
    assert "[00:00] 今天天气不错" in text
    assert "说话人" not in text
    segments = http.get(f"/jobs/{job_id}/segments").json()
    assert segments[0]["speaker"] is None


def test_b15_download_before_done_is_not_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSCRIBER_STUB", "1")
    monkeypatch.setenv("TRANSCRIBER_STUB_SLEEP", "2")
    data = tmp_path / "data"
    wav = tmp_path / "slow.wav"
    synth_wav(wav, seconds=0.2)
    with TestClient(create_app(data)) as http:
        job_id = post_file(http, wav, tier="fast").json()["job_id"]
        deadline = time.time() + 10
        status = ""
        while time.time() < deadline:
            status = http.get(f"/jobs/{job_id}").json()["status"]
            if status == "running":
                break
            time.sleep(0.05)
        assert status == "running"
        for path in ("transcript", "segments"):
            response = http.get(f"/jobs/{job_id}/{path}")
            assert response.status_code == 404
            assert response.status_code != 500
            body = response.json()
            assert body["status"] == "running"
            assert "error" in body


def test_b15_download_after_done(client: tuple[TestClient, Path], tmp_path: Path) -> None:
    http, _data = client
    wav = tmp_path / "ready.wav"
    synth_wav(wav, seconds=0.2)
    job_id = post_file(http, wav, tier="fast").json()["job_id"]
    assert wait_job(http, job_id)["status"] == "done"
    transcript = http.get(f"/jobs/{job_id}/transcript")
    segments = http.get(f"/jobs/{job_id}/segments")
    assert transcript.status_code == 200
    assert "逐字稿" in transcript.text
    assert segments.status_code == 200
    assert isinstance(segments.json(), list)
    missing = http.get("/jobs/does-not-exist/transcript")
    assert missing.status_code == 404
    assert missing.status_code != 500


def test_b16_corrupt_file_is_error_without_traceback(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    http, _data = client
    bad = tmp_path / "broken.wav"
    bad.write_bytes(b"this is not a wav file " * 20)
    job_id = post_file(http, bad, tier="fast").json()["job_id"]
    body = wait_job(http, job_id)
    assert body["status"] == "error"
    message = str(body["error"])
    assert "预处理" in message
    assert "Traceback" not in message
    assert "token" not in message.lower()


def test_b21_killed_worker_does_not_kill_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSCRIBER_STUB", "1")
    monkeypatch.setenv("TRANSCRIBER_STUB_SLEEP", "3")
    data = tmp_path / "data"
    wav = tmp_path / "kill.wav"
    nxt = tmp_path / "next.wav"
    synth_wav(wav, seconds=0.2)
    synth_wav(nxt, seconds=0.2)
    with TestClient(create_app(data)) as http:
        job_id = post_file(http, wav, tier="fast").json()["job_id"]
        deadline = time.time() + 10
        while time.time() < deadline:
            if http.get(f"/jobs/{job_id}").json()["status"] == "running":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("任务没有进入 running")
        pid_path = data / "worker.pid"
        old_pid = int(pid_path.read_text(encoding="utf-8"))
        os.kill(old_pid, 9)
        health = http.get("/health")
        assert health.status_code == 200
        assert health.json() == {"ok": True}
        failed = wait_job(http, job_id, timeout=15)
        assert failed["status"] == "error"
        assert "异常退出" in str(failed["error"])
        new_pid = _wait_new_worker(pid_path, old_pid)
        assert new_pid != old_pid
        os.kill(new_pid, 0)
        follow = post_file(http, nxt, tier="fast").json()["job_id"]
        # 新 worker 仍按启动时的 sleep 转写，给足时间。
        followed = wait_job(http, follow, timeout=20)
        assert followed["status"] == "done"
        assert http.get(f"/jobs/{job_id}").json()["status"] == "error"


def test_invalid_tier_is_400(client: tuple[TestClient, Path], tmp_path: Path) -> None:
    http, _data = client
    wav = tmp_path / "tier.wav"
    synth_wav(wav, seconds=0.2)
    response = post_file(http, wav, tier="ultra")
    assert response.status_code == 400
    assert "tier" in response.json()["error"]
    missing = http.post("/jobs", json={})
    assert missing.status_code == 400
    assert "url" in missing.json()["error"]


def test_error_job_is_not_retried(client: tuple[TestClient, Path], tmp_path: Path) -> None:
    http, _data = client
    bad = tmp_path / "nope.wav"
    bad.write_bytes(b"not-audio")
    job_id = post_file(http, bad, tier="fast").json()["job_id"]
    body = wait_job(http, job_id)
    assert body["status"] == "error"
    time.sleep(0.6)
    again = http.get(f"/jobs/{job_id}").json()
    assert again["status"] == "error"
    assert again["error"] == body["error"]


def test_f2_invalid_job_id_is_404_before_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非法 job_id 在查库和拼路径之前就是 404，不能借路径逃出任务目录。"""
    monkeypatch.setenv("TRANSCRIBER_STUB", "1")
    monkeypatch.setenv("TRANSCRIBER_STUB_SLEEP", "0")
    data = tmp_path / "data"
    outside = tmp_path / "etc"
    outside.mkdir()
    canary = outside / "keep.txt"
    canary.write_text("keep", encoding="utf-8")
    store = JobStore(data / "jobs.db")
    short_id = "a" * 31
    traversal = "../../etc"
    dotdot = ".."
    for job_id in (short_id, traversal, "", dotdot):
        _insert_done_job(store.db_path, job_id)

    for job_id in (traversal, "", short_id, dotdot):
        assert service.public_job(store, job_id) is None
        assert service.job_detail_for_page(store, data, job_id) is None
        status, _payload = service.minutes_payload(store, data, job_id)
        assert status == 404
        status, _payload = service.combined_payload(store, data, job_id)
        assert status == 404
        status, _body = service.regenerate_minutes(store, job_id)
        assert status == 404
        assert service.delete_job(store, data, job_id) is False
        assert store.get(job_id) is not None
    assert canary.is_file()
    assert data.is_dir()

    routes = (
        ("GET", "/jobs/{job_id}"),
        ("GET", "/jobs/{job_id}/transcript"),
        ("GET", "/jobs/{job_id}/segments"),
        ("GET", "/jobs/{job_id}/minutes"),
        ("GET", "/jobs/{job_id}/combined"),
        ("GET", "/jobs/{job_id}/view"),
        ("POST", "/jobs/{job_id}/minutes/regenerate"),
        ("POST", "/jobs/{job_id}/delete"),
        ("DELETE", "/jobs/{job_id}"),
    )
    with TestClient(create_app(data)) as http:
        for job_id in (traversal, "", short_id):
            response = http.get(f"/jobs/{job_id}/minutes")
            assert response.status_code == 404
        for method, template in routes:
            response = http.request(method, template.format(job_id=short_id))
            assert response.status_code == 404, (method, template)
            dot_response = http.request(method, template.format(job_id="%2e%2e"))
            assert dot_response.status_code == 404, (method, template)
    assert store.get(short_id) is not None
    assert store.get(dotdot) is not None
    assert canary.is_file()
    assert data.is_dir()


def _insert_done_job(db: Path, job_id: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """
            INSERT INTO jobs (
                id, status, tier, source_name, source_kind, created_at,
                minutes_status, minutes_error
            ) VALUES (?, 'done', 'fast', 'x', 'upload', ?, 'error', '纪要失败')
            """,
            (job_id, "2026-01-01T00:00:00.000000Z"),
        )
        conn.commit()
    finally:
        conn.close()


def test_f1_api_times_are_eight_hours_ahead_of_stored_utc(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    """GET /jobs/{id} 的时间是 Asia/Shanghai（+08:00），与库里的 UTC 墙钟相差 8 小时。"""
    http, data = client
    wav = tmp_path / "tz.wav"
    synth_wav(wav, seconds=0.2)
    job_id = post_file(http, wav, tier="fast").json()["job_id"]
    assert wait_job(http, job_id)["status"] == "done"
    stored_created = "2026-01-15T16:30:45.123456Z"
    stored_finished = "2026-01-15T18:05:01.654321Z"
    _set_stored_times(data / "jobs.db", job_id, stored_created, stored_finished)

    body = http.get(f"/jobs/{job_id}").json()

    assert body["created_at"] == "2026-01-16T00:30:45.123456+08:00"
    assert body["finished_at"] == "2026-01-16T02:05:01.654321+08:00"
    _assert_wall_clock_plus_eight(stored_created, str(body["created_at"]))
    _assert_wall_clock_plus_eight(stored_finished, str(body["finished_at"]))
    assert _stored_times(data / "jobs.db", job_id) == (stored_created, stored_finished)


def _created_at_index_columns(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        found = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND tbl_name = 'jobs' AND name = 'idx_jobs_created_at'
            """
        ).fetchone()
        assert found is not None
        info = conn.execute("PRAGMA index_info(idx_jobs_created_at)").fetchall()
    finally:
        conn.close()
    return [str(column[2]) for column in info]


def test_jobs_created_at_index_backfills_on_startup(tmp_path: Path) -> None:
    """存量库启动时补 created_at 索引，新库同样建上。"""
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            tier TEXT NOT NULL,
            source_name TEXT,
            source_kind TEXT NOT NULL,
            source_path TEXT,
            url TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            error TEXT,
            duration REAL,
            minutes_status TEXT NOT NULL DEFAULT 'pending',
            minutes_error TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO jobs (
            id, status, tier, source_name, source_kind, created_at, minutes_status
        ) VALUES (
            'old', 'done', 'fast', 'old.wav', 'upload',
            '2026-01-01T00:00:00.000000Z', 'done'
        )
        """
    )
    conn.commit()
    conn.close()

    before = sqlite3.connect(legacy)
    try:
        missing = before.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'index' AND name = 'idx_jobs_created_at'
            """
        ).fetchone()
    finally:
        before.close()
    assert missing is None

    store = JobStore(legacy)
    assert _created_at_index_columns(legacy) == ["created_at"]
    assert store.get("old") is not None
    assert store.list_recent_jobs(limit=100)[0]["id"] == "old"

    fresh = tmp_path / "fresh.db"
    JobStore(fresh)
    assert _created_at_index_columns(fresh) == ["created_at"]


def _set_stored_times(db: Path, job_id: str, created_at: str, finished_at: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE jobs SET created_at = ?, finished_at = ? WHERE id = ?",
            (created_at, finished_at, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def _stored_times(db: Path, job_id: str) -> tuple[str, str | None]:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT created_at, finished_at FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0]), None if row[1] is None else str(row[1])


def _assert_wall_clock_plus_eight(stored_utc: str, shown: str) -> None:
    stored = datetime.strptime(stored_utc, "%Y-%m-%dT%H:%M:%S.%fZ")
    shown_at = datetime.fromisoformat(shown)
    assert shown_at.utcoffset() == timedelta(hours=8)
    assert shown.endswith("+08:00")
    assert shown_at.replace(tzinfo=None) - stored == timedelta(hours=8)


def _wait_new_worker(pid_path: Path, old_pid: int) -> int:
    deadline = time.time() + 10
    while time.time() < deadline:
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text(encoding="utf-8"))
            except ValueError:
                pid = old_pid
            if pid != old_pid:
                try:
                    os.kill(pid, 0)
                except OSError:
                    pass
                else:
                    return pid
        time.sleep(0.1)
    raise AssertionError("worker 没有被重新拉起")


@pytest.mark.parametrize(
    ("limit_input", "expected_count"),
    [
        (-10, 1),
        (-1, 1),
        (0, 1),
        (1, 1),
        (5, 5),
        (50, 50),
        (100, 100),
        (101, 100),
        (200, 100),
    ],
)
def test_list_recent_jobs_limit_clamp(
    tmp_path: Path, limit_input: int, expected_count: int
) -> None:
    db_path = tmp_path / "jobs.db"
    store = JobStore(db_path)
    # 写入 105 条任务以覆盖超 100 与正常范围
    for i in range(105):
        store.create_job(
            tier="fast",
            source_name=f"test_{i:03d}.wav",
            source_kind="upload",
            source_path=None,
            url=None,
            job_id=f"job_{i:03d}",
        )
    jobs = store.list_recent_jobs(limit=limit_input)
    assert len(jobs) == expected_count


def test_b2_public_json_contains_minutes_status_and_error(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    job_id = store.create_job(
        tier="fast",
        source_name="test.wav",
        source_kind="upload",
        source_path=None,
        url=None,
    )
    with TestClient(create_app(data)) as http:
        # 初始 pending 状态
        resp = http.get(f"/jobs/{job_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["minutes_status"] == "pending"
        assert body["minutes_error"] is None

        # 失败状态
        store.set_minutes_status(job_id, "error", error="LLM 连接超时")
        resp = http.get(f"/jobs/{job_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["minutes_status"] == "error"
        assert body["minutes_error"] == "LLM 连接超时"

        # 成功状态
        store.set_minutes_status(job_id, "done")
        resp = http.get(f"/jobs/{job_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["minutes_status"] == "done"
        assert body["minutes_error"] is None


def test_b3_duration_boundary_equal_allowed_and_over_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSCRIBER_STUB", "1")
    monkeypatch.setenv("TRANSCRIBER_STUB_SLEEP", "0")
    wav = tmp_path / "probe.wav"
    synth_wav(wav, seconds=0.2)

    # 1. 等于上限（0.2s）：允许转写成功走到 done
    monkeypatch.setenv("TRANSCRIBER_MAX_DURATION_SECONDS", "0.2")
    data_allowed = tmp_path / "data_allowed"
    with TestClient(create_app(data_allowed)) as http:
        job_id = post_file(http, wav, tier="fast").json()["job_id"]
        body = wait_job(http, job_id)
        assert body["status"] == "done"

    # 2. 超过上限（上限 0.19s < 音频 0.2s）：转写前拒绝置 error
    monkeypatch.setenv("TRANSCRIBER_MAX_DURATION_SECONDS", "0.19")
    data_rejected = tmp_path / "data_rejected"
    with TestClient(create_app(data_rejected)) as http:
        job_id = post_file(http, wav, tier="fast").json()["job_id"]
        body = wait_job(http, job_id)
        assert body["status"] == "error"
        assert "超过上限" in str(body["error"])


def _set_job_status(db_path: Path, job_id: str, status: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    conn.commit()
    conn.close()


def test_f1_audio_endpoint_range_and_full(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    job_id = "0123456789abcdef0123456789abcdef"
    store.create_job(
        job_id=job_id,
        source_name="test.wav",
        tier="fast",
        source_kind="upload",
        source_path=None,
        url=None,
    )
    _set_job_status(data / "jobs.db", job_id, "done")

    job_dir = data / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    audio_content = b"0123456789abcdefghijklmnopqrstuvwxyz"
    (job_dir / "audio.wav").write_bytes(audio_content)

    with TestClient(create_app(data)) as http:
        # 不带 Range 返回 200 全量
        resp_full = http.get(f"/jobs/{job_id}/audio")
        assert resp_full.status_code == 200
        assert resp_full.headers["content-type"] == "audio/wav"
        assert resp_full.headers["accept-ranges"] == "bytes"
        assert resp_full.content == audio_content

        # 带 Range 返回 206 与正确 Content-Range
        resp_range = http.get(f"/jobs/{job_id}/audio", headers={"Range": "bytes=0-9"})
        assert resp_range.status_code == 206
        assert resp_range.headers["content-type"] == "audio/wav"
        assert resp_range.headers["content-range"] == f"bytes 0-9/{len(audio_content)}"
        assert resp_range.content == audio_content[:10]


def test_f1_audio_endpoint_source_selection(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    app = create_app(data)

    # 1. 原始文件优先：上传任务 source.mp3 优先于 audio.wav
    j1 = "11111111111111111111111111111111"
    store.create_job(
        job_id=j1,
        source_name="speech.mp3",
        tier="fast",
        source_kind="upload",
        source_path=None,
        url=None,
    )
    _set_job_status(data / "jobs.db", j1, "done")
    j1_dir = data / "jobs" / j1
    j1_dir.mkdir(parents=True, exist_ok=True)
    (j1_dir / "source.mp3").write_bytes(b"MP3DATA")
    (j1_dir / "audio.wav").write_bytes(b"WAVDATA")

    # 2. 原始文件优先：链接任务 download.m4a 优先于 audio.wav
    j2 = "22222222222222222222222222222222"
    store.create_job(
        job_id=j2,
        source_name="web.m4a",
        tier="fast",
        source_kind="url",
        source_path=None,
        url="https://example.com/web.m4a",
    )
    _set_job_status(data / "jobs.db", j2, "done")
    j2_dir = data / "jobs" / j2
    j2_dir.mkdir(parents=True, exist_ok=True)
    (j2_dir / "download.m4a").write_bytes(b"M4ADATA")
    (j2_dir / "audio.wav").write_bytes(b"WAVDATA")

    # 3. 浏览器可播格式外（如 flac）直接回落 audio.wav
    j3 = "33333333333333333333333333333333"
    store.create_job(
        job_id=j3,
        source_name="music.flac",
        tier="fast",
        source_kind="upload",
        source_path=None,
        url=None,
    )
    _set_job_status(data / "jobs.db", j3, "done")
    j3_dir = data / "jobs" / j3
    j3_dir.mkdir(parents=True, exist_ok=True)
    (j3_dir / "source.flac").write_bytes(b"FLACDATA")
    (j3_dir / "audio.wav").write_bytes(b"WAVDATA_FALLBACK")

    # 4. 浏览器可播格式外且 audio.wav 缺失 -> 404
    j4 = "44444444444444444444444444444444"
    store.create_job(
        job_id=j4,
        source_name="track.mkv",
        tier="fast",
        source_kind="url",
        source_path=None,
        url="https://example.com/track.mkv",
    )
    _set_job_status(data / "jobs.db", j4, "done")
    j4_dir = data / "jobs" / j4
    j4_dir.mkdir(parents=True, exist_ok=True)
    (j4_dir / "download.mkv").write_bytes(b"MKVDATA")

    # 5. 皆缺 -> 404
    j5 = "55555555555555555555555555555555"
    store.create_job(
        job_id=j5,
        source_name="empty.wav",
        tier="fast",
        source_kind="upload",
        source_path=None,
        url=None,
    )
    _set_job_status(data / "jobs.db", j5, "done")
    j5_dir = data / "jobs" / j5
    j5_dir.mkdir(parents=True, exist_ok=True)

    with TestClient(app) as http:
        r1 = http.get(f"/jobs/{j1}/audio")
        assert r1.status_code == 200
        assert r1.headers["content-type"] == "audio/mpeg"
        assert r1.content == b"MP3DATA"

        r2 = http.get(f"/jobs/{j2}/audio")
        assert r2.status_code == 200
        assert r2.headers["content-type"] == "audio/mp4"
        assert r2.content == b"M4ADATA"

        r3 = http.get(f"/jobs/{j3}/audio")
        assert r3.status_code == 200
        assert r3.headers["content-type"] == "audio/wav"
        assert r3.content == b"WAVDATA_FALLBACK"

        r4 = http.get(f"/jobs/{j4}/audio")
        assert r4.status_code == 404
        assert "音频文件不存在" in r4.json()["error"]

        r5 = http.get(f"/jobs/{j5}/audio")
        assert r5.status_code == 404
        assert "音频文件不存在" in r5.json()["error"]


def test_f1_audio_endpoint_errors_and_traversal(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    app = create_app(data)

    # 1. 未完成任务返回 409
    j_queued = "66666666666666666666666666666666"
    store.create_job(
        job_id=j_queued,
        source_name="queued.wav",
        tier="fast",
        source_kind="upload",
        source_path=None,
        url=None,
    )
    j_dir = data / "jobs" / j_queued
    j_dir.mkdir(parents=True, exist_ok=True)
    (j_dir / "audio.wav").write_bytes(b"DATA")

    with TestClient(app) as http:
        resp_queued = http.get(f"/jobs/{j_queued}/audio")
        assert resp_queued.status_code == 409
        assert resp_queued.json()["status"] == "queued"
        assert "尚未完成" in resp_queued.json()["error"]

        # 2. 任务不存在返回 404
        resp_none = http.get("/jobs/77777777777777777777777777777777/audio")
        assert resp_none.status_code == 404
        assert resp_none.json()["error"] == "任务不存在"

        # 3. 伪造路径越界返回 404
        resp_traversal = http.get("/jobs/..%2f..%2fetc/audio")
        assert resp_traversal.status_code == 404


def test_audio_not_done_returns_409_body_unchanged(tmp_path: Path) -> None:
    """任务存在但未完成时音频端点返回 409，error 与 status 字段保持原样。"""
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    app = create_app(data)
    cases = (
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "pending"),
        ("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "failed"),
    )
    for job_id, status in cases:
        store.create_job(
            job_id=job_id,
            source_name=f"{status}.wav",
            tier="fast",
            source_kind="upload",
            source_path=None,
            url=None,
        )
        _set_job_status(data / "jobs.db", job_id, status)

    with TestClient(app) as http:
        for job_id, status in cases:
            resp = http.get(f"/jobs/{job_id}/audio")
            assert resp.status_code == 409
            assert resp.json() == {
                "error": "任务尚未完成，音频不可用",
                "status": status,
            }


def test_s1_audio_endpoint_symlink_traversal_rejected(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    app = create_app(data)

    # 目标任务：存在正常音频
    j_target = "11111111111111111111111111111111"
    store.create_job(
        job_id=j_target,
        source_name="target.wav",
        tier="fast",
        source_kind="upload",
        source_path=None,
        url=None,
    )
    _set_job_status(data / "jobs.db", j_target, "done")
    j_target_dir = data / "jobs" / j_target
    j_target_dir.mkdir(parents=True, exist_ok=True)
    target_audio = j_target_dir / "audio.wav"
    target_audio.write_bytes(b"SECRET_TARGET_AUDIO")

    # 攻击任务：目录内放置指向目标任务音频的符号链接
    j_attacker = "22222222222222222222222222222222"
    store.create_job(
        job_id=j_attacker,
        source_name="attacker.mp3",
        tier="fast",
        source_kind="upload",
        source_path=None,
        url=None,
    )
    _set_job_status(data / "jobs.db", j_attacker, "done")
    j_attacker_dir = data / "jobs" / j_attacker
    j_attacker_dir.mkdir(parents=True, exist_ok=True)
    symlink_file = j_attacker_dir / "source.mp3"
    symlink_file.symlink_to(target_audio)

    with TestClient(app) as http:
        # 目标任务正常获取
        resp_target = http.get(f"/jobs/{j_target}/audio")
        assert resp_target.status_code == 200
        assert resp_target.content == b"SECRET_TARGET_AUDIO"

        # 攻击任务通过符号链接串读其他任务音频，应被 404 拒绝
        resp_attacker = http.get(f"/jobs/{j_attacker}/audio")
        assert resp_attacker.status_code == 404
        assert resp_attacker.json()["error"] == "音频文件不存在"


def test_i1_audio_endpoint_fmt_wav(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    app = create_app(data)

    # 1. 原始文件存在且与 wav 并存：默认端点返回原始，?fmt=wav 强制返回 wav（以内容与类型区分）
    j_both = "33333333333333333333333333333333"
    store.create_job(
        job_id=j_both,
        source_name="speech.mp3",
        tier="fast",
        source_kind="upload",
        source_path=None,
        url=None,
    )
    _set_job_status(data / "jobs.db", j_both, "done")
    j_both_dir = data / "jobs" / j_both
    j_both_dir.mkdir(parents=True, exist_ok=True)
    orig_bytes = b"ORIGINAL_MP3_PAYLOAD_BYTES"
    wav_bytes = b"CONVERTED_WAV_PAYLOAD_BYTES"
    (j_both_dir / "source.mp3").write_bytes(orig_bytes)
    (j_both_dir / "audio.wav").write_bytes(wav_bytes)

    # 2. 原始文件存在但缺失 wav 文件：?fmt=wav 返回 404
    j_no_wav = "44444444444444444444444444444444"
    store.create_job(
        job_id=j_no_wav,
        source_name="only_orig.mp3",
        tier="fast",
        source_kind="upload",
        source_path=None,
        url=None,
    )
    _set_job_status(data / "jobs.db", j_no_wav, "done")
    j_no_wav_dir = data / "jobs" / j_no_wav
    j_no_wav_dir.mkdir(parents=True, exist_ok=True)
    (j_no_wav_dir / "source.mp3").write_bytes(orig_bytes)

    with TestClient(app) as http:
        # 默认端点返回原始 mp3
        resp_default = http.get(f"/jobs/{j_both}/audio")
        assert resp_default.status_code == 200
        assert resp_default.headers["content-type"] == "audio/mpeg"
        assert resp_default.content == orig_bytes

        # ?fmt=wav 强制返回 wav，内容/字节严格区分于原始
        resp_fmt_wav = http.get(f"/jobs/{j_both}/audio?fmt=wav")
        assert resp_fmt_wav.status_code == 200
        assert resp_fmt_wav.headers["content-type"] == "audio/wav"
        assert resp_fmt_wav.content == wav_bytes
        assert resp_fmt_wav.content != resp_default.content

        # 缺失 wav 时 ?fmt=wav 返回 404
        resp_missing_wav = http.get(f"/jobs/{j_no_wav}/audio?fmt=wav")
        assert resp_missing_wav.status_code == 404
        assert resp_missing_wav.json()["error"] == "音频文件不存在"






