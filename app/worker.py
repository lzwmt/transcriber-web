"""转写 worker 及其监督。worker 是独立子进程，崩溃后由监督拉起。"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from app import config
from app.jobs import JobStore
from app.pipeline import run_job, run_minutes

logger = logging.getLogger(__name__)

POLL_SECONDS = 0.25
_CRASH_MESSAGE = "worker 进程异常退出，任务未完成"


def worker_main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    data = config.data_dir()
    store = JobStore(data / "jobs.db")
    logger.info("worker started pid=%s data=%s", os.getpid(), data)
    while True:
        job = store.claim_next()
        if job is not None:
            logger.info("worker claim job=%s tier=%s", job["id"], job["tier"])
            run_job(store, data, job)
            continue
        minutes_job = store.claim_minutes()
        if minutes_job is not None:
            logger.info("worker claim minutes=%s", minutes_job["id"])
            run_minutes(store, data, minutes_job)
            continue
        time.sleep(POLL_SECONDS)


class WorkerSupervisor:
    def __init__(self, data: Path, store: JobStore) -> None:
        self.data = data
        self.store = store
        self.proc: subprocess.Popen[bytes] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = 0.0

    def start(self) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        self._spawn()
        self._thread = threading.Thread(target=self._watch, name="worker-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # 先停监督，再结束子进程。这样正常关闭不会被当成崩溃，遗留 running 留给下次启动。
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._terminate()

    def _spawn(self) -> None:
        root = str(Path(__file__).resolve().parent.parent)
        env = os.environ.copy()
        env["TRANSCRIBER_DATA_DIR"] = str(self.data)
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.worker"],
            cwd=root,
            env=env,
            start_new_session=True,
        )
        self.proc = proc
        self._started_at = time.monotonic()
        (self.data / "worker.pid").write_text(str(proc.pid), encoding="utf-8")
        logger.info("spawned worker pid=%s", proc.pid)

    def _watch(self) -> None:
        while not self._stop.is_set():
            proc = self.proc
            if proc is not None and proc.poll() is not None:
                # 先改状态再拉起，避免新进程把崩溃中的任务再领走。
                self.store.mark_running_as_error(_CRASH_MESSAGE)
                _kill_group(proc.pid)
                lifetime = time.monotonic() - self._started_at
                delay = 0.3 if lifetime > 1 else 1.0
                if self._stop.wait(delay):
                    return
                if self._stop.is_set():
                    return
                self._spawn()
                continue
            self._stop.wait(POLL_SECONDS)

    def _terminate(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        _signal_group(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _signal_group(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return


def _kill_group(pid: int) -> None:
    _signal_group(pid, signal.SIGKILL)


if __name__ == "__main__":
    worker_main()
