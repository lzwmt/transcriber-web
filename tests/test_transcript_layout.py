"""E 节逐字稿样式的读数验收：两列结构、时间列 52px 无背景、说话人 nowrap 标签、高亮只落正文块。

判据来源：SPEC-BATCH-20261103 E 节 + CHECKLIST-BATCH-20261103 E1/E2/E4。
这里量的是 computed 值（真浏览器 + 真样式表），不靠文本对账：
- 样式表内容整段内联进页面，DOM 取自 GET /jobs/{id}/view 的真实渲染结果；
- 两组视口（390×844 对应 E1、1280×800 对应 E4）跑同一套读数，结论必须一致；
- 每个视口内两套主题（账本 / 录音棚）都量，期望色取自 docs/design-tokens.md。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("playwright")

from fastapi.testclient import TestClient  # noqa: E402
from verify.browser import chromium_page  # noqa: E402
from verify.tokens import same_color, same_transparent  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STYLESHEET = ROOT / "app" / "static" / "style.css"
TOKENS_DOC = ROOT / "docs" / "design-tokens.md"

# 视口：390×844 是 E1 的手机档，1280×800 是 E4 的桌面档。参数表直接写字面量，
# 因为 tests/test_governance.py 的基线对账只能静态统计字面量列表。

_MEASURE_JS = """
() => {
  const q = (sel) => document.querySelector(sel);
  const css = (el, prop) => (el ? getComputedStyle(el).getPropertyValue(prop).trim() : null);
  const line = q(".seg-line");
  const time = line.querySelector(".seg-time");
  const body = line.querySelector(".seg-body");
  const ts = line.querySelector(".ts");
  const label = line.querySelector(".seg-speaker");
  const lineRect = line.getBoundingClientRect();
  const timeRect = time.getBoundingClientRect();
  const bodyRect = body.getBoundingClientRect();
  const labelRect = label ? label.getBoundingClientRect() : null;
  const labelRects = [];
  if (label && label.firstChild) {
    const range = document.createRange();
    range.selectNodeContents(label);
    for (const r of range.getClientRects()) {
      labelRects.push({ w: Math.round(r.width), h: Math.round(r.height) });
    }
  }
  const now = q(".seg-line.now");
  const nowTime = now ? now.querySelector(".seg-time") : null;
  const nowBody = now ? now.querySelector(".seg-body") : null;
  return {
    viewport: { width: window.innerWidth, height: window.innerHeight },
    lineWidth: Math.round(lineRect.width),
    timeWidth: Math.round(timeRect.width),
    bodyWidth: Math.round(bodyRect.width),
    tsWidth: ts ? Math.round(ts.getBoundingClientRect().width) : null,
    timeTop: Math.round(timeRect.top),
    bodyTop: Math.round(bodyRect.top),
    columnGap: Math.round(bodyRect.left - timeRect.right),
    timeBackground: css(time, "background-color"),
    tsBackground: css(ts, "background-color"),
    labelWhiteSpace: css(label, "white-space"),
    labelDisplay: css(label, "display"),
    labelLineRects: labelRects,
    labelHeight: labelRect ? Math.round(labelRect.height) : null,
    labelLineHeight: css(label, "line-height"),
    nowLineBackground: css(now, "background-color"),
    nowTimeBackground: css(nowTime, "background-color"),
    nowBodyBackground: css(nowBody, "background-color"),
    nowBodyRadius: css(nowBody, "border-top-left-radius"),
    bodyBackground: css(body, "background-color"),
    // hover 态：用 CSS.supports 无法模拟 hover，改为查样式表里是否存在
    // 给 .seg-time / .ts 设 background 的 hover 规则（读到即算覆盖）。
    hoverBackgrounds: (() => {
      const hits = [];
      for (const sheet of document.styleSheets) {
        let rules;
        try {
          rules = sheet.cssRules;
        } catch (err) {
          continue;
        }
        for (const rule of rules) {
          const sel = rule.selectorText || "";
          if (!sel.includes(":hover")) continue;
          const touchesTime =
            sel.indexOf(".seg-time") >= 0 || sel.indexOf(".ts") >= 0;
          if (!touchesTime) continue;
          const bg = rule.style && rule.style.getPropertyValue("background");
          const bgColor = rule.style && rule.style.getPropertyValue("background-color");
          hits.push({ sel, bg: bg || bgColor || "" });
        }
      }
      return hits;
    })(),
    pageBackground: css(document.body, "background-color"),
    documentScrollWidth: document.documentElement.scrollWidth,
    documentClientWidth: document.documentElement.clientWidth,
  };
}
"""


def _theme_tokens() -> dict[str, dict[str, str]]:
    """从 docs/design-tokens.md 的两段 CSS 取变量，期望色不写死在测试里。"""
    text = TOKENS_DOC.read_text(encoding="utf-8")
    fences = re.findall(r"```css\n(.*?)```", text, flags=re.DOTALL)
    assert len(fences) == 2
    themes: dict[str, dict[str, str]] = {}
    for name, block in zip(("ledger", "studio"), fences, strict=True):
        values: dict[str, str] = {}
        for match in re.finditer(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})", block):
            values[match.group(1)] = match.group(2)
        assert values, name
        themes[name] = values
    return themes


def _insert_job_with_segments(db: Path) -> str:
    """直写任务库 + segments.json：两名说话人交替出现，用于量标签与分色。"""
    conn = sqlite3.connect(db)
    job_id = uuid4().hex
    try:
        conn.execute(
            """
            INSERT INTO jobs (
                id, status, tier, source_name, source_kind, source_path, url,
                created_at, finished_at, error, duration, minutes_status
            ) VALUES (?, 'done', 'fast', ?, 'upload', NULL, NULL, ?, ?, NULL, 12.0, 'done')
            """,
            (
                job_id,
                "对谈样例.wav",
                "2026-04-01T00:00:00.000000Z",
                "2026-04-01T00:00:12.000000Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    job_dir = db.parent / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    segments = [
        {
            "start": 0.0,
            "end": 4.0,
            "speaker": "说话人1",
            "text": "第一段由说话人一来讲，内容足够长，窄屏下也要能自然换行。",
        },
        {"start": 4.0, "end": 8.0, "speaker": "说话人2", "text": "第二段换成说话人二。"},
        {"start": 8.0, "end": 12.0, "speaker": "说话人1", "text": "第三段回到说话人一。"},
    ]
    (job_dir / "segments.json").write_text(
        json.dumps(segments, ensure_ascii=False), encoding="utf-8"
    )
    return job_id


def _inline_stylesheet(html: str) -> str:
    """把外链样式表整段内联，并去掉 app.js：只量样式与结构，不跑播放器逻辑。"""
    css = STYLESHEET.read_text(encoding="utf-8")
    html = html.replace(
        '<link rel="stylesheet" href="/static/style.css">',
        f"<style>\n{css}\n</style>",
    )
    return html.replace('<script src="/static/app.js"></script>', "")


@pytest.mark.parametrize(("width", "height"), [(390, 844), (1280, 800)])
def test_e1_e2_e4_transcript_readings(
    client: tuple[TestClient, Path], width: int, height: int
) -> None:
    """E1（390）与 E4（1280）同套读数：时间列 52px 不被压缩、说话人标签横排不折行、
    E2 高亮只落正文块且时间列无背景覆盖。两档主题都量。"""
    http, data = client
    job_id = _insert_job_with_segments(data / "jobs.db")
    page_html = http.get(f"/jobs/{job_id}/view").text
    assert 'class="seg-time"' in page_html
    assert 'class="seg-body"' in page_html
    tokens = _theme_tokens()

    with chromium_page(color_scheme="light", width=width, height=height) as page:
        page.set_content(_inline_stylesheet(page_html), wait_until="domcontentloaded")
        # 让第二段成为「当前播放段」，复现高亮态后再量。
        page.evaluate(
            "() => document.querySelectorAll('.seg-line')[1].classList.add('now')"
        )
        # .seg-body 有 200ms 背景过渡，等它落定再读，否则量到的是过渡中间色。
        page.wait_for_timeout(350)
        for theme in ("ledger", "studio"):
            page.evaluate(
                "(theme) => document.documentElement.setAttribute('data-theme', theme)",
                theme,
            )
            # 换主题也会触发背景过渡，等它落定再量。
            page.wait_for_timeout(350)
            data_read = page.evaluate(_MEASURE_JS)
            assert isinstance(data_read, dict)
            _assert_readings(data_read, tokens[theme], theme, width)


def _assert_readings(
    data_read: dict[str, Any], tokens: dict[str, str], theme: str, width: int
) -> None:
    where = f"{theme}@{width}px"

    # 视口真的生效（E1/E4 分档的读数是两套，不是同一套跑两次）。
    viewport = data_read["viewport"]
    assert isinstance(viewport, dict)
    assert viewport["width"] == width

    # 两列结构：时间列与正文列同一行、左右分开、列 gap 在 52px 之外另计。
    assert data_read["timeWidth"] == 52, f"{where} 时间列不是 52px：{data_read['timeWidth']}"
    assert data_read["tsWidth"] is not None and int(data_read["tsWidth"]) <= 52, where
    assert data_read["bodyWidth"] > 0, where
    assert abs(int(data_read["timeTop"]) - int(data_read["bodyTop"])) <= 2, where
    # 列 gap 12px 在 52px 之外另计；正文块为给高亮留内边距而左右各外扩 8px，
    # 所以量到的盒间距是 4px —— 判据是「不重叠且仍有间距」，时间列不被挤占。
    assert int(data_read["columnGap"]) >= 4, f"{where} 两列没有分开：{data_read['columnGap']}"

    # 时间列独立、无背景：任何状态（含 .now）都不得被背景覆盖。
    assert same_transparent(str(data_read["timeBackground"])), (
        f"{where} 时间列有背景：{data_read['timeBackground']}"
    )
    assert same_transparent(str(data_read["tsBackground"])), (
        f"{where} 时间戳被背景覆盖：{data_read['tsBackground']}"
    )
    assert same_transparent(str(data_read["tsBackground"])), (
        f"{where} 时间戳被背景覆盖：{data_read['tsBackground']}"
    )
    # hover 态同样不许出现背景覆盖（规格写的是「hover/当前播放任何状态」）。
    # 无头浏览器无法真触发 :hover，改为扫样式表：任何给时间列/时间戳设 background
    # 的 :hover 规则都算覆盖，直接判不合格。
    hover_hits = data_read["hoverBackgrounds"]
    assert isinstance(hover_hits, list)
    offenders = [hit for hit in hover_hits if str(hit.get("bg") or "").strip()]
    assert offenders == [], f"{where} 存在给时间列/时间戳设背景的 hover 规则：{offenders}"
    # 说话人 = 横排小标签：nowrap、单行、宽度不被逐字折行撑成多行。
    assert data_read["labelWhiteSpace"] == "nowrap", where
    assert data_read["labelDisplay"] == "inline-block", where
    line_rects = data_read["labelLineRects"]
    assert isinstance(line_rects, list)
    assert len(line_rects) == 1, f"{where} 说话人标签折成 {len(line_rects)} 行"
    assert data_read["labelLineHeight"] == "20px", where
    assert int(str(data_read["labelHeight"])) == 20, (
        f"{where} 标签高度 {data_read['labelHeight']} 说明发生了折行"
    )

    # 高亮只落右列正文块（浅底 + 圆角），行根与时间列都不留背景。
    assert same_transparent(str(data_read["nowLineBackground"])), (
        f"{where} 行根仍有背景：{data_read['nowLineBackground']}"
    )
    assert same_transparent(str(data_read["nowTimeBackground"])), (
        f"{where} 当前段时间列被高亮覆盖：{data_read['nowTimeBackground']}"
    )
    assert same_color(str(data_read["nowBodyBackground"]), tokens["--data-pale"]), (
        f"{where} 当前段正文块背景不是 --data-pale：{data_read['nowBodyBackground']}"
    )
    assert data_read["nowBodyRadius"] not in {None, "0px"}, where
    assert same_transparent(str(data_read["bodyBackground"])), (
        f"{where} 非当前段正文块不该有底色：{data_read['bodyBackground']}"
    )

    # 窄屏不许溢出（时间列不被压缩的旁证：整页没有横向滚动）。
    assert int(data_read["documentScrollWidth"]) <= int(data_read["documentClientWidth"]), (
        f"{where} 页面横向溢出："
        f"{data_read['documentScrollWidth']} > {data_read['documentClientWidth']}"
    )


def test_e1_speaker_tint_rotates_by_first_appearance(
    client: tuple[TestClient, Path],
) -> None:
    """说话人分色沿用 tokens tint：按首次出现顺序轮转 spk-0..3，同一人同色、不同人不同色。"""
    http, data = client
    job_id = _insert_job_with_segments(data / "jobs.db")
    page_html = http.get(f"/jobs/{job_id}/view").text
    assert 'class="seg-speaker spk-0"' in page_html
    assert 'class="seg-speaker spk-1"' in page_html
    classes = re.findall(r'class="seg-speaker (spk-\d)"', page_html)
    assert classes == ["spk-0", "spk-1", "spk-0"]

    css = STYLESHEET.read_text(encoding="utf-8")
    tokens = _theme_tokens()
    # 每个 tint 档都成对取既有 bg/ink 变量，不新造色板。
    for index, (bg, ink) in enumerate(
        (
            ("--neu-bg", "--neu-ink"),
            ("--pos-bg", "--pos-ink"),
            ("--warn-bg", "--warn-ink"),
            ("--neg-bg", "--neg-ink"),
        )
    ):
        block = re.search(
            rf"\.seg-speaker\.spk-{index} \{{([^}}]*)\}}", css, flags=re.DOTALL
        )
        assert block is not None, f"缺 spk-{index} 分色规则"
        body = block.group(1)
        assert f"background: var({bg})" in body
        assert f"color: var({ink})" in body
        for theme in ("ledger", "studio"):
            assert bg in tokens[theme] and ink in tokens[theme], f"{theme} 缺 {bg}/{ink}"
