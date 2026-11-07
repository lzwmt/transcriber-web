"""输入落盘之后的流水线：抓音频、转 wav、查时长、转写、写产物。"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import config
from app.engines import transcribe
from app.jobs import JobStore
from app.minutes import sanitize_minutes_error, summarize
from app.redact import mask_source_name, redact_sensitive, strip_urls
from app.transcript import render_transcript

logger = logging.getLogger(__name__)

_AUDIO_SUFFIXES = {
    ".wav",
    ".mp3",
    ".m4a",
    ".flac",
    ".ogg",
    ".opus",
    ".webm",
    ".mp4",
    ".mkv",
    ".aac",
    ".wma",
}


class PipelineError(Exception):
    """给任务记录看的失败原因。消息本身就是摘要，不含堆栈。"""


def job_directory(data: Path, job_id: str) -> Path:
    return data / "jobs" / job_id


def run_job(store: JobStore, data: Path, job: dict[str, Any]) -> None:
    job_id = str(job["id"])
    try:
        _run(store, data, job)
    except Exception as exc:
        message = summarize_error(exc)
        logger.error("job %s failed: %s", job_id, message)
        store.mark_error(job_id, message)
        return
    store.mark_done(job_id)
    run_minutes(store, data, job)


def summarize_error(exc: Exception) -> str:
    if isinstance(exc, PipelineError):
        text = str(exc)
    else:
        text = f"任务失败：{exc}"
    text = redact_sensitive(text)
    text = strip_urls(text)
    text = " ".join(text.split())
    if len(text) > 500:
        text = text[:500]
    return text or "任务失败"


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise PipelineError(f"无法读取音频时长：{_tail(proc.stderr)}")
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise PipelineError("无法读取音频时长") from exc


def run_minutes(store: JobStore, data: Path, job: dict[str, Any]) -> None:
    """转写已完成后生成纪要。失败只改 minutes_status，不改 job 的 done。"""
    job_id = str(job["id"])
    store.set_minutes_status(job_id, "running")
    try:
        directory = job_directory(data, job_id)
        segments_path = directory / "segments.json"
        if not segments_path.is_file():
            raise RuntimeError("缺少 segments.json，无法生成纪要")
        payload = json.loads(segments_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise RuntimeError("segments.json 格式无效")
        markdown = summarize(payload)
        (directory / "minutes.md").write_text(markdown, encoding="utf-8")
        store.set_minutes_status(job_id, "done")
    except Exception as exc:
        message = sanitize_minutes_error(exc)
        logger.error("minutes %s failed: %s", job_id, message)
        store.set_minutes_status(job_id, "error", message)


def _duration_limit(job: dict[str, Any]) -> float:
    """时长上限按来源取：录音走专属上限 + 2 秒容差，其余走通用上限（B1-3）。"""
    if str(job["source_kind"]) == "recording":
        return config.record_max_seconds() + config.RECORD_DURATION_TOLERANCE_SECONDS
    return config.max_duration_seconds()

def _run(store: JobStore, data: Path, job: dict[str, Any]) -> None:
    directory = job_directory(data, str(job["id"]))
    directory.mkdir(parents=True, exist_ok=True)
    source = _materialize_input(job, directory)
    wav_path = directory / "audio.wav"
    _to_wav(source, wav_path)
    duration = probe_duration(wav_path)
    store.set_duration(str(job["id"]), duration)
    # 时长闸按来源分口径（修复轮 r1 B1-3 勘误-2，覆盖 §7-② 的「录音豁免」）：
    # 录音走专属上限（拼合侧已先按 record_max_seconds + 2 秒拒过，这里再兜一道），
    # 上传/链接仍用通用上限；两者超限都报同一种 PipelineError。
    limit = _duration_limit(job)
    if duration > limit:
        raise PipelineError(f"音频时长 {duration:.1f} 秒，超过上限 {limit:.0f} 秒")
    segments = transcribe(str(wav_path), str(job["tier"]), job_id=str(job["id"]))
    (directory / "segments.json").write_text(
        json.dumps(segments, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    markdown = render_transcript(
        source_name=mask_source_name(str(job["source_name"] or "unknown")),
        duration=duration,
        tier=str(job["tier"]),
        generated_at=generated_at,
        segments=segments,
    )
    (directory / "transcript.md").write_text(markdown, encoding="utf-8")


def _materialize_input(job: dict[str, Any], directory: Path) -> Path:
    kind = str(job["source_kind"])
    if kind == "upload":
        raw = job.get("source_path")
        if not raw:
            raise PipelineError("上传文件缺失")
        path = Path(str(raw))
        if not path.is_file():
            raise PipelineError("上传文件缺失")
        return path
    if kind == "recording":
        # 录音拼合（LZW-62）：入口侧已把各段解码拼成 source_path 指向的 wav，
        # 这里只复用该路径，不加新列、不再拼一次。
        raw = job.get("source_path")
        if not raw:
            raise PipelineError("录音文件缺失")
        path = Path(str(raw))
        if not path.is_file():
            raise PipelineError("录音文件缺失")
        return path
    if kind == "url":
        url = str(job.get("url") or "")
        if not url:
            raise PipelineError("缺少链接")
        return _download_audio(url, directory)
    raise PipelineError("未知的任务来源")


def _download_audio(url: str, directory: Path) -> Path:
    template = str(directory / "download.%(ext)s")
    proc = subprocess.run(
        [
            *_yt_dlp_bin(),
            "-f",
            "bestaudio/best",
            "--no-playlist",
            "--no-progress",
            "--no-update",
            "--ignore-config",
            "--restrict-filenames",
            "--retries",
            "0",
            "--fragment-retries",
            "0",
            "--socket-timeout",
            "15",
            "-o",
            template,
            url,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = _tail(proc.stderr or proc.stdout)
        raise PipelineError(f"链接抓取失败：{detail}")
    files = sorted(path for path in directory.glob("download.*") if path.is_file())
    if not files:
        raise PipelineError("链接抓取失败：没有得到音频文件")
    return files[0]


def _to_wav(source: Path, destination: Path) -> None:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not destination.is_file():
        raise PipelineError(f"音频预处理失败：{_tail(proc.stderr)}")


def _tail(text: str, limit: int = 300) -> str:
    compact = " ".join((text or "").split())
    if not compact:
        return "无详细输出"
    if len(compact) > limit:
        compact = compact[-limit:]
    return redact_sensitive(compact)


def _yt_dlp_bin() -> list[str]:
    bundled = Path(sys.executable).with_name("yt-dlp")
    if bundled.is_file():
        return [str(bundled)]
    return ["yt-dlp"]


def safe_suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in _AUDIO_SUFFIXES and len(suffix) <= 8:
        return suffix
    return ".bin"
