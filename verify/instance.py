"""隔离实例：临时数据目录、桩模式、127.0.0.1 上从 8911 起避让端口。"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from verify import PORT_SPAN, PORT_START, PRODUCTION_PORT, ROOT, TMP_DIR

_HEALTH_TIMEOUT_SECONDS = 45.0
_STOP_TIMEOUT_SECONDS = 5.0


class PortInUse(RuntimeError):
    """选中的端口在启动前被占用。"""


class IsolatedInstance:
    """一次 run.sh 拉起的桩实例。用完必须 stop，停掉进程并删掉临时目录。"""

    def __init__(
        self,
        proc: subprocess.Popen[bytes],
        port: int,
        data_dir: Path,
        log_path: Path,
        log_handle: object,
    ) -> None:
        self.proc = proc
        self.port = port
        self.data_dir = data_dir
        self.log_path = log_path
        self._log_handle = log_handle
        self._stopped = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @classmethod
    def launch(cls, *, stub_sleep: float = 0) -> IsolatedInstance:
        port = _reserve_port()
        data_dir = (TMP_DIR / uuid4().hex).resolve()
        data_dir.mkdir(parents=True)
        log_path = data_dir / "server.log"
        log_handle = log_path.open("wb")
        env = _child_env(port, data_dir, stub_sleep=stub_sleep)
        try:
            proc = subprocess.Popen(
                ["/bin/bash", str(ROOT / "run.sh")],
                cwd=str(ROOT),
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            log_handle.close()
            shutil.rmtree(data_dir, ignore_errors=True)
            raise
        return cls(proc, port, data_dir, log_path, log_handle)

    def wait_healthy(self, timeout: float = _HEALTH_TIMEOUT_SECONDS) -> None:
        deadline = time.monotonic() + timeout
        last_error = "尚未连接"
        url = f"{self.base_url}/health"
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                log = self.server_log_excerpt()
                lowered = log.lower()
                if "address already in use" in lowered:
                    raise PortInUse(log[-500:])
                code = self.proc.returncode
                raise RuntimeError(f"实例退出 code={code}：{log[-1000:]}")
            try:
                status, raw = request_bytes(self.base_url, "/health", timeout=2)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = str(exc)
                time.sleep(0.3)
                continue
            if status != 200:
                last_error = f"HTTP {status}"
                time.sleep(0.3)
                continue
            try:
                body = json.loads(raw.decode())
            except json.JSONDecodeError as exc:
                last_error = str(exc)
                time.sleep(0.3)
                continue
            if isinstance(body, dict) and body.get("ok") is True:
                self._require_loopback()
                return
            last_error = raw.decode(errors="replace")[:200]
            time.sleep(0.3)
        raise RuntimeError(f"健康检查超时 {url}：{last_error}")

    def get_text(self, path: str) -> str:
        status, raw = request_bytes(self.base_url, path, timeout=15)
        if status != 200:
            raise RuntimeError(f"GET {path} HTTP {status}")
        return raw.decode()

    def server_log_excerpt(self, limit: int = 4000) -> str:
        if not self.log_path.is_file():
            return "无日志：没有服务输出文件"
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            return "无日志：服务没有写出标准输出"
        if len(text) <= limit:
            return text
        return text[-limit:]

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        worker_pid = _read_worker_pid(self.data_dir)
        _close_handle(self._log_handle)
        _signal_group(self.proc.pid, signal.SIGTERM)
        if worker_pid is not None:
            _signal_group(worker_pid, signal.SIGTERM)
        _wait_proc(self.proc, _STOP_TIMEOUT_SECONDS)
        if process_alive(self.proc.pid):
            _signal_group(self.proc.pid, signal.SIGKILL)
            _wait_proc(self.proc, 3)
        if worker_pid is not None and process_alive(worker_pid):
            _signal_group(worker_pid, signal.SIGKILL)
        time.sleep(0.2)
        for pid in pids_with_env("TRANSCRIBER_DATA_DIR", str(self.data_dir)):
            _signal_group(pid, signal.SIGKILL)
        time.sleep(0.2)
        problems: list[str] = []
        leftover = [
            pid
            for pid in pids_with_env("TRANSCRIBER_DATA_DIR", str(self.data_dir))
            if process_alive(pid)
        ]
        if leftover:
            problems.append(f"残留进程 {leftover}")
        if process_alive(self.proc.pid):
            problems.append(f"服务进程仍在 {self.proc.pid}")
        if worker_pid is not None and process_alive(worker_pid):
            problems.append(f"worker 仍在 {worker_pid}")
        if self.data_dir.exists():
            shutil.rmtree(self.data_dir)
        if self.data_dir.exists():
            problems.append(f"临时目录仍在 {self.data_dir}")
        _remove_empty_tmp_parent()
        still = listeners(self.port)
        if still:
            problems.append(f"端口 {self.port} 仍在监听 {still}")
        if problems:
            raise RuntimeError("销毁隔离实例未干净：" + "；".join(problems))

    def _require_loopback(self) -> None:
        found = listeners(self.port)
        if not found:
            raise RuntimeError(f"健康检查已通过，但 {self.port} 没有 LISTEN 记录")
        allowed = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
        if any(ip not in allowed for ip in found):
            raise RuntimeError(f"实例监听了非回环地址：{found}")


def start_instance(*, stub_sleep: float = 0) -> IsolatedInstance:
    """从 8911 起找空端口。只在端口被占用时换下一个，其它启动失败不重试。"""
    last = "没有尝试"
    for _ in range(PORT_SPAN):
        inst = IsolatedInstance.launch(stub_sleep=stub_sleep)
        try:
            inst.wait_healthy()
        except PortInUse as exc:
            last = str(exc)
            inst.stop()
            continue
        except Exception:
            try:
                inst.stop()
            except Exception as stop_exc:
                raise RuntimeError(f"实例启动失败且销毁失败：{stop_exc}") from stop_exc
            raise
        return inst
    raise RuntimeError(f"从 {PORT_START} 起的端口都被占用：{last}")


@contextmanager
def isolated_instance(*, stub_sleep: float = 0) -> Iterator[IsolatedInstance]:
    inst = start_instance(stub_sleep=stub_sleep)
    try:
        yield inst
    finally:
        inst.stop()


def request_bytes(
    base_url: str,
    path: str,
    *,
    payload: dict[str, object] | None = None,
    timeout: float = 10,
) -> tuple[int, bytes]:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def post_json(
    base_url: str, path: str, payload: dict[str, object]
) -> tuple[int, dict[str, object]]:
    status, raw = request_bytes(base_url, path, payload=payload, timeout=30)
    try:
        body = json.loads(raw.decode())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} 返回的不是 JSON（HTTP {status}）") from exc
    if not isinstance(body, dict):
        raise RuntimeError(f"{path} 的 JSON 不是对象")
    return status, body


def listeners(port: int) -> list[str]:
    """该端口上处于 LISTEN 的地址。用于确认只绑了 127.0.0.1。"""
    port_hex = f"{port:04X}"
    found = _tcp_listeners(Path("/proc/net/tcp"), port_hex, ipv6=False)
    tcp6 = Path("/proc/net/tcp6")
    if tcp6.is_file():
        found.extend(_tcp_listeners(tcp6, port_hex, ipv6=True))
    return found


def pids_with_env(key: str, value: str) -> list[int]:
    target = f"{key}={value}".encode()
    found: list[int] = []
    me = os.getpid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            raw = (entry / "environ").read_bytes()
        except OSError:
            continue
        if target in raw.split(b"\0"):
            found.append(pid)
    return found


def _child_env(port: int, data_dir: Path, *, stub_sleep: float = 0) -> dict[str, str]:
    env = os.environ.copy()
    # run.sh 里的 uv run 会按默认组同步，可能把 verify 分组的 Playwright 卸掉。
    env["UV_NO_SYNC"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["TRANSCRIBER_HOST"] = "127.0.0.1"
    env["TRANSCRIBER_PORT"] = str(port)
    env["TRANSCRIBER_DATA_DIR"] = str(data_dir)
    env["TRANSCRIBER_STUB"] = "1"
    env["TRANSCRIBER_STUB_SLEEP"] = _sleep_text(stub_sleep)
    for key in (
        "TRANSCRIBER_STUB_LLM",
        "TRANSCRIBER_STUB_LLM_ERROR",
        "TRANSCRIBER_STUB_LLM_ERROR_PATH",
        "TRANSCRIBER_STUB_LLM_TEXT",
        "TRANSCRIBER_ENGINE_MARKER",
    ):
        env.pop(key, None)
    return env


def _sleep_text(seconds: float) -> str:
    if seconds < 0:
        raise RuntimeError(f"STUB_SLEEP 不能为负：{seconds}")
    if seconds == int(seconds):
        return str(int(seconds))
    return str(seconds)


def _reserve_port() -> int:
    for port in range(PORT_START, PORT_START + PORT_SPAN):
        if port == PRODUCTION_PORT:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"从 {PORT_START} 起 {PORT_SPAN} 个端口都不可用")


def _tcp_listeners(path: Path, port_hex: str, *, ipv6: bool) -> list[str]:
    found: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return found
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        local, state = parts[1], parts[3]
        ip_hex, found_port = local.split(":", 1)
        if found_port.upper() != port_hex or state != "0A":
            continue
        found.append(_decode_addr(ip_hex, ipv6=ipv6))
    return found


def _decode_addr(ip_hex: str, *, ipv6: bool) -> str:
    raw = bytes.fromhex(ip_hex)
    if not ipv6:
        return socket.inet_ntoa(raw[::-1])
    words = [raw[index : index + 4][::-1] for index in range(0, 16, 4)]
    return socket.inet_ntop(socket.AF_INET6, b"".join(words))


def _read_worker_pid(data_dir: Path) -> int | None:
    path = data_dir / "worker.pid"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text.isdigit():
        return None
    return int(text)


def _close_handle(handle: object) -> None:
    close = getattr(handle, "close", None)
    if not callable(close):
        return
    closed = getattr(handle, "closed", False)
    if closed:
        return
    close()


def _signal_group(pid: int, sig: int) -> None:
    if pid <= 1 or pid == os.getpid():
        return
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            return


def _wait_proc(proc: subprocess.Popen[bytes], timeout: float) -> None:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return


def _remove_empty_tmp_parent() -> None:
    if not TMP_DIR.is_dir():
        return
    try:
        TMP_DIR.rmdir()
    except OSError:
        return


def process_alive(pid: int) -> bool:
    stat = Path(f"/proc/{pid}/stat")
    if not stat.is_file():
        return False
    try:
        text = stat.read_text(encoding="utf-8")
    except OSError:
        return False
    end = text.rfind(")")
    if end < 0 or end + 2 >= len(text):
        return True
    return text[end + 2] != "Z"
