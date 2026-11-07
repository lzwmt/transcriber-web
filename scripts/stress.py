"""长音频压测：提交一条任务，轮询转写与纪要，写出 JSON 与 Markdown 报告。

转写一旦 done，纪要失败或等到超时仍下载逐字稿并填写转写侧校验。
不改业务服务。纯函数可供测试直接导入；联网、ffprobe 与 docker 只在 main 里发生。
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

# 规格固定每 10 秒看一次任务、健康检查和容器内存。
POLL_INTERVAL_S = 10.0
# 规格判据：末段时间戳与音频时长的偏差要小于 5%。
_COVERAGE_LIMIT_PCT = 5.0

# docker stats 用二进制单位。报告里的 MB 与 MiB 对齐，1 GiB = 1024。
_MEM_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>KiB|MiB|GiB|TiB|PiB|EiB|B)\b",
    re.IGNORECASE,
)
_UNIT_TO_MB = {
    "B": 1.0 / (1024 * 1024),
    "KIB": 1.0 / 1024,
    "MIB": 1.0,
    "GIB": 1024.0,
    "TIB": 1024.0 * 1024,
    "PIB": 1024.0**3,
    "EIB": 1024.0**4,
}
# 逐字稿是 [MM:SS]（分钟可以超过 59）或 [HH:MM:SS]。取文本里最后一个。
_STAMP_RE = re.compile(r"\[(\d+):(\d{2})(?::(\d{2}))?\]")
_SECTION_TITLES = ("一句话摘要", "议题", "决定", "待办", "未定事项")
_FAILED_STATUS = {"error", "failed"}


class _Timeout(Exception):
    """整体时限用尽。"""


class StressReport(TypedDict):
    tier: str
    audio: str
    base_url: str
    container: str
    timeout_s: float
    audio_duration_s: float | None
    job_id: str | None
    submitted_at: str | None
    transcribe_done_at: str | None
    minutes_done_at: str | None
    transcribe_s: float | None
    minutes_s: float | None
    total_s: float | None
    rtf: float | None
    restart_count_start: int | None
    restart_count_end: int | None
    health_failures: int
    mem_sample_failures: int
    mem_peak_mb: float | None
    status: str | None
    minutes_status: str | None
    error: str | None
    timeout: bool
    transcript_nonempty: bool
    last_segment_end_s: float | None
    coverage_deviation_pct: float | None
    coverage_ok: bool | None
    minutes_sections_ok: bool
    combined_nonempty: bool
    ok: bool


class ArtifactChecks(TypedDict):
    transcript_nonempty: bool
    last_segment_end_s: float | None
    coverage_deviation_pct: float | None
    coverage_ok: bool | None
    minutes_sections_ok: bool
    combined_nonempty: bool


def parse_docker_mem_mb(stats_text: str) -> float:
    """从 docker stats 的内存列取出已用内存，单位 MB（与 MiB 相同）。"""
    match = _MEM_RE.search(stats_text)
    if match is None:
        raise ValueError(f"无法解析 docker 内存：{stats_text!r}")
    value = float(match.group("value"))
    unit = match.group("unit").upper()
    factor = _UNIT_TO_MB.get(unit)
    if factor is None:
        raise ValueError(f"无法解析 docker 内存单位：{unit}")
    return value * factor


def last_segment_end_s(transcript: str) -> float | None:
    """解析逐字稿最后一个时间戳。没有时间戳时返回 None。"""
    found: float | None = None
    for match in _STAMP_RE.finditer(transcript):
        head = int(match.group(1))
        mid = int(match.group(2))
        tail = match.group(3)
        if tail is None:
            found = float(head * 60 + mid)
        else:
            found = float(head * 3600 + mid * 60 + int(tail))
    return found


def coverage_deviation_pct(last_segment_end_s: float, audio_duration_s: float) -> float:
    """末段时间戳相对音频时长的绝对偏差，单位是百分比。"""
    if audio_duration_s <= 0:
        raise ValueError("audio_duration_s 必须大于 0")
    delta = abs(last_segment_end_s - audio_duration_s)
    return delta / audio_duration_s * 100.0


def minutes_sections_ok(text: str) -> bool:
    """五节都以 Markdown 标题出现才算齐全。正文里的同名文字不算。"""
    found: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        title = stripped.lstrip("#").strip()
        if title in _SECTION_TITLES:
            found.add(title)
    return found == set(_SECTION_TITLES)


def artifact_checks(
    transcript: bytes,
    minutes: bytes,
    combined: bytes,
    audio_duration_s: float | None,
) -> ArtifactChecks:
    """用三件产物的字节和音频时长算出校验字段。拿不到的产物传空字节。"""
    transcript_text = transcript.decode("utf-8", errors="replace")
    minutes_text = minutes.decode("utf-8", errors="replace")
    combined_text = combined.decode("utf-8", errors="replace")
    nonempty = bool(transcript_text.strip())
    last: float | None = None
    deviation: float | None = None
    covered: bool | None = None
    if nonempty:
        last = last_segment_end_s(transcript_text)
        if last is not None and audio_duration_s is not None and audio_duration_s > 0:
            deviation = round(coverage_deviation_pct(last, audio_duration_s), 4)
            covered = deviation < _COVERAGE_LIMIT_PCT
    checked: ArtifactChecks = {
        "transcript_nonempty": nonempty,
        "last_segment_end_s": last,
        "coverage_deviation_pct": deviation,
        "coverage_ok": covered,
        "minutes_sections_ok": minutes_sections_ok(minutes_text),
        "combined_nonempty": bool(combined_text.strip()),
    }
    return checked


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _clip(text: str) -> str:
    line = text.strip().splitlines()
    if not line:
        return ""
    return line[0][:500]


def _short_error(exc: BaseException) -> str:
    return _clip(str(exc)) or exc.__class__.__name__


def _budget(deadline: float, cap: float | None = None) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _Timeout()
    if cap is None:
        return remaining
    return min(cap, remaining)


def _empty_report(args: argparse.Namespace) -> StressReport:
    report: StressReport = {
        "tier": str(args.tier),
        "audio": str(args.audio),
        "base_url": str(args.base_url).rstrip("/"),
        "container": str(args.container),
        "timeout_s": float(args.timeout),
        "audio_duration_s": None,
        "job_id": None,
        "submitted_at": None,
        "transcribe_done_at": None,
        "minutes_done_at": None,
        "transcribe_s": None,
        "minutes_s": None,
        "total_s": None,
        "rtf": None,
        "restart_count_start": None,
        "restart_count_end": None,
        "health_failures": 0,
        "mem_sample_failures": 0,
        "mem_peak_mb": None,
        "status": None,
        "minutes_status": None,
        "error": None,
        "timeout": False,
        "transcript_nonempty": False,
        "last_segment_end_s": None,
        "coverage_deviation_pct": None,
        "coverage_ok": None,
        "minutes_sections_ok": False,
        "combined_nonempty": False,
        "ok": False,
    }
    return report


def _command_output(command: list[str], timeout: float) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"命令超时：{command[0]}") from exc
    if completed.returncode != 0:
        detail = _clip(completed.stderr or completed.stdout or "")
        message = f"命令失败：{command[0]}"
        if detail:
            message = f"{message} {detail}"
        raise RuntimeError(message)
    return completed.stdout.strip()


def _run_within(command: list[str], deadline: float, cap: float) -> str:
    try:
        return _command_output(command, _budget(deadline, cap))
    except RuntimeError as exc:
        if time.monotonic() >= deadline:
            raise _Timeout() from exc
        raise


def _probe_duration(audio: Path, deadline: float) -> float:
    text = _run_within(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio),
        ],
        deadline,
        60,
    )
    try:
        duration = float(text)
    except ValueError as exc:
        raise RuntimeError(f"无法解析音频时长：{text}") from exc
    if duration <= 0:
        raise RuntimeError(f"音频时长无效：{duration}")
    return duration


def _read_restart_count(container: str, timeout: float) -> int:
    text = _command_output(
        ["docker", "inspect", "-f", "{{.RestartCount}}", container],
        timeout,
    )
    try:
        return int(text)
    except ValueError as exc:
        raise RuntimeError(f"无法解析 RestartCount：{text}") from exc


def _restart_count_within(container: str, deadline: float) -> int:
    try:
        return _read_restart_count(container, _budget(deadline, 30))
    except RuntimeError as exc:
        if time.monotonic() >= deadline:
            raise _Timeout() from exc
        raise


def _connection(
    parsed: urllib.parse.SplitResult,
    timeout: float,
) -> http.client.HTTPConnection:
    host = parsed.hostname
    if not host or parsed.scheme not in {"http", "https"}:
        raise RuntimeError(f"不支持的服务地址：{parsed.geturl()}")
    if parsed.scheme == "https":
        port = parsed.port or 443
        return http.client.HTTPSConnection(host, port, timeout=timeout)
    port = parsed.port or 80
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _request_path(parsed: urllib.parse.SplitResult) -> str:
    path = parsed.path or "/"
    if parsed.query:
        return f"{path}?{parsed.query}"
    return path


def _safe_filename(audio: Path) -> str:
    name = audio.name.replace("\\", "_").replace('"', "")
    name = name.replace("\r", "").replace("\n", "")
    return name or "audio"


def _post_audio(url: str, audio: Path, tier: str, timeout: float) -> tuple[int, bytes]:
    parsed = urllib.parse.urlsplit(url)
    boundary = f"----transcriberstress{time.monotonic_ns()}"
    filename = _safe_filename(audio)
    preamble = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="tier"\r\n\r\n'
        f"{tier}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    closing = f"\r\n--{boundary}--\r\n".encode()
    length = len(preamble) + audio.stat().st_size + len(closing)
    conn = _connection(parsed, timeout)
    try:
        conn.putrequest("POST", _request_path(parsed))
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(length))
        conn.putheader("Connection", "close")
        conn.endheaders()
        conn.send(preamble)
        with audio.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                conn.send(chunk)
        conn.send(closing)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _submit(base: str, audio: Path, tier: str, deadline: float) -> str:
    try:
        status, body = _post_audio(f"{base}/jobs", audio, tier, _budget(deadline))
    except TimeoutError as exc:
        raise _Timeout() from exc
    except (OSError, http.client.HTTPException) as exc:
        raise RuntimeError(f"提交失败：{exc}") from exc
    if status != 200:
        detail = _clip(body.decode("utf-8", errors="replace"))
        message = f"提交失败 HTTP {status}"
        if detail:
            message = f"{message}：{detail}"
        raise RuntimeError(message)
    payload = _json_object(body)
    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise RuntimeError("提交失败：响应没有 job_id")
    return job_id


def _http_get(url: str, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("响应不是 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("响应不是 JSON 对象")
    return payload


def _json_object_or_none(raw: bytes) -> dict[str, Any] | None:
    try:
        return _json_object(raw)
    except RuntimeError:
        return None


def _health_ok(status: int, body: bytes) -> bool:
    if status != 200:
        return False
    try:
        payload: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return payload == {"ok": True}


def _get_within(url: str, deadline: float) -> tuple[int, bytes] | None:
    try:
        return _http_get(url, _budget(deadline, 60))
    except (urllib.error.URLError, OSError) as exc:
        if time.monotonic() >= deadline:
            raise _Timeout() from exc
        return None


def _past_deadline(deadline: float, exc: BaseException) -> None:
    if time.monotonic() >= deadline:
        raise _Timeout() from exc


def _sample_health(report: StressReport, base: str, deadline: float, timeout: float) -> None:
    try:
        status, body = _http_get(f"{base}/health", timeout)
    except (urllib.error.URLError, OSError) as exc:
        _past_deadline(deadline, exc)
        report["health_failures"] += 1
        return
    if not _health_ok(status, body):
        report["health_failures"] += 1


def _sample_mem(report: StressReport, container: str, deadline: float, timeout: float) -> None:
    try:
        usage = _command_output(
            ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container],
            timeout,
        )
        used_mb = parse_docker_mem_mb(usage)
    except (RuntimeError, ValueError, OSError) as exc:
        _past_deadline(deadline, exc)
        report["mem_sample_failures"] += 1
        return
    peak = report["mem_peak_mb"]
    if peak is None or used_mb > peak:
        report["mem_peak_mb"] = round(used_mb, 3)


def _sample(report: StressReport, base: str, container: str, deadline: float) -> None:
    timeout = _budget(deadline, 30)
    _sample_health(report, base, deadline, timeout)
    _sample_mem(report, container, deadline, timeout)


def _log_poll(report: StressReport, phase: str) -> None:
    print(
        f"{phase} status={report['status']} minutes_status={report['minutes_status']} "
        f"health_failures={report['health_failures']} mem_peak_mb={report['mem_peak_mb']}",
        flush=True,
    )


def _sleep_poll(deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _Timeout()
    time.sleep(min(POLL_INTERVAL_S, remaining))


def _message(payload: dict[str, Any] | None, default: str) -> str:
    if payload is None:
        return default
    message = payload.get("error")
    if isinstance(message, str) and message.strip():
        return _clip(message)
    return default


def _poll_transcription(
    report: StressReport,
    base: str,
    job_id: str,
    container: str,
    deadline: float,
    submit_mono: float,
) -> float | None:
    url = f"{base}/jobs/{job_id}"
    while True:
        _sample(report, base, container, deadline)
        result = _get_within(url, deadline)
        if result is not None:
            status_code, body = result
            if status_code == 404:
                report["status"] = "error"
                report["error"] = "任务不存在"
            elif status_code == 200:
                payload = _json_object_or_none(body)
                status = payload.get("status") if payload else None
                if isinstance(status, str):
                    report["status"] = status
                if status == "done":
                    observed = time.monotonic()
                    report["transcribe_done_at"] = _now()
                    report["transcribe_s"] = round(observed - submit_mono, 3)
                    _log_poll(report, "transcribe")
                    return observed
                if status in _FAILED_STATUS:
                    report["error"] = _message(payload, "任务失败")
        _log_poll(report, "transcribe")
        if report["error"] is not None:
            return None
        _sleep_poll(deadline)


def _poll_minutes(
    report: StressReport,
    base: str,
    job_id: str,
    container: str,
    deadline: float,
    transcribe_mono: float,
) -> bytes | None:
    url = f"{base}/jobs/{job_id}/minutes"
    while True:
        _sample(report, base, container, deadline)
        result = _get_within(url, deadline)
        if result is not None:
            status_code, body = result
            if status_code == 200:
                observed = time.monotonic()
                report["minutes_done_at"] = _now()
                report["minutes_s"] = round(observed - transcribe_mono, 3)
                report["minutes_status"] = "done"
                _log_poll(report, "minutes")
                return body
            payload = _json_object_or_none(body)
            minutes_status = payload.get("minutes_status") if payload else None
            job_status = payload.get("status") if payload else None
            if isinstance(minutes_status, str):
                report["minutes_status"] = minutes_status
            if isinstance(job_status, str):
                report["status"] = job_status
            if minutes_status == "error" or job_status in _FAILED_STATUS:
                report["error"] = _message(payload, "任务失败")
            elif minutes_status == "done":
                report["error"] = _message(payload, "纪要文件不可用")
        _log_poll(report, "minutes")
        if report["error"] is not None:
            return None
        _sleep_poll(deadline)


def _download_failure(label: str, status: int | None) -> str:
    if status is None:
        return f"{label}下载失败"
    return f"{label}下载失败 HTTP {status}"


def _download_artifact(url: str, deadline: float) -> tuple[int | None, bytes]:
    """下载一件产物。非 200 的响应体丢掉，只留下状态码和空字节。

    整体时限还在时，网络错误仍向外抛。时限已经用尽时改为空字节，
    让纪要等到超时之后仍能把转写产物记进报告。
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        try:
            status, body = _http_get(url, 60)
        except (urllib.error.URLError, TimeoutError, OSError):
            return None, b""
        if status != 200:
            return status, b""
        return status, body
    try:
        status, body = _http_get(url, min(60.0, remaining))
    except TimeoutError as exc:
        if time.monotonic() >= deadline:
            raise _Timeout() from exc
        raise RuntimeError(f"下载超时：{url}") from exc
    except (urllib.error.URLError, OSError) as exc:
        if time.monotonic() >= deadline:
            raise _Timeout() from exc
        raise RuntimeError(f"下载失败：{exc}") from exc
    if status != 200:
        return status, b""
    return status, body


def _note_artifact_errors(
    report: StressReport,
    transcript_status: int | None,
    combined_status: int | None,
) -> None:
    if report["error"] is not None:
        return
    if transcript_status != 200:
        report["error"] = _download_failure("逐字稿", transcript_status)
        return
    if not report["transcript_nonempty"]:
        report["error"] = "逐字稿为空"
        return
    if report["last_segment_end_s"] is None:
        report["error"] = "逐字稿里没有时间戳"
        return
    deviation = report["coverage_deviation_pct"]
    if deviation is None:
        report["error"] = "无法计算覆盖偏差"
        return
    if report["coverage_ok"] is not True:
        report["error"] = f"覆盖偏差 {deviation}% 不低于 5%"
        return
    if not report["minutes_sections_ok"]:
        report["error"] = "纪要缺少五节标题"
        return
    if combined_status != 200:
        report["error"] = _download_failure("合并稿", combined_status)
        return
    if not report["combined_nonempty"]:
        report["error"] = "合并稿为空"


def _apply_artifact_checks(report: StressReport, checks: ArtifactChecks) -> None:
    report["transcript_nonempty"] = checks["transcript_nonempty"]
    report["last_segment_end_s"] = checks["last_segment_end_s"]
    report["coverage_deviation_pct"] = checks["coverage_deviation_pct"]
    report["coverage_ok"] = checks["coverage_ok"]
    report["minutes_sections_ok"] = checks["minutes_sections_ok"]
    report["combined_nonempty"] = checks["combined_nonempty"]


def _check_artifacts(
    report: StressReport,
    base: str,
    job_id: str,
    minutes_body: bytes | None,
    deadline: float,
) -> None:
    transcript = b""
    combined = b""
    transcript_status: int | None = None
    combined_status: int | None = None
    try:
        transcript_status, transcript = _download_artifact(
            f"{base}/jobs/{job_id}/transcript",
            deadline,
        )
        if minutes_body is None:
            _, minutes_body = _download_artifact(f"{base}/jobs/{job_id}/minutes", deadline)
        combined_status, combined = _download_artifact(
            f"{base}/jobs/{job_id}/combined",
            deadline,
        )
    except _Timeout:
        report["timeout"] = True
        if report["error"] is None:
            report["error"] = "超时"
    except RuntimeError as exc:
        # 下载失败不能盖掉纪要错误，也不能跳过已经拿到的逐字稿。
        if report["error"] is None and not report["timeout"]:
            report["error"] = _short_error(exc)
    if minutes_body is None:
        minutes_body = b""
    _apply_artifact_checks(
        report,
        artifact_checks(transcript, minutes_body, combined, report["audio_duration_s"]),
    )
    _note_artifact_errors(report, transcript_status, combined_status)


def _await_minutes(
    report: StressReport,
    base: str,
    job_id: str,
    container: str,
    deadline: float,
    transcribe_mono: float,
) -> bytes | None:
    """轮询纪要。失败或超时返回 None，调用方仍继续校验已完成的转写。"""
    try:
        return _poll_minutes(report, base, job_id, container, deadline, transcribe_mono)
    except _Timeout:
        report["timeout"] = True
        if report["error"] is None:
            report["error"] = "超时"
        return None


def _capture_end(report: StressReport, container: str, submit_mono: float) -> None:
    try:
        report["restart_count_end"] = _read_restart_count(container, 30)
    except (RuntimeError, OSError) as exc:
        if report["error"] is None and not report["timeout"]:
            report["error"] = _short_error(exc)
    total = round(time.monotonic() - submit_mono, 3)
    report["total_s"] = total
    duration = report["audio_duration_s"]
    if duration is not None and duration > 0:
        report["rtf"] = total / duration


def _execute(args: argparse.Namespace, report: StressReport) -> None:
    audio = Path(args.audio)
    base = report["base_url"]
    container = report["container"]
    deadline = time.monotonic() + report["timeout_s"]
    if not audio.is_file():
        raise RuntimeError(f"找不到音频：{audio}")
    if audio.stat().st_size <= 0:
        raise RuntimeError("音频文件为空")
    submit_mono: float | None = None
    try:
        report["audio_duration_s"] = _probe_duration(audio, deadline)
        report["restart_count_start"] = _restart_count_within(container, deadline)
        submit_mono = time.monotonic()
        job_id = _submit(base, audio, report["tier"], deadline)
        report["job_id"] = job_id
        report["submitted_at"] = _now()
        print(f"job_id={job_id}", flush=True)
        transcribe_mono = _poll_transcription(
            report,
            base,
            job_id,
            container,
            deadline,
            submit_mono,
        )
        if transcribe_mono is not None:
            minutes_body = _await_minutes(
                report,
                base,
                job_id,
                container,
                deadline,
                transcribe_mono,
            )
            _check_artifacts(report, base, job_id, minutes_body, deadline)
    except _Timeout:
        report["timeout"] = True
        if report["error"] is None:
            report["error"] = "超时"
    finally:
        if submit_mono is not None:
            _capture_end(report, container, submit_mono)


def _evaluate(report: StressReport) -> None:
    report["ok"] = bool(
        not report["timeout"]
        and report["error"] is None
        and report["status"] == "done"
        and report["minutes_done_at"] is not None
        and report["transcript_nonempty"]
        and report["minutes_sections_ok"]
        and report["combined_nonempty"]
        and report["coverage_ok"] is True
    )


def _md(value: object) -> str:
    if value is None:
        return "—"
    return str(value)


def _yes(value: bool) -> str:
    return "是" if value else "否"


def _coverage_line(report: StressReport) -> str:
    deviation = report["coverage_deviation_pct"]
    if deviation is None or report["coverage_ok"] is None:
        return "—"
    mark = "达到" if report["coverage_ok"] else "未达到"
    return f"{deviation}%（判据 <5%：{mark}）"


def _render_report_md(report: StressReport) -> str:
    if report["timeout"]:
        result = "超时"
    elif report["ok"]:
        result = "通过"
    else:
        result = "失败"
    lines = [
        f"# 压测报告（{report['tier']}）",
        "",
        f"- 结果：{result}",
        f"- 音频：{report['audio']}",
        f"- 音频时长（秒）：{_md(report['audio_duration_s'])}",
        f"- 任务：{_md(report['job_id'])}",
        f"- 服务：{report['base_url']}",
        f"- 容器：{report['container']}",
        f"- 时限（秒）：{report['timeout_s']}",
        "",
        "## 耗时",
        "",
        f"- 提交：{_md(report['submitted_at'])}",
        f"- 转写完成：{_md(report['transcribe_done_at'])}（{_md(report['transcribe_s'])} 秒）",
        f"- 纪要完成：{_md(report['minutes_done_at'])}（{_md(report['minutes_s'])} 秒）",
        f"- 总耗时（秒）：{_md(report['total_s'])}",
        f"- RTF（总耗时 / 音频时长）：{_md(report['rtf'])}",
        "",
        "## 资源",
        "",
        f"- 内存峰值（MB）：{_md(report['mem_peak_mb'])}",
        f"- 内存采样失败次数：{report['mem_sample_failures']}",
        f"- 健康检查失败次数：{report['health_failures']}",
        f"- RestartCount 开始：{_md(report['restart_count_start'])}",
        f"- RestartCount 结束：{_md(report['restart_count_end'])}",
        "",
        "## 产物",
        "",
        f"- 逐字稿非空：{_yes(report['transcript_nonempty'])}",
        f"- 末段时间戳（秒）：{_md(report['last_segment_end_s'])}",
        f"- 覆盖偏差：{_coverage_line(report)}",
        f"- 纪要五节齐全：{_yes(report['minutes_sections_ok'])}",
        f"- 合并稿非空：{_yes(report['combined_nonempty'])}",
        f"- 超时：{_yes(report['timeout'])}",
        "",
        "## 说明",
        "",
        f"- 任务状态：{_md(report['status'])}",
        f"- 纪要状态：{_md(report['minutes_status'])}",
        f"- 错误：{_md(report['error'])}",
        "",
    ]
    return "\n".join(lines)


def _write_reports(out_dir: Path, report: StressReport) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"stress-report-{report['tier']}.json"
    md_path = out_dir / f"stress-report-{report['tier']}.md"
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    json_path.write_text(payload, encoding="utf-8")
    md_path.write_text(_render_report_md(report), encoding="utf-8")
    return json_path, md_path


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="提交一条长音频并写出压测报告")
    parser.add_argument("--audio", required=True, type=Path, help="音频文件路径")
    parser.add_argument(
        "--tier",
        required=True,
        choices=("fast", "precise"),
        help="fast 或 precise",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8900", help="服务根地址")
    parser.add_argument("--timeout", type=float, default=10800, help="整体时限，秒")
    parser.add_argument("--out-dir", required=True, type=Path, help="报告输出目录")
    parser.add_argument(
        "--container",
        default="transcriber-transcriber-1",
        help="采样内存与 RestartCount 的容器名",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if float(args.timeout) < 0:
        print("timeout 不能为负数", file=sys.stderr)
        return 2
    report = _empty_report(args)
    try:
        _execute(args, report)
    except Exception as exc:
        report["error"] = _short_error(exc)
    _evaluate(report)
    try:
        json_path, md_path = _write_reports(Path(args.out_dir), report)
    except OSError as exc:
        print(f"写报告失败：{exc}", file=sys.stderr)
        return 1
    print(f"json={json_path}", flush=True)
    print(f"md={md_path}", flush=True)
    if report["ok"]:
        return 0
    print(report["error"] or "压测未通过", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
