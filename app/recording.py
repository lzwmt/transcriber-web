"""在线录音（LZW-62）：会话、切片、逐段解码拼合、生成 job。

口径（规格 D 节 + 修复轮 r1）：
- 会话由服务端创建下发 id（uuid4().hex，与 job 分开编号，不可猜测）；
- 5 秒一片，单片 ≤5MB、单会话 ≤720 片，片号 ∈[0,720)，同片号幂等；
- 未完成会话（collecting / awaiting_upload）全局上限 3 个；
- 7 天无新片且未完成即过期，过期会话的切片惰性清理；
- 切片边收边计数落临时文件，校验（可解码、不截断、实测时长 ≤片长 ×1.5）
  通过才原子 rename 并回 200（I5/I7）；
- 拼合必须逐段解码为 WAV 再拼接（禁止字节直拼），片号须恰为 [0, N) 连续，
  拼合后 ffprobe 总时长与各段之和误差 ≤2 秒、且 ≤录音专属上限 +2 秒才生成 job；
- 会话状态迁移、job 登记、会话关闭各在一条事务里完成，重复 finish 不产生第二个 job；
- 上限/阈值只从服务端 env 读，请求参数带同名值不生效。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any
from uuid import uuid4

from app import config
from app.jobs import JobStore
from app.pipeline import PipelineError, job_directory, probe_duration

RECORDINGS_DIRNAME = "recordings"
OPEN_SESSION_LIMIT = 3
# 拼合总时长与各段之和的允许误差（秒），与 ffprobe 口径一致，不走 env。
DURATION_TOLERANCE_SECONDS = config.RECORD_DURATION_TOLERANCE_SECONDS
# 单片实测时长相对片长的允许倍数：浏览器编码会略长，超过即当异常片（B1-1）。
MAX_PART_SECONDS_FACTOR = 1.5
# 解码告警里代表「容器被截断」的标记（I5）：出现即当坏片。
_TRUNCATION_MARKERS = ("File ended prematurely", "Truncating packet")

_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# 会话状态机：collecting → awaiting_upload → assembling → 已生成 job 后 done。
STATUS_COLLECTING = "collecting"
STATUS_AWAITING_UPLOAD = "awaiting_upload"
STATUS_ASSEMBLING = "assembling"
STATUS_DONE = "done"
STATUS_EXPIRED = "expired"

_STATUS_TEXT = {
    STATUS_COLLECTING: "录音中",
    STATUS_AWAITING_UPLOAD: "录音待补传",
    STATUS_ASSEMBLING: "拼合中",
    STATUS_DONE: "已转写",
    STATUS_EXPIRED: "已过期",
}

# 允许继续收片的状态；其余（assembling / done / expired）一律拒（I2）。
_UPLOADABLE = frozenset({STATUS_COLLECTING, STATUS_AWAITING_UPLOAD})


class RecordingError(Exception):
    """带 HTTP 状态码的业务失败。消息本身就是可读摘要，不含堆栈。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def is_session_id(value: str) -> bool:
    return bool(_SESSION_ID_RE.fullmatch(value))


def recordings_root(data: Path) -> Path:
    return data / RECORDINGS_DIRNAME


def session_directory(data: Path, session_id: str) -> Path:
    return recordings_root(data) / session_id


def parse_expected_parts(value: object) -> int | None:
    """校验采集端自报的切片总数（R2）：必须 int、非 bool、落在 [1, record_max_parts()]。

    先判上下界再让调用方构造 range/set，避免 `expected_parts=10**9` 直接 OOM。
    缺省（None）返回 None，表示不按片数校验。
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecordingError(400, "expected_parts 必须是整数片数")
    ceiling = config.record_max_parts()
    if value < 1 or value > ceiling:
        raise RecordingError(400, f"expected_parts 必须在 1 到 {ceiling} 之间")
    return value


def limits_payload() -> dict[str, Any]:
    """下发给录音页的服务端口径。全部来自 env，请求参数改不动。"""
    return {
        "max_seconds": config.record_max_seconds(),
        "warn_seconds": list(config.record_warn_seconds()),
        "part_seconds": config.record_part_seconds(),
        "part_bytes": config.record_part_bytes(),
        "max_parts": config.record_max_parts(),
        "open_limit": OPEN_SESSION_LIMIT,
    }


def create_session(
    store: JobStore,
    data: Path,
    *,
    tier: str,
    source_name: str | None,
) -> dict[str, Any]:
    """建会话。未完成会话已达上限时 409。"""
    name = source_name or store.peek_unnamed_name()
    session_id = store.create_recording_session(
        source_name=name,
        tier=tier,
        max_open=OPEN_SESSION_LIMIT,
    )
    if session_id is None:
        raise RecordingError(
            409,
            f"已有 {OPEN_SESSION_LIMIT} 个录音待补传会话，请先处理或删除后再录",
        )
    session_directory(data, session_id).mkdir(parents=True, exist_ok=True)
    payload = limits_payload()
    payload.update({"session_id": session_id, "source_name": name, "tier": tier})
    return payload


def session_status(store: JobStore, data: Path, session_id: str) -> dict[str, Any]:
    if not is_session_id(session_id):
        raise RecordingError(404, "录音会话不存在")
    session = store.get_recording_session(session_id)
    if session is None:
        raise RecordingError(404, "录音会话不存在")
    parts = session.get("parts") or []
    indices = [int(part["part_index"]) for part in parts]
    recorded = sum(_part_duration(part) for part in parts)
    return {
        "session_id": session_id,
        "status": str(session["status"]),
        "status_text": _STATUS_TEXT.get(str(session["status"]), str(session["status"])),
        "source_name": session.get("source_name"),
        "tier": session.get("tier"),
        "job_id": session.get("job_id"),
        "saved_parts": len(parts),
        "next_index": (max(indices) + 1) if indices else 0,
        "recorded_seconds": round(recorded, 3),
        "limits": limits_payload(),
    }


class PartUpload:
    """一片的上传事务（I5/I7）。

    边写边计数，超限立刻拒（不把 body 全读进内存）；收完后校验「可解码、没被
    截断、实测时长 ≤ 片长 ×1.5」，通过才落盘 rename 并写片进度行，最后回 200。
    """

    def __init__(
        self,
        store: JobStore,
        session_id: str,
        index: int,
        directory: Path,
        temp_path: Path,
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._index = index
        self._directory = directory
        self._temp = temp_path
        self._probe_dir = directory / f"probe-{uuid4().hex}"
        self._limit = config.record_part_bytes()
        self._written = 0
        self._handle: IO[bytes] | None = None

    def open(self) -> None:
        self._handle = self._temp.open("wb")

    def write(self, chunk: bytes) -> None:
        """写一块。超限抛 413；调用方负责 abort。"""
        if self._handle is None:
            raise RecordingError(500, "切片写入未初始化")
        self._written += len(chunk)
        if self._written > self._limit:
            raise RecordingError(413, f"单片超过大小上限 {self._limit} 字节")
        self._handle.write(chunk)

    def abort(self) -> None:
        """丢弃本次上传：关句柄、删临时文件与探针目录。"""
        self._close()
        try:
            self._temp.unlink(missing_ok=True)
        except OSError:
            pass
        _remove_tree(self._probe_dir)

    def finish(self) -> dict[str, Any]:
        """收尾：校验 → 写片进度 → rename。任何失败都不留临时文件。"""
        self._close()
        try:
            payload = self._validate_and_store()
        except BaseException:
            self.abort()
            raise
        _remove_tree(self._probe_dir)
        return payload

    def _close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None

    def _validate_and_store(self) -> dict[str, Any]:
        if self._written == 0:
            raise RecordingError(400, "切片为空")
        measured = _probe_part_duration(self._temp, self._probe_dir)
        ceiling = config.record_part_seconds() * MAX_PART_SECONDS_FACTOR
        if measured > ceiling:
            raise RecordingError(
                400,
                f"单片时长 {measured:.1f} 秒，超过单片上限 {ceiling:.1f} 秒",
            )
        # R3：先落盘成正式片、再写片进度行。顺序反过来会出现「DB 有记录而文件不存在」；
        # 状态判定由 record_part 在同一事务里做，被拒时把正式片回滚成旧内容。
        target = self._directory / f"{self._index}.webm"
        backup = target.with_name(f"{self._index}.{uuid4().hex}.prev")
        had_previous = target.is_file()
        if had_previous:
            target.replace(backup)
        try:
            self._temp.replace(target)
        except OSError as exc:
            if had_previous:
                backup.replace(target)
            raise RecordingError(500, f"切片落盘失败：{exc}") from exc
        outcome = self._store.record_part(
            session_id=self._session_id,
            part_index=self._index,
            size=self._written,
            duration=measured,
            status=STATUS_AWAITING_UPLOAD,
            allowed_from=_UPLOADABLE,
        )
        if outcome != "ok":
            target.unlink(missing_ok=True)
            if had_previous:
                backup.replace(target)
            if outcome == "missing":
                raise RecordingError(404, "录音会话不存在")
            raise RecordingError(409, "录音会话已结束，不能再补片")
        backup.unlink(missing_ok=True)
        parts = self._store.list_recording_parts(self._session_id)
        return {
            "session_id": self._session_id,
            "part_index": self._index,
            "saved_parts": len(parts),
            "bytes": self._written,
            "seconds": round(measured, 3),
            "status": STATUS_AWAITING_UPLOAD,
        }


def begin_part(
    store: JobStore,
    data: Path,
    session_id: str,
    index: int,
) -> PartUpload:
    """开一片的上传：先做只读预检（会话在、状态可收片、片号在范围内）。"""
    if not is_session_id(session_id):
        raise RecordingError(404, "录音会话不存在")
    max_parts = config.record_max_parts()
    if index < 0 or index >= max_parts:
        raise RecordingError(400, f"片号必须在 0 到 {max_parts - 1} 之间")
    session = store.get_recording_session(session_id)
    if session is None:
        raise RecordingError(404, "录音会话不存在")
    status = str(session["status"])
    if status not in _UPLOADABLE:
        if status == STATUS_EXPIRED:
            raise RecordingError(409, "录音会话已过期，不能再补片")
        raise RecordingError(409, "录音会话已结束，不能再补片")
    directory = session_directory(data, session_id)
    directory.mkdir(parents=True, exist_ok=True)
    upload = PartUpload(
        store,
        session_id,
        index,
        directory,
        directory / f"{index}.{uuid4().hex}.part",
    )
    upload.open()
    return upload


def finish_session(
    store: JobStore,
    data: Path,
    session_id: str,
    *,
    tier: str,
    source_name: str | None,
    expected_parts: int | None = None,
) -> dict[str, Any]:
    """拼合各段并生成普通 job（source_kind=recording）。重复调用返回同一个 job。

    expected_parts 是采集端自报的切片总数：带上时缺尾片也能查出来（I4）。
    """
    if not is_session_id(session_id):
        raise RecordingError(404, "录音会话不存在")
    # 纵深防御：调用方已校验过，这里再判一次，保证不会用未限界的值构造 range（R2）。
    expected_parts = parse_expected_parts(expected_parts)
    session = store.get_recording_session(session_id)
    if session is None:
        raise RecordingError(404, "录音会话不存在")
    status = str(session["status"])
    if status == STATUS_DONE and session.get("job_id"):
        return {"session_id": session_id, "job_id": session["job_id"], "reused": True}
    if status == STATUS_EXPIRED:
        raise RecordingError(409, "录音会话已过期，无法拼合")
    if status == STATUS_ASSEMBLING:
        return _wait_for_assembly(store, session_id)

    if not (session.get("parts") or []):
        raise RecordingError(409, "还没有已保存的录音切片")

    transition, indices = store.begin_recording_assembly(session_id)
    if transition == "missing":
        raise RecordingError(404, "录音会话不存在")
    if transition in {"done", "busy"}:
        return _wait_for_assembly(store, session_id)
    if transition == "closed":
        raise RecordingError(409, "录音会话已过期，无法拼合")
    # 片号来自迁移同一事务的快照（R3）：拼合集合恰为迁移时刻的已提交集合，
    # 之后到达的片已因状态为 assembling 被拒，不会「200 后静默丢弃」。
    if not indices:
        store.update_recording_session(session_id, status=STATUS_AWAITING_UPLOAD)
        raise RecordingError(409, "还没有已保存的录音切片")
    # 片号必须恰为 [0, N) 连续；采集端报了总数时还必须齐到 expected_parts（I4）。
    expected = len(indices) if expected_parts is None else expected_parts
    if indices != list(range(expected)):
        missing = sorted(set(range(expected)) - set(indices))
        hint = f"缺第 {missing[0]} 片" if missing else "片号不连续"
        store.update_recording_session(session_id, status=STATUS_AWAITING_UPLOAD)
        raise RecordingError(409, f"录音切片有缺号（{hint}），请先补传")

    job_id = job_id_for_session(session_id)
    directory = job_directory(data, job_id)
    try:
        merged, total, _sums = _assemble(store, data, session_id, indices)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / "source.wav"
        shutil.move(str(merged), str(destination))
    except RecordingError:
        _revert_assembly(store, data, session_id, directory)
        raise
    except Exception as exc:  # 拼合失败要把会话放回可补传，别卡在 assembling
        _revert_assembly(store, data, session_id, directory)
        raise RecordingError(500, f"录音拼合失败：{exc}") from exc

    outcome = store.complete_recording_session(
        session_id,
        job_id=job_id,
        tier=tier,
        source_name=source_name,
        source_path=str(destination),
    )
    if outcome == "missing":
        _revert_assembly(store, data, session_id, directory)
        raise RecordingError(404, "录音会话不存在")
    if outcome == "stale":
        # 拼合期间会话被回收（R4）：本路产物作废，退回待补传，不建 job、不删片。
        _revert_assembly(store, data, session_id, directory)
        raise RecordingError(409, "录音拼合已失效（会话被回收），请重新结束转写")
    _remove_tree(session_directory(data, session_id))
    return {
        "session_id": session_id,
        "job_id": job_id,
        "duration": round(total, 3),
        "saved_parts": len(indices),
        "reused": outcome == "reused",
    }


def job_id_for_session(session_id: str) -> str:
    """由会话 id 派生的 job id：重复执行不会产生第二个 job（I6）。"""
    return hashlib.sha256(f"recording:{session_id}".encode()).hexdigest()[:32]


def _wait_for_assembly(store: JobStore, session_id: str) -> dict[str, Any]:
    """另一个请求正在拼合：等它出结果，返回同一个 job（I2 幂等）。"""
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    while datetime.now(UTC) < deadline:
        session = store.get_recording_session(session_id)
        if session is None:
            raise RecordingError(404, "录音会话不存在")
        status = str(session["status"])
        if status == STATUS_DONE and session.get("job_id"):
            return {"session_id": session_id, "job_id": session["job_id"], "reused": True}
        if status in _UPLOADABLE:
            raise RecordingError(409, "录音拼合未完成，请稍后再试")
        if status == STATUS_EXPIRED:
            raise RecordingError(409, "录音会话已过期，无法拼合")
        time.sleep(0.2)
    raise RecordingError(409, "录音正在拼合，请稍后查看")


def _revert_assembly(store: JobStore, data: Path, session_id: str, directory: Path) -> None:
    """拼合失败：会话退回待补传、清掉拼合临时目录与半成品 job 目录（I6）。

    只在**自己仍持有 assembling** 时才回退与清理：会话已被回收、或另一路拼合已接手时
    不动状态、不删目录，免得把别人的 `source.wav` 删掉（R4）。
    """
    session = store.get_recording_session(session_id)
    if session is not None and str(session["status"]) == STATUS_ASSEMBLING:
        store.update_recording_session(session_id, status=STATUS_AWAITING_UPLOAD)
        _remove_tree(session_directory(data, session_id) / "decoded")
        _remove_tree(directory)


def expire_sessions(store: JobStore, data: Path, now: datetime | None = None) -> int:
    """惰性清理：超龄 assembling 退回待补传；超 TTL 的未完成会话转过期并清切片（I6）。"""
    reclaim_stale_sessions(store, data, now=now)
    moment = _moment(now)
    cutoff = moment - timedelta(seconds=config.record_session_ttl_seconds())
    stamp = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    expired = store.expire_recording_sessions(stamp)
    for session_id in expired:
        _remove_tree(session_directory(data, session_id))
    return len(expired)


def reclaim_stale_sessions(store: JobStore, data: Path, now: datetime | None = None) -> int:
    """把超龄的 assembling 会话退回 awaiting_upload，并清掉拼合临时目录（I6）。

    进程在拼合中途被杀会留下 assembling 卡死且不可见；启动时与惰性清理时各跑一次。
    """
    moment = _moment(now)
    cutoff = moment - timedelta(seconds=config.RECORD_ASSEMBLING_TIMEOUT_SECONDS)
    stamp = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    reclaimed = store.reclaim_stale_assembling(stamp)
    for session_id in reclaimed:
        _remove_tree(session_directory(data, session_id) / "decoded")
    return len(reclaimed)


def _moment(now: datetime | None) -> datetime:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def session_cards(store: JobStore, data: Path) -> list[dict[str, Any]]:
    """列表里的会话行：待补传在前、已过期在后。过期行可手动删除。"""
    expire_sessions(store, data)
    rows: list[dict[str, Any]] = []
    for session in store.open_recording_sessions(100):
        rows.append(_session_card(session, deletable=False))
    for session in _expired_sessions(store):
        rows.append(_session_card(session, deletable=True))
    return rows


def delete_session(store: JobStore, data: Path, session_id: str) -> bool:
    if not is_session_id(session_id):
        return False
    if not store.delete_recording_session(session_id):
        return False
    _remove_tree(session_directory(data, session_id))
    return True


def _expired_sessions(store: JobStore) -> list[dict[str, Any]]:
    return [session for session in store.list_recording_sessions("expired")]


def _session_card(session: dict[str, Any], *, deletable: bool) -> dict[str, Any]:
    status = str(session["status"])
    parts = session.get("parts") or []
    recorded = sum(_part_duration(part) for part in parts)
    created = str(session.get("created_at") or "")
    return {
        "row_kind": "session",
        "id": str(session["id"]),
        "status": status,
        "status_text": _STATUS_TEXT.get(status, status),
        "source_name": str(session.get("source_name") or "未命名"),
        "tier": str(session.get("tier") or "fast"),
        "tier_label": "精档" if str(session.get("tier")) == "precise" else "快档",
        "duration_label": f"{int(recorded // 60)}:{int(recorded % 60):02d}",
        "saved_parts": len(parts),
        "recorded_seconds": round(recorded, 3),
        "minutes_mark": "—",
        "created_at": created,
        "created_label": created.replace("T", " ").replace("Z", ""),
        "view_url": f"/recordings/{session['id']}/view",
        "deletable": deletable,
        "error_short": "",
        "source_domain": "",
        "source_label": "在线录音",
    }


def _part_duration(part: dict[str, Any]) -> float:
    raw = part.get("duration")
    if raw is None:
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    return value


def _probe_part_duration(source: Path, work: Path) -> float:
    """实测一片时长：先解码成 WAV 再 ffprobe；截断或不可解码都当坏片（I5）。"""
    work.mkdir(parents=True, exist_ok=True)
    target = work / "probe.wav"
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
            str(target),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not target.is_file():
        raise RecordingError(400, f"录音切片无法解码：{_tail(proc.stderr)}")
    stderr = proc.stderr or ""
    if any(marker in stderr for marker in _TRUNCATION_MARKERS):
        raise RecordingError(400, "录音切片不完整（上传被截断），请重传这一片")
    try:
        duration = probe_duration(target)
    except PipelineError as exc:
        raise RecordingError(400, f"无法读取切片时长：{exc}") from exc
    if duration <= 0:
        raise RecordingError(400, "录音切片时长为 0，请重传这一片")
    return duration


def _assemble(
    store: JobStore,
    data: Path,
    session_id: str,
    indices: list[int],
) -> tuple[Path, float, float]:
    """逐段解码为 WAV 再拼接，返回（拼合 WAV 路径，拼合时长，各段时长之和）。

    每处理完一片刷一次会话心跳（R4）：慢机上 720 片大会话不会被惰性回收误判成卡死。
    """
    work = session_directory(data, session_id)
    decoded_dir = work / "decoded"
    _remove_tree(decoded_dir)
    decoded_dir.mkdir(parents=True, exist_ok=True)
    decoded: list[Path] = []
    for index in indices:
        source = work / f"{index}.webm"
        if not source.is_file():
            raise RecordingError(409, f"第 {index} 片文件缺失，请重新补传")
        target = decoded_dir / f"{index:04d}.wav"
        _decode_to_wav(source, target)
        decoded.append(target)
        if not store.touch_recording_assembly(session_id):
            # 会话已不在 assembling（被回收或另一路接手）：停止本路拼合（R4）。
            raise RecordingError(409, "录音拼合已失效（会话被回收），请重新结束转写")
    listing = decoded_dir / "concat.txt"
    listing.write_text(
        "".join(f"file '{path.name}'\n" for path in decoded), encoding="utf-8"
    )
    merged = decoded_dir / "merged.wav"
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(merged),
        ],
        capture_output=True,
        text=True,
        cwd=str(decoded_dir),
    )
    if proc.returncode != 0 or not merged.is_file():
        raise RecordingError(500, "录音拼接失败")
    try:
        total = probe_duration(merged)
        sums = sum(probe_duration(path) for path in decoded)
    except PipelineError as exc:
        raise RecordingError(500, f"无法读取拼合时长：{exc}") from exc
    if abs(total - sums) > DURATION_TOLERANCE_SECONDS:
        raise RecordingError(
            500,
            f"拼合时长 {total:.1f} 秒与各段之和 {sums:.1f} 秒误差超过 "
            f"{DURATION_TOLERANCE_SECONDS:.0f} 秒",
        )
    # 录音专属上限：超 1 小时的录音在拼合阶段就拒，不生成 job（B1-2）。
    limit = config.record_max_seconds()
    if total > limit + DURATION_TOLERANCE_SECONDS:
        raise RecordingError(
            413,
            f"录音总时长 {total:.1f} 秒，超过上限 {limit:.0f} 秒",
        )
    return merged, total, sums


def _decode_to_wav(source: Path, destination: Path) -> None:
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
        raise RecordingError(400, f"录音切片无法解码：{_tail(proc.stderr)}")


def _tail(text: str, limit: int = 200) -> str:
    compact = " ".join((text or "").split())
    if not compact:
        return "无详细输出"
    return compact[-limit:]


def _cleanup_parts(store: JobStore, data: Path, session_id: str) -> None:
    _remove_tree(session_directory(data, session_id))
    store.delete_recording_parts(session_id)


def _remove_tree(directory: Path) -> None:
    try:
        shutil.rmtree(directory, ignore_errors=True)
    except OSError:
        return
