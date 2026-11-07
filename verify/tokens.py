"""执行时从 design-tokens.md 和样式源读取 pill 色，不把色值写死在检查里。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from verify import STYLE_PATH, TOKENS_PATH

_THEME_HEADINGS = {
    "ledger": "## 主题 A · 账本（浅色）",
    "studio": "## 主题 B · 录音棚（深色）",
}
_RULE = re.compile(
    r"\.workbench\s+\.pill-(queued|running|done|error)\s*\{([^}]*)\}",
    re.MULTILINE,
)
_DECL = re.compile(
    r"(?<![\w-])(background|color)\s*:\s*var\(\s*(--[a-z0-9-]+)\s*\)"
)
_TOKEN = re.compile(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\b")
_VALUE = re.compile(r"(?<![\w-])(--[a-z0-9-]+)\s*:\s*([^;]+);")
_RGB = re.compile(
    r"rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})"
    r"(?:\s*[,/]\s*([0-9.]+%?))?\s*\)"
)


@dataclass(frozen=True)
class PillRule:
    status: str
    background_var: str
    color_var: str


def load_pill_rules() -> dict[str, PillRule]:
    css = STYLE_PATH.read_text(encoding="utf-8")
    found: dict[str, PillRule] = {}
    for match in _RULE.finditer(css):
        status = match.group(1)
        variables: dict[str, str] = {}
        for decl in _DECL.finditer(match.group(2)):
            prop, variable = decl.group(1), decl.group(2)
            previous = variables.get(prop)
            if previous is not None and previous != variable:
                raise RuntimeError(f".pill-{status} 的 {prop} 指向了两个变量")
            variables[prop] = variable
        background = variables.get("background")
        color = variables.get("color")
        if background is None or color is None:
            raise RuntimeError(f".pill-{status} 没有同时声明 background 与 color 变量")
        found[status] = PillRule(status, background, color)
    if not found:
        raise RuntimeError("样式源没有 .workbench .pill-* 规则")
    return found


def load_theme_colors() -> dict[str, dict[str, str]]:
    text = TOKENS_PATH.read_text(encoding="utf-8")
    themes: dict[str, dict[str, str]] = {}
    for theme, heading in _THEME_HEADINGS.items():
        colors: dict[str, str] = {}
        for match in _TOKEN.finditer(_section(text, heading)):
            colors["--" + match.group(1)] = match.group(2).lower()
        if not colors:
            raise RuntimeError(f"design-tokens.md 的 {heading} 没有颜色")
        themes[theme] = colors
    return themes


def _clean_value(value: str) -> str:
    """md 值后可能挂 /* 注释 */ 或（中文注解），剥掉再与样式源比对。"""
    text = re.sub(r"/\*.*?\*/", "", value)
    text = re.sub(r"（[^）]*）", "", text)
    return text.strip().lower()


def load_tokens_md() -> dict[str, dict[str, str]]:
    """design-tokens.md 两主题全部变量（含 px 值），键为 --name。"""
    text = TOKENS_PATH.read_text(encoding="utf-8")
    themes: dict[str, dict[str, str]] = {}
    for theme, heading in _THEME_HEADINGS.items():
        tokens = {
            match.group(1): _clean_value(match.group(2))
            for match in _VALUE.finditer(_section(text, heading))
        }
        if not tokens:
            raise RuntimeError(f"design-tokens.md 的 {heading} 没有变量")
        themes[theme] = tokens
    return themes


def _css_block(css: str, selector: str) -> str:
    """截取样式源里 selector 对应的块体（selector 需含结尾 {）。"""
    start = css.find(selector)
    if start < 0:
        raise RuntimeError(f"样式源缺少 {selector}")
    depth = 0
    for index in range(start + len(selector) - 1, len(css)):
        char = css[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return css[start + len(selector) : index]
    raise RuntimeError(f"样式源 {selector} 块没有闭合")


def load_tokens_css() -> dict[str, dict[str, str]]:
    """app/static/style.css :root 与 [data-theme=studio] 块的变量。"""
    css = STYLE_PATH.read_text(encoding="utf-8")
    themes: dict[str, dict[str, str]] = {}
    for theme, selector in (("ledger", ":root {"), ("studio", '[data-theme="studio"] {')):
        tokens = {
            match.group(1): _clean_value(match.group(2))
            for match in _VALUE.finditer(_css_block(css, selector))
        }
        if not tokens:
            raise RuntimeError(f"样式源的 {selector} 没有变量")
        themes[theme] = tokens
    return themes

def same_transparent(value: str) -> bool:
    """computed background-color 是否完全透明：透明 / alpha=0 的 rgb 或 rgba 都算。"""
    text = value.strip().lower()
    if text in {"", "transparent"}:
        return True
    match = _RGB.fullmatch(text)
    if match is None:
        return False
    alpha = match.group(4)
    if alpha is None:
        return False
    if alpha.endswith("%"):
        return float(alpha[:-1]) == 0.0
    return float(alpha) == 0.0


def same_color(left: str, right: str) -> bool:
    try:
        return parse_color(left) == parse_color(right)
    except ValueError:
        return False


def parse_color(value: str) -> tuple[int, int, int]:
    text = value.strip().lower()
    if text.startswith("#"):
        return _parse_hex(text)
    match = _RGB.fullmatch(text)
    if match is None:
        raise ValueError(value)
    channels = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if any(channel > 255 for channel in channels):
        raise ValueError(value)
    alpha = match.group(4)
    if alpha is not None and alpha not in {"1", "1.0", "100%"}:
        raise ValueError(value)
    return channels


def _parse_hex(text: str) -> tuple[int, int, int]:
    hexpart = text[1:]
    if len(hexpart) == 3:
        hexpart = "".join(ch * 2 for ch in hexpart)
    if len(hexpart) == 8:
        if hexpart[6:] not in {"ff"}:
            raise ValueError(text)
        hexpart = hexpart[:6]
    if len(hexpart) != 6 or any(ch not in "0123456789abcdef" for ch in hexpart):
        raise ValueError(text)
    return (int(hexpart[0:2], 16), int(hexpart[2:4], 16), int(hexpart[4:6], 16))


def _section(text: str, heading: str) -> str:
    start = text.find(heading)
    if start < 0:
        raise RuntimeError(f"design-tokens.md 缺少标题：{heading}")
    rest = text[start + len(heading) :]
    next_heading = rest.find("\n## ")
    if next_heading >= 0:
        rest = rest[:next_heading]
    return rest
