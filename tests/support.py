"""测试辅助。不放 test_ 前缀，避免被 pytest 收集。"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient


def synth_wav(path: Path, seconds: float = 0.4, rate: int = 44100, channels: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
            f"sine=frequency=440:duration={seconds}:sample_rate={rate}",
            "-ac",
            str(channels),
            str(path),
        ],
        check=True,
    )


def post_file(client: TestClient, path: Path, tier: str | None = None) -> Any:
    data = {} if tier is None else {"tier": tier}
    with path.open("rb") as handle:
        return client.post(
            "/jobs",
            files={"file": (path.name, handle, "application/octet-stream")},
            data=data,
        )


def wait_minutes(client: TestClient, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        response = client.get(f"/jobs/{job_id}/minutes")
        if response.status_code == 200:
            return {"minutes_status": "done", "text": response.text}
        if response.status_code == 404:
            try:
                body = response.json()
            except Exception:
                body = {"error": response.text}
            last = body if isinstance(body, dict) else {"error": str(body)}
            if last.get("minutes_status") == "error":
                return last
        time.sleep(0.05)
    raise AssertionError(f"纪要没有结束：{last}")


def wait_job(client: TestClient, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        response = client.get(f"/jobs/{job_id}")
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        last = body
        if body["status"] in {"done", "error"}:
            return body
        time.sleep(0.05)
    raise AssertionError(f"任务没有结束：{last}")


def probe_audio(path: Path) -> tuple[int, int]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = json.loads(proc.stdout)["streams"][0]
    return int(stream["sample_rate"]), int(stream["channels"])


def synth_webm(path: Path, seconds: float = 0.4) -> None:
    """造一片浏览器可录出的 webm/opus（每片是独立完整容器，与前端实现一致）。"""
    wav = path.with_suffix(".src.wav")
    synth_wav(wav, seconds=seconds, rate=16000, channels=1)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(wav),
            "-c:a",
            "libopus",
            str(path),
        ],
        check=True,
    )
    wav.unlink(missing_ok=True)
