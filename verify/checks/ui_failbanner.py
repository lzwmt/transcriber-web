"""UI-FAILBANNER：24 小时窗口内的失败条，以及点击后滚到上传区。"""

from __future__ import annotations

from verify import DESKTOP_WIDTH
from verify.browser import chromium_page
from verify.evidence import CheckResult, checklist_line, render_verdict
from verify.seed import INSIDE_ERROR, INSIDE_NAME, OUTSIDE_NAME

_ITEM = "UI-FAILBANNER"
# 上传区被 scrollIntoView({block:"center"}) 放到视口中线，top = (视口高 - 元素高) / 2。
# 元素高约 234。要让 top 落入 [0, 96]，视口高须在 [234, 426]。
# 1280×800 时这一页不溢出，点击后 scrollY 仍是 0，数值判据无法成立。
_VIEWPORT_HEIGHT = 360
_MEASURE_JS = """
() => {
  const banner = document.querySelector(".fail-banner p");
  const drop = document.querySelector("[data-dropzone]");
  const rect = drop ? drop.getBoundingClientRect() : null;
  const names = Array.from(document.querySelectorAll("table.jobs .file-name"))
    .map((el) => el.textContent.trim());
  return {
    scrollY: window.scrollY,
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    bannerCount: document.querySelectorAll(".fail-banner").length,
    bannerText: banner ? banner.textContent.trim() : "",
    uploadTop: rect ? rect.top : null,
    names,
  };
}
"""


def run_ui_failbanner(base_url: str) -> CheckResult:
    criterion = checklist_line("- UI-FAILBANNER：")
    problems: list[str] = []
    page_errors: list[str] = []
    expected = f"1 条转写失败：{INSIDE_NAME} {INSIDE_ERROR}"
    with chromium_page(
        color_scheme="light",
        width=DESKTOP_WIDTH,
        height=_VIEWPORT_HEIGHT,
    ) as page:
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        response = page.goto(base_url + "/", wait_until="load")
        if response is None or response.status != 200:
            status = None if response is None else response.status
            problems.append(f"工作台 HTTP {status}")
        page.wait_for_selector(".fail-banner [data-reupload]")
        page.evaluate("window.scrollTo(0, 0)")
        before = page.evaluate("() => window.scrollY")
        page.locator(".fail-banner [data-reupload]").click()
        measured = page.evaluate(_MEASURE_JS)
        text = page.inner_text("body")
        screenshot = page.screenshot(full_page=False)
    if page_errors:
        problems.append("未捕获异常：" + " | ".join(page_errors))
    if not isinstance(measured, dict):
        problems.append("页面测量没有返回对象")
        measured = {}
    if not isinstance(before, int | float):
        problems.append(f"点击前 scrollY 无法读取：{before!r}")
        before = None
    value_lines = _judge(measured, before, expected, problems)
    passed = not problems
    if passed:
        conclusion = (
            f"提示条是「{expected}」，只计入 24 小时内的 {INSIDE_NAME}。"
            f"点击重新上传后 scrollY 从 {value_lines[0].split('=', 1)[1]} "
            f"增到 { _field(value_lines, 'scrollY_after') }，"
            f"上传区 top={ _field(value_lines, 'upload_top') }，落在 [0, 96]。"
            f"视口 {DESKTOP_WIDTH}×{_VIEWPORT_HEIGHT}。"
        )
        anomalies = "无"
    else:
        conclusion = "失败条计数或点击后的滚动位置没有满足 V2b 判据。"
        anomalies = "；".join(problems)
    files = [
        "items/UI-FAILBANNER/verdict.md",
        "items/UI-FAILBANNER/log.txt",
        "items/UI-FAILBANNER/shot-final.png",
        "items/UI-FAILBANNER/page.txt",
        "items/UI-FAILBANNER/values.txt",
    ]
    log = "\n".join(
        [
            f"item={_ITEM}",
            f"base_url={base_url}",
            f"viewport={DESKTOP_WIDTH}x{_VIEWPORT_HEIGHT}",
            "click=.fail-banner [data-reupload]",
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
    before: object,
    expected: str,
    problems: list[str],
) -> list[str]:
    banner = str(measured.get("bannerText") or "")
    names = measured.get("names")
    name_list = [str(item) for item in names] if isinstance(names, list) else []
    after = measured.get("scrollY")
    top = measured.get("uploadTop")
    width = measured.get("innerWidth")
    height = measured.get("innerHeight")
    increased = (
        isinstance(before, int | float)
        and isinstance(after, int | float)
        and after > before
    )
    top_ok = isinstance(top, int | float) and 0 <= float(top) <= 96
    lines = [
        f"scrollY_before={_fmt(before)}",
        f"scrollY_after={_fmt(after)}",
        f"upload_top={_fmt(top)}",
        f"scrollY_increased={'true' if increased else 'false'}",
        f"upload_top_in_0_96={'true' if top_ok else 'false'}",
        f"viewport={width}x{height}",
        "banner_count=" + _fmt(measured.get("bannerCount")),
        f"banner_text={banner}",
        f"banner_contains_filename={'true' if INSIDE_NAME in banner else 'false'}",
        f"banner_contains_reason={'true' if INSIDE_ERROR in banner else 'false'}",
        f"outside_in_banner={'true' if OUTSIDE_NAME in banner else 'false'}",
        f"inside_in_table={'true' if INSIDE_NAME in name_list else 'false'}",
        f"outside_in_table={'true' if OUTSIDE_NAME in name_list else 'false'}",
    ]
    if width != DESKTOP_WIDTH or height != _VIEWPORT_HEIGHT:
        problems.append(f"视口不是 {DESKTOP_WIDTH}×{_VIEWPORT_HEIGHT}：{width}×{height}")
    if measured.get("bannerCount") != 1:
        problems.append(f"提示条数量是 {measured.get('bannerCount')!r}")
    if banner != expected:
        problems.append(f"提示条文案是 {banner!r}，期望 {expected!r}")
    if OUTSIDE_NAME in banner:
        problems.append("提示条计入了 24 小时外的失败")
    if INSIDE_NAME not in name_list or OUTSIDE_NAME not in name_list:
        problems.append(f"表格没有同时列出两条失败：{name_list}")
    if not increased:
        problems.append(f"点击后 scrollY={after!r}，点击前 {before!r}")
    if not top_ok:
        problems.append(f"上传区 top={top!r}，不在 [0, 96]")
    return lines


def _field(lines: list[str], key: str) -> str:
    prefix = key + "="
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix) :]
    return ""


def _fmt(value: object) -> str:
    if isinstance(value, float):
        text = f"{value:.3f}".rstrip("0").rstrip(".")
        return text or "0"
    if value is None:
        return ""
    return str(value)
