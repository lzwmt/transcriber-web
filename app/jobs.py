"""SQLite 任务表。转写状态 queued / running / done / error；纪要另有 minutes_status。"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

STATUSES = frozenset({"queued", "running", "done", "error"})
MINUTES_STATUSES = frozenset({"pending", "running", "done", "error"})


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _public(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "status": row["status"],
        "tier": row["tier"],
        "source_name": row["source_name"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
        "minutes_status": row["minutes_status"],
        "minutes_error": row["minutes_error"],
    }


class JobStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
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
            self._migrate(conn)
            conn.commit()
        finally:
            conn.close()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "duration" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN duration REAL")
        if "minutes_status" not in columns:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN minutes_status TEXT NOT NULL DEFAULT 'pending'"
            )
        if "minutes_error" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN minutes_error TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs (created_at)"
        )
        # 「未命名N」的全局计数器与任务同库：服务重启不重置，清空库后才从 1 起。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS counters (
                name TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            )
            """
        )
        # 在线录音会话与切片进度（LZW-62）：与任务同库，服务重启不丢；
        # worker 不 claim 这两张表，只有网页/API 侧读写。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recording_sessions (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                source_name TEXT,
                tier TEXT NOT NULL,
                job_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recording_parts (
                session_id TEXT NOT NULL,
                part_index INTEGER NOT NULL,
                bytes INTEGER NOT NULL,
                duration REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, part_index)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recording_sessions_updated_at "
            "ON recording_sessions (updated_at)"
        )

    def create_job(
        self,
        *,
        tier: str,
        source_name: str | None = None,
        source_kind: str,
        source_path: str | None,
        url: str | None,
        job_id: str | None = None,
    ) -> str:
        """建任务行。source_name=None 时在**同一事务内**取号并插入（I1）。"""
        job_id = job_id or uuid4().hex
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            resolved_name = self._resolve_source_name(conn, source_name)
            conn.execute(
                """
                INSERT INTO jobs (
                    id, status, tier, source_name, source_kind, source_path, url,
                    created_at, minutes_status
                ) VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (job_id, tier, resolved_name, source_kind, source_path, url, _now()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return job_id

    # 「未命名N」只由链接/录音的默认命名消费：全局单一计数器，只增不复用，
    # 撞上已有标题（如手工改名占用）时顺延，取号与写库在同一事务里完成。
    _UNNAMED_COUNTER = "unnamed"
    _UNNAMED_PREFIX = "未命名"
    _UNNAMED_PATTERN = re.compile(r"^未命名\d+$")

    def _resolve_source_name(self, conn: sqlite3.Connection, source_name: str | None) -> str:
        """定最终显示名：未传名、或传的「未命名N」已被占用时，在同一事务内重新取号（I1/R1）。

        录音页预填的「未命名N」只是预览、不占号；两页同时结束会带着同一个预填值过来。
        把它当作未命名重新取号，既不改前端，也不会落出两条同名任务。
        """
        if source_name is None:
            return self._allocate_unnamed_name(conn)
        if not self._UNNAMED_PATTERN.match(source_name):
            return source_name
        taken = conn.execute(
            "SELECT 1 FROM jobs WHERE source_name = ? LIMIT 1", (source_name,)
        ).fetchone()
        if taken is None:
            return source_name
        return self._allocate_unnamed_name(conn)

    def _next_unnamed_candidate(self, conn: sqlite3.Connection) -> str:
        """在给定连接上算出下一个可用的「未命名N」，**只读不写**（撞号顺延）。"""
        row = conn.execute(
            "SELECT value FROM counters WHERE name = ?", (self._UNNAMED_COUNTER,)
        ).fetchone()
        number = int(row["value"]) if row is not None else 0
        while True:
            number += 1
            candidate = f"{self._UNNAMED_PREFIX}{number}"
            taken = conn.execute(
                "SELECT 1 FROM jobs WHERE source_name = ? LIMIT 1", (candidate,)
            ).fetchone()
            if taken is None:
                return candidate

    def _allocate_unnamed_name(self, conn: sqlite3.Connection) -> str:
        """在当前事务内取号并写回计数器。调用方负责 BEGIN/commit，取号与插行因此同事务。"""
        candidate = self._next_unnamed_candidate(conn)
        number = int(candidate[len(self._UNNAMED_PREFIX) :])
        conn.execute(
            """
            INSERT INTO counters (name, value) VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET value = excluded.value
            """,
            (self._UNNAMED_COUNTER, number),
        )
        return candidate

    def peek_unnamed_name(self) -> str:
        """预览下一个「未命名N」：**只看不取号、不落库**。录音页预填用它。"""
        conn = self._connect()
        try:
            return self._next_unnamed_candidate(conn)
        finally:
            conn.close()

    def rename_job(self, job_id: str, name: str) -> bool:
        """只改显示名（source_name）。任务不存在返回 False。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                "UPDATE jobs SET source_name = ? WHERE id = ?", (name, job_id)
            )
            conn.commit()
            return int(updated.rowcount) == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, job_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return dict(row)

    def public(self, job_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return _public(row)

    def claim_next(self) -> dict[str, Any] | None:
        """把最早的 queued 任务原子地改成 running。没有任务时返回 None。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'queued'
                ORDER BY created_at, rowid
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            updated = conn.execute(
                "UPDATE jobs SET status = 'running' WHERE id = ? AND status = 'queued'",
                (row["id"],),
            )
            conn.commit()
            if updated.rowcount != 1:
                return None
            data = dict(row)
            data["status"] = "running"
            return data
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_done(self, job_id: str) -> None:
        self._finish(job_id, "done", None)

    def mark_error(self, job_id: str, message: str) -> None:
        self._finish(job_id, "error", message)

    def _finish(self, job_id: str, status: str, message: str | None) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, error = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (status, message, _now(), job_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_running_as_error(self, message: str) -> int:
        """服务重启或 worker 崩溃时，把仍是 running 的任务标成 error。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """
                UPDATE jobs
                SET status = 'error', error = ?, finished_at = ?
                WHERE status = 'running'
                """,
                (message, _now()),
            )
            minutes = conn.execute(
                """
                UPDATE jobs
                SET minutes_status = 'error', minutes_error = ?
                WHERE minutes_status = 'running'
                """,
                (message,),
            )
            conn.commit()
            return int(updated.rowcount) + int(minutes.rowcount)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_recent_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        """按 created_at、rowid 倒序取最近 limit 条。入口限制在 1..100。"""
        clamped_limit = max(1, min(limit, 100))
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (clamped_limit,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def count_jobs(self) -> int:
        """全部任务数，不受列表封顶影响。"""
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS job_count FROM jobs").fetchone()
        finally:
            conn.close()
        assert row is not None
        return int(row["job_count"])

    def has_unfinished_job(self) -> bool:
        """排队、处理中，或已完成但纪要仍 pending/running。"""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM jobs
                    WHERE status IN ('queued', 'running')
                       OR (
                            status = 'done'
                            AND minutes_status IN ('pending', 'running')
                       )
                ) AS unfinished
                """
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        return bool(row["unfinished"])

    def set_duration(self, job_id: str, duration: float) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE jobs SET duration = ? WHERE id = ?",
                (duration, job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def set_minutes_status(
        self, job_id: str, status: str, error: str | None = None
    ) -> None:
        if status not in MINUTES_STATUSES:
            raise ValueError(f"未知纪要状态：{status}")
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE jobs
                SET minutes_status = ?, minutes_error = ?
                WHERE id = ?
                """,
                (status, error, job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def claim_minutes(self) -> dict[str, Any] | None:
        """把已转写完成且纪要 pending 的任务改成 running。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'done' AND minutes_status = 'pending'
                ORDER BY created_at, rowid
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            updated = conn.execute(
                """
                UPDATE jobs
                SET minutes_status = 'running', minutes_error = NULL
                WHERE id = ? AND minutes_status = 'pending' AND status = 'done'
                """,
                (row["id"],),
            )
            conn.commit()
            if updated.rowcount != 1:
                return None
            data = dict(row)
            data["minutes_status"] = "running"
            data["minutes_error"] = None
            return data
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def request_minutes_regenerate(self, job_id: str) -> str | None:
        """把纪要标成 pending 以便 worker 重新生成。任务不存在返回 None。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                conn.commit()
                return None
            if row["status"] != "done":
                conn.commit()
                return "not_done"
            if row["minutes_status"] == "running":
                conn.commit()
                return "running"
            conn.execute(
                """
                UPDATE jobs
                SET minutes_status = 'pending', minutes_error = NULL
                WHERE id = ?
                """,
                (job_id,),
            )
            conn.commit()
            return "ok"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def delete_job(self, job_id: str) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()
            return int(updated.rowcount) == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def usage_totals(self, start_utc: str, end_utc: str) -> dict[str, float | int]:
        """一条 SQL：created_at 在 [start, end) 内的时长合计与精档次数。"""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(COALESCE(duration, 0)), 0) AS total_seconds,
                    COUNT(CASE WHEN tier = 'precise' THEN 1 END) AS precise_count
                FROM jobs
                WHERE created_at >= ? AND created_at < ?
                """,
                (start_utc, end_utc),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        return {
            "total_seconds": float(row["total_seconds"] or 0),
            "precise_count": int(row["precise_count"] or 0),
        }

    def recent_failures(self, start_utc: str, end_utc: str) -> dict[str, Any] | None:
        """finished_at 落在 [start, end]、status 为 error 的失败。空 finished_at 不计入。

        一条查询：窗口计数加上按 finished_at、rowid 倒序的最近一条。
        """
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) OVER () AS failure_count,
                    source_name AS latest_name,
                    error AS latest_error
                FROM jobs
                WHERE status = 'error'
                  AND finished_at IS NOT NULL
                  AND finished_at != ''
                  AND finished_at >= ?
                  AND finished_at <= ?
                ORDER BY finished_at DESC, rowid DESC
                LIMIT 1
                """,
                (start_utc, end_utc),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "count": int(row["failure_count"] or 0),
            "latest_name": row["latest_name"],
            "latest_error": row["latest_error"],
        }

    # --- 在线录音会话（LZW-62） -------------------------------------------
    # 会话状态：collecting / awaiting_upload / assembling / done / expired。
    # worker 不 claim 这两张表；列表与网页侧只读。

    RECORDING_SESSION_STATUSES = frozenset(
        {"collecting", "awaiting_upload", "assembling", "done", "expired"}
    )
    RECORDING_OPEN_STATUSES = ("collecting", "awaiting_upload")

    def create_recording_session(
        self,
        *,
        source_name: str,
        tier: str,
        session_id: str | None = None,
        max_open: int | None = None,
    ) -> str | None:
        """建录音会话。max_open 给定时先数未完成会话，达到上限返回 None（不建）。"""
        new_id = session_id or uuid4().hex
        stamp = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if max_open is not None:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS open_count FROM recording_sessions
                    WHERE status IN ('collecting', 'awaiting_upload')
                    """
                ).fetchone()
                open_count = int(row["open_count"]) if row is not None else 0
                if open_count >= max_open:
                    conn.rollback()
                    return None
            conn.execute(
                """
                INSERT INTO recording_sessions (
                    id, status, source_name, tier, job_id, created_at, updated_at, closed_at
                ) VALUES (?, 'collecting', ?, ?, NULL, ?, ?, NULL)
                """,
                (new_id, source_name, tier, stamp, stamp),
            )
            conn.commit()
            return new_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_recording_session(self, session_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM recording_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        data = dict(row)
        data["parts"] = self.list_recording_parts(session_id)
        return data

    def list_recording_parts(self, session_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT part_index, bytes, duration, updated_at
                FROM recording_parts WHERE session_id = ?
                ORDER BY part_index
                """,
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]
    def record_part(
        self,
        *,
        session_id: str,
        part_index: int,
        size: int,
        duration: float | None,
        status: str,
        allowed_from: frozenset[str] | None = None,
    ) -> str:
        """记一片（I2）：会话状态判定与写片同一事务。

        返回落库结果：`ok` 成功、`missing` 会话不存在、`closed` 当前状态不允许补片。
        同片号重复上传幂等（覆盖旧行、不重复计数）。
        """
        stamp = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM recording_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                return "missing"
            current = str(row["status"])
            if allowed_from is not None and current not in allowed_from:
                conn.rollback()
                return "closed"
            conn.execute(
                """
                INSERT INTO recording_parts (session_id, part_index, bytes, duration, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id, part_index) DO UPDATE SET
                    bytes = excluded.bytes,
                    duration = excluded.duration,
                    updated_at = excluded.updated_at
                """,
                (session_id, part_index, size, duration, stamp),
            )
            conn.execute(
                "UPDATE recording_sessions SET status = ?, updated_at = ? WHERE id = ?",
                (status, stamp, session_id),
            )
            conn.commit()
            return "ok"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_recording_session(
        self,
        session_id: str,
        *,
        status: str | None = None,
        source_name: str | None = None,
        tier: str | None = None,
        job_id: str | None = None,
        close: bool = False,
    ) -> bool:
        """改会话字段（None 表示不改）。会话不存在返回 False。"""
        stamp = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """
                UPDATE recording_sessions SET
                    status = COALESCE(?, status),
                    source_name = COALESCE(?, source_name),
                    tier = COALESCE(?, tier),
                    job_id = COALESCE(?, job_id),
                    updated_at = ?,
                    closed_at = CASE WHEN ? THEN ? ELSE closed_at END
                WHERE id = ?
                """,
                (status, source_name, tier, job_id, stamp, 1 if close else 0, stamp, session_id),
            )
            conn.commit()
            return int(updated.rowcount) == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    def open_recording_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        """未完成的会话（collecting / awaiting_upload），按 updated_at 倒序。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM recording_sessions
                WHERE status IN ('collecting', 'awaiting_upload')
                ORDER BY updated_at DESC, rowid DESC
                LIMIT ?
                """,
                (max(1, min(limit, 100)),),
            ).fetchall()
            sessions = [dict(row) for row in rows]
            for session in sessions:
                session["parts"] = self.list_recording_parts(str(session["id"]))
        finally:
            conn.close()
        return sessions

    def count_open_recording_sessions(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS open_count FROM recording_sessions
                WHERE status IN ('collecting', 'awaiting_upload')
                """
            ).fetchone()
        finally:
            conn.close()
        return int(row["open_count"]) if row is not None else 0

    def expire_recording_sessions(self, cutoff_utc: str) -> list[str]:
        """把 updated_at < cutoff 的未完成会话转 expired，返回这批会话 id。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id FROM recording_sessions
                WHERE status IN ('collecting', 'awaiting_upload') AND updated_at < ?
                """,
                (cutoff_utc,),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if ids:
                placeholders = ", ".join("?" for _ in ids)
                conn.execute(
                    f"""
                    UPDATE recording_sessions
                    SET status = 'expired', closed_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    (_now(), *ids),
                )
            conn.commit()
            return ids
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def delete_recording_session(self, session_id: str) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            removed = conn.execute(
                "DELETE FROM recording_sessions WHERE id = ?", (session_id,)
            )
            conn.execute("DELETE FROM recording_parts WHERE session_id = ?", (session_id,))
            conn.commit()
            return int(removed.rowcount) == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def begin_recording_assembly(self, session_id: str) -> tuple[str, list[int]]:
        """把会话从可补传状态迁到 assembling：状态判定、迁移与片号快照同事务（I2/R3）。

        返回 `(结果, 片号列表)`：结果 `ok`（本次抢到拼合）/ `done`（已生成过 job）/
        `busy`（已有请求在拼合）/ `closed`（已过期或已失败）/ `missing`（会话不存在）。
        片号在迁移的同一事务里读出，拼合集合因此恰好等于「迁移时刻的已提交集合」；
        快照之后到达的片会因状态已是 assembling 被 `record_part` 拒（非 200）。
        """
        stamp = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, job_id FROM recording_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                return "missing", []
            status = str(row["status"])
            if status == "done" and row["job_id"]:
                conn.rollback()
                return "done", []
            if status == "assembling":
                conn.rollback()
                return "busy", []
            if status not in ("collecting", "awaiting_upload"):
                conn.rollback()
                return "closed", []
            updated = conn.execute(
                """
                UPDATE recording_sessions SET status = 'assembling', updated_at = ?
                WHERE id = ? AND status IN ('collecting', 'awaiting_upload')
                """,
                (stamp, session_id),
            )
            if int(updated.rowcount) != 1:
                conn.rollback()
                return "busy", []
            parts = conn.execute(
                """
                SELECT part_index FROM recording_parts
                WHERE session_id = ? ORDER BY part_index
                """,
                (session_id,),
            ).fetchall()
            indices = [int(part["part_index"]) for part in parts]
            conn.commit()
            return "ok", indices
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def touch_recording_assembly(self, session_id: str) -> bool:
        """拼合进行中的心跳：只刷 updated_at，且只在仍持有 assembling 时生效（R4）。

        720 片大会话在慢机上拼合可能超过回收阈值；每处理完一片刷一次，
        惰性回收就不会把正在干活的那一路误判成卡死。
        """
        stamp = _now()
        conn = self._connect()
        try:
            updated = conn.execute(
                """
                UPDATE recording_sessions SET updated_at = ?
                WHERE id = ? AND status = 'assembling'
                """,
                (stamp, session_id),
            )
            conn.commit()
            return int(updated.rowcount) == 1
        finally:
            conn.close()

    def complete_recording_session(
        self,
        session_id: str,
        *,
        job_id: str,
        tier: str,
        source_name: str | None,
        source_path: str,
    ) -> str:
        """拼合产物登记成 job 并关闭会话：插行、置 done、清片进度同事务（I2/I6）。

        job_id 由会话 id 派生，重复执行不会产生第二个 job（INSERT ... DO NOTHING）。
        只接受**当前仍持有 assembling** 的会话：被回收（退回 awaiting_upload）后原拼合方
        再 complete 会被拒，不建 job、不删片（R4）。
        返回 `done`（本次登记）/ `reused`（已经登记过）/ `stale`（已不是 assembling）/
        `missing`（会话不存在）。
        """
        stamp = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, job_id FROM recording_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                return "missing"
            if str(row["status"]) == "done" and row["job_id"]:
                conn.commit()
                return "reused"
            resolved_name = self._resolve_source_name(conn, source_name)
            conn.execute(
                """
                INSERT INTO jobs (
                    id, status, tier, source_name, source_kind, source_path, url,
                    created_at, minutes_status
                ) VALUES (?, 'queued', ?, ?, 'recording', ?, NULL, ?, 'pending')
                ON CONFLICT(id) DO NOTHING
                """,
                (job_id, tier, resolved_name, source_path, stamp),
            )
            closed = conn.execute(
                """
                UPDATE recording_sessions SET
                    status = 'done', tier = ?, source_name = ?, job_id = ?,
                    updated_at = ?, closed_at = ?
                WHERE id = ? AND status = 'assembling'
                """,
                (tier, resolved_name, job_id, stamp, stamp, session_id),
            )
            if int(closed.rowcount) != 1:
                # 并发里被别的路径改了状态：整笔回滚，不留 job、不删片。
                conn.rollback()
                return "stale"
            conn.execute("DELETE FROM recording_parts WHERE session_id = ?", (session_id,))
            conn.commit()
            return "done"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def fail_recording_session(self, session_id: str, *, status: str = "failed") -> bool:
        """把会话置终局失败状态并清片进度行（一条事务），供不可挽回的拼合失败使用。"""
        stamp = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """
                UPDATE recording_sessions SET status = ?, updated_at = ?, closed_at = ?
                WHERE id = ? AND status <> 'done'
                """,
                (status, stamp, stamp, session_id),
            )
            conn.execute("DELETE FROM recording_parts WHERE session_id = ?", (session_id,))
            conn.commit()
            return int(updated.rowcount) == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reclaim_stale_assembling(self, cutoff_utc: str) -> list[str]:
        """把 updated_at < cutoff 的 assembling 会话退回 awaiting_upload（I6）。

        进程在拼合中途被杀会留下 assembling 卡死；启动时与惰性清理时各跑一次。
        """
        stamp = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id FROM recording_sessions
                WHERE status = 'assembling' AND updated_at < ?
                """,
                (cutoff_utc,),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if ids:
                placeholders = ", ".join("?" for _ in ids)
                conn.execute(
                    f"""
                    UPDATE recording_sessions SET status = 'awaiting_upload', updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    (stamp, *ids),
                )
            conn.commit()
            return ids
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    def delete_recording_parts(self, session_id: str) -> int:
        """删掉某会话的全部片进度行（会话已拼合完成或会话删除时调用）。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            removed = conn.execute(
                "DELETE FROM recording_parts WHERE session_id = ?", (session_id,)
            )
            conn.commit()
            return int(removed.rowcount)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_recording_sessions(self, status: str, limit: int = 100) -> list[dict[str, Any]]:
        """按状态取会话（含片进度），updated_at 倒序。列表里的已过期行用它。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM recording_sessions
                WHERE status = ?
                ORDER BY updated_at DESC, rowid DESC
                LIMIT ?
                """,
                (status, max(1, min(limit, 100))),
            ).fetchall()
            sessions = [dict(row) for row in rows]
        finally:
            conn.close()
        for session in sessions:
            session["parts"] = self.list_recording_parts(str(session["id"]))
        return sessions
