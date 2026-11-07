"""TOKENS：design-tokens.md 与样式源变量零差异 + 非变量项浏览器计算样式实测。"""

from __future__ import annotations

from dataclasses import dataclass

from verify.browser import Scheme, chromium_page
from verify.evidence import CheckResult, checklist_line, render_verdict
from verify.tokens import load_tokens_css, load_tokens_md, same_color

_ITEM = "TOKENS"
_THEME_TITLES = {"ledger": "账本（浅色）", "studio": "录音棚（深色）"}

# 非变量登记项：键、主题、选择器、计算属性、期望值、比较方式。
_NON_VARIABLE = (
    ("row-height", "ledger", ".workbench .jobs tbody tr", "height", "52px", "px"),
    ("row-height", "studio", ".workbench .jobs tbody tr", "height", "64px", "px"),
    ("h1-size", "ledger", ".workbench h1", "font-size", "24px", "px"),
    ("h1-size", "studio", ".workbench h1", "font-size", "20px", "px"),
    ("body-bg", "studio", "body", "background-color", "#070707", "color"),
    ("frame-radius", "studio", ".workbench", "border-radius", "16px", "px"),
)

_MEASURE_JS = """
(selectors) => {
  const out = {};
  for (const [key, selector, prop] of selectors) {
    const el = document.querySelector(selector);
    out[key] = el ? getComputedStyle(el).getPropertyValue(prop).trim() : null;
  }
  return out;
}
"""


@dataclass
class TokenCapture:
    measured: dict[str, str | None]
    text: str
    screenshot: bytes


def run_tokens(base_url: str) -> CheckResult:
    criterion = checklist_line("脚本 diff（仓库")
    problems: list[str] = []
    diff_lines, mismatches = _diff_lines()
    problems.extend(mismatches)
    ledger = _capture(base_url, "light", "账本", problems)
    studio = _capture(base_url, "dark", "录音棚", problems)
    value_lines = _judge_values(ledger, studio, problems)
    passed = not problems
    if passed:
        phrase = _counts_phrase(load_tokens_md(), load_tokens_css())
        conclusion = (
            f"design-tokens.md 与 app/static/style.css {phrase}零差异；"
            "非变量项（行高 52/64、h1 24/20、录音棚 body 底 #070707、frame 圆角 16）"
            "浏览器计算样式实测全部与登记值一致。"
        )
        anomalies = "无"
    else:
        conclusion = "tokens 对账没有满足 V4 判据。"
        anomalies = "；".join(problems)
    files = [
        "items/TOKENS/verdict.md",
        "items/TOKENS/run.txt",
        "items/TOKENS/values.txt",
        "items/TOKENS/shot-final.png",
        "items/TOKENS/shot-studio.png",
        "items/TOKENS/page.txt",
    ]
    log = "\n".join(
        [
            f"item={_ITEM}",
            f"base_url={base_url}",
            f"diff_count={len(mismatches)}",
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
            section="V4",
        ),
        log_txt=log,
        page_txt=ledger.text,
        screenshot=ledger.screenshot,
        extra={
            "run.txt": "\n".join(diff_lines),
            "values.txt": "\n".join(value_lines),
            "shot-studio.png": studio.screenshot,
        },
    )


def _counts_phrase(
    md: dict[str, dict[str, str]],
    css: dict[str, dict[str, str]],
) -> str:
    """按两份 token 字典的实测长度拼结论短语，不写死个数。"""
    parts: list[str] = []
    for theme in ("ledger", "studio"):
        label = "账本" if theme == "ledger" else "录音棚"
        md_n = len(md[theme])
        css_n = len(css[theme])
        if md_n == css_n:
            parts.append(f"{label} {md_n} 个")
        else:
            parts.append(f"{label} md {md_n} 个、css {css_n} 个")
    return " / ".join(parts) + "变量"


def _diff_lines() -> tuple[list[str], list[str]]:
    """md↔css 变量对账全文进 run.txt，返回 (行列表, 不一致清单)。"""
    md = load_tokens_md()
    css = load_tokens_css()
    lines = [
        f"item={_ITEM}",
        "source_tokens=docs/design-tokens.md",
        "source_css=app/static/style.css",
    ]
    mismatches: list[str] = []
    for theme in ("ledger", "studio"):
        lines.append(f"== {_THEME_TITLES[theme]} ==")
        theme_bad = 0
        for name in sorted(set(md[theme]) | set(css[theme])):
            left = md[theme].get(name, "缺失")
            right = css[theme].get(name, "缺失")
            if left != right:
                theme_bad += 1
                mismatches.append(f"{name} md={left} css={right}")
                lines.append(f"{name} md={left} css={right} 不一致")
            else:
                lines.append(f"{name} md={left} css={right} 一致")
        lines.append(f"{theme} diff={theme_bad}")
    return lines, mismatches


def _capture(
    base_url: str,
    color_scheme: Scheme,
    label: str,
    problems: list[str],
) -> TokenCapture:
    theme = "ledger" if color_scheme == "light" else "studio"
    with chromium_page(color_scheme=color_scheme) as page:
        response = page.goto(base_url + "/", wait_until="load")
        if response is None or response.status != 200:
            status = None if response is None else response.status
            problems.append(f"{label}工作台 HTTP {status}")
        page.wait_for_selector("table.jobs tbody tr")
        selectors = [
            [f"{theme}.{key}", selector, prop]
            for key, item_theme, selector, prop, _expected, _kind in _NON_VARIABLE
            if item_theme == theme
        ]
        measured = page.evaluate(_MEASURE_JS, selectors)
        text = page.inner_text("body")
        screenshot = page.screenshot(full_page=True)
    return TokenCapture(
        measured=measured if isinstance(measured, dict) else {},
        text=text,
        screenshot=screenshot,
    )


def _judge_values(
    ledger: TokenCapture,
    studio: TokenCapture,
    problems: list[str],
) -> list[str]:
    captures = {"ledger": ledger, "studio": studio}
    lines = ["非变量项浏览器计算样式实测（1280x800 getComputedStyle）："]
    matched = 0
    for key, theme, _selector, _prop, expected, kind in _NON_VARIABLE:
        actual = captures[theme].measured.get(f"{theme}.{key}")
        match = _matches(actual, expected, kind)
        if match:
            matched += 1
        else:
            problems.append(f"{key} {theme} 实测 {actual or '空'} 与登记值 {expected} 不一致")
        lines.append(
            f"{key} {theme} computed={actual or '空'} expected={expected} "
            f"match={'true' if match else 'false'}"
        )
    lines.append(f"结论：{matched}/{len(_NON_VARIABLE)} 项一致")
    return lines


def _matches(actual: str | None, expected: str, kind: str) -> bool:
    if not actual:
        return False
    if kind == "color":
        return same_color(actual, expected)
    try:
        return float(actual.removesuffix("px")) == float(expected.removesuffix("px"))
    except ValueError:
        return actual == expected
