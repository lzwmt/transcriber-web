"""W4 主题初始化：模板级断言，不启动浏览器。

仓库无 JS 运行环境，app.js 与 CSS 只能文本对账，行为由清单 U 条实测兜底。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
STYLESHEET = '<link rel="stylesheet" href="/static/style.css">'


def _home_html(client: tuple[TestClient, Path]) -> str:
    http, _data = client
    response = http.get("/")
    assert response.status_code == 200
    return response.text


def _inline_theme_script(html: str) -> str:
    link_at = html.find(STYLESHEET)
    assert link_at != -1
    head = html[:link_at]
    match = re.search(r"<script>(?P<body>.*)</script>\s*$", head, flags=re.DOTALL)
    assert match is not None
    return match.group("body")


def test_w4_inline_script_before_stylesheet(client: tuple[TestClient, Path]) -> None:
    """主题初始化脚本在样式表之前，只对模板里的脚本文本。

    源码对账理由见模块头。
    """
    html = _home_html(client)
    script = _inline_theme_script(html)
    link_at = html.find(STYLESHEET)
    assert html.find("<script>") < link_at
    assert html.find(STYLESHEET) < html.find('<script src="/static/app.js"></script>')
    assert "localStorage.getItem(" in script
    assert "theme" in script
    assert "try" in script
    assert "catch" in script
    get_at = script.find("localStorage.getItem(")
    assert script.rfind("try", 0, get_at) != -1
    assert script.find("catch", get_at) != -1


def test_w4_invalid_saved_theme_falls_back_and_listener_is_conditional(
    client: tuple[TestClient, Path],
) -> None:
    """非法存档回退到系统主题，且只在未手动选择时监听系统变化。

    源码对账理由见模块头。
    """
    script = _inline_theme_script(_home_html(client))
    assert '=== "ledger"' in script
    assert '=== "studio"' in script
    assert script.count("return stored") == 1
    assert "prefers-color-scheme: dark" in script
    assert 'matches ? "studio" : "ledger"' in script
    saved_at = script.find("if (saved)")
    else_at = script.find("} else {")
    listen_at = script.find('addEventListener("change"')
    assert saved_at != -1
    assert else_at != -1
    assert listen_at != -1
    assert saved_at < else_at < listen_at
    assert script.find("addEventListener", listen_at + 1) == -1
    listener = script[listen_at:]
    assert "readSaved()" in listener
    assert listener.find("readSaved()") < listener.find("applySystem()")
    assert '"click"' not in script


def test_w4_toggle_persists_choice_and_syncs_icon(client: tuple[TestClient, Path]) -> None:
    """切换按钮在顶栏。点击后的存储和图标同步只对 app.js 源码。

    源码对账理由见模块头。
    """
    html = _home_html(client)
    assert 'aria-label="切换主题"' in html
    assert "data-theme-toggle" in html
    assert 'data-icon="sun"' in html
    assert 'data-icon="moon"' in html
    button_at = html.find("data-theme-toggle")
    header_at = html.find('<header class="topbar">')
    header_end = html.find("</header>", header_at)
    assert header_at != -1
    assert header_at < button_at < header_end
    button = html[header_at:header_end]
    assert button.count("<svg") >= 2

    js = _static_text(client, "/static/app.js")
    head, separator, tail = js.partition("})();")
    assert separator == "})();"
    assert "[data-new-form]" in head
    assert "文件和链接只能提交一种" in head
    assert "请上传文件或粘贴链接" in head
    assert "data-theme-toggle" not in head
    assert "[data-new-form]" not in tail
    assert 'querySelector("[data-theme-toggle]")' in tail
    assert 'addEventListener("click"' in tail
    assert 'setAttribute("data-theme"' in tail
    assert '=== "ledger" ? "studio" : "ledger"' in tail
    set_at = tail.find('localStorage.setItem("theme"')
    assert set_at != -1
    assert tail.rfind("try", 0, set_at) != -1
    assert tail.find("catch", set_at) != -1
    assert 'data-icon="sun"' in tail
    assert 'data-icon="moon"' in tail


FONT_STACK = (
    '"Geist","Inter",-apple-system,BlinkMacSystemFont,"Segoe UI","Helvetica Neue",'
    'Arial,"PingFang SC","Microsoft YaHei","Noto Sans SC",sans-serif'
)


def _static_text(client: tuple[TestClient, Path], path: str) -> str:
    http, _data = client
    response = http.get(path)
    assert response.status_code == 200
    return response.text


def _css(client: tuple[TestClient, Path]) -> str:
    return _static_text(client, "/static/style.css")


def _rule_body(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match is not None, selector
    return match.group(1)


def _declarations(block: str) -> dict[str, str]:
    stripped = re.sub(r"/\*.*?\*/", "", block, flags=re.DOTALL)
    pairs = re.findall(r"(--[A-Za-z0-9-]+)\s*:\s*([^;]+)", stripped)
    return {name: value.strip() for name, value in pairs}


def _token_declarations(block: str) -> dict[str, str]:
    """从 design-tokens.md 的 CSS 块取变量。行尾「（注）」不是值，例如 999px（胶囊）。"""
    values: dict[str, str] = {}
    for name, raw in _declarations(block).items():
        values[name] = re.sub(r"（[^）]*）\s*$", "", raw).strip()
    return values


def _design_token_maps() -> tuple[dict[str, str], dict[str, str]]:
    text = (ROOT / "docs" / "design-tokens.md").read_text(encoding="utf-8")
    fences = re.findall(r"```css\n(.*?)```", text, flags=re.DOTALL)
    assert len(fences) == 2
    return _token_declarations(fences[0]), _token_declarations(fences[1])


def test_theme_css_tokens_match_design_tokens(client: tuple[TestClient, Path]) -> None:
    """style.css 的令牌与 docs/design-tokens.md 的两段 CSS 对账。

    文档头不再引用仓内不存在的路径；左右 padding 取 --pad。
    源码对账理由见模块头。
    """
    css = _css(client)
    tokens_doc = (ROOT / "docs" / "design-tokens.md").read_text(encoding="utf-8")
    assert "CHANGELOG-UI.md" not in tokens_doc
    assert "explorations/" not in tokens_doc
    assert "左右 padding 取 --pad（账本 24、录音棚 22）" in tokens_doc
    root_at = css.find(":root")
    studio_at = css.find('[data-theme="studio"]')
    assert 0 <= root_at < studio_at
    ledger = _declarations(_rule_body(css[root_at:], ":root"))
    studio = _declarations(_rule_body(css[studio_at:], '[data-theme="studio"]'))
    ledger_tokens, studio_tokens = _design_token_maps()
    assert ledger == ledger_tokens
    assert studio == studio_tokens

    page = _rule_body(css, "html,\nbody")
    assert "background: var(--ground);" in page
    assert "color: var(--text);" in page
    assert FONT_STACK in page
    assert "line-height: 1.5;" in page
    topbar = _rule_body(css, ".topbar")
    assert "background: var(--ground);" in topbar
    assert "border-bottom: 1px solid var(--border);" in topbar
    assert "display: flex;" in topbar
    assert "gap: 12px;" in topbar
    assert "margin-bottom: 20px;" in topbar
    chrome = re.search(
        r'html\[data-theme="studio"\] body[\s\S]*?\{([^}]*)\}',
        css,
    )
    assert chrome is not None
    assert "#070707" in chrome.group(1)

    assert "12 / 14 / 16 / 20 / 24 / 32" in css
    assert "height: 36px;" in _rule_body(css, ".theme-toggle")
    assert "height: 44px;" in _rule_body(css, ".btn.lg")
    assert "height:" not in _rule_body(css, ".btn")
    assert "background: var(--surface);" in _rule_body(css, 'html[data-theme="studio"] .card')
    assert "var(--accent)" in _rule_body(css, 'html[data-theme="studio"] .btn.primary')
    assert "http://" not in css
    assert "https://" not in css
    assert "@import" not in css


def test_color_scheme_follows_theme(client: tuple[TestClient, Path]) -> None:
    """账本声明 light，录音棚声明 dark，让原生控件跟主题走。

    源码对账理由见模块头。
    """
    css = _css(client)
    assert "color-scheme: light;" in _rule_body(css, ":root")
    assert "color-scheme: dark;" in _rule_body(css, '[data-theme="studio"]')


def test_studio_badge_running_uses_warn_tokens(client: tuple[TestClient, Path]) -> None:
    """录音棚处理中徽标用警告色。账本 .badge-running 保持原来的蓝底。

    源码对账理由见模块头。
    """
    css = _css(client)
    studio = _rule_body(css, 'html[data-theme="studio"] .badge-running')
    assert "background: var(--warn-bg);" in studio
    assert "color: var(--warn-ink);" in studio
    ledger = _rule_body(css, ".badge-running")
    assert "background: #dbeafe;" in ledger
    assert "color: #1d4ed8;" in ledger
