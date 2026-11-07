"""造数。POST /jobs 只能得到排队任务，稳定的多状态和 24 小时窗口靠直写任务库。"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from verify.instance import post_json

# 列表页把 running 显示成「处理中」，详情页徽标仍是「运行中」。这里对齐列表。
LABEL_BY_STATUS = {
    "queued": "排队",
    "running": "处理中",
    "done": "已完成",
    "error": "失败",
}
REQUIRED_STATUSES = ("running", "done", "error")
INSIDE_NAME = "24h内失败.wav"
OUTSIDE_NAME = "24h外失败.wav"
INSIDE_ERROR = "样例原因：文件无法解码"
OUTSIDE_ERROR = "过期样例原因"
DONE_NAME = "全完成.wav"
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class SeededJob:
    job_id: str
    status: str
    source_name: str
    label: str


@dataclass(frozen=True)
class FailurePair:
    inside: SeededJob
    outside: SeededJob


def seed_empty(data_dir: Path) -> None:
    """空实例：确认任务库里没有任务，不写入。"""
    count = len(jobs_in(data_dir))
    if count != 0:
        raise RuntimeError(f"空实例要求任务数为 0，实际 {count}")


def seed_multi_status(data_dir: Path, now: datetime | None = None) -> list[SeededJob]:
    """处理中、已完成、失败各一条。失败的 finished_at 放在 24 小时外，避免冒出失败条。"""
    moment = _aware(now)
    specs = (
        ("running", "处理中.wav", moment - timedelta(seconds=30), None, None, "pending"),
        (
            "done",
            "已完成.wav",
            moment - timedelta(minutes=5),
            moment - timedelta(hours=2),
            None,
            "done",
        ),
        (
            "error",
            "失败.wav",
            moment - timedelta(hours=48),
            moment - timedelta(hours=48),
            "引擎失败",
            "error",
        ),
    )
    seeded: list[SeededJob] = []
    for status, name, created_at, finished_at, error, minutes_status in specs:
        job_id = _insert(
            data_dir,
            status=status,
            source_name=name,
            created_at=created_at,
            finished_at=finished_at,
            error=error,
            minutes_status=minutes_status,
            duration=65.0 if status == "done" else None,
        )
        seeded.append(
            SeededJob(job_id, status, name, LABEL_BY_STATUS[status])
        )
    present = {job.status for job in seeded}
    missing = [status for status in REQUIRED_STATUSES if status not in present]
    if missing:
        raise RuntimeError(f"多状态造数缺少 {missing}")
    return seeded


def seed_failures_24h(data_dir: Path, now: datetime | None = None) -> FailurePair:
    """一条失败落在 24 小时内，一条落在 24 小时外。窗口按 finished_at。"""
    moment = _aware(now)
    inside_at = moment - timedelta(hours=1)
    outside_at = moment - timedelta(hours=25)
    inside = SeededJob(
        _insert(
            data_dir,
            status="error",
            source_name=INSIDE_NAME,
            created_at=inside_at,
            finished_at=inside_at,
            error=INSIDE_ERROR,
            minutes_status="error",
            duration=None,
        ),
        "error",
        INSIDE_NAME,
        LABEL_BY_STATUS["error"],
    )
    outside = SeededJob(
        _insert(
            data_dir,
            status="error",
            source_name=OUTSIDE_NAME,
            created_at=outside_at,
            finished_at=outside_at,
            error=OUTSIDE_ERROR,
            minutes_status="error",
            duration=None,
        ),
        "error",
        OUTSIDE_NAME,
        LABEL_BY_STATUS["error"],
    )
    return FailurePair(inside, outside)


def seed_all_done(data_dir: Path, now: datetime | None = None) -> list[SeededJob]:
    """只有已完成任务，纪要也已结束，首页不轮询。"""
    moment = _aware(now)
    finished = moment - timedelta(hours=2)
    job_id = _insert(
        data_dir,
        status="done",
        source_name=DONE_NAME,
        created_at=finished,
        finished_at=finished,
        error=None,
        minutes_status="done",
        duration=65.0,
    )
    return [SeededJob(job_id, "done", DONE_NAME, LABEL_BY_STATUS["done"])]


def write_short_wav(path: Path, seconds: float = 0.2) -> None:
    """短 wav，只为了让上传任务能走进桩引擎。时长不代替 STUB_SLEEP。"""
    # 依赖宿主机自带的 ffmpeg（lavfi 正弦波），verify 不负责安装。
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
            f"sine=frequency=440:duration={seconds}",
            "-ac",
            "1",
            str(path),
        ],
        check=True,
    )


def seed_player_job(data_dir: Path, now: datetime | None = None) -> SeededJob:
    """带可播音频与多段逐字稿的已完成任务。供 PLAYER-BASIC 与 PLAYER-SYNC 使用。"""
    moment = _aware(now)
    finished = moment - timedelta(minutes=10)
    job_id = _insert(
        data_dir,
        status="done",
        source_name="样例文稿.wav",
        created_at=finished,
        finished_at=finished,
        error=None,
        minutes_status="done",
        duration=12.0,
    )
    job_dir = data_dir / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    write_short_wav(job_dir / "audio.wav", seconds=12.0)
    segments = [
        {
            "start": 0.0,
            "end": 4.0,
            "speaker": "王磊",
            "text": "上周灰度的数据出来了，新用户次日留存从 31% 涨到 38%。",
        },
        {
            "start": 4.0,
            "end": 8.0,
            "speaker": "陈晓",
            "text": "付费页我们一直没改，用户看到的还是老价格方案，这个结果不意外。",
        },
        {
            "start": 8.0,
            "end": 12.0,
            "speaker": "李然",
            "text": "那这周先把付费页的 A/B 方案定下来，周五之前给我两个版本。",
        },
    ]
    (job_dir / "segments.json").write_text(
        json.dumps(segments, ensure_ascii=False), encoding="utf-8"
    )
    (job_dir / "minutes.md").write_text(
        "# 纪要\n\n新引导次日留存提升，本周聚焦付费页测试。\n", encoding="utf-8"
    )
    (job_dir / "transcript.md").write_text(
        "00:00 王磊: 上周灰度数据出来了\n", encoding="utf-8"
    )
    return SeededJob(job_id, "done", "样例文稿.wav", LABEL_BY_STATUS["done"])


def seed_player_empty_jobs(
    data_dir: Path, now: datetime | None = None
) -> tuple[SeededJob, SeededJob]:
    """一条未完成（running）任务，一条已完成但无音频的任务。供 PLAYER-EMPTY 使用。"""
    moment = _aware(now)
    running_id = _insert(
        data_dir,
        status="running",
        source_name="处理中文稿.wav",
        created_at=moment - timedelta(seconds=20),
        finished_at=None,
        error=None,
        minutes_status="pending",
        duration=None,
    )
    running_job = SeededJob(
        running_id, "running", "处理中文稿.wav", LABEL_BY_STATUS["running"]
    )

    missing_id = _insert(
        data_dir,
        status="done",
        source_name="缺音频文稿.wav",
        created_at=moment - timedelta(hours=1),
        finished_at=moment - timedelta(hours=1),
        error=None,
        minutes_status="done",
        duration=30.0,
    )
    missing_dir = data_dir / "jobs" / missing_id
    missing_dir.mkdir(parents=True, exist_ok=True)
    segments = [
        {"start": 0.0, "end": 2.0, "speaker": "李然", "text": "测试缺失音频。"}
    ]
    (missing_dir / "segments.json").write_text(
        json.dumps(segments, ensure_ascii=False), encoding="utf-8"
    )
    (missing_dir / "minutes.md").write_text(
        "# 纪要\n\n测试缺失音频纪要内容。\n", encoding="utf-8"
    )
    missing_job = SeededJob(
        missing_id, "done", "缺音频文稿.wav", LABEL_BY_STATUS["done"]
    )

    return running_job, missing_job


def submit_file_job(base_url: str, path: Path, tier: str = "fast") -> str:
    """multipart 上传。POST /jobs 的成功字段是 job_id。"""
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"上传文件不可用：{path}")
    boundary = uuid4().hex
    filename = path.name.replace('"', "")
    if not filename:
        raise RuntimeError(f"上传文件没有文件名：{path}")
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="tier"\r\n\r\n{tier}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode()
        + path.read_bytes()
        + f"\r\n--{boundary}--\r\n".encode()
    )
    request = urllib.request.Request(
        base_url.rstrip("/") + "/jobs",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        payload = json.loads(raw.decode())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"POST /jobs 返回的不是 JSON（HTTP {status}）") from exc
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"POST /jobs HTTP {status}：{payload}")
    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
        raise RuntimeError(f"POST /jobs 没有返回 job_id：{payload}")
    return job_id


def submit_url_job(base_url: str, url: str, tier: str = "fast") -> str:
    """API 造数通道。POST /jobs 的成功字段是 job_id。"""
    status, body = post_json(base_url, "/jobs", {"url": url, "tier": tier})
    if status != 200:
        raise RuntimeError(f"POST /jobs HTTP {status}：{body}")
    job_id = body.get("job_id")
    if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
        raise RuntimeError(f"POST /jobs 没有返回 job_id：{body}")
    return job_id


def jobs_in(data_dir: Path) -> list[dict[str, object]]:
    db = data_dir / "jobs.db"
    if not db.is_file():
        raise RuntimeError(f"任务库不存在：{db}")
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        rows = conn.execute(
            """
            SELECT id, status, source_name, finished_at, error, minutes_status
            FROM jobs
            ORDER BY created_at, rowid
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _insert(
    data_dir: Path,
    *,
    status: str,
    source_name: str,
    created_at: datetime,
    finished_at: datetime | None,
    error: str | None,
    minutes_status: str,
    duration: float | None,
) -> str:
    db = data_dir / "jobs.db"
    if not db.is_file():
        raise RuntimeError(f"任务库不存在：{db}")
    job_id = uuid4().hex
    conn = sqlite3.connect(db, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            """
            INSERT INTO jobs (
                id, status, tier, source_name, source_kind, source_path, url,
                created_at, finished_at, error, duration, minutes_status
            ) VALUES (?, ?, 'fast', ?, 'upload', NULL, NULL, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                status,
                source_name,
                _stamp(created_at),
                _stamp(finished_at) if finished_at is not None else None,
                error,
                duration,
                minutes_status,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return job_id


def _aware(now: datetime | None) -> datetime:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _stamp(value: datetime) -> str:
    """与 app.service 写入任务库的 UTC 文本一致，便于按字符串比较 24 小时窗口。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
