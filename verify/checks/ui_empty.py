"""UI-EMPTY：空实例工作台的引导文案、输入卡和报错。"""

from __future__ import annotations

import json
import re

from playwright.sync_api import ConsoleMessage, Page, Request, Response, ViewportSize

from verify import DESKTOP_WIDTH, LIST_TEMPLATE
from verify.browser import chromium_page
from verify.evidence import CheckResult, checklist_line, render_verdict

_GUIDE = re.compile(r'class="empty-guide">([^<]+)</p>')
_PROBE_URL = "https://example.com/verify-empty"
_ITEM = "UI-EMPTY"
_PROBE_JS = """
() => {
  const box = (el) => {
    if (!el) {
      return null;
    }
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return {
      disabled: Boolean(el.disabled),
      display: style.display,
      visibility: style.visibility,
      width: rect.width,
      height: rect.height,
    };
  };
  const error = document.querySelector("[data-error]");
  const guide = document.querySelector(".empty-guide");
  const submit = document.querySelector(".composer button[type=submit]");
  return {
    guide: guide ? guide.textContent.trim() : "",
    table: Boolean(document.querySelector("table.jobs")),
    banner: Boolean(document.querySelector(".fail-banner")),
    errorHidden: error ? Boolean(error.hidden) : null,
    errorText: error ? error.textContent.trim() : null,
    composer: box(document.querySelector(".composer")),
    file: box(document.querySelector(".composer input[type=file]")),
    url: box(document.querySelector(".composer input[name=url]")),
    submit: box(submit),
    submitText: submit ? submit.textContent.trim() : "",
    theme: document.documentElement.getAttribute("data-theme"),
    width: window.innerWidth,
  };
}
"""


def empty_guide_copy() -> str:
    text = LIST_TEMPLATE.read_text(encoding="utf-8")
    match = _GUIDE.search(text)
    if match is None:
        raise RuntimeError("list.html 没有 empty-guide 文案")
    return match.group(1).strip()


def run_ui_empty(base_url: str) -> CheckResult:
    guide = empty_guide_copy()
    criterion = checklist_line("- UI-EMPTY：")
    problems: list[str] = []
    page_errors: list[str] = []
    console_errors: list[str] = []
    failed_requests: list[str] = []
    with chromium_page(color_scheme="light") as page:
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        page.on("console", lambda msg: _on_console(msg, console_errors))
        page.on("requestfailed", lambda req: _on_request_failed(req, failed_requests))
        page.on("response", lambda resp: _on_http_error(resp, failed_requests))
        response = page.goto(base_url + "/", wait_until="load")
        if response is None or response.status != 200:
            status = None if response is None else response.status
            problems.append(f"工作台 HTTP {status}")
        page.wait_for_selector(".empty-guide")
        _probe_url_field(page, problems)
        probe = page.evaluate(_PROBE_JS)
        text = page.inner_text("body")
        screenshot = page.screenshot(full_page=True)
        viewport = page.viewport_size
    if not isinstance(probe, dict):
        problems.append("页面测量没有返回对象")
        probe = {}
    _judge_empty(
        probe,
        guide,
        viewport,
        page_errors,
        console_errors,
        failed_requests,
        problems,
    )
    passed = not problems
    if passed:
        conclusion = (
            f"空实例工作台有引导文案「{guide}」，"
            "链接框可输入，文件选择与开始转写可用，"
            "页面没有 pageerror、console.error 或失败请求。"
        )
        anomalies = "无"
    else:
        conclusion = "空实例工作台没有满足 V2a 的空状态判据。"
        anomalies = "；".join(problems)
    files = [
        "items/UI-EMPTY/verdict.md",
        "items/UI-EMPTY/log.txt",
        "items/UI-EMPTY/shot-final.png",
        "items/UI-EMPTY/page.txt",
    ]
    log = "\n".join(
        [
            f"item={_ITEM}",
            f"base_url={base_url}",
            f"viewport={viewport}",
            "probe:",
            json.dumps(probe, ensure_ascii=False, indent=2),
            "pageerrors:",
            "\n".join(page_errors) if page_errors else "无",
            "console.errors:",
            "\n".join(console_errors) if console_errors else "无",
            "failed_requests:",
            "\n".join(failed_requests) if failed_requests else "无",
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
        page_txt=text,
        screenshot=screenshot,
    )


def _probe_url_field(page: Page, problems: list[str]) -> None:
    try:
        page.fill('input[name="url"]', _PROBE_URL)
        filled = page.input_value('input[name="url"]')
        if filled != _PROBE_URL:
            problems.append(f"链接框读回 {filled!r}")
    except Exception as exc:
        problems.append(f"链接框不可输入：{exc}")
    try:
        page.fill('input[name="url"]', "")
        if page.input_value('input[name="url"]') != "":
            problems.append("链接框清空后仍有内容")
    except Exception as exc:
        problems.append(f"链接框无法清空：{exc}")


def _on_console(message: ConsoleMessage, sink: list[str]) -> None:
    if message.type == "error":
        sink.append(message.text)


def _on_request_failed(request: Request, sink: list[str]) -> None:
    reason = request.failure or ""
    sink.append(f"requestfailed {request.method} {request.url} {reason}".rstrip())


def _on_http_error(response: Response, sink: list[str]) -> None:
    if response.status >= 400:
        sink.append(
            f"HTTP {response.status} {response.request.method} {response.url}"
        )


def _judge_empty(
    probe: dict[str, object],
    guide: str,
    viewport: ViewportSize | None,
    page_errors: list[str],
    console_errors: list[str],
    failed_requests: list[str],
    problems: list[str],
) -> None:
    if viewport is None or viewport.get("width") != DESKTOP_WIDTH:
        problems.append(f"视口不是 {DESKTOP_WIDTH}：{viewport}")
    if probe.get("guide") != guide:
        problems.append(f"引导文案是 {probe.get('guide')!r}，模板是 {guide!r}")
    if probe.get("table") is True:
        problems.append("空实例出现了任务表")
    if probe.get("banner") is True:
        problems.append("空实例出现了失败条")
    if probe.get("errorHidden") is not True or probe.get("errorText") not in {"", None}:
        problems.append(
            f"错误条可见 hidden={probe.get('errorHidden')} text={probe.get('errorText')!r}"
        )
    if probe.get("theme") != "ledger":
        problems.append(f"浅色模拟后 data-theme={probe.get('theme')!r}")
    if not _visible(probe.get("composer")):
        problems.append("输入卡不可见")
    if not _visible(probe.get("url")) or _disabled(probe.get("url")):
        problems.append("链接框不可用")
    if not _visible(probe.get("submit")) or _disabled(probe.get("submit")):
        problems.append("开始转写按钮不可用")
    if probe.get("submitText") != "开始转写":
        problems.append(f"提交按钮文案是 {probe.get('submitText')!r}")
    file_box = probe.get("file")
    if not isinstance(file_box, dict) or file_box.get("disabled") is True:
        problems.append("文件选择不可用")
    if page_errors:
        problems.append("未捕获异常：" + " | ".join(page_errors))
    if console_errors:
        problems.append("console.error：" + " | ".join(console_errors))
    if failed_requests:
        problems.append("失败请求：" + " | ".join(failed_requests))


def _visible(box: object) -> bool:
    if not isinstance(box, dict):
        return False
    if box.get("display") == "none" or box.get("visibility") == "hidden":
        return False
    width = box.get("width")
    height = box.get("height")
    if not isinstance(width, int | float) or not isinstance(height, int | float):
        return False
    return width > 0 and height > 0


def _disabled(box: object) -> bool:
    return not isinstance(box, dict) or box.get("disabled") is True
