"""UI-MOBILE：390×844 下列不裁切，隐藏列不出现在可见表格里。"""

from __future__ import annotations

from verify import MOBILE_HEIGHT, MOBILE_WIDTH
from verify.browser import chromium_page
from verify.evidence import CheckResult, checklist_line, render_verdict
from verify.seed import SeededJob

_ITEM = "UI-MOBILE"
_KEPT = ("文件", "状态", "时长", "操作")
_HIDDEN = ("档位", "纪要", "提交时间")
_DOM_ORDER = ("文件", "状态", "档位", "时长", "纪要", "提交时间", "操作")
_KEPT_CLASS = {"col-file", "col-status", "col-duration", "col-action"}
_HIDDEN_CLASS = {"col-tier", "col-minutes", "col-submitted"}
_MEASURE_JS = """
() => {
  const describe = (el) => {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return {
      className: el.className,
      text: (el.textContent || "").trim(),
      visibleText: el.getClientRects().length > 0 ? (el.innerText || "").trim() : "",
      display: style.display,
      clientWidth: el.clientWidth,
      scrollWidth: el.scrollWidth,
      left: rect.left,
      right: rect.right,
      rects: el.getClientRects().length,
    };
  };
  const table = document.querySelector("table.jobs");
  const scrolling = document.scrollingElement;
  return {
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    scrollWidth: scrolling ? scrolling.scrollWidth : null,
    docScrollWidth: document.documentElement.scrollWidth,
    bodyScrollWidth: document.body.scrollWidth,
    headers: Array.from(document.querySelectorAll("table.jobs thead th")).map(describe),
    cells: Array.from(document.querySelectorAll("table.jobs tbody td")).map(describe),
    tableText: table ? table.innerText : "",
  };
}
"""


def run_ui_mobile(base_url: str, jobs: list[SeededJob]) -> CheckResult:
    criterion = checklist_line("- UI-MOBILE：")
    problems: list[str] = []
    page_errors: list[str] = []
    with chromium_page(
        color_scheme="light",
        width=MOBILE_WIDTH,
        height=MOBILE_HEIGHT,
    ) as page:
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        response = page.goto(base_url + "/", wait_until="load")
        if response is None or response.status != 200:
            status = None if response is None else response.status
            problems.append(f"工作台 HTTP {status}")
        page.wait_for_selector("table.jobs thead th")
        measured = page.evaluate(_MEASURE_JS)
        text = page.inner_text("body")
        screenshot = page.screenshot(full_page=False)
        viewport = page.viewport_size
    if page_errors:
        problems.append("未捕获异常：" + " | ".join(page_errors))
    if viewport is None or viewport.get("width") != MOBILE_WIDTH:
        problems.append(f"视口不是 {MOBILE_WIDTH}×{MOBILE_HEIGHT}：{viewport}")
    if viewport is not None and viewport.get("height") != MOBILE_HEIGHT:
        problems.append(f"视口高度不是 {MOBILE_HEIGHT}：{viewport}")
    if not isinstance(measured, dict):
        problems.append("页面测量没有返回对象")
        measured = {}
    value_lines = _judge(measured, jobs, problems)
    passed = not problems
    if passed:
        conclusion = (
            f"390×844 下 scrollWidth={MOBILE_WIDTH}。"
            "可见列是文件、状态、时长、操作，文字没有超出单元格，也没有超出视口。"
            "档位、纪要、提交时间三列 display=none，表格可见文字里没有它们。"
        )
        anomalies = "无"
    else:
        conclusion = "手机视口的列或横向宽度没有满足 V2b 判据。"
        anomalies = "；".join(problems)
    files = [
        "items/UI-MOBILE/verdict.md",
        "items/UI-MOBILE/log.txt",
        "items/UI-MOBILE/shot-final.png",
        "items/UI-MOBILE/page.txt",
        "items/UI-MOBILE/values.txt",
    ]
    log = "\n".join(
        [
            f"item={_ITEM}",
            f"base_url={base_url}",
            f"viewport={viewport}",
            "values:",
            "\n".join(value_lines),
            "pageerrors:",
            "\n".join(page_errors) if page_errors else "无",
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
            section="V2b",
        ),
        log_txt=log,
        page_txt=text,
        screenshot=screenshot,
        extra={"values.txt": "\n".join(value_lines)},
    )


def _judge(
    measured: dict[str, object],
    jobs: list[SeededJob],
    problems: list[str],
) -> list[str]:
    headers = _rows(measured.get("headers"))
    cells = _rows(measured.get("cells"))
    table_text = measured.get("tableText")
    table_text = table_text if isinstance(table_text, str) else ""
    scroll_width = measured.get("scrollWidth")
    lines = [
        f"viewport={measured.get('innerWidth')}x{measured.get('innerHeight')}",
        f"scrollWidth={_fmt(scroll_width)}",
        f"documentElement.scrollWidth={_fmt(measured.get('docScrollWidth'))}",
        f"body.scrollWidth={_fmt(measured.get('bodyScrollWidth'))}",
        "kept=" + ",".join(_KEPT),
        "hidden=" + ",".join(_HIDDEN),
    ]
    if not _same_number(scroll_width, MOBILE_WIDTH):
        problems.append(f"scrollWidth={scroll_width!r}，期望 {MOBILE_WIDTH}")
    if not _same_number(measured.get("docScrollWidth"), MOBILE_WIDTH):
        problems.append(
            f"documentElement.scrollWidth={measured.get('docScrollWidth')!r}"
        )
    if measured.get("innerWidth") != MOBILE_WIDTH:
        problems.append(f"innerWidth={measured.get('innerWidth')!r}")
    if not jobs:
        problems.append("没有造数任务")
    header_texts = [str(row.get("text") or "") for row in headers]
    if header_texts != list(_DOM_ORDER):
        problems.append(f"表头顺序是 {header_texts}，期望 {list(_DOM_ORDER)}")
    visible = [
        str(row.get("text") or "")
        for row in headers
        if row.get("display") != "none"
    ]
    if visible != list(_KEPT):
        problems.append(f"可见表头是 {visible}，期望 {list(_KEPT)}")
    for row in headers:
        _judge_column(row, "header", problems, lines)
    for row in cells:
        _judge_column(row, "cell", problems, lines)
    for name in (job.source_name for job in jobs):
        if name not in table_text:
            problems.append(f"表格可见文字没有 {name}")
    for word in _HIDDEN:
        present = word in table_text
        lines.append(f"table_text_has_{word}={'true' if present else 'false'}")
        if present:
            problems.append(f"表格可见文字仍有隐藏列「{word}」")
    return lines


def _judge_column(
    row: dict[str, object],
    kind: str,
    problems: list[str],
    lines: list[str],
) -> None:
    text = str(row.get("text") or "")
    class_name = str(row.get("className") or "")
    display = str(row.get("display") or "")
    hidden = class_name in _HIDDEN_CLASS
    if class_name not in _KEPT_CLASS and class_name not in _HIDDEN_CLASS:
        problems.append(f"{kind} 未知列 {class_name} {text}")
    clipped = False
    if hidden:
        if display != "none" or row.get("rects") != 0:
            problems.append(f"{kind} {text} 仍可见 display={display} rects={row.get('rects')}")
        if str(row.get("visibleText") or ""):
            problems.append(f"{kind} {text} 的可见文字不是空的")
    else:
        if display == "none" or row.get("rects") == 0:
            problems.append(f"{kind} {text} 被藏起来了 display={display}")
        clipped = _clipped(row)
        if clipped:
            problems.append(
                f"{kind} {text} 被裁切 "
                f"client={row.get('clientWidth')} scroll={row.get('scrollWidth')} "
                f"left={row.get('left')} right={row.get('right')}"
            )
    lines.append(
        f"{kind} {text} display={display} rects={row.get('rects')} "
        f"clientWidth={row.get('clientWidth')} scrollWidth={row.get('scrollWidth')} "
        f"left={_fmt(row.get('left'))} right={_fmt(row.get('right'))} "
        f"clipped={'true' if clipped else 'false'}"
    )


def _clipped(row: dict[str, object]) -> bool:
    client = row.get("clientWidth")
    scroll = row.get("scrollWidth")
    left = row.get("left")
    right = row.get("right")
    if not isinstance(client, int | float) or not isinstance(scroll, int | float):
        return True
    if scroll > client:
        return True
    if not isinstance(left, int | float) or not isinstance(right, int | float):
        return True
    return float(left) < -0.5 or float(right) > MOBILE_WIDTH + 0.5


def _rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _same_number(value: object, expected: int) -> bool:
    return isinstance(value, int | float) and float(value) == float(expected)


def _fmt(value: object) -> str:
    if isinstance(value, float):
        text = f"{value:.3f}".rstrip("0").rstrip(".")
        return text or "0"
    if value is None:
        return ""
    return str(value)
