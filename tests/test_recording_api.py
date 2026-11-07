"""在线录音接口（LZW-62 D 节）：开关、切片校验、拼合、上限、过期。

对照清单：D0b / D2 / D7 / D10 / D11 / D12 / D13。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.support import synth_webm, wait_job


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIBER_FEAT_RECORDING", "1")


def _create(client: TestClient, **payload: Any) -> Any:
    return client.post("/recordings", json=payload or {"tier": "fast"})


def _upload(client: TestClient, session_id: str, index: int, blob: bytes) -> Any:
    return client.post(
        f"/recordings/{session_id}/parts/{index}",
        content=blob,
        headers={"content-type": "application/octet-stream", "x-part-duration": "5"},
    )


def _job_duration(client: TestClient, job_id: str) -> float:
    body = client.get(f"/jobs/{job_id}").json()
    return float(body.get("duration") or 0.0)


def _ffprobe_seconds(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(json.loads(proc.stdout)["format"]["duration"])


# --- D0b：开关 -------------------------------------------------------------


def test_d0b_recording_closed_by_default(client: tuple[TestClient, Path]) -> None:
    """开关默认关：录音页、详情页、全部接口一律 404，且不建会话。"""
    http, _data = client
    assert http.get("/record").status_code == 404
    assert http.post("/recordings", json={"tier": "fast"}).status_code == 404
    unknown = "0" * 32
    assert http.get(f"/recordings/{unknown}").status_code == 404
    assert http.post(f"/recordings/{unknown}/finish", json={}).status_code == 404
    assert http.delete(f"/recordings/{unknown}").status_code == 404
    assert http.get(f"/recordings/{unknown}/view").status_code == 404
    # 工作台没有录音入口
    assert 'href="/record"' not in http.get("/").text


def test_d0b_recording_reopens_with_flag(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, _data = client
    _enable(monkeypatch)
    page = http.get("/record")
    assert page.status_code == 200
    assert "data-record" in page.text
    created = _create(http, tier="fast")
    assert created.status_code == 200
    assert created.json()["session_id"]


def test_d0b_existing_jobs_unaffected(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """开关关掉后，已生成的录音 job 照常可看（开关只挡入口与接口）。"""
    from tests.support import synth_wav

    http, _data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast").json()["session_id"]
    part = tmp_path / "part0.webm"
    synth_webm(part, seconds=1.0)
    assert _upload(http, session_id, 0, part.read_bytes()).status_code == 200
    job_id = http.post(f"/recordings/{session_id}/finish", json={"tier": "fast"}).json()["job_id"]
    monkeypatch.delenv("TRANSCRIBER_FEAT_RECORDING", raising=False)
    assert http.get("/record").status_code == 404
    assert http.get(f"/jobs/{job_id}/view").status_code == 200
    assert synth_wav is not None


# --- D11：切片与会话校验 ---------------------------------------------------


def test_d11_unknown_session_rejected(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, _data = client
    _enable(monkeypatch)
    assert _upload(http, "0" * 32, 0, b"x").status_code == 404
    assert _upload(http, "not-a-session", 0, b"x").status_code == 404
    assert http.get(f"/recordings/{'0' * 32}").status_code == 404


def test_d11_part_index_out_of_range(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, _data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast").json()["session_id"]
    assert _upload(http, session_id, 720, b"x").status_code == 400
    assert _upload(http, session_id, -1, b"x").status_code == 400


def test_d11_part_too_large(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """超片大小上限按 413 拒（流式计数、超限即中断）；上限内的正常片照收。"""
    http, data = client
    _enable(monkeypatch)
    part = tmp_path / "part0.webm"
    synth_webm(part, seconds=0.4)
    limit = part.stat().st_size
    monkeypatch.setenv("TRANSCRIBER_RECORD_PART_BYTES", str(limit))
    session_id = _create(http, tier="fast").json()["session_id"]
    rejected = _upload(http, session_id, 0, b"x" * (limit + 1))
    assert rejected.status_code == 413
    # I7：超限中断后服务端不留任何残留（临时文件与片文件都没有）
    assert list(recording_dir(data, session_id).rglob("*")) == []
    assert _upload(http, session_id, 0, part.read_bytes()).status_code == 200


def test_d11_same_index_is_idempotent(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """同片号重传覆盖旧片、不重复计数（片号集合仍为 {0}）。"""
    http, _data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast").json()["session_id"]
    first_blob = tmp_path / "a.webm"
    second_blob = tmp_path / "b.webm"
    synth_webm(first_blob, seconds=0.4)
    synth_webm(second_blob, seconds=1.0)
    first = _upload(http, session_id, 0, first_blob.read_bytes())
    second = _upload(http, session_id, 0, second_blob.read_bytes())
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["saved_parts"] == 1
    assert second.json()["saved_parts"] == 1
    status = http.get(f"/recordings/{session_id}").json()
    assert status["saved_parts"] == 1
    assert status["next_index"] == 1
    assert abs(status["recorded_seconds"] - 1.0) <= 0.5


def test_d11_open_session_limit(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, _data = client
    _enable(monkeypatch)
    ids = [_create(http, tier="fast").json()["session_id"] for _ in range(3)]
    assert len(set(ids)) == 3
    fourth = _create(http, tier="fast")
    assert fourth.status_code == 409
    assert "待补传" in fourth.json()["error"]
    # 删掉一个后可再建
    assert http.delete(f"/recordings/{ids[0]}").status_code == 200
    assert _create(http, tier="fast").status_code == 200


def test_d11_expired_session_is_cleaned(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TTL 过后列表行转「已过期」，服务端该会话切片目录被清空。"""
    from app import recording
    from app.jobs import JobStore

    http, data = client
    _enable(monkeypatch)
    monkeypatch.setenv("TRANSCRIBER_RECORD_SESSION_TTL_SECONDS", "1")
    session_id = _create(http, tier="fast").json()["session_id"]
    part = tmp_path / "part0.webm"
    synth_webm(part, seconds=1.0)
    assert _upload(http, session_id, 0, part.read_bytes()).status_code == 200
    session_dir = recording.session_directory(data, session_id)
    assert (session_dir / "0.webm").is_file()

    store = JobStore(data / "jobs.db")
    # 用一个远超 TTL 的“现在”触发惰性清理
    from datetime import UTC, datetime, timedelta

    expired = recording.expire_sessions(store, data, now=datetime.now(UTC) + timedelta(seconds=30))
    assert expired == 1
    assert not session_dir.exists()
    expired_row = store.get_recording_session(session_id)
    assert expired_row is not None
    assert expired_row["status"] == "expired"

    cards = recording.session_cards(store, data)
    row = next(card for card in cards if card["id"] == session_id)
    assert row["status_text"] == "已过期"
    assert row["deletable"] is True
    # 过期会话不能再补片
    assert _upload(http, session_id, 1, b"x").status_code == 409


def test_d11_expired_row_appears_in_list(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import recording
    from app.jobs import JobStore

    http, data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast").json()["session_id"]
    part = tmp_path / "part0.webm"
    synth_webm(part, seconds=1.0)
    _upload(http, session_id, 0, part.read_bytes())
    monkeypatch.setenv("TRANSCRIBER_RECORD_SESSION_TTL_SECONDS", "1")
    from datetime import UTC, datetime, timedelta

    future = datetime.now(UTC) + timedelta(seconds=30)
    recording.expire_sessions(JobStore(data / "jobs.db"), data, now=future)
    page = http.get("/").text
    assert "已过期" in page
    assert f'data-delete-session="{session_id}"' in page


# --- D7：待补传行在列表、重启不丢 -----------------------------------------


def test_d7_awaiting_row_survives_restart(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.jobs import JobStore
    from app.main import create_app

    http, data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast", name="周会").json()["session_id"]
    part = tmp_path / "part0.webm"
    synth_webm(part, seconds=1.0)
    _upload(http, session_id, 0, part.read_bytes())
    page = http.get("/").text
    assert "录音待补传" in page
    assert "周会" in page
    assert "/record" in page or "data-record" in page

    # 重启（新进程新 app 实例）后仍显示该行
    store = JobStore(data / "jobs.db")
    assert store.get_recording_session(session_id) is not None
    restarted = create_app(data)
    with TestClient(restarted) as again:
        assert "录音待补传" in again.get("/").text

        # 回页补传剩余片并结束：会话行转成 job 行，状态进「排队/处理中」，不需人工干预
        second = tmp_path / "part1.webm"
        synth_webm(second, seconds=1.0)
        assert _upload(again, session_id, 1, second.read_bytes()).status_code == 200
        finished = again.post(
            f"/recordings/{session_id}/finish", json={"tier": "fast", "name": "周会"}
        )
        assert finished.status_code == 200
        page = again.get("/").text
        assert "录音待补传" not in page
        assert "周会" in page
        assert "排队" in page or "处理中" in page


# --- D13：详情页无播放器无下载 -------------------------------------------


def test_d13_detail_page_has_no_player_or_download(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    http, _data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast").json()["session_id"]
    part = tmp_path / "part0.webm"
    synth_webm(part, seconds=2.0)
    _upload(http, session_id, 0, part.read_bytes())
    page = http.get(f"/recordings/{session_id}/view")
    assert page.status_code == 200
    body = page.text
    assert "data-saved-parts" in body
    assert "data-recorded" in body
    for forbidden in ("<audio", "<video", "download", "/download", "data-play"):
        assert forbidden not in body


# --- D12：两段式录音拼合 ------------------------------------------------


def test_d12_two_segments_concatenate(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """两段独立 webm 拼合后时长等于各段之和（误差 ≤2 秒），且出稿非空。"""
    http, data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast").json()["session_id"]
    first = tmp_path / "a.webm"
    second = tmp_path / "b.webm"
    synth_webm(first, seconds=2.0)
    synth_webm(second, seconds=3.0)
    assert _upload(http, session_id, 0, first.read_bytes()).status_code == 200
    assert _upload(http, session_id, 1, second.read_bytes()).status_code == 200

    finished = http.post(f"/recordings/{session_id}/finish", json={"tier": "fast"})
    assert finished.status_code == 200
    body = finished.json()
    assert body["saved_parts"] == 2
    assert 4.0 <= body["duration"] <= 6.0

    source = data / "jobs" / body["job_id"] / "source.wav"
    assert source.is_file()
    probed = _ffprobe_seconds(source)
    assert abs(probed - body["duration"]) <= 2.0
    # 出稿非空（证明确实走了流水线，而不是把 webm 字节直接拼起来）
    job = wait_job(http, body["job_id"])
    assert job["status"] == "done"
    transcript = http.get(f"/jobs/{body['job_id']}/transcript")
    assert transcript.status_code == 200
    assert transcript.text.strip(), "录音任务必须出稿非空"
    segments = http.get(f"/jobs/{body['job_id']}/segments")
    assert segments.status_code == 200
    assert segments.json(), "录音任务必须有分段"


def test_d12_missing_index_blocks_finish(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """缺号不许拼合，提示先补传。"""
    http, _data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast").json()["session_id"]
    part = tmp_path / "part.webm"
    synth_webm(part, seconds=1.0)
    _upload(http, session_id, 0, part.read_bytes())
    _upload(http, session_id, 2, part.read_bytes())
    blocked = http.post(f"/recordings/{session_id}/finish", json={"tier": "fast"})
    assert blocked.status_code == 409
    assert "缺号" in blocked.json()["error"]


def test_d12_finish_is_idempotent(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    http, _data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast").json()["session_id"]
    part = tmp_path / "part.webm"
    synth_webm(part, seconds=1.0)
    _upload(http, session_id, 0, part.read_bytes())
    first = http.post(f"/recordings/{session_id}/finish", json={"tier": "fast"}).json()
    again = http.post(f"/recordings/{session_id}/finish", json={"tier": "fast"}).json()
    assert first["job_id"] == again["job_id"]
    assert again["reused"] is True


def test_finish_without_parts_is_rejected(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, _data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast").json()["session_id"]
    blocked = http.post(f"/recordings/{session_id}/finish", json={"tier": "fast"})
    assert blocked.status_code == 409


# --- D10：时长闸按来源豁免 ------------------------------------------------


def test_d10_recording_over_limit_passes_gate(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """录音 job 走同一流水线但豁免时长闸：3605 秒不被拒（上限由采集端保证）。"""
    from app import config, pipeline
    from app.jobs import JobStore

    _http, data = client
    store = JobStore(data / "jobs.db")
    job_id = "a" * 32
    source = data / "jobs" / job_id / "source.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    # 造一段 3605 秒的“录音”（8kHz 单声道，避免文件过大）
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono",
            "-t",
            "3605",
            str(source),
        ],
        check=True,
    )
    assert config.max_duration_seconds() == 3600.0
    store.create_job(
        job_id=job_id,
        tier="fast",
        source_name="超限录音",
        source_kind="recording",
        source_path=str(source),
        url=None,
    )
    # 拼合入口实测时长照常落库
    assert pipeline.probe_duration(source) >= 3600.0
    # 录音分支能取到 source_path，不报“录音文件缺失”
    row = store.get(job_id)
    assert row is not None
    materialized = pipeline._materialize_input(row, data / "jobs" / job_id)
    assert Path(materialized).is_file()


def test_d10_upload_over_limit_still_rejected(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """普通上传 job 的时长闸行为不变（同 3605 秒仍被拒）。"""
    from app import config
    from app.pipeline import PipelineError, probe_duration

    http, data = client
    monkeypatch.setenv("TRANSCRIBER_MAX_DURATION_SECONDS", "3600")
    long_wav = tmp_path / "long.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono",
            "-t",
            "3605",
            str(long_wav),
        ],
        check=True,
    )
    duration = probe_duration(long_wav)
    assert duration > config.max_duration_seconds()
    with long_wav.open("rb") as handle:
        rejected = http.post(
            "/jobs",
            files={"file": ("long.wav", handle, "application/octet-stream")},
        )
    # 上传侧照常在转写前按实测时长拒绝：任务置 error 且不出稿
    assert rejected.status_code == 200
    body = wait_job(http, rejected.json()["job_id"])
    assert body["status"] == "error"
    assert "超过上限" in str(body["error"])
    assert PipelineError is not None


# --- D9：上限与阈值只经服务端 env 注入 ------------------------------------


def test_d9_limits_come_from_env_not_request(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """请求体里带同名上限/阈值不生效；env 注入才生效。"""
    http, _data = client
    _enable(monkeypatch)
    monkeypatch.setenv("TRANSCRIBER_RECORD_MAX_SECONDS", "20")
    monkeypatch.setenv("TRANSCRIBER_RECORD_WARN_SECONDS", "8,3")
    body = _create(
        http,
        tier="fast",
        max_seconds=9999,
        warn_seconds=[9999],
        part_seconds=9999,
        part_bytes=9999,
        max_parts=9999,
    ).json()
    assert body["max_seconds"] == 20.0
    assert body["warn_seconds"] == [8.0, 3.0]
    assert body["part_seconds"] == 5.0
    assert body["part_bytes"] == 5 * 1024 * 1024
    assert body["max_parts"] == 720
    # 页面上渲染的也是 env 值
    page = http.get("/record").text
    assert 'data-max-seconds="20' in page
    # 模板用 join(',') 直接渲染浮点数，值本身仍是 env 注入的那一份
    assert 'data-warn-seconds="8.0,3.0"' in page


def test_d1_precise_tier_carries_to_job(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D1：录音页选精档结束录音，生成的 job 档位是精档。"""
    http, _data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="precise").json()["session_id"]
    part = tmp_path / "part.webm"
    synth_webm(part, seconds=1.0)
    assert _upload(http, session_id, 0, part.read_bytes()).status_code == 200
    finished = http.post(
        f"/recordings/{session_id}/finish", json={"tier": "precise", "name": "精档录音"}
    ).json()
    job = http.get(f"/jobs/{finished['job_id']}").json()
    assert job["tier"] == "precise"
    assert job["source_name"] == "精档录音"
    # 来源落库为 recording，列表按「在线录音」标注
    page = http.get("/").text
    assert "在线录音" in page
    assert "精档录音" in page


def test_d6_job_shows_in_list_with_recording_source(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D6：结束并转写后列表出现该任务，来源标「在线录音」。"""
    http, _data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast", name="周会录音").json()["session_id"]
    part = tmp_path / "part.webm"
    synth_webm(part, seconds=1.0)
    _upload(http, session_id, 0, part.read_bytes())
    finished = http.post(
        f"/recordings/{session_id}/finish", json={"tier": "fast", "name": "周会录音"}
    ).json()
    page = http.get("/").text
    assert "周会录音" in page
    assert "在线录音" in page
    assert f"/jobs/{finished['job_id']}/view" in page


# --- 修复轮 r1：B1 / I1 / I2 / I4 / I5 / I6 / I8 -------------------------


def _long_webm(path: Path, seconds: float) -> Path:
    """造一段指定时长的完整 webm（用真实编码，便于逐段解码校验）。"""
    synth_webm(path, seconds=seconds)
    return path


def test_b1_part_longer_than_part_seconds_is_rejected(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B1-1：单片实测时长超过 part_seconds×1.5 即拒（不信客户端名义值）。"""
    http, data = client
    _enable(monkeypatch)
    monkeypatch.setenv("TRANSCRIBER_RECORD_PART_SECONDS", "2")
    session_id = _create(http, tier="fast").json()["session_id"]
    long_part = _long_webm(tmp_path / "long.webm", 5.0)
    rejected = _upload(http, session_id, 0, long_part.read_bytes())
    assert rejected.status_code == 400
    assert "单片时长" in rejected.json()["error"]
    # 拒绝后不留片文件、也不占片号
    assert not (recording_dir(data, session_id) / "0.webm").exists()
    assert http.get(f"/recordings/{session_id}").json()["saved_parts"] == 0
    # 合规长度的片照收
    ok_part = _long_webm(tmp_path / "ok.webm", 2.0)
    assert _upload(http, session_id, 0, ok_part.read_bytes()).status_code == 200


def test_b1_assembled_total_over_limit_is_rejected(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B1-2：拼合总时长超 record_max_seconds+2 秒即拒，且不生成 job。"""
    from app.jobs import JobStore

    http, data = client
    _enable(monkeypatch)
    # 片长放宽到 10 秒、总上限压到 3 秒：两片各 3 秒，合计 6 > 3 + 2 秒容差
    monkeypatch.setenv("TRANSCRIBER_RECORD_PART_SECONDS", "10")
    monkeypatch.setenv("TRANSCRIBER_RECORD_MAX_SECONDS", "3")
    session_id = _create(http, tier="fast").json()["session_id"]
    for index in (0, 1):
        part = _long_webm(tmp_path / f"p{index}.webm", 3.0)
        assert _upload(http, session_id, index, part.read_bytes()).status_code == 200
    store = JobStore(data / "jobs.db")
    before = store.count_jobs()
    rejected = http.post(f"/recordings/{session_id}/finish", json={"tier": "fast"})
    assert rejected.status_code == 413
    assert "超过上限" in rejected.json()["error"]
    assert store.count_jobs() == before, "超限拼合不许生成 job"
    # 会话退回可补传，可删
    row = store.get_recording_session(session_id)
    assert row is not None and row["status"] == "awaiting_upload"


def test_i4_missing_first_or_last_part_blocks_finish(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """I4：片号必须恰为 [0, N)，缺首片或末段被截掉都拒。"""
    http, _data = client
    _enable(monkeypatch)
    part = _long_webm(tmp_path / "p.webm", 1.0)
    blob = part.read_bytes()

    missing_first = _create(http, tier="fast").json()["session_id"]
    assert _upload(http, missing_first, 1, blob).status_code == 200
    rejected = http.post(f"/recordings/{missing_first}/finish", json={"tier": "fast"})
    assert rejected.status_code == 409
    assert "缺号" in rejected.json()["error"]

    # 缺尾片：采集端自报 2 片、服务端只有第 0 片 → 必须拒
    missing_last = _create(http, tier="fast").json()["session_id"]
    assert _upload(http, missing_last, 0, blob).status_code == 200
    tail = http.post(
        f"/recordings/{missing_last}/finish",
        json={"tier": "fast", "expected_parts": 2},
    )
    assert tail.status_code == 409
    assert "缺号" in tail.json()["error"]
    # 补齐第 1 片后同一个 finish 照过
    assert _upload(http, missing_last, 1, blob).status_code == 200
    assert (
        http.post(
            f"/recordings/{missing_last}/finish",
            json={"tier": "fast", "expected_parts": 2},
        ).status_code
        == 200
    )


def test_i5_truncated_part_is_rejected_and_can_be_reuploaded(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """I5：截断片被拒、不留片文件，补传完整片后照收。"""
    http, data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast").json()["session_id"]
    good = _long_webm(tmp_path / "good.webm", 2.0)
    blob = good.read_bytes()
    truncated = blob[: len(blob) // 2]
    rejected = _upload(http, session_id, 0, truncated)
    assert rejected.status_code == 400
    assert "不完整" in rejected.json()["error"] or "无法解码" in rejected.json()["error"]
    assert not (recording_dir(data, session_id) / "0.webm").exists()
    assert http.get(f"/recordings/{session_id}").json()["saved_parts"] == 0
    assert _upload(http, session_id, 0, blob).status_code == 200


def test_i2_finish_then_upload_is_rejected(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """I2：会话 done 之后再到片一律拒（迟到片不许复活会话）。"""
    http, _data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast").json()["session_id"]
    part = _long_webm(tmp_path / "p.webm", 1.0)
    blob = part.read_bytes()
    assert _upload(http, session_id, 0, blob).status_code == 200
    assert http.post(f"/recordings/{session_id}/finish", json={"tier": "fast"}).status_code == 200
    late = _upload(http, session_id, 1, blob)
    assert late.status_code == 409
    assert "已结束" in late.json()["error"]


def test_i2_concurrent_finish_creates_one_job(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """I2：两线程同时 finish 只出一个 job，两次都拿到同一个 job_id。"""
    from concurrent.futures import ThreadPoolExecutor

    from app import recording
    from app.jobs import JobStore

    http, data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast").json()["session_id"]
    part = _long_webm(tmp_path / "p.webm", 1.0)
    assert _upload(http, session_id, 0, part.read_bytes()).status_code == 200

    store = JobStore(data / "jobs.db")
    before = store.count_jobs()

    def run_finish() -> dict[str, Any]:
        return recording.finish_session(
            store, data, session_id, tier="fast", source_name="并发录音"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(run_finish), pool.submit(run_finish)]]
    job_ids = {str(result["job_id"]) for result in results}
    assert len(job_ids) == 1
    assert store.count_jobs() == before + 1


def test_i6_stale_assembling_is_reclaimed(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """I6：assembling 卡死超龄后退回待补传，列表里重新可见且可继续。"""
    from datetime import UTC, datetime, timedelta

    from app import recording
    from app.jobs import JobStore

    http, data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast").json()["session_id"]
    part = _long_webm(tmp_path / "p.webm", 1.0)
    assert _upload(http, session_id, 0, part.read_bytes()).status_code == 200

    store = JobStore(data / "jobs.db")
    # 构造「拼合中途被杀」：直接置 assembling 并把 updated_at 挪到很久以前
    transition, _indices = store.begin_recording_assembly(session_id)
    assert transition == "ok"
    stale = datetime.now(UTC) - timedelta(seconds=600)
    with store._connect() as conn:  # noqa: SLF001 - 测试构造崩溃现场
        conn.execute(
            "UPDATE recording_sessions SET updated_at = ? WHERE id = ?",
            (stale.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), session_id),
        )
    assembling = store.get_recording_session(session_id)
    assert assembling is not None and assembling["status"] == "assembling"

    reclaimed = recording.reclaim_stale_sessions(store, data)
    assert reclaimed == 1
    back = store.get_recording_session(session_id)
    assert back is not None and back["status"] == "awaiting_upload"
    # 回收后会话重新出现在列表，且能正常结束
    assert "录音待补传" in http.get("/").text
    assert http.post(f"/recordings/{session_id}/finish", json={"tier": "fast"}).status_code == 200


def test_i1_default_names_do_not_collide(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """I1：两条不带名字的录音走真实路径，落到两个不同的「未命名N」。"""
    http, _data = client
    _enable(monkeypatch)
    part = _long_webm(tmp_path / "p.webm", 1.0)
    blob = part.read_bytes()
    names: list[str] = []
    for _ in range(2):
        session_id = _create(http, tier="fast").json()["session_id"]
        assert _upload(http, session_id, 0, blob).status_code == 200
        body = http.post(f"/recordings/{session_id}/finish", json={"tier": "fast"}).json()
        job = http.get(f"/jobs/{body['job_id']}").json()
        names.append(str(job["source_name"]))
    assert len(set(names)) == 2, f"默认名必须互不相同，实际：{names}"
    assert all(name.startswith("未命名") for name in names)


def test_i8_recording_job_over_own_limit_fails_in_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I8：录音 job 超专属上限时真跑 pipeline._run 会失败（删专属上限分支必红）。

    不用 client fixture：本用例直调 pipeline._run，不起 HTTP 服务就不会经
    lifespan 拉起后台 worker 子进程（app/worker.py 以 0.25s 轮询 jobs.db），
    否则 worker 会在测试把 job 保持 queued 期间 claim 同一 job 并对同一个
    audio.wav 并发跑 ffmpeg，直调侧 ffprobe 撞上被覆盖写中的文件，偶发报
    「无法读取音频时长」而非「超过上限」（LZW-86 复现记录见 RESULT.md）。
    """
    import pytest as _pytest
    from app import config, pipeline
    from app.jobs import JobStore

    data = tmp_path
    # 上限压到 3 秒：造 6 秒录音，_run 必须按专属上限拒
    monkeypatch.setenv("TRANSCRIBER_RECORD_MAX_SECONDS", "3")
    assert config.record_max_seconds() == 3.0
    job_id = "c" * 32
    source = data / "jobs" / job_id / "source.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono",
            "-t",
            "6",
            str(source),
        ],
        check=True,
    )
    store = JobStore(data / "jobs.db")
    store.create_job(
        job_id=job_id,
        tier="fast",
        source_name="超限录音",
        source_kind="recording",
        source_path=str(source),
        url=None,
    )
    row = store.get(job_id)
    assert row is not None
    with _pytest.raises(pipeline.PipelineError) as caught:
        pipeline._run(store, data, row)
    assert "超过上限" in str(caught.value)
    # 同一条 6 秒音频在普通上传口径下不受录音专属上限约束（对照）
    monkeypatch.setenv("TRANSCRIBER_MAX_DURATION_SECONDS", "60")
    assert pipeline._duration_limit({"source_kind": "upload"}) == 60.0
    assert pipeline._duration_limit({"source_kind": "recording"}) == 5.0


def recording_dir(data: Path, session_id: str) -> Path:
    from app import recording

    return recording.session_directory(data, session_id)



# --- 修复轮 r3：R1 ---------------------------------------------------------


def test_r1_same_prefilled_name_is_renumbered(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R1：两个录音页都带同一个预填「未命名N」，真实 finish 后名字必须不同。"""
    http, _data = client
    _enable(monkeypatch)
    part = _long_webm(tmp_path / "p.webm", 1.0)
    blob = part.read_bytes()
    # 两个录音页同时打开：预览接口给两边的是同一个预填名（都还没占号）
    created = [_create(http, tier="fast").json() for _ in range(2)]
    assert len({item["source_name"] for item in created}) == 1, "预览名应当相同"
    shared = str(created[0]["source_name"])
    assert shared.startswith("未命名")

    names: list[str] = []
    for item in created:
        session_id = str(item["session_id"])
        assert _upload(http, session_id, 0, blob).status_code == 200
        body = http.post(
            f"/recordings/{session_id}/finish",
            json={"tier": "fast", "name": shared},
        ).json()
        names.append(str(http.get(f"/jobs/{body['job_id']}").json()["source_name"]))
    assert len(set(names)) == 2, f"同一个预填名必须被重新取号，实际：{names}"
    # 第一个保留预填值，第二个顺延到新号
    assert shared in names
    assert all(name.startswith("未命名") for name in names)


def test_r1_unoccupied_prefilled_name_is_kept(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R1 反面：预填名没被占用时原样保留（不许无条件改名）。"""
    http, _data = client
    _enable(monkeypatch)
    part = _long_webm(tmp_path / "p.webm", 1.0)
    session_id = _create(http, tier="fast").json()["session_id"]
    assert _upload(http, session_id, 0, part.read_bytes()).status_code == 200
    body = http.post(
        f"/recordings/{session_id}/finish", json={"tier": "fast", "name": "会议纪要"}
    ).json()
    assert http.get(f"/jobs/{body['job_id']}").json()["source_name"] == "会议纪要"


def test_r1_occupied_unnamed_name_on_create_job_is_renumbered(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1：链接侧同一路径也适用——显式传一个已被占用的「未命名N」会被顺延。"""
    from app.jobs import JobStore

    _http, data = client
    _enable(monkeypatch)
    store = JobStore(data / "jobs.db")
    first = store.create_job(
        tier="fast", source_name="未命名7", source_kind="upload", source_path=None, url=None
    )
    second = store.create_job(
        tier="fast", source_name="未命名7", source_kind="upload", source_path=None, url=None
    )
    first_row = store.get(first)
    second_row = store.get(second)
    assert first_row is not None and second_row is not None
    rows = {str(first_row["source_name"]), str(second_row["source_name"])}
    assert len(rows) == 2, f"同名必须顺延，实际：{rows}"


# --- 修复轮 r3：R2 ---------------------------------------------------------

def test_r2_expected_parts_bounds_and_bool(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R2：expected_parts 限 [1, record_max_parts()]，10**9 与 bool 一律 400。"""
    http, _data = client
    _enable(monkeypatch)
    part = _long_webm(tmp_path / "p.webm", 1.0)
    blob = part.read_bytes()
    ceiling = 720  # RECORD_MAX_PARTS_DEFAULT

    # 上界内的正常值通过（=720 时只缺号，不是 400）
    session_id = _create(http, tier="fast").json()["session_id"]
    assert _upload(http, session_id, 0, blob).status_code == 200
    accepted = http.post(
        f"/recordings/{session_id}/finish",
        json={"tier": "fast", "expected_parts": ceiling},
    )
    assert accepted.status_code == 409, "上界值应通过参数校验、因缺号被拒"

    for bad in (10**9, 0, -1, ceiling + 1, True, "720", 1.5):
        target = _create(http, tier="fast").json()["session_id"]
        assert _upload(http, target, 0, blob).status_code == 200
        rejected = http.post(
            f"/recordings/{target}/finish",
            json={"tier": "fast", "expected_parts": bad},
        )
        assert rejected.status_code == 400, f"{bad!r} 应被拒"
        assert "expected_parts" in rejected.json()["error"]
        # 被拒的会话仍可补传/删除，腾出名额给下一轮
        assert http.delete(f"/recordings/{target}").status_code == 200


# --- 修复轮 r3：R3 ---------------------------------------------------------

def test_r3_part_arriving_after_assembly_snapshot_is_rejected(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R3：拼合快照之后到达的片必须被拒（非 200），不许 200 后静默丢弃。"""
    from app.jobs import JobStore

    http, data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast").json()["session_id"]
    part = _long_webm(tmp_path / "p.webm", 1.0)
    blob = part.read_bytes()
    assert _upload(http, session_id, 0, blob).status_code == 200

    store = JobStore(data / "jobs.db")
    transition, indices = store.begin_recording_assembly(session_id)
    assert transition == "ok"
    assert indices == [0], "快照应当只含迁移时刻已提交的片"

    # 快照之后再到一片：状态已是 assembling，必须拒
    late = _upload(http, session_id, 1, blob)
    assert late.status_code == 409, "快照后的片不许回 200"
    assert "已结束" in late.json()["error"] or "不能再补片" in late.json()["error"]
    # 被拒的片不落盘、不写进度行
    assert not (recording_dir(data, session_id) / "1.webm").exists()
    assert [int(p["part_index"]) for p in store.list_recording_parts(session_id)] == [0]


def test_r3_upload_writes_file_before_db(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R3：上传先落盘成正式片、后写进度行——被拒时不出现「DB 有记录而文件不存在」。"""
    from app.jobs import JobStore

    http, data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast").json()["session_id"]
    part = _long_webm(tmp_path / "p.webm", 1.0)
    store = JobStore(data / "jobs.db")
    transition, _indices = store.begin_recording_assembly(session_id)
    assert transition == "ok"
    rejected = _upload(http, session_id, 0, part.read_bytes())
    assert rejected.status_code == 409
    assert store.list_recording_parts(session_id) == []
    assert list(recording_dir(data, session_id).glob("*.webm")) == []
    assert list(recording_dir(data, session_id).glob("*.prev")) == []


# --- 修复轮 r3：R4 ---------------------------------------------------------

def test_r4_complete_after_reclaim_is_rejected(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R4：会话被回收后，原拼合方再 complete 必须被拒：不建 job、不删片。"""
    from datetime import UTC, datetime, timedelta

    from app import recording
    from app.jobs import JobStore

    http, data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast").json()["session_id"]
    part = _long_webm(tmp_path / "p.webm", 1.0)
    assert _upload(http, session_id, 0, part.read_bytes()).status_code == 200

    store = JobStore(data / "jobs.db")
    transition, _indices = store.begin_recording_assembly(session_id)
    assert transition == "ok"
    before_jobs = store.count_jobs()

    # 模拟慢机：回收方按超龄阈值把这路判成卡死
    stale = datetime.now(UTC) - timedelta(seconds=600)
    with store._connect() as conn:  # noqa: SLF001 - 测试构造回收现场
        conn.execute(
            "UPDATE recording_sessions SET updated_at = ? WHERE id = ?",
            (stale.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), session_id),
        )
    assert recording.reclaim_stale_sessions(store, data) == 1
    row = store.get_recording_session(session_id)
    assert row is not None and row["status"] == "awaiting_upload"

    # 原拼合方拿到结果后再登记：被拒，且不留 job、片行还在
    outcome = store.complete_recording_session(
        session_id,
        job_id=recording.job_id_for_session(session_id),
        tier="fast",
        source_name=None,
        source_path=str(tmp_path / "merged.wav"),
    )
    assert outcome == "stale"
    assert store.count_jobs() == before_jobs
    assert [int(p["part_index"]) for p in store.list_recording_parts(session_id)] == [0]


def test_r4_heartbeat_keeps_slow_assembly_alive(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R4：拼合每片刷心跳，慢机大会话不会被惰性回收误判成卡死。"""
    from datetime import UTC, datetime, timedelta

    from app import recording
    from app.jobs import JobStore

    http, data = client
    _enable(monkeypatch)
    session_id = _create(http, tier="fast").json()["session_id"]
    part = _long_webm(tmp_path / "p.webm", 1.0)
    blob = part.read_bytes()
    for index in (0, 1):
        assert _upload(http, session_id, index, blob).status_code == 200

    store = JobStore(data / "jobs.db")
    transition, indices = store.begin_recording_assembly(session_id)
    assert transition == "ok" and indices == [0, 1]

    # 构造「慢机」：拼合开始后 updated_at 已超过回收阈值
    stale = datetime.now(UTC) - timedelta(seconds=600)
    with store._connect() as conn:  # noqa: SLF001 - 测试构造慢机现场
        conn.execute(
            "UPDATE recording_sessions SET updated_at = ? WHERE id = ?",
            (stale.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), session_id),
        )
    before = store.get_recording_session(session_id)
    assert before is not None

    # 真跑一次拼合：每片刷心跳，拼合结束后 updated_at 必须被推到阈值之后
    recording._assemble(store, data, session_id, indices)  # noqa: SLF001
    after = store.get_recording_session(session_id)
    assert after is not None
    assert str(after["status"]) == "assembling", "心跳不许改状态"
    assert after["updated_at"] > before["updated_at"], "每片必须刷心跳"
    # 心跳把 updated_at 推到当下，惰性回收因此不会误伤这一路
    assert recording.reclaim_stale_sessions(store, data) == 0
    # 心跳只在 assembling 时生效（不存在/非 assembling 返回 False，不改状态）
    assert store.touch_recording_assembly("0" * 32) is False
