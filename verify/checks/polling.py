"""POLLING：用首页 GET 的 access log 分时计数核对轮询开停。"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Page

from verify.browser import chromium_page
from verify.evidence import CheckResult, checklist_line, render_verdict
from verify.instance import IsolatedInstance, isolated_instance
from verify.seed import DONE_NAME, jobs_in, seed_all_done, submit_file_job, write_short_wav

_ITEM = "POLLING"
_HOME_GET = re.compile(r'"GET / HTTP/1\.[01]"')
_FORM_SECONDS = 10.0
_IDLE_SECONDS = 15.0
_SLEEP_ATTEMPTS = (45, 90, 135)
_HOLD_URL = "https://example.invalid/verify-polling"
_HOLD_NAME = "hold-me.wav"
_COUNT_RULE = '只数 uvicorn access log 里请求行恰好为 "GET / HTTP/1.x" 的行'
_FORM_JS = """
() => {
  const input = document.querySelector(".composer input[type=file]");
  const file = input && input.files && input.files[0];
  const nameEl = document.querySelector("[data-file-name]");
  const url = document.querySelector(".composer input[name=url]");
  const precise = document.getElementById("tier-precise");
  const fast = document.getElementById("tier-fast");
  const pill = document.querySelector("table.jobs .pill");
  const live = document.getElementById("live");
  return {
    fileName: file ? file.name : "",
    shownName: nameEl ? nameEl.textContent.trim() : "",
    url: url ? url.value : "",
    precise: precise ? precise.checked : false,
    fast: fast ? fast.checked : false,
    pill: pill ? pill.textContent.trim() : "",
    live: live ? live.hasAttribute("data-live-poll") : false,
  };
}
"""


@dataclass
class _Shot:
    label: str
    elapsed: float
    count: int


@dataclass
class _Positive:
    sleep_s: int
    void_reason: str = ""
    problems: list[str] = field(default_factory=list)
    shots: list[_Shot] = field(default_factory=list)
    form: dict[str, object] = field(default_factory=dict)
    page_txt: str = ""
    screenshot: bytes = b""
    get_lines: list[str] = field(default_factory=list)
    server_log: str = ""


@dataclass
class _Counter:
    problems: list[str] = field(default_factory=list)
    shots: list[_Shot] = field(default_factory=list)
    get_lines: list[str] = field(default_factory=list)
    server_log: str = ""
    page_note: str = ""


def run_polling() -> CheckResult:
    criterion = checklist_line("STUB_SLEEP")
    voided: list[str] = []
    chosen: _Positive | None = None
    last: _Positive | None = None
    for sleep_s in _SLEEP_ATTEMPTS:
        last = _positive(sleep_s)
        if last.problems:
            chosen = last
            break
        if last.void_reason:
            voided.append(f"STUB_SLEEP={sleep_s} {last.void_reason}，本轮作废")
            continue
        chosen = last
        break
    if chosen is None:
        chosen = last if last is not None else _Positive(sleep_s=_SLEEP_ATTEMPTS[-1])
        chosen.problems.append("判据窗口内任务提前完成，调大 STUB_SLEEP 后仍作废")
    counter = _counterexample()
    if not chosen.void_reason:
        _judge_positive(chosen)
    _judge_counter(counter)
    problems = [*chosen.problems, *counter.problems]
    snapshots = _snapshots(chosen, counter, voided)
    passed = not problems and not chosen.void_reason
    if passed:
        conclusion = _pass_text(chosen, counter)
        anomalies = "无"
    else:
        conclusion = "轮询时序没有满足 V3 判据。"
        anomalies = "；".join(problems)
    files = [
        "items/POLLING/verdict.md",
        "items/POLLING/log.txt",
        "items/POLLING/shot-final.png",
        "items/POLLING/page.txt",
        "items/POLLING/snapshots.txt",
        "items/POLLING/run.txt",
    ]
    log = "\n".join(
        [
            f"item={_ITEM}",
            "voided:",
            "\n".join(voided) if voided else "无",
            f"stub_sleep={chosen.sleep_s}",
            "form:",
            json.dumps(chosen.form, ensure_ascii=False),
            "problems:",
            "\n".join(problems) if problems else "无",
            "counter:",
            counter.page_note,
            "正例服务输出摘录：",
            chosen.server_log or "无",
            "反例服务输出摘录：",
            counter.server_log or "无",
        ]
    )
    screenshot = chosen.screenshot
    if not screenshot.startswith(b"\x89PNG"):
        problems.append("没有截到表单保持窗")
        passed = False
        screenshot = _blank_png()
        conclusion = "轮询时序没有满足 V3 判据。"
        anomalies = "；".join(problems)
    run = "\n".join(
        [
            f"# 正例 STUB_SLEEP={chosen.sleep_s} 首页 GET 行（{_COUNT_RULE}）",
            "\n".join(chosen.get_lines) or "无",
            "# 反例全完成实例首页 GET 行（同口径）",
            "\n".join(counter.get_lines) or "无",
        ]
    )
    return CheckResult(
        item_id=_ITEM,
        passed=passed,
        verdict_md=render_verdict(
            item_id=_ITEM,
            criterion=criterion,
            passed=passed,
            conclusion=conclusion,
            files=files,
            anomalies=anomalies,
            section="V3",
        ),
        log_txt=log,
        page_txt=chosen.page_txt,
        screenshot=screenshot,
        extra={"snapshots.txt": snapshots, "run.txt": run},
    )


def _positive(sleep_s: int) -> _Positive:
    result = _Positive(sleep_s=sleep_s)
    with isolated_instance(stub_sleep=float(sleep_s)) as inst:
        wav = inst.data_dir / _HOLD_NAME
        write_short_wav(wav)
        with chromium_page(color_scheme="light") as page:
            _drive_positive(inst, page, wav, result)
        result.get_lines = _home_lines(inst.log_path)
        result.server_log = inst.server_log_excerpt(8000)
    return result


def _drive_positive(
    inst: IsolatedInstance,
    page: Page,
    wav: Path,
    result: _Positive,
) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))
    try:
        job_id = submit_file_job(inst.base_url, wav, tier="fast")
        response = page.goto(inst.base_url + "/", wait_until="load")
        if response is None or response.status != 200:
            status = None if response is None else response.status
            result.problems.append(f"工作台 HTTP {status}")
            return
        page.wait_for_selector("table.jobs .pill")
        result.screenshot = page.screenshot(full_page=True)
        result.page_txt = page.inner_text("body")
        if not page.evaluate(
            "() => document.getElementById('live').hasAttribute('data-live-poll')"
        ):
            result.problems.append("处理中页面没有 data-live-poll")
            return
        if _stop(inst, job_id, "打开页面时", result):
            return
        page.set_input_files(".composer input[type=file]", str(wav))
        page.fill(".composer input[name=url]", _HOLD_URL)
        page.locator('label[for="tier-precise"]').click()
        origin = time.monotonic()
        _mark(result, inst, origin, "form_actions_done")
        if _stop(inst, job_id, "表单保持窗开始时", result):
            return
        _sleep_at_least(_FORM_SECONDS)
        _mark(result, inst, origin, "form_window_end")
        result.form = _object(page.evaluate(_FORM_JS))
        result.page_txt = page.inner_text("body")
        result.screenshot = page.screenshot(full_page=True)
        if _stop(inst, job_id, "表单保持窗结束时", result):
            return
        _mark(result, inst, origin, "processing_idle_start")
        _sleep_at_least(_IDLE_SECONDS)
        _mark(result, inst, origin, "processing_idle_end")
        if _stop(inst, job_id, "处理中静置结束时", result):
            return
        if not _wait_done(inst, page, job_id, result):
            return
        _mark(result, inst, origin, "completed")
        _sleep_at_least(_IDLE_SECONDS)
        _mark(result, inst, origin, "after_complete_idle")
    finally:
        if page_errors:
            result.problems.append("未捕获异常：" + " | ".join(page_errors))


def _counterexample() -> _Counter:
    result = _Counter()
    with isolated_instance() as inst:
        seed_all_done(inst.data_dir)
        origin = time.monotonic()
        with chromium_page(color_scheme="light") as page:
            _mark(result, inst, origin, "before_load")
            response = page.goto(inst.base_url + "/", wait_until="load")
            if response is None or response.status != 200:
                status = None if response is None else response.status
                result.problems.append(f"全完成实例 HTTP {status}")
            page.wait_for_selector("table.jobs .pill")
            _mark(result, inst, origin, "first_load")
            probe = _object(page.evaluate(_FORM_JS))
            result.page_note = (
                f"name={probe.get('pill')} live={probe.get('live')} "
                f"text_has_{DONE_NAME}="
                f"{DONE_NAME in page.inner_text('body')}"
            )
            if probe.get("live") is not False:
                result.problems.append("全完成实例仍带 data-live-poll")
            if DONE_NAME not in page.inner_text("body"):
                result.problems.append(f"全完成实例没有显示 {DONE_NAME}")
            if probe.get("pill") != "已完成":
                result.problems.append(f"全完成实例 pill={probe.get('pill')!r}")
            _sleep_at_least(_IDLE_SECONDS)
            _mark(result, inst, origin, "idle_end")
        result.get_lines = _home_lines(inst.log_path)
        result.server_log = inst.server_log_excerpt(8000)
    return result


def _wait_done(
    inst: IsolatedInstance,
    page: Page,
    job_id: str,
    result: _Positive,
) -> bool:
    deadline = time.monotonic() + result.sleep_s + 30
    while time.monotonic() < deadline:
        row = _job_row(inst.data_dir, job_id)
        status = str(row.get("status") or "")
        minutes = str(row.get("minutes_status") or "")
        if status == "error":
            result.problems.append(f"任务失败：{row.get('error')}")
            return False
        pill = page.evaluate(
            """() => {
              const node = document.querySelector("table.jobs .pill");
              return node ? node.textContent.trim() : "";
            }"""
        )
        if status == "done" and minutes == "done" and pill == "已完成":
            _stable_count(inst)
            return True
        time.sleep(0.25)
    result.problems.append("超时仍未观察到任务完成")
    return False


def _stop(inst: IsolatedInstance, job_id: str, where: str, result: _Positive) -> bool:
    """窗口内已经结束就作废本轮。返回 True 时调用方停止本轮。"""
    row = _job_row(inst.data_dir, job_id)
    status = str(row.get("status") or "")
    minutes = str(row.get("minutes_status") or "")
    if status == "error":
        result.problems.append(f"{where}任务失败：{row.get('error')}")
        return True
    if status in {"queued", "running"}:
        return False
    if status == "done" and minutes in {"pending", "running"}:
        return False
    result.void_reason = f"{where}提前完成（{status}/{minutes}）"
    return True


def _judge_positive(result: _Positive) -> None:
    form_delta = _delta(result.shots, "form_actions_done", "form_window_end")
    if form_delta is None or form_delta < 1:
        result.problems.append(f"表单保持窗 10 秒首页 GET 增量 {form_delta} < 1")
    _require_span(result, "form_actions_done", "form_window_end", _FORM_SECONDS, "表单保持窗")
    form = result.form
    if form.get("fileName") != _HOLD_NAME or form.get("shownName") != _HOLD_NAME:
        result.problems.append(
            f"文件名丢失 file={form.get('fileName')!r} shown={form.get('shownName')!r}"
        )
    if form.get("url") != _HOLD_URL:
        result.problems.append(f"链接丢失 {form.get('url')!r}")
    if form.get("precise") is not True or form.get("fast") is not False:
        result.problems.append(
            f"精档丢失 precise={form.get('precise')!r} fast={form.get('fast')!r}"
        )
    if _HOLD_NAME not in result.page_txt:
        result.problems.append("page.txt 没有保留的文件名")
    idle_delta = _delta(result.shots, "processing_idle_start", "processing_idle_end")
    if idle_delta is None or idle_delta < 2:
        result.problems.append(f"处理中静置 15 秒首页 GET 增量 {idle_delta} < 2")
    _require_span(
        result,
        "processing_idle_start",
        "processing_idle_end",
        _IDLE_SECONDS,
        "处理中静置",
    )
    stopped = _delta(result.shots, "completed", "after_complete_idle")
    if stopped != 0:
        n1 = _count_at(result.shots, "completed")
        n2 = _count_at(result.shots, "after_complete_idle")
        result.problems.append(f"完成后仍在轮询 N1={n1} N2={n2}")
    _require_span(result, "completed", "after_complete_idle", _IDLE_SECONDS, "完成后静置")


def _judge_counter(counter: _Counter) -> None:
    first = _count_at(counter.shots, "first_load")
    before = _count_at(counter.shots, "before_load")
    first_delta = None if first is None or before is None else first - before
    if first is None or first < 1 or first_delta is None or first_delta < 1:
        counter.problems.append(
            f"全完成实例首次加载计数 {first}（较加载前增量 {first_delta}）"
        )
    idle = _delta(counter.shots, "first_load", "idle_end")
    if idle != 0:
        counter.problems.append(f"全完成实例静置 15 秒首页 GET 增量 {idle}")
    _require_span_shots(
        counter.shots,
        counter.problems,
        "first_load",
        "idle_end",
        _IDLE_SECONDS,
        "全完成静置",
    )


def _pass_text(result: _Positive, counter: _Counter) -> str:
    form_delta = _delta(result.shots, "form_actions_done", "form_window_end")
    idle_delta = _delta(result.shots, "processing_idle_start", "processing_idle_end")
    n1 = _count_at(result.shots, "completed")
    n2 = _count_at(result.shots, "after_complete_idle")
    first = _count_at(counter.shots, "first_load")
    idle = _delta(counter.shots, "first_load", "idle_end")
    retry = f"STUB_SLEEP={result.sleep_s}。"
    return (
        f"{retry}表单保持 10 秒内首页 GET 增量 {form_delta}，"
        f"文件 {_HOLD_NAME}、链接 {_HOLD_URL}、精档都还在。"
        f"处理中静置 15 秒增量 {idle_delta}。"
        f"完成后 N1={n1}，再静置 15 秒 N2={n2}。"
        f"全完成实例首次加载计数 {first}，静置 15 秒增量 {idle}。"
    )


def _snapshots(result: _Positive, counter: _Counter, voided: list[str]) -> str:
    lines = [
        f"口径={_COUNT_RULE}",
        "stub_sleep_seconds=" + str(result.sleep_s),
        "voided=" + ("；".join(voided) if voided else "无"),
        f"form_file={result.form.get('fileName')}",
        f"form_shown={result.form.get('shownName')}",
        f"form_url={result.form.get('url')}",
        f"form_precise={result.form.get('precise')}",
        f"form_fast={result.form.get('fast')}",
        f"form_pill={result.form.get('pill')}",
    ]
    lines.extend(_shot_lines("positive", result.shots))
    lines.append("counter_stub_sleep_seconds=0")
    lines.append(f"counter_page={counter.page_note}")
    lines.extend(_shot_lines("counter", counter.shots))
    first = _count_at(counter.shots, "first_load")
    before = _count_at(counter.shots, "before_load")
    idle = _delta(counter.shots, "first_load", "idle_end")
    first_delta = None if first is None or before is None else first - before
    first_ok = (
        first is not None
        and first >= 1
        and first_delta is not None
        and first_delta >= 1
    )
    lines.append(
        "criterion counter_first_load>=1 "
        f"actual={first} delta_from_before={first_delta} "
        f"pass={'true' if first_ok else 'false'}"
    )
    lines.append(
        f"criterion counter_idle_delta==0 actual={idle} "
        f"pass={'true' if idle == 0 else 'false'}"
    )
    lines.append("positive_homepage_get_lines:")
    lines.extend(f"{index} | {line}" for index, line in enumerate(result.get_lines, start=1))
    if not result.get_lines:
        lines.append("无")
    lines.append("counter_homepage_get_lines:")
    lines.extend(f"{index} | {line}" for index, line in enumerate(counter.get_lines, start=1))
    if not counter.get_lines:
        lines.append("无")
    return "\n".join(lines)


def _shot_lines(prefix: str, shots: list[_Shot]) -> list[str]:
    lines: list[str] = []
    previous: _Shot | None = None
    for shot in shots:
        delta = ""
        if previous is not None:
            delta = f" delta={shot.count - previous.count}"
        lines.append(
            f"{prefix} t={shot.elapsed:.3f} {shot.label} count={shot.count}{delta}"
        )
        previous = shot
    return lines


def _mark(result: _Positive | _Counter, inst: IsolatedInstance, origin: float, label: str) -> None:
    result.shots.append(
        _Shot(label, time.monotonic() - origin, _home_count(inst.log_path))
    )


def _delta(shots: list[_Shot], start: str, end: str) -> int | None:
    start_count = _count_at(shots, start)
    end_count = _count_at(shots, end)
    if start_count is None or end_count is None:
        return None
    return end_count - start_count


def _count_at(shots: list[_Shot], label: str) -> int | None:
    found = [shot.count for shot in shots if shot.label == label]
    if not found:
        return None
    return found[-1]


def _require_span(
    result: _Positive,
    start: str,
    end: str,
    seconds: float,
    label: str,
) -> None:
    _require_span_shots(result.shots, result.problems, start, end, seconds, label)


def _require_span_shots(
    shots: list[_Shot],
    problems: list[str],
    start: str,
    end: str,
    seconds: float,
    label: str = "静置",
) -> None:
    start_at = _elapsed(shots, start)
    end_at = _elapsed(shots, end)
    if start_at is None or end_at is None or end_at - start_at < seconds:
        problems.append(f"{label}实际时长不足 {seconds:g} 秒（{start_at} → {end_at}）")


def _elapsed(shots: list[_Shot], label: str) -> float | None:
    found = [shot.elapsed for shot in shots if shot.label == label]
    if not found:
        return None
    return found[-1]


def _stable_count(inst: IsolatedInstance) -> int:
    """完成请求刚写入日志时再读一次，避免把停轮询前的最后一次 GET 漏在 N1 之外。"""
    deadline = time.monotonic() + 2
    current = _home_count(inst.log_path)
    while time.monotonic() < deadline:
        time.sleep(0.4)
        nxt = _home_count(inst.log_path)
        if nxt == current:
            return current
        current = nxt
    return current


def _job_row(data_dir: Path, job_id: str) -> dict[str, object]:
    for row in jobs_in(data_dir):
        if row.get("id") == job_id:
            return row
    raise RuntimeError(f"任务不在库里：{job_id}")


def _home_count(path: Path) -> int:
    return len(_home_lines(path))


def _home_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    return [line for line in text.splitlines() if _HOME_GET.search(line)]


def _object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    return {}


def _sleep_at_least(seconds: float) -> float:
    start = time.monotonic()
    deadline = start + seconds
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return time.monotonic() - start
        time.sleep(left)


def _blank_png() -> bytes:
    """1×1 PNG。只有截图根本没生成时才用来满足证据文件形态。"""
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
