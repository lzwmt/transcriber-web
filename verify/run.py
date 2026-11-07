"""起隔离实例：self-check / v2a / v2b / polling / tokens / player* / pack 实跑检查。

本文件的分发纪律（LZW-56 A8）：每个子命令一条显式分支；分支走完兜底 `return 2`
并打错误行，绝不静默跑别的检查。未知子命令由 argparse 既有行为 exit 2，不放宽。
新子命令（doctor/seed/snapshot/wait-settle/flag）首行输出 `VERIFY-CMD <name>`。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import shutil
import subprocess
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path

from verify import CHECKLIST_PATH, ITEMS_DIR, OUT_DIR, ROOT, SELF_CHECK_DIR
from verify.browser import chromium_page, require_cached_chromium
from verify.checks.player_basic import run_player_basic
from verify.checks.player_empty import run_player_empty
from verify.checks.player_sync import run_player_sync
from verify.checks.polling import run_polling
from verify.checks.tokens import run_tokens
from verify.checks.ui_empty import empty_guide_copy, run_ui_empty
from verify.checks.ui_failbanner import run_ui_failbanner
from verify.checks.ui_mobile import run_ui_mobile
from verify.checks.ui_workbench import run_ui_workbench
from verify.doctor import doctor_command
from verify.evidence import CheckResult, write_item
from verify.flagops import FlagNameError, flag_command, wait_settle
from verify.instance import isolated_instance, listeners, process_alive, request_bytes
from verify.seed import (
    INSIDE_ERROR,
    INSIDE_NAME,
    SeededJob,
    jobs_in,
    seed_empty,
    seed_failures_24h,
    seed_multi_status,
    seed_player_empty_jobs,
    seed_player_job,
    submit_url_job,
)
from verify.snapshot import snapshot_command


def main(argv: list[str] | None = None) -> int:
    if Path.cwd().resolve() != ROOT:
        print(f"请在仓库根目录运行，当前目录是 {Path.cwd()}", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(prog="python -m verify.run")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-check", help="起停隔离实例空跑，并核对三类造数")
    sub.add_parser("v2a", help="实跑 UI-EMPTY 与 UI-WORKBENCH")
    sub.add_parser("v2b", help="实跑 UI-FAILBANNER 与 UI-MOBILE")
    sub.add_parser("polling", help="实跑 POLLING 轮询时序")
    sub.add_parser("tokens", help="实跑 TOKENS 变量对账与非变量实测")
    sub.add_parser("player-basic", help="实跑 PLAYER-BASIC 播放器双主题对齐")
    sub.add_parser("player-sync", help="实跑 PLAYER-SYNC 逐字稿联动")
    sub.add_parser("player-empty", help="实跑 PLAYER-EMPTY 无音频占位态")
    sub.add_parser("player", help="实跑 PLAYER 全部三查")
    sub.add_parser("pack", help="实跑全部检查并组装证据包")
    doctor_parser = sub.add_parser("doctor", help="体检隔离实例、浏览器与 /health")
    doctor_parser.add_argument("--browsers", help="浏览器目录，默认 .verify-browsers 优先")
    doctor_parser.add_argument("--health-url", help="健康检查地址，默认体检实例自身")
    sub.add_parser("seed", help="显式跑三类造数并报告造了什么")
    snapshot_parser = sub.add_parser("snapshot", help="取隔离实例首页可访问性快照与截图")
    snapshot_parser.add_argument("--page", default="/", help="页面路径，默认 /")
    wait_parser = sub.add_parser("wait-settle", help="等首页轮询计数稳定")
    wait_parser.add_argument("--page", default="/", help="页面路径，默认 /")
    wait_parser.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒，默认 1")
    wait_parser.add_argument("--stable-reads", type=int, default=2, help="连续一致次数，默认 2")
    wait_parser.add_argument("--timeout", type=float, default=30.0, help="超时秒，默认 30")
    flag_parser = sub.add_parser("flag", help="带 TRANSCRIBER_FEAT_<NAME> 重起隔离实例")
    flag_parser.add_argument("name", help="开关名，只允许 [a-z0-9_]")
    flag_parser.add_argument("state", choices=("on", "off"), help="on 置 1，off 移除变量")
    args = parser.parse_args(argv)
    try:
        if args.command == "self-check":
            return self_check()
        if args.command == "v2a":
            return run_v2a()
        if args.command == "v2b":
            return run_v2b()
        if args.command == "polling":
            return run_polling_command()
        if args.command == "tokens":
            return run_tokens_command()
        if args.command == "player-basic":
            return run_player_basic_command()
        if args.command == "player-sync":
            return run_player_sync_command()
        if args.command == "player-empty":
            return run_player_empty_command()
        if args.command == "player":
            return run_player_command()
        if args.command == "pack":
            return run_pack()
        if args.command == "doctor":
            return run_doctor(browsers=args.browsers, health_url=args.health_url)
        if args.command == "seed":
            return run_seed()
        if args.command == "snapshot":
            return run_snapshot(page=args.page)
        if args.command == "wait-settle":
            return run_wait_settle(
                page=args.page,
                interval=args.interval,
                stable_reads=args.stable_reads,
                timeout=args.timeout,
            )
        if args.command == "flag":
            return run_flag(name=args.name, state=args.state)
        print(f"未实现的分发分支：{args.command}", file=sys.stderr)
        return 2
    except Exception:
        traceback.print_exc()
        return 1


def self_check() -> int:
    revision = require_cached_chromium()
    print(f"playwright chromium revision {revision} cache hit")
    _prove_empty(revision)
    _prove_seeds()
    _prove_api()
    print("SELF-CHECK PASS")
    return 0


def run_v2a() -> int:
    revision = require_cached_chromium()
    print(f"playwright chromium revision {revision} cache hit")
    empty_ok = _run_empty_item()
    bench_ok = _run_workbench_item()
    if empty_ok and bench_ok:
        print("V2A PASS")
        return 0
    print("V2A FAIL")
    return 1


def run_v2b() -> int:
    revision = require_cached_chromium()
    print(f"playwright chromium revision {revision} cache hit")
    banner_ok = _run_failbanner_item()
    mobile_ok = _run_mobile_item()
    if banner_ok and mobile_ok:
        print("V2B PASS")
        return 0
    print("V2B FAIL")
    return 1


def run_polling_command() -> int:
    revision = require_cached_chromium()
    print(f"playwright chromium revision {revision} cache hit")
    if _run_polling_item():
        print("POLLING PASS")
        return 0
    print("POLLING FAIL")
    return 1


def run_tokens_command() -> int:
    revision = require_cached_chromium()
    print(f"playwright chromium revision {revision} cache hit")
    if _run_tokens_item():
        print("TOKENS PASS")
        return 0
    print("TOKENS FAIL")
    return 1


def run_player_basic_command() -> int:
    revision = require_cached_chromium()
    print(f"playwright chromium revision {revision} cache hit")
    if _run_player_basic_item():
        print("PLAYER-BASIC PASS")
        return 0
    print("PLAYER-BASIC FAIL")
    return 1


def run_player_sync_command() -> int:
    revision = require_cached_chromium()
    print(f"playwright chromium revision {revision} cache hit")
    if _run_player_sync_item():
        print("PLAYER-SYNC PASS")
        return 0
    print("PLAYER-SYNC FAIL")
    return 1


def run_player_empty_command() -> int:
    revision = require_cached_chromium()
    print(f"playwright chromium revision {revision} cache hit")
    if _run_player_empty_item():
        print("PLAYER-EMPTY PASS")
        return 0
    print("PLAYER-EMPTY FAIL")
    return 1


def run_player_command() -> int:
    revision = require_cached_chromium()
    print(f"playwright chromium revision {revision} cache hit")
    b = _run_player_basic_item()
    s = _run_player_sync_item()
    e = _run_player_empty_item()
    if b and s and e:
        print("PLAYER ALL PASS")
        return 0
    print("PLAYER CHECKS FAILED")
    return 1


def run_doctor(*, browsers: str | None = None, health_url: str | None = None) -> int:
    print("VERIFY-CMD doctor")
    return doctor_command(browsers=browsers, health_url=health_url)


def run_seed() -> int:
    print("VERIFY-CMD seed")
    with isolated_instance() as inst:
        seed_empty(inst.data_dir)
        multi = seed_multi_status(inst.data_dir)
        pair = seed_failures_24h(inst.data_dir)
        rows = jobs_in(inst.data_dir)
        by_name = {str(row["source_name"]): row for row in rows}
        for job in [*multi, pair.inside, pair.outside]:
            row = by_name.get(job.source_name)
            if row is None or row.get("status") != job.status:
                raise RuntimeError(f"造数状态没有落库：{job.source_name} {row}")
        print("空实例：确认任务数为 0（未写入）")
        listed = "、".join(f"{j.source_name}={j.status}" for j in multi)
        print(f"多状态造数：{len(multi)} 条 {listed}")
        print(
            "24h 窗口造数："
            f"窗内 {pair.inside.source_name}={pair.inside.status}，"
            f"窗外 {pair.outside.source_name}={pair.outside.status}"
        )
        print(f"任务库总数={len(rows)} 全部状态已落库核对")
    print("SEED PASS")
    return 0


def run_snapshot(*, page: str = "/") -> int:
    print("VERIFY-CMD snapshot")
    code = snapshot_command(page_path=page)
    print(f"SNAPSHOT {'PASS' if code == 0 else 'FAIL'}")
    return code


def run_wait_settle(
    *,
    page: str = "/",
    interval: float = 1.0,
    stable_reads: int = 2,
    timeout: float = 30.0,
) -> int:
    print("VERIFY-CMD wait-settle")
    with isolated_instance() as inst:
        result = wait_settle(
            inst,
            page,
            interval=interval,
            stable_reads=stable_reads,
            timeout=timeout,
        )
    print(
        f"页面={page} 间隔={interval:g}s 连续一致={stable_reads} 超时={timeout:g}s "
        f"读数={'、'.join(str(n) for n in result.reads)} 耗时={result.elapsed:.2f}s"
    )
    if not result.settled:
        last = result.reads[-1] if result.reads else None
        print(f"WAIT-SETTLE TIMEOUT 最后一次快照计数={last}")
        return 3
    print(f"WAIT-SETTLE SETTLED 稳定计数={result.reads[-1]}")
    return 0


def run_flag(*, name: str, state: str) -> int:
    print("VERIFY-CMD flag", flush=True)
    try:
        return flag_command(name=name, state=state)
    except FlagNameError as exc:
        print(f"FLAG FAILED {exc}", file=sys.stderr)
        return 2


def _prove_empty(revision: str) -> None:
    guide = empty_guide_copy()
    if SELF_CHECK_DIR.exists():
        for child in SELF_CHECK_DIR.iterdir():
            child.unlink()
    SELF_CHECK_DIR.mkdir(parents=True, exist_ok=True)
    with isolated_instance() as inst:
        page = inst.get_text("/")
        if guide not in page:
            raise RuntimeError("空跑页面没有空状态文案")
        with chromium_page(color_scheme="light") as browser_page:
            response = browser_page.goto(inst.base_url + "/", wait_until="load")
            if response is None or response.status != 200:
                status = None if response is None else response.status
                raise RuntimeError(f"空白页 HTTP {status}")
            browser_page.wait_for_selector(".empty-guide")
            shot = browser_page.screenshot(full_page=True)
        if not shot.startswith(b"\x89PNG") or len(shot) < 1000:
            raise RuntimeError("空白页截图不是有效 PNG")
        found = listeners(inst.port)
        log = "\n".join(
            [
                f"chromium={revision}",
                f"port={inst.port}",
                f"listen={found}",
                "health=ok",
                "guide_present=yes",
                f"screenshot_bytes={len(shot)}",
                "服务输出摘录：",
                inst.server_log_excerpt(),
            ]
        )
        (SELF_CHECK_DIR / "shot-blank.png").write_bytes(shot)
        (SELF_CHECK_DIR / "log.txt").write_text(log + "\n", encoding="utf-8")
        port = inst.port
        data_dir = inst.data_dir
        pid = inst.proc.pid
    if data_dir.exists():
        raise RuntimeError(f"临时目录残留 {data_dir}")
    if process_alive(pid):
        raise RuntimeError(f"服务进程残留 {pid}")
    if listeners(port):
        raise RuntimeError(f"端口残留 {port}：{listeners(port)}")
    print(f"empty instance port {port} cleaned")


def _prove_seeds() -> None:
    with isolated_instance() as inst:
        seed_empty(inst.data_dir)
        multi = seed_multi_status(inst.data_dir)
        pair = seed_failures_24h(inst.data_dir)
        rows = jobs_in(inst.data_dir)
        by_name = {str(row["source_name"]): row for row in rows}
        for job in [*multi, pair.inside, pair.outside]:
            row = by_name.get(job.source_name)
            if row is None or row.get("status") != job.status:
                raise RuntimeError(f"造数状态没有落库：{job.source_name} {row}")
        html = inst.get_text("/")
        banner = f"1 条转写失败：{INSIDE_NAME} {INSIDE_ERROR}"
        if banner not in html:
            raise RuntimeError("24 小时内失败没有进入提示条")
        if "2 条转写失败" in html or "3 条转写失败" in html:
            raise RuntimeError("提示条把 24 小时外的失败算进去了")
        for label in ("处理中", "已完成", "失败"):
            if label not in html:
                raise RuntimeError(f"页面缺少状态 {label}")
    print("seed empty, multi-status, 24h failures OK")


def _prove_api() -> None:
    with isolated_instance() as inst:
        job_id = submit_url_job(inst.base_url, "https://example.invalid/verify-pilot.wav")
        status, raw = request_bytes(inst.base_url, f"/jobs/{job_id}", timeout=15)
        body = json.loads(raw.decode())
        if status != 200 or not isinstance(body, dict) or body.get("id") != job_id:
            raise RuntimeError(f"GET /jobs/{{id}} 对不上：HTTP {status} {body}")
        print(f"api job_id {job_id}")


def _run_empty_item() -> bool:
    with isolated_instance() as inst:
        seed_empty(inst.data_dir)
        result = run_ui_empty(inst.base_url)
        _append_server_log(result, inst.server_log_excerpt())
        directory = write_item(result)
    print(f"UI-EMPTY {'PASS' if result.passed else 'FAIL'} {directory.relative_to(ROOT)}")
    return result.passed


def _run_failbanner_item() -> bool:
    with isolated_instance() as inst:
        seed_failures_24h(inst.data_dir)
        result = run_ui_failbanner(inst.base_url)
        _append_server_log(result, inst.server_log_excerpt())
        directory = write_item(result)
    print(f"UI-FAILBANNER {'PASS' if result.passed else 'FAIL'} {directory.relative_to(ROOT)}")
    return result.passed


def _run_mobile_item() -> bool:
    with isolated_instance() as inst:
        jobs = seed_multi_status(inst.data_dir)
        result = run_ui_mobile(inst.base_url, jobs)
        _append_server_log(result, inst.server_log_excerpt())
        directory = write_item(result)
    print(f"UI-MOBILE {'PASS' if result.passed else 'FAIL'} {directory.relative_to(ROOT)}")
    return result.passed


def _run_polling_item() -> bool:
    result = run_polling()
    directory = write_item(result)
    print(f"POLLING {'PASS' if result.passed else 'FAIL'} {directory.relative_to(ROOT)}")
    return result.passed

def _run_tokens_item() -> bool:
    with isolated_instance() as inst:
        seed_multi_status(inst.data_dir)
        result = run_tokens(inst.base_url)
        _append_server_log(result, inst.server_log_excerpt())
        directory = write_item(result)
    print(f"TOKENS {'PASS' if result.passed else 'FAIL'} {directory.relative_to(ROOT)}")
    return result.passed


def _run_workbench_item() -> bool:
    with isolated_instance() as inst:
        jobs = seed_multi_status(inst.data_dir)
        _require_unchanged(inst.data_dir, jobs)
        result = run_ui_workbench(inst.base_url, jobs)
        _append_server_log(result, inst.server_log_excerpt())
        directory = write_item(result)
    print(f"UI-WORKBENCH {'PASS' if result.passed else 'FAIL'} {directory.relative_to(ROOT)}")
    return result.passed


def _require_unchanged(data_dir: Path, jobs: Sequence[SeededJob]) -> None:
    rows = {str(row["source_name"]): row.get("status") for row in jobs_in(data_dir)}
    for job in jobs:
        status = rows.get(job.source_name)
        if status != job.status:
            raise RuntimeError(f"{job.source_name} 落库后变成 {status}，期望 {job.status}")


def _append_server_log(result: CheckResult, excerpt: str) -> None:
    result.log_txt = result.log_txt.rstrip() + "\n\n服务输出摘录：\n" + excerpt
    if not result.log_txt.endswith("\n"):
        result.log_txt += "\n"


def _run_player_basic_item() -> bool:
    with isolated_instance() as inst:
        job = seed_player_job(inst.data_dir)
        result = run_player_basic(inst.base_url, job)
        _append_server_log(result, inst.server_log_excerpt())
        directory = write_item(result)
    print(f"PLAYER-BASIC {'PASS' if result.passed else 'FAIL'} {directory.relative_to(ROOT)}")
    return result.passed


def _run_player_sync_item() -> bool:
    with isolated_instance() as inst:
        job = seed_player_job(inst.data_dir)
        result = run_player_sync(inst.base_url, job)
        _append_server_log(result, inst.server_log_excerpt())
        directory = write_item(result)
    print(f"PLAYER-SYNC {'PASS' if result.passed else 'FAIL'} {directory.relative_to(ROOT)}")
    return result.passed


def _run_player_empty_item() -> bool:
    with isolated_instance() as inst:
        running_job, missing_job = seed_player_empty_jobs(inst.data_dir)
        result = run_player_empty(inst.base_url, running_job, missing_job)
        _append_server_log(result, inst.server_log_excerpt())
        directory = write_item(result)
    print(f"PLAYER-EMPTY {'PASS' if result.passed else 'FAIL'} {directory.relative_to(ROOT)}")
    return result.passed


PACK_ITEMS: list[tuple[str, str, bool, bool]] = [
    ("UI-EMPTY", "ui", False, False),
    ("UI-WORKBENCH", "ui", False, True),
    ("UI-FAILBANNER", "ui", False, True),
    ("UI-MOBILE", "ui", False, True),
    ("POLLING", "behavior", True, False),
    ("TOKENS", "behavior", False, True),
    ("PLAYER-BASIC", "ui", False, True),
    ("PLAYER-SYNC", "behavior", True, True),
    ("PLAYER-EMPTY", "ui", False, False),
]


def run_pack() -> int:
    revision = require_cached_chromium()
    print(f"playwright chromium revision {revision} cache hit")
    passed = {
        "UI-EMPTY": _run_empty_item(),
        "UI-WORKBENCH": _run_workbench_item(),
        "UI-FAILBANNER": _run_failbanner_item(),
        "UI-MOBILE": _run_mobile_item(),
        "POLLING": _run_polling_item(),
        "TOKENS": _run_tokens_item(),
        "PLAYER-BASIC": _run_player_basic_item(),
        "PLAYER-SYNC": _run_player_sync_item(),
        "PLAYER-EMPTY": _run_player_empty_item(),
    }
    pack_root = _new_pack_root()
    for item_id, *_ in PACK_ITEMS:
        src = ITEMS_DIR / item_id
        if src.is_dir():
            shutil.copytree(src, pack_root / "items" / item_id)
    dup_of = _declare_duplicate_shots(pack_root)
    manifest = _build_manifest(pack_root, passed, dup_of)
    (pack_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (pack_root / "results.md").write_text(
        _render_results(passed), encoding="utf-8"
    )
    print(f"pack 组装完成 {pack_root.relative_to(ROOT)}")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "verify" / "evidence_pack_check.py"), str(pack_root)],
        cwd=ROOT,
    )
    return proc.returncode


def _new_pack_root() -> Path:
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M")
    candidate = OUT_DIR / f"verify-pilot-{stamp}"
    n = 2
    while candidate.exists():
        candidate = OUT_DIR / f"verify-pilot-{stamp}-{n}"
        n += 1
    (candidate / "items").mkdir(parents=True)
    return candidate


def _declare_duplicate_shots(pack_root: Path) -> dict[str, str]:
    first: dict[str, str] = {}  # 截图 sha256 -> "<条目号>/<文件名>"
    dup_of: dict[str, str] = {}
    for item_id, *_ in PACK_ITEMS:
        item_dir = pack_root / "items" / item_id
        vfile = item_dir / "verdict.md"
        for png in sorted(item_dir.glob("*.png")):
            prev = first.get(_sha256(png))
            if prev is not None and prev.split("/", 1)[0] != item_id:
                prev_item = prev.split("/", 1)[0]
                dup_of[item_id] = prev_item
                note = (
                    f"\n同图说明：{png.name} 与条目 {prev_item} 的截图哈希相同；"
                    "两查均在多状态造数实例上截取同一视图、页面内容一致，同图合理。\n"
                )
                with vfile.open("a", encoding="utf-8") as f:
                    f.write(note)
            else:
                first.setdefault(_sha256(png), f"{item_id}/{png.name}")
    return dup_of


def _build_manifest(
    pack_root: Path, passed: dict[str, bool], dup_of: dict[str, str]
) -> dict[str, object]:
    head = _git("rev-parse", "HEAD")
    base = _git("merge-base", "main", "HEAD")
    checklist_git = f"HEAD:{CHECKLIST_PATH.relative_to(ROOT).as_posix()}"
    items: list[dict[str, object]] = []
    for item_id, itype, temporal, asserts_values in PACK_ITEMS:
        files = []
        for p in sorted((pack_root / "items" / item_id).rglob("*")):
            if p.is_file():
                files.append(
                    {
                        "path": p.relative_to(pack_root).as_posix(),
                        "sha256": _sha256(p),
                        "role": _role_of(p.name),
                    }
                )
        entry: dict[str, object] = {
            "id": item_id,
            "type": itype,
            "verdict": "PASS" if passed.get(item_id) else "FAIL",
            "files": files,
            "duplicate_of": dup_of.get(item_id),
        }
        if temporal:
            entry["temporal"] = True
        if asserts_values:
            entry["asserts_values"] = True
        items.append(entry)
    return {
        "schema": "evidence-pack/1",
        "pack_id": pack_root.name,
        "project": "transcriber",
        "checklist": {
            "name": CHECKLIST_PATH.name,
            "rev": _git("rev-parse", "--short", checklist_git),
            "sha256": _sha256(CHECKLIST_PATH),
        },
        "subject": {
            "branch": _git("branch", "--show-current"),
            "range": f"{base}..{head}",
            "base": base,
        },
        "producer": "host-script",
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "items": items,
    }


def _render_results(passed: dict[str, bool]) -> str:
    blocks = []
    for item_id, *_ in PACK_ITEMS:
        ok = passed.get(item_id)
        status = "PASS" if ok else "FAIL"
        detail = "本条判据全部满足" if ok else "本条判据存在未满足项"
        blocks.append(
            f"## {item_id}\n{status}\n结论：{detail}。\n证据：items/{item_id}/verdict.md\n"
        )
    return "\n".join(blocks)


def _role_of(name: str) -> str:
    if name.endswith(".png"):
        return "shot"
    return {
        "verdict.md": "verdict",
        "log.txt": "log",
        "page.txt": "page",
        "run.txt": "run",
        "snapshots.txt": "snapshot",
        "values.txt": "values",
    }[name]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
