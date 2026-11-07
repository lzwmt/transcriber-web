"""doctor：体检隔离实例起停、Playwright 浏览器（.verify-browsers 优先）与 /health。

结果打印到标准输出并落盘 `verify-out/doctor-<时间>.txt`；任一项失败 exit 1 并在
「失败项」里点名，供 A7 两反例（浏览器目录缺失、健康检查端口不可达）取证。

`--browsers` 一旦显式给出就以该目录为准，不再退回宿主缓存——反例 (a) 依赖这条口径。
"""

from __future__ import annotations

import datetime
import json
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from verify import OUT_DIR
from verify.browser import HOST_CACHE, PROJECT_BROWSERS, chromium_revision
from verify.instance import listeners, process_alive, request_bytes, start_instance

ITEM_INSTANCE = "隔离实例"
ITEM_BROWSER = "浏览器"
ITEM_HEALTH = "/health"
_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
_PROBE_TIMEOUT = 5


@dataclass
class _InstanceCheck:
    ok: bool
    detail: str
    health_ok: bool = False
    health_detail: str = "未探测"
    problems: list[str] = field(default_factory=list)


def doctor_command(*, browsers: str | None = None, health_url: str | None = None) -> int:
    """跑三类体检，落盘报告。返回 0 全通过，1 有失败项。"""
    stamp = _stamp()
    lines = [f"时间={datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds')}"]
    failures: list[str] = []

    instance = _check_instance()
    lines.append(_item_line(ITEM_INSTANCE, instance.ok, instance.detail))
    if not instance.ok:
        failures.append(ITEM_INSTANCE)

    browser_ok, browser_detail = _check_browser(browsers)
    lines.append(_item_line(ITEM_BROWSER, browser_ok, browser_detail))
    if not browser_ok:
        failures.append(ITEM_BROWSER)

    if health_url is None:
        health_ok, health_detail = instance.health_ok, instance.health_detail
        source = "隔离实例自身 /health"
    else:
        health_ok, health_detail = _check_health(health_url)
        source = health_url
    lines.append(_item_line(ITEM_HEALTH, health_ok, f"{health_detail}（目标 {source}）"))
    if not health_ok:
        failures.append(ITEM_HEALTH)

    verdict = "PASS" if not failures else "FAIL"
    lines.append(f"结论：{verdict}")
    lines.append("失败项：" + ("无" if not failures else "；".join(failures)))
    path = _write_report(stamp, lines)
    print("\n".join(lines))
    print(f"报告：{path.relative_to(OUT_DIR.parent)}")
    return 0 if not failures else 1


def _check_instance() -> _InstanceCheck:
    try:
        inst = start_instance()
    except Exception as exc:
        return _InstanceCheck(False, f"起实例失败：{_exc_text(exc)}")
    port, pid, data_dir = inst.port, inst.proc.pid, inst.data_dir
    problems: list[str] = []
    health_ok = False
    health_detail = "未探测"
    try:
        health_ok, health_detail = _probe_instance_health(inst.base_url, problems)
        found = listeners(port)
        if not found:
            problems.append(f"端口 {port} 没有 LISTEN 记录")
        elif any(ip not in _LOOPBACK for ip in found):
            problems.append(f"端口 {port} 监听了非回环地址：{found}")
    finally:
        try:
            inst.stop()
        except Exception as exc:
            problems.append(f"销毁未干净：{_exc_text(exc)}")
    if process_alive(pid):
        problems.append(f"服务进程残留 {pid}")
    if data_dir.exists():
        problems.append(f"临时目录残留 {data_dir}")
    still = listeners(port)
    if still:
        problems.append(f"端口 {port} 仍在监听 {still}")
    detail = f"port={port} pid={pid} 起停干净" if not problems else "；".join(problems)
    return _InstanceCheck(not problems, detail, health_ok, health_detail, problems)


def _probe_instance_health(base_url: str, problems: list[str]) -> tuple[bool, str]:
    try:
        status, raw = request_bytes(base_url, "/health", timeout=_PROBE_TIMEOUT)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        problems.append(f"实例 /health 不可达：{_exc_text(exc)}")
        return False, f"GET {base_url}/health 不可达：{_exc_text(exc)}"
    body_text = raw.decode(errors="replace")[:120]
    detail = f"GET {base_url}/health → HTTP {status} {body_text}"
    try:
        body = json.loads(raw.decode())
    except json.JSONDecodeError:
        problems.append(f"实例 /health 不是 JSON：{body_text}")
        return False, detail
    if status != 200 or not isinstance(body, dict) or body.get("ok") is not True:
        problems.append(f"实例 /health 未返回 ok=true：{body_text}")
        return False, detail
    return True, detail


def _browser_roots(override: str | None) -> tuple[Path, ...]:
    if override is not None:
        return (Path(override).expanduser().resolve(),)
    return (PROJECT_BROWSERS, HOST_CACHE)


def _check_browser(override: str | None) -> tuple[bool, str]:
    try:
        revision = chromium_revision()
    except Exception as exc:
        return False, f"取 chromium revision 失败：{_exc_text(exc)}"
    roots = _browser_roots(override)
    names = (f"chromium-{revision}", f"chromium_headless_shell-{revision}")
    missing: list[str] = []
    hit: Path | None = None
    for root in roots:
        root_missing = [
            str(root / name / "INSTALLATION_COMPLETE")
            for name in names
            if not (root / name / "INSTALLATION_COMPLETE").is_file()
        ]
        if root_missing:
            missing.extend(root_missing)
        elif hit is None:
            hit = root
    scope = "显式指定目录" if override is not None else ".verify-browsers 优先、宿主缓存兜底"
    if hit is None:
        return False, f"chromium {revision} 缺 INSTALLATION_COMPLETE（{scope}）：" + "；".join(
            missing
        )
    return True, f"chromium {revision} 命中 {hit}（{scope}）"


def _check_health(url: str) -> tuple[bool, str]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False, f"健康检查地址不合法：{url}"
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        status, raw = request_bytes(origin, path, timeout=_PROBE_TIMEOUT)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"GET {url} 不可达：{_exc_text(exc)}"
    body_text = raw.decode(errors="replace")[:120]
    detail = f"GET {url} → HTTP {status} {body_text}"
    try:
        body = json.loads(raw.decode())
    except json.JSONDecodeError:
        return False, detail + "（不是 JSON）"
    if status != 200 or not isinstance(body, dict) or body.get("ok") is not True:
        return False, detail + "（未返回 ok=true）"
    return True, detail


def _item_line(item: str, ok: bool, detail: str) -> str:
    return f"{item}：{'PASS' if ok else 'FAIL'} {detail}"


def _stamp() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


def _write_report(stamp: str, lines: list[str]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"doctor-{stamp}.txt"
    n = 2
    while path.exists():
        path = OUT_DIR / f"doctor-{stamp}-{n}.txt"
        n += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _exc_text(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
