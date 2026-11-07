"""flag：带 `TRANSCRIBER_FEAT_<NAME>` 重起隔离实例（勘误 N1）。

隔离实例由 `instance.py` 以 `Popen(env=env)` 启动，运行中无法翻开关；所以本子命令的
语义是「带目标 env 变量重起隔离实例」。只作用于隔离实例，绝不碰生产 8900 与生产容器。

wait-settle 与 flag 共用这里的实例复用与计数读取：快照计数逻辑来自
`verify/checks/polling.py`，按勘误 N2 只 import、不改 checks 源文件。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from verify.browser import chromium_page
from verify.checks.polling import _home_count
from verify.instance import IsolatedInstance, isolated_instance

FLAG_PREFIX = "TRANSCRIBER_FEAT_"
_NAME = re.compile(r"[a-z0-9_]+")
_READ_CHUNK = 5.0


class FlagNameError(ValueError):
    """开关名不合规。"""


def flag_env_name(name: str) -> str:
    """`name` → `TRANSCRIBER_FEAT_<NAME>`。只允许 [a-z0-9_]，空或含非法字符抛 FlagNameError。

    与 app 侧口径对齐：不做 `strip()` 之类的宽容归一化，`" demo "` 这类带空白或大小写的
    名字一律当非法拒绝，避免 CLI 静默改写成另一个开关名。
    """
    if not name or not _NAME.fullmatch(name):
        raise FlagNameError(f"开关名只允许 [a-z0-9_]，不接受空白或其它字符：{name!r}")
    return FLAG_PREFIX + name.upper()


def read_flag_env(pid: int, env_name: str) -> str | None:
    """从 /proc/<pid>/environ 回读变量值。进程不在或变量缺失返回 None。"""
    path = Path(f"/proc/{pid}/environ")
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    for entry in raw.split(b"\0"):
        key, sep, value = entry.partition(b"=")
        if sep and key.decode(errors="replace") == env_name:
            return value.decode(errors="replace")
    return None


@dataclass
class SettleResult:
    settled: bool
    reads: list[int] = field(default_factory=list)
    elapsed: float = 0.0
    note: str = ""


def wait_settle(
    inst: IsolatedInstance,
    page_path: str = "/",
    *,
    interval: float = 1.0,
    stable_reads: int = 2,
    timeout: float = 30.0,
    visit: bool = True,
) -> SettleResult:
    """轮询首页 GET 计数直到连续 `stable_reads` 次一致。

    口径（规格 S4）：默认 1 秒间隔、连续 2 次一致判稳、默认超时 30 秒；超时由调用方
    exit 3 并输出最后一次快照计数。计数逻辑复用 checks/polling.py，不改其源文件。
    """
    if interval <= 0:
        raise ValueError(f"interval 必须为正：{interval}")
    if stable_reads < 1:
        raise ValueError(f"stable-reads 必须 ≥1：{stable_reads}")
    if timeout <= 0:
        raise ValueError(f"timeout 必须为正：{timeout}")
    if visit:
        _visit(inst, page_path)
    start = time.monotonic()
    reads: list[int] = []
    while True:
        reads.append(_count_for_page(inst, page_path))
        if len(reads) >= stable_reads and _tail_stable(reads, stable_reads):
            return SettleResult(True, reads, time.monotonic() - start, "连续一致")
        if time.monotonic() - start >= timeout:
            return SettleResult(False, reads, time.monotonic() - start, "超时")
        time.sleep(interval)


def _visit(inst: IsolatedInstance, page_path: str) -> None:
    """先像用户一样打开一次页面，让计数有真实流量可观测（空实例否则恒 0）。"""
    try:
        with chromium_page(color_scheme="light") as page:
            page.goto(inst.base_url + page_path, wait_until="load")
    except Exception as exc:  # 浏览器不可用不该让稳定判据伪装成通过
        raise RuntimeError(f"打开页面失败：{type(exc).__name__}: {exc}") from exc


def _tail_stable(reads: list[int], stable_reads: int) -> bool:
    tail = reads[-stable_reads:]
    return len(tail) == stable_reads and len(set(tail)) == 1


def _count_for_page(inst: IsolatedInstance, page_path: str) -> int:
    """首页（默认 `/`）计数走 checks/polling.py；其它页面按该路径的 GET 行计数。"""
    if page_path in ("/", ""):
        return _home_count(inst.log_path)
    return _path_count(inst.log_path, page_path)


def _path_count(log_path: Path, page_path: str) -> int:
    target = f'"GET {page_path} HTTP/1.'
    if not log_path.is_file():
        return 0
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return sum(1 for line in text.splitlines() if target in line)


def flag_command(name: str, state: str) -> int:
    """带变量重起隔离实例并回读 /proc/<pid>/environ。返回 0 成功 / 1 证法不成立。"""
    if state not in ("on", "off"):
        raise ValueError(f"state 只允许 on|off：{state!r}")
    env_name = flag_env_name(name)
    expected = "1" if state == "on" else None
    target = "<未设>" if expected is None else expected
    print(f"开关={env_name} 目标值={target} 语义=带变量重起隔离实例")
    previous = os.environ.get(env_name)
    if expected is None:
        os.environ.pop(env_name, None)
    else:
        os.environ[env_name] = expected
    try:
        with isolated_instance() as inst:
            pid = inst.proc.pid
            value = _read_flag_with_retry(pid, env_name)
            print(f"实例 pid={pid} port={inst.port}")
            print(f"回读 /proc/{pid}/environ {env_name}={value if value is not None else '<未设>'}")
            if value != expected:
                print(
                    f"证法不成立：期望 {expected if expected is not None else '<未设>'}，"
                    f"实际 {value if value is not None else '<未设>'}"
                )
                return 1
            if expected is None:
                print("证法成立：重起后的隔离实例没有该变量")
            else:
                print("证法成立：重起后的隔离实例带上了目标变量")
    finally:
        if previous is None:
            os.environ.pop(env_name, None)
        else:
            os.environ[env_name] = previous
    return 0


def _read_flag_with_retry(pid: int, env_name: str) -> str | None:
    """进程刚 exec 时 environ 可能还没稳定，短暂重读。"""
    deadline = time.monotonic() + _READ_CHUNK
    value = read_flag_env(pid, env_name)
    while value is None and time.monotonic() < deadline:
        time.sleep(0.2)
        value = read_flag_env(pid, env_name)
    return value
