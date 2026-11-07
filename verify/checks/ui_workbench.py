"""UI-WORKBENCH：1280 账本与深色模拟录音棚下的状态 pill 色值。"""

from __future__ import annotations

import json
from typing import cast

from verify import DESKTOP_WIDTH
from verify.browser import Scheme, chromium_page
from verify.evidence import CheckResult, checklist_line, render_verdict
from verify.seed import REQUIRED_STATUSES, SeededJob
from verify.tokens import PillRule, load_pill_rules, load_theme_colors, same_color

_ITEM = "UI-WORKBENCH"
_ROWS_JS = """
() => {
  const rows = Array.from(document.querySelectorAll("table.jobs tbody tr"))
    .map((tr) => {
      const pill = tr.querySelector(".pill");
      const nameEl = tr.querySelector(".file-name");
      const style = pill ? getComputedStyle(pill) : null;
      return {
        name: nameEl ? nameEl.textContent.trim() : "",
        pillText: pill ? pill.textContent.trim() : "",
        pillClass: pill ? pill.className : "",
        backgroundColor: style ? style.backgroundColor : "",
        color: style ? style.color : "",
      };
    });
  return {
    theme: document.documentElement.getAttribute("data-theme"),
    width: window.innerWidth,
    banner: Boolean(document.querySelector(".fail-banner")),
    rows,
  };
}
"""
_VARS_JS = """
(names) => {
  const style = getComputedStyle(document.documentElement);
  const out = {};
  for (const name of names) {
    out[name] = style.getPropertyValue(name).trim();
  }
  return out;
}
"""


def run_ui_workbench(base_url: str, jobs: list[SeededJob]) -> CheckResult:
    rules = load_pill_rules()
    themes = load_theme_colors()
    names = _variable_names(rules)
    criterion = checklist_line("- UI-WORKBENCH：")
    problems: list[str] = []
    _require_seed(jobs, problems)
    ledger = _capture(base_url, "light", names, problems, "账本")
    studio = _capture(base_url, "dark", names, problems, "录音棚")
    value_lines = [
        "source_tokens=docs/design-tokens.md",
        "source_css=app/static/style.css",
        f"viewport={DESKTOP_WIDTH}",
    ]
    _judge_theme(
        "ledger",
        "shot-final.png",
        ledger,
        jobs,
        rules,
        themes,
        problems,
        value_lines,
    )
    _judge_theme(
        "studio",
        "shot-studio.png",
        studio,
        jobs,
        rules,
        themes,
        problems,
        value_lines,
    )
    page_txt = _page_text(ledger["text"], studio["text"])
    passed = not problems
    if passed:
        conclusion = (
            "1280 宽账本与深色模拟录音棚下，造数中的处理中、已完成、失败 pill "
            "计算底色和字色都与样式变量以及 design-tokens.md 对应色值一致。"
        )
        anomalies = "无"
    else:
        conclusion = "工作台 pill 色值或主题抽查没有满足 V2a 判据。"
        anomalies = "；".join(problems)
    files = [
        "items/UI-WORKBENCH/verdict.md",
        "items/UI-WORKBENCH/log.txt",
        "items/UI-WORKBENCH/shot-final.png",
        "items/UI-WORKBENCH/shot-studio.png",
        "items/UI-WORKBENCH/page.txt",
        "items/UI-WORKBENCH/values.txt",
    ]
    log = "\n".join(
        [
            f"item={_ITEM}",
            f"base_url={base_url}",
            "ledger=" + json.dumps(_public_capture(ledger), ensure_ascii=False),
            "studio=" + json.dumps(_public_capture(studio), ensure_ascii=False),
            "problems:",
            "\n".join(problems) if problems else "无",
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
        ),
        log_txt=log,
        page_txt=page_txt,
        screenshot=cast(bytes, ledger["screenshot"]),
        extra={
            "shot-studio.png": cast(bytes, studio["screenshot"]),
            "values.txt": "\n".join(value_lines),
        },
    )


def _capture(
    base_url: str,
    color_scheme: Scheme,
    names: list[str],
    problems: list[str],
    label: str,
) -> dict[str, object]:
    page_errors: list[str] = []
    with chromium_page(color_scheme=color_scheme) as page:
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        response = page.goto(base_url + "/", wait_until="load")
        if response is None or response.status != 200:
            status = None if response is None else response.status
            problems.append(f"{label}工作台 HTTP {status}")
        page.wait_for_selector("table.jobs .pill")
        measured = page.evaluate(_ROWS_JS)
        variables = page.evaluate(_VARS_JS, names)
        text = page.inner_text("body")
        screenshot = page.screenshot(full_page=True)
        viewport = page.viewport_size
    if page_errors:
        problems.append(label + "未捕获异常：" + " | ".join(page_errors))
    if viewport is None or viewport.get("width") != DESKTOP_WIDTH:
        problems.append(f"{label}视口不是 {DESKTOP_WIDTH}：{viewport}")
    return {
        "measured": measured if isinstance(measured, dict) else {},
        "variables": variables if isinstance(variables, dict) else {},
        "text": text,
        "screenshot": screenshot,
        "page_errors": page_errors,
    }


def _judge_theme(
    expected_theme: str,
    shot_name: str,
    capture: dict[str, object],
    jobs: list[SeededJob],
    rules: dict[str, PillRule],
    themes: dict[str, dict[str, str]],
    problems: list[str],
    value_lines: list[str],
) -> None:
    measured = capture["measured"]
    variables = capture["variables"]
    if not isinstance(measured, dict) or not isinstance(variables, dict):
        problems.append(f"{expected_theme} 测量结果无法读取")
        return
    actual = measured.get("theme")
    width = measured.get("width")
    value_lines.append(
        f"theme_expected={expected_theme} data-theme={actual} "
        f"innerWidth={width} shot={shot_name}"
    )
    if actual != expected_theme:
        problems.append(f"{shot_name} 期望 data-theme={expected_theme}，实际 {actual!r}")
    if width != DESKTOP_WIDTH:
        problems.append(f"{shot_name} innerWidth={width}，期望 {DESKTOP_WIDTH}")
    if measured.get("banner") is True:
        problems.append(f"{shot_name} 出现了失败条")
    theme_key = actual if isinstance(actual, str) and actual in themes else expected_theme
    palette = themes[theme_key]
    rows = measured.get("rows")
    if not isinstance(rows, list):
        problems.append(f"{shot_name} 没有任务行")
        return
    by_name: dict[str, dict[str, object]] = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("name"), str):
            by_name[str(row["name"])] = row
    expected_names = {job.source_name for job in jobs}
    if set(by_name) != expected_names:
        problems.append(
            f"{shot_name} 任务行 {sorted(by_name)} 与造数 {sorted(expected_names)} 不一致"
        )
    for job in jobs:
        row = by_name.get(job.source_name)
        if row is None:
            continue
        _judge_pill(job, row, rules, palette, variables, shot_name, problems, value_lines)


def _judge_pill(
    job: SeededJob,
    row: dict[str, object],
    rules: dict[str, PillRule],
    palette: dict[str, str],
    variables: dict[str, object],
    shot_name: str,
    problems: list[str],
    value_lines: list[str],
) -> None:
    prefix = f"{shot_name} {job.source_name}"
    pill_text = row.get("pillText")
    pill_class = str(row.get("pillClass") or "")
    if pill_text != job.label:
        problems.append(f"{prefix} pill 文案 {pill_text!r}，期望 {job.label!r}")
    if f"pill-{job.status}" not in pill_class.split():
        problems.append(f"{prefix} class={pill_class!r}，期望 pill-{job.status}")
    rule = rules.get(job.status)
    if rule is None:
        problems.append(f"{prefix} 样式源没有 .pill-{job.status}")
        return
    computed_bg = str(row.get("backgroundColor") or "")
    computed_fg = str(row.get("color") or "")
    _compare(
        prefix,
        "background-color",
        computed_bg,
        rule.background_var,
        variables,
        palette,
        problems,
        value_lines,
    )
    _compare(
        prefix,
        "color",
        computed_fg,
        rule.color_var,
        variables,
        palette,
        problems,
        value_lines,
    )


def _compare(
    prefix: str,
    prop: str,
    computed: str,
    variable: str,
    variables: dict[str, object],
    palette: dict[str, str],
    problems: list[str],
    value_lines: list[str],
) -> None:
    variable_value = variables.get(variable)
    shown_var = variable_value.strip() if isinstance(variable_value, str) else ""
    token = palette.get(variable, "")
    var_ok = bool(shown_var) and same_color(computed, shown_var)
    token_ok = bool(token) and same_color(computed, token)
    if not token:
        problems.append(f"{prefix} 的 {variable} 不在 design-tokens.md 当前主题")
    if not var_ok or not token_ok:
        detail = f"variable={variable}:{shown_var or '空'} design_token={token or '空'}"
        problems.append(f"{prefix} {prop}={computed} {detail}")
    value_lines.append(
        f"pill {prefix} {prop} computed={computed} variable={variable} "
        f"variable_value={shown_var or '空'} design_token={token or '空'} "
        f"match={'true' if var_ok and token_ok else 'false'}"
    )


def _require_seed(jobs: list[SeededJob], problems: list[str]) -> None:
    present = {job.status for job in jobs}
    missing = [status for status in REQUIRED_STATUSES if status not in present]
    if missing:
        problems.append(f"造数缺少状态 {missing}")


def _variable_names(rules: dict[str, PillRule]) -> list[str]:
    names: list[str] = []
    for rule in rules.values():
        for name in (rule.background_var, rule.color_var):
            if name not in names:
                names.append(name)
    return names


def _page_text(ledger: object, studio: object) -> str:
    ledger_text = ledger if isinstance(ledger, str) else ""
    studio_text = studio if isinstance(studio, str) else ""
    if ledger_text == studio_text:
        return ledger_text
    return (
        "【账本 shot-final.png】\n"
        + ledger_text
        + "\n\n【录音棚 shot-studio.png】\n"
        + studio_text
    )


def _public_capture(capture: dict[str, object]) -> dict[str, object]:
    return {
        "measured": capture.get("measured"),
        "variables": capture.get("variables"),
        "page_errors": capture.get("page_errors"),
    }
