"""页面与 JSON API 共用的业务函数。路由不写 SQL。"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import re
import shutil
import socket
import threading
import time
import unicodedata
import urllib.parse
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from starlette.datastructures import UploadFile

from app import config
from app.jobs import JobStore
from app.minutes import render_minutes_html
from app.pipeline import job_directory, safe_suffix
from app.redact import mask_source_name, redact_sensitive, strip_urls
from app.transcript import format_mmss

logger = logging.getLogger(__name__)

JsonResult = tuple[int, dict[str, Any]]

# 任务号只由 uuid4().hex 生成。路径参数先过这道，避免拿 ../ 去拼目录。
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# 展示时区。库内仍是 UTC 字符串，不在读写存储时改写。
_SHANGHAI = ZoneInfo("Asia/Shanghai")

_STATUS_LABEL = {
    "queued": "排队",
    "running": "运行中",
    "done": "已完成",
    "error": "失败",
}
_TIER_LABEL = {"fast": "快档", "precise": "精档"}
_SOURCE_LABEL = {"url": "链接", "upload": "上传", "recording": "在线录音"}
_MINUTES_LABEL = {
    "pending": "纪要排队中",
    "running": "纪要生成中",
    "done": "纪要已生成",
    "error": "纪要失败",
}


# 浏览器可播格式集合写死为：mp3 / m4a / mp4 / webm / ogg / opus / wav
BROWSER_PLAYABLE_AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {"mp3", "m4a", "mp4", "webm", "ogg", "opus", "wav"}
)

AUDIO_MEDIA_TYPES: dict[str, str] = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "webm": "audio/webm",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "wav": "audio/wav",
}


def resolve_job_audio(directory: Path) -> Path | None:
    """按选源顺序定位可播音频文件：

    1. 原始文件优先：source.*（上传任务）与 download.*（链接任务）。
       若原始文件扩展名在可播集合内，直接使用。
       若原始文件格式不在浏览器可播集合内（如某些容器或格式），直接回落 audio.wav。
    2. 否则回落 audio.wav。
    3. 皆缺返回 None。
    """
    if not directory.is_dir():
        return None

    source_candidates = sorted(p for p in directory.glob("source.*") if p.is_file())
    download_candidates = sorted(p for p in directory.glob("download.*") if p.is_file())
    original_candidates = source_candidates + download_candidates

    if original_candidates:
        orig = original_candidates[0]
        ext = orig.suffix.lower().lstrip(".")
        if ext in BROWSER_PLAYABLE_AUDIO_EXTENSIONS:
            return orig
        # 集合外的原始格式直接回落 audio.wav

    wav_path = directory / "audio.wav"
    if wav_path.is_file():
        return wav_path

    return None


def job_audio_has_fallback(directory: Path) -> bool:
    """当任务同时有「可播集合内的原始文件」与 audio.wav 且两者不是同一文件时为 True。"""
    if not directory.is_dir():
        return False
    source_candidates = sorted(p for p in directory.glob("source.*") if p.is_file())
    download_candidates = sorted(p for p in directory.glob("download.*") if p.is_file())
    original_candidates = source_candidates + download_candidates
    if not original_candidates:
        return False
    orig = original_candidates[0]
    ext = orig.suffix.lower().lstrip(".")
    if ext not in BROWSER_PLAYABLE_AUDIO_EXTENSIONS:
        return False
    wav_path = directory / "audio.wav"
    if not wav_path.is_file():
        return False
    try:
        if orig.resolve() == wav_path.resolve():
            return False
    except OSError:
        return False
    return True


def is_job_id(job_id: str) -> bool:
    return _JOB_ID_RE.fullmatch(job_id) is not None


def validate_tier(value: object) -> str:
    if value is None or value == "":
        return "fast"
    if not isinstance(value, str) or value not in config.VALID_TIERS:
        raise ValueError("tier 必须是 fast 或 precise")
    return value


MAX_SOURCE_NAME_CHARS: int = 120


def validate_source_name(value: object) -> str:
    """改名入参：去首尾空白后非空、≤120 个 Unicode 码点、不含控制字符（含换行）。"""
    if not isinstance(value, str):
        raise ValueError("名称不合法")
    name = value.strip()
    if not name:
        raise ValueError("名称不能为空")
    if len(name) > MAX_SOURCE_NAME_CHARS:
        raise ValueError(f"名称最多 {MAX_SOURCE_NAME_CHARS} 个字符")
    if any(unicodedata.category(char) == "Cc" for char in name):
        raise ValueError("名称不能包含换行或控制字符")
    return name


def rename_job(store: JobStore, job_id: str, value: object) -> JsonResult:
    """只改显示名。任务不存在 404，名称不合法 400，成功回存库后的名字。"""
    if not is_job_id(job_id):
        return 404, {"error": "任务不存在"}
    try:
        name = validate_source_name(value)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    if not store.rename_job(job_id, name):
        return 404, {"error": "任务不存在"}
    return 200, {"ok": True, "source_name": name}


def source_domain(url: str) -> str:
    """来源域名：只给副行/详情元信息用，完整 URL 不进标题位。"""
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""
    host = host.strip().lower()
    if host.startswith("www."):
        host = host[len("www.") :]
    return host


def public_job(store: JobStore, job_id: str) -> dict[str, Any] | None:
    if not is_job_id(job_id):
        return None
    job = store.public(job_id)
    if job is None:
        return None
    shown = dict(job)
    shown["source_name"] = mask_source_name(str(shown.get("source_name") or ""))
    shown["error"] = _sanitize_error(shown.get("error"))
    shown["created_at"] = _shanghai_iso(shown.get("created_at"))
    shown["finished_at"] = _shanghai_iso(shown.get("finished_at"))
    return shown


HostResolver = Callable[[str], Sequence[str | ipaddress.IPv4Address | ipaddress.IPv6Address]]

DNS_TIMEOUT_SECONDS: float = 5.0
MAX_CONCURRENT_DNS_RESOLUTIONS: int = 8
# 默认硬期限 30 秒，从成功 acquire 起算，不随调用方 timeout 加长。
# 正常 DNS 解析远低于此量级。30 秒是挂死判定阈值，不是性能参数。
# timeout 大于硬期限时，闸位在期限到点先被收回，调用方可能仍在 join。
_RESOLVE_SLOT_HARD_DEADLINE_SECONDS: float = 30.0
# 在飞解析线程总上限 32 = 闸位 8 + 已收回但未结束的僵尸额度 24。
_RESOLVE_ZOMBIE_BUDGET: int = 24
_RESOLVE_INFLIGHT_LIMIT: int = MAX_CONCURRENT_DNS_RESOLUTIONS + _RESOLVE_ZOMBIE_BUDGET
_DNS_RESOLUTION_GATE = threading.BoundedSemaphore(MAX_CONCURRENT_DNS_RESOLUTIONS)
_resolve_inflight = 0
_resolve_inflight_lock = threading.Lock()


def _start_resolve_thread(thread: threading.Thread) -> None:
    thread.start()


def _start_resolve_timer(timer: threading.Timer) -> None:
    timer.start()


def _resolve_inflight_count() -> int:
    with _resolve_inflight_lock:
        return _resolve_inflight


def _reserve_resolve_inflight(cap: int) -> bool:
    global _resolve_inflight
    with _resolve_inflight_lock:
        if _resolve_inflight >= cap:
            return False
        _resolve_inflight += 1
        return True


def _release_resolve_inflight() -> None:
    global _resolve_inflight
    with _resolve_inflight_lock:
        _resolve_inflight -= 1


def _resolve_with_timeout(
    resolver: HostResolver,
    hostname: str,
    timeout: float,
    *,
    hard_deadline: float | None = None,
    inflight_limit: int | None = None,
) -> Sequence[str | ipaddress.IPv4Address | ipaddress.IPv6Address]:
    if hard_deadline is not None and hard_deadline <= 0:
        raise ValueError("hard_deadline must be > 0")
    budget = max(0.0, timeout)
    started = time.monotonic()
    if not _DNS_RESOLUTION_GATE.acquire(timeout=budget):
        raise TimeoutError("DNS resolution timed out")
    remaining = budget - (time.monotonic() - started)
    # 硬期限从成功 acquire 起算，不随 timeout 加长。
    # 调用方 timeout 大于硬期限时，闸位会在期限到点被提前收回，调用方可能仍在 join。
    # 现行没有这种调用：check_url_safety 默认 5 秒，本仓测试里的 timeout 也不超过 3 秒。
    limit = (
        _RESOLVE_SLOT_HARD_DEADLINE_SECONDS
        if hard_deadline is None
        else hard_deadline
    )
    cap = _RESOLVE_INFLIGHT_LIMIT if inflight_limit is None else inflight_limit

    released = False
    release_lock = threading.Lock()

    def release_once() -> None:
        nonlocal released
        with release_lock:
            if released:
                return
            released = True
        _DNS_RESOLUTION_GATE.release()

    if not _reserve_resolve_inflight(cap):
        release_once()
        logger.warning("dns resolve inflight cap %s reached; returning slot", cap)
        raise TimeoutError("DNS resolution timed out")

    result: list[Sequence[str | ipaddress.IPv4Address | ipaddress.IPv6Address]] = []
    exception: list[BaseException] = []

    def target() -> None:
        try:
            try:
                result.append(resolver(hostname))
            except BaseException as exc:
                exception.append(exc)
        finally:
            try:
                release_once()
            finally:
                _release_resolve_inflight()

    thread = threading.Thread(target=target, daemon=True)
    # 挂死线程到不了 finally。watchdog 到点归还闸位，线程计入在飞数直到真正结束。
    # 在飞总数上限 32（闸位 8 + 僵尸额度 24）。并发可以超过 8，但不会再往上长。
    # 已达上限时，新调用拿到闸位后立即归还并抛 TimeoutError，不再启动新线程。
    # finally 与 watchdog 只归还一次，避免 BoundedSemaphore 被 release 两次。
    timer = threading.Timer(limit, release_once)
    timer.daemon = True
    try:
        _start_resolve_thread(thread)
    except BaseException:
        release_once()
        _release_resolve_inflight()
        raise
    try:
        _start_resolve_timer(timer)
    except BaseException:
        release_once()
        raise
    thread.join(timeout=max(0.0, remaining))
    if thread.is_alive():
        raise TimeoutError("DNS resolution timed out")
    timer.cancel()
    if exception:
        raise exception[0]
    return result[0]


def default_resolver(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    if host.isdigit():
        try:
            return [ipaddress.ip_address(int(host))]
        except ValueError:
            pass
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return []
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr:
            try:
                ips.append(ipaddress.ip_address(sockaddr[0]))
            except ValueError:
                pass
    return ips


def check_url_safety(
    url: str,
    *,
    resolver: HostResolver | None = None,
    dns_timeout: float | None = None,
) -> tuple[bool, str]:
    if not (url.startswith("http://") or url.startswith("https://")):
        return False, "url 必须是 http 或 https 链接"
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return False, "url 格式无效"

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return False, "url 必须是 http 或 https 链接"

    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        return False, "url 不能包含用户信息"

    hostname = parsed.hostname
    if not hostname:
        return False, "url 缺少主机名"

    timeout = dns_timeout if dns_timeout is not None else DNS_TIMEOUT_SECONDS
    resolve = resolver or default_resolver
    try:
        resolved = _resolve_with_timeout(resolve, hostname, timeout=timeout)
    except TimeoutError:
        return False, "url 解析超时"
    except Exception:
        return False, "url 主机名无法解析"

    if not resolved:
        return False, "url 主机名无法解析"

    for item in resolved:
        if isinstance(item, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            ip = item
        elif isinstance(item, str):
            try:
                ip = (
                    ipaddress.ip_address(int(item))
                    if item.isdigit()
                    else ipaddress.ip_address(item)
                )
            except ValueError:
                return False, f"无效的 IP 地址: {item}"
        else:
            return False, "无效的解析结果"

        if not ip.is_global:
            return False, "禁止访问非公网地址"

    return True, ""


def create_url_job(
    store: JobStore,
    root: Path,
    url: str,
    tier: str,
    *,
    resolver: HostResolver | None = None,
    dns_timeout: float | None = None,
) -> JsonResult:
    ok, error_msg = check_url_safety(url, resolver=resolver, dns_timeout=dns_timeout)
    if not ok:
        return 400, {"error": error_msg}
    job_id = uuid4().hex
    job_directory(root, job_id).mkdir(parents=True, exist_ok=True)
    store.create_job(
        job_id=job_id,
        tier=tier,
        source_name=None,  # 取号与插行在同一事务内完成（I1）
        source_kind="url",
        source_path=None,
        url=url,
    )
    return 200, {"job_id": job_id}


async def create_upload_job(
    store: JobStore,
    root: Path,
    upload: UploadFile,
    tier: str,
) -> JsonResult:
    filename = Path(upload.filename or "audio").name
    if not filename or filename in {".", ".."}:
        filename = "audio"
    job_id = uuid4().hex
    directory = job_directory(root, job_id)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"source{safe_suffix(filename)}"
    limit = config.max_upload_bytes()
    total = 0
    try:
        with destination.open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    handle.close()
                    _remove_tree(directory)
                    return 413, {"error": f"上传文件超过大小上限 {limit} 字节"}
                handle.write(chunk)
    except Exception:
        logger.exception("save upload failed")
        _remove_tree(directory)
        return 400, {"error": "上传文件无法保存"}
    finally:
        await upload.close()
    if total == 0:
        _remove_tree(directory)
        return 400, {"error": "上传文件为空"}
    store.create_job(
        job_id=job_id,
        tier=tier,
        source_name=filename,
        source_kind="upload",
        source_path=str(destination),
        url=None,
    )
    return 200, {"job_id": job_id}


def monthly_usage(store: JobStore, now: datetime) -> dict[str, Any]:
    """当月用量。月界是 Asia/Shanghai，查询范围换成 UTC 后交给 store 的一条 SQL。"""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local = now.astimezone(_SHANGHAI)
    start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_local.month == 12:
        next_local = start_local.replace(year=start_local.year + 1, month=1)
    else:
        next_local = start_local.replace(month=start_local.month + 1)
    raw = store.usage_totals(_utc_stamp(start_local), _utc_stamp(next_local))
    total_seconds = float(raw["total_seconds"])
    precise_count = int(raw["precise_count"])
    text = f"本月 {_format_usage_duration(total_seconds)} · 精档 {precise_count} 次"
    return {
        "total_seconds": total_seconds,
        "precise_count": precise_count,
        "text": text,
    }


def _sanitize_error(value: object) -> str | None:
    if value is None:
        return None
    return strip_urls(redact_sensitive(str(value)))


def recent_failure_banner(store: JobStore, now: datetime) -> dict[str, Any] | None:
    """最近 24 小时内按 finished_at 统计的失败条。没有则 None。原因截到 60 字。"""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    end = now.astimezone(UTC)
    start = end - timedelta(hours=24)
    found = store.recent_failures(_utc_stamp(start), _utc_stamp(end))
    if found is None:
        return None
    return {
        "count": int(found["count"]),
        "latest_name": mask_source_name(str(found.get("latest_name") or "未命名")),
        "latest_error": _truncate_chars(_sanitize_error(found.get("latest_error")), 60),
    }


def list_jobs_for_page(store: JobStore) -> dict[str, Any]:
    """倒序任务。页面最多 100 条，total 是库里的总数。"""
    rows = store.list_recent_jobs(100)
    total = store.count_jobs()
    cards: list[dict[str, Any]] = []
    for job in rows:
        card = _job_card(job)
        status = str(card["status"])
        card["status_text"] = "处理中" if status == "running" else str(card["status_label"])
        card["error_short"] = _truncate_chars(_sanitize_error(job.get("error")), 60)
        card["source_kind"] = str(job.get("source_kind") or "upload")
        card["source_label"] = _SOURCE_LABEL.get(card["source_kind"], "上传")
        card["minutes_mark"] = _minutes_mark(job)
        cards.append(card)
    return {"jobs": cards, "total": total}


def _minutes_mark(job: dict[str, Any]) -> str:
    minutes_status = str(job.get("minutes_status") or "pending")
    if minutes_status == "done":
        return "有纪要"
    if minutes_status == "error":
        return "纪要失败"
    if str(job.get("status") or "") == "done" and minutes_status in {"pending", "running"}:
        return "生成中"
    return "—"


def job_has_unfinished(store: JobStore) -> bool:
    return store.has_unfinished_job()


def _segment_seconds(value: Any) -> float:
    """段起止时间。缺失、None、非数字与非有限值都按现有缺省口径记 0。"""
    raw = value or 0
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return parsed


def job_detail_for_page(
    store: JobStore, root: Path, job_id: str
) -> dict[str, Any] | None:
    if not is_job_id(job_id):
        return None
    job = store.get(job_id)
    if job is None:
        return None
    directory = job_directory(root, job_id)
    minutes_status = str(job.get("minutes_status") or "pending")
    minutes_text = ""
    minutes_html = ""
    if minutes_status == "done":
        minutes_path = directory / "minutes.md"
        if minutes_path.is_file():
            minutes_text = minutes_path.read_text(encoding="utf-8")
            minutes_html = render_minutes_html(minutes_text)
    segments: list[dict[str, Any]] = []
    segments_path = directory / "segments.json"
    if segments_path.is_file():
        try:
            payload = json.loads(segments_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = []
        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                start = _segment_seconds(item.get("start"))
                end = _segment_seconds(item.get("end"))
                segments.append(
                    {
                        "start": start,
                        "end": end,
                        "stamp": format_mmss(start),
                        "speaker": item.get("speaker"),
                        "text": str(item.get("text") or ""),
                    }
                )
    unfinished = job["status"] in {"queued", "running"} or (
        job["status"] == "done" and minutes_status in {"pending", "running"}
    )
    has_audio = False
    has_audio_fallback = False
    if job["status"] == "done":
        has_audio = resolve_job_audio(directory) is not None
        has_audio_fallback = job_audio_has_fallback(directory)
    card = _job_card(job)
    card.update(
        {
            "audio_ready": has_audio,
            "audio_url": f"/jobs/{job_id}/audio" if has_audio else None,
            "audio_has_fallback": has_audio_fallback,
            "minutes_status": minutes_status,
            "minutes_error": _sanitize_error(job.get("minutes_error")),
            "minutes_label": _MINUTES_LABEL.get(minutes_status, minutes_status),
            "minutes_html": minutes_html,
            "minutes_ready": minutes_status == "done" and bool(minutes_text),
            "segments": segments,
            "unfinished": unfinished,
            "processing": job["status"] in {"queued", "running"}
            or minutes_status in {"pending", "running"},
        }
    )
    return card


def delete_job(store: JobStore, root: Path, job_id: str) -> bool:
    if not is_job_id(job_id):
        return False
    existed = store.delete_job(job_id)
    if not existed:
        return False
    _remove_tree(job_directory(root, job_id))
    return True


def regenerate_minutes(store: JobStore, job_id: str) -> JsonResult:
    if not is_job_id(job_id):
        return 404, {"error": "任务不存在"}
    result = store.request_minutes_regenerate(job_id)
    if result is None:
        return 404, {"error": "任务不存在"}
    if result == "not_done":
        return 409, {"error": "任务尚未完成，无法生成纪要"}
    if result == "running":
        return 409, {"error": "纪要正在生成"}
    return 200, {"ok": True, "minutes_status": "pending"}


def minutes_payload(
    store: JobStore, root: Path, job_id: str
) -> tuple[int, dict[str, Any] | Path]:
    if not is_job_id(job_id):
        return 404, {"error": "任务不存在"}
    job = store.get(job_id)
    if job is None:
        return 404, {"error": "任务不存在"}
    if job["status"] != "done":
        return 404, {
            "error": "任务尚未完成，结果不可下载",
            "status": job["status"],
            "minutes_status": job.get("minutes_status"),
        }
    minutes_status = str(job.get("minutes_status") or "pending")
    if minutes_status != "done":
        error = "纪要尚未生成"
        if minutes_status == "error":
            error = _sanitize_error(job.get("minutes_error")) or "纪要生成失败"
        return 404, {
            "error": error,
            "status": job["status"],
            "minutes_status": minutes_status,
        }
    path = (job_directory(root, job_id) / "minutes.md").resolve()
    jobs_root = (root / "jobs").resolve()
    if not path.is_relative_to(jobs_root) or not path.is_file():
        return 404, {
            "error": "结果文件不存在",
            "status": job["status"],
            "minutes_status": minutes_status,
        }
    return 200, path


def combined_payload(
    store: JobStore, root: Path, job_id: str
) -> tuple[int, dict[str, Any] | str]:
    if not is_job_id(job_id):
        return 404, {"error": "任务不存在"}
    status, payload = minutes_payload(store, root, job_id)
    if status != 200:
        return status, payload if isinstance(payload, dict) else {"error": "纪要不可用"}
    minutes_path = payload
    assert isinstance(minutes_path, Path)
    transcript_path = (job_directory(root, job_id) / "transcript.md").resolve()
    jobs_root = (root / "jobs").resolve()
    if not transcript_path.is_relative_to(jobs_root) or not transcript_path.is_file():
        return 404, {
            "error": "逐字稿不存在",
            "status": "done",
            "minutes_status": "done",
        }
    combined = render_combined(
        minutes_path.read_text(encoding="utf-8"),
        transcript_path.read_text(encoding="utf-8"),
    )
    return 200, combined


def render_combined(minutes: str, transcript: str) -> str:
    return minutes.rstrip() + "\n\n---\n\n" + transcript.lstrip()


def _job_card(job: dict[str, Any]) -> dict[str, Any]:
    status = str(job["status"])
    tier = str(job["tier"])
    minutes_status = str(job.get("minutes_status") or "pending")
    return {
        "id": job["id"],
        "status": status,
        "status_label": _STATUS_LABEL.get(status, status),
        "tier": tier,
        "tier_label": _TIER_LABEL.get(tier, tier),
        "source_name": mask_source_name(str(job.get("source_name") or "未命名")),
        "created_at": job.get("created_at"),
        "created_label": _format_created(job.get("created_at")),
        "duration": job.get("duration"),
        "duration_label": _format_duration(job.get("duration")),
        "error": _sanitize_error(job.get("error")),
        "minutes_status": minutes_status,
        "minutes_done": minutes_status == "done",
        "source_kind": str(job.get("source_kind") or "upload"),
        "source_domain": source_domain(str(job.get("url") or "")),
        "meta_line": " · ".join(
            [
                _format_duration(job.get("duration")),
                _TIER_LABEL.get(tier, tier),
                _format_created(job.get("created_at")),
            ]
        ),
    }


def _shanghai_iso(value: object) -> object:
    parsed = _parse_utc_timestamp(value)
    if parsed is None:
        return value
    return parsed.astimezone(_SHANGHAI).isoformat(timespec="microseconds")


def _parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_created(value: object) -> str:
    parsed = _parse_utc_timestamp(value)
    if parsed is None:
        if not isinstance(value, str) or len(value) < 16:
            return "时间未知"
        return value[:16].replace("T", " ")
    return parsed.astimezone(_SHANGHAI).strftime("%Y-%m-%d %H:%M")


def _format_duration(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "时长未知"
    total = int(max(0.0, float(value)))
    minutes, secs = divmod(total, 60)
    if minutes <= 0:
        return f"{secs} 秒"
    return f"{minutes} 分 {secs} 秒"


def _format_usage_duration(seconds: float) -> str:
    """顶栏用量时长。秒数先 int 向 0 截断，再按整分钟向 0 取整。

    - 不足 1 小时：「Y 分钟」。0 秒和不足 1 分钟都是「0 分钟」。
    - 满 1 小时且余分钟为 0：「X 小时」，不写「0 分钟」。
    - 满 1 小时且有余分钟：「X 小时 Y 分钟」。
    """
    total = int(max(0.0, float(seconds)))
    minutes_total = total // 60
    if minutes_total < 60:
        return f"{minutes_total} 分钟"
    hours, minutes = divmod(minutes_total, 60)
    if minutes == 0:
        return f"{hours} 小时"
    return f"{hours} 小时 {minutes} 分钟"


def _truncate_chars(value: object, limit: int) -> str:
    """超长时末尾加「…」，结果长度不超过 limit（省略号占 1）。"""
    if value is None or limit <= 0:
        return ""
    text = str(value)
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1] + "…"


def _utc_stamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _remove_tree(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)
