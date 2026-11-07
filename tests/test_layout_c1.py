"""C 节入口布局的读数验收：三行结构、等宽首行、无溢出、档位作用域。

判据来源：SPEC-BATCH-20261103 C 节 + CHECKLIST-BATCH-20261103 C1/C2。
这里量的是真浏览器 + 真样式表（把外链样式表内联，不跑 app.js）：
- 390×844 与 1280×800 两档各跑一套读数；
- 溢出按元素级判据（.composer 与 .recent 各自 scrollWidth ≤ clientWidth），
  table.jobs 与 documentElement 的读数一并记录，作并列证据；
- 录音入口显隐走 feature_enabled("recording")，关时整块零 DOM。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright")

from fastapi.testclient import TestClient  # noqa: E402
from verify.browser import chromium_page  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STYLESHEET = ROOT / "app" / "static" / "style.css"

_MEASURE_JS = """
() => {
  const read = (el) => {
    if (!el) {
      return null;
    }
    const rect = el.getBoundingClientRect();
    return {
      left: Math.round(rect.left),
      top: Math.round(rect.top),
      right: Math.round(rect.right),
      bottom: Math.round(rect.bottom),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
      clientWidth: el.clientWidth,
      scrollWidth: el.scrollWidth,
    };
  };
  const blocks = {
    record: document.querySelector("[data-record-entry]"),
    dropzone: document.querySelector("[data-dropzone]"),
    url: document.querySelector(".composer input[name=url]"),
    tier: document.querySelector(".tier-switch"),
    submit: document.querySelector(".composer button[type=submit]"),
  };
  const boxes = {};
  for (const key of Object.keys(blocks)) {
    boxes[key] = read(blocks[key]);
  }
  return {
    viewport: { width: window.innerWidth, height: window.innerHeight },
    composer: read(document.querySelector(".composer")),
    recent: read(document.querySelector(".recent")),
    table: read(document.querySelector("table.jobs")),
    doc: {
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    },
    row1: read(document.querySelector(".composer-row1")),
    boxes,
  };
}
"""


def _inline_stylesheet(html: str) -> str:
    css = STYLESHEET.read_text(encoding="utf-8")
    html = html.replace(
        '<link rel="stylesheet" href="/static/style.css">',
        f"<style>\n{css}\n</style>",
    )
    return html.replace('<script src="/static/app.js"></script>', "")


def _boxes(measured: dict[str, Any]) -> dict[str, Any]:
    boxes = measured.get("boxes")
    assert isinstance(boxes, dict)
    return boxes


def _overlaps(a: dict[str, Any], b: dict[str, Any]) -> bool:
    horizontal = a["left"] < b["right"] and b["left"] < a["right"]
    vertical = a["top"] < b["bottom"] and b["top"] < a["bottom"]
    return horizontal and vertical


@pytest.mark.parametrize(("width", "height"), [(390, 844), (1280, 800)])
def test_c1_composer_three_rows_and_no_overflow(
    client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
    width: int,
    height: int,
) -> None:
    """C1：行1 录音/上传等宽并排、行2 链接整行、行3 档位+开始转写；无重叠、无溢出。"""
    http, _data = client
    monkeypatch.setenv("TRANSCRIBER_FEAT_RECORDING", "1")
    page_html = http.get("/").text
    assert "data-record-entry" in page_html

    with chromium_page(color_scheme="light", width=width, height=height) as page:
        page.set_content(_inline_stylesheet(page_html), wait_until="domcontentloaded")
        measured = page.evaluate(_MEASURE_JS)
    assert isinstance(measured, dict)
    viewport = measured["viewport"]
    assert viewport["width"] == width
    boxes = _boxes(measured)
    for key in ("record", "dropzone", "url", "tier", "submit"):
        assert boxes[key] is not None, f"{width}px 缺少 {key} 块"

    record, dropzone, url, tier, submit = (
        boxes["record"],
        boxes["dropzone"],
        boxes["url"],
        boxes["tier"],
        boxes["submit"],
    )

    # 行1 左右次序固定：录音在左、上传在右。
    assert record["left"] <= dropzone["left"], f"{width}px 行1 左右次序反了"
    if width >= 1280:
        # 桌面档：两块同一行、等宽（等宽判据留 1px 取整余量）。
        assert record["top"] == dropzone["top"], f"{width}px 行1 不在同一行"
        assert abs(record["height"] - dropzone["height"]) <= 1, f"{width}px 行1 不等高"
        assert abs(record["width"] - dropzone["width"]) <= 1, f"{width}px 行1 不等宽"
    else:
        # 手机档：沿用 design-tokens.md「断点 860px 以下单栏」，行1 两块竖排堆叠，
        # 但仍须等宽铺满整行、上下相邻不重叠。
        assert record["top"] < dropzone["top"], f"{width}px 行1 没有堆叠"
        assert record["bottom"] < dropzone["top"], f"{width}px 行1 两块相邻却没让开"
        assert abs(record["width"] - dropzone["width"]) <= 1, f"{width}px 行1 不等宽"
        assert abs(record["width"] - url["width"]) <= 1, f"{width}px 行1 没铺满整行"

    # 行2：链接整行——桌面档比行1 任一块宽；手机档单栏下与行1 同宽（同样铺满）。
    assert url["left"] == record["left"], f"{width}px 链接左边界没对齐"
    if width >= 1280:
        assert url["width"] > record["width"], f"{width}px 链接不是整行"
    assert url["top"] > dropzone["bottom"], f"{width}px 链接不在行1 之下"

    # 行3：档位与开始转写在行2 之下；1280 下并排、390 下换行堆叠。
    assert tier["top"] > url["bottom"], f"{width}px 行3 不在行2 之下"
    assert submit["top"] >= tier["top"], f"{width}px 开始转写跑到档位上方"
    if width >= 1280:
        assert submit["left"] > tier["right"], f"{width}px 行3 没有并排"
    else:
        assert submit["top"] > tier["bottom"], f"{width}px 行3 没有换行堆叠"

    # 四块互不重叠。
    blocks = (record, dropzone, url, tier, submit)
    for index, first in enumerate(blocks):
        for second in blocks[index + 1 :]:
            assert not _overlaps(first, second), f"{width}px 有块重叠"

    # 元素级溢出判据（Q8）：.composer 与 .recent 各自不横向溢出。
    for name in ("composer", "recent"):
        box = measured[name]
        assert box is not None, f"{width}px 缺少 {name}"
        assert box["scrollWidth"] <= box["clientWidth"], (
            f"{width}px {name} 横向溢出 {box['scrollWidth']}>{box['clientWidth']}"
        )
    # 并列读数：documentElement 与 table.jobs 一并记入证据，不作判据。
    doc = measured["doc"]
    assert doc["scrollWidth"] <= doc["clientWidth"], (
        f"{width}px 文档横向溢出 {doc['scrollWidth']}>{doc['clientWidth']}"
    )


def test_c2_tier_controls_only_belong_to_upload_and_link(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2：档位单选只出现在行3 的表单域里，录音入口块不含任何提交字段。"""
    http, _data = client
    monkeypatch.setenv("TRANSCRIBER_FEAT_RECORDING", "1")
    html = http.get("/").text
    start = html.find('class="composer-row3"')
    assert start != -1
    tail = html[start:]
    assert tail.count('name="tier"') == 2
    assert 'id="tier-fast"' in tail
    assert 'id="tier-precise"' in tail
    assert ">开始转写<" in tail

    entry_start = html.find("data-record-entry")
    entry_end = html.find('class="dropzone"', entry_start)
    assert entry_start != -1 and entry_end != -1
    entry = html[entry_start:entry_end]
    for field in ('name="tier"', 'name="url"', 'name="file"'):
        assert field not in entry, f"录音入口块不该有 {field}"
    assert 'href="/record"' in entry


def test_d0_entry_is_rendered_only_when_recording_flag_is_on(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D0 入口侧：开关关闭时整块不输出 DOM；打开后出现链接与安全上下文提示。"""
    http, _data = client
    monkeypatch.delenv("TRANSCRIBER_FEAT_RECORDING", raising=False)
    off = http.get("/").text
    assert "data-record-entry" not in off
    assert "在线录音" not in off
    assert 'href="/record"' not in off

    monkeypatch.setenv("TRANSCRIBER_FEAT_RECORDING", "1")
    on = http.get("/").text
    assert "data-record-entry" in on
    assert "在线录音" in on
    assert 'href="/record"' in on
    assert "当前不是安全上下文（需 HTTPS 访问），录音不可用；其余功能照常。" in on
    # 非安全上下文提示归入口侧（Q3）：入口块自带 isSecureContext 判定。
    entry_start = on.find("data-record-entry")
    entry_end = on.find('class="dropzone"', entry_start)
    entry = on[entry_start:entry_end]
    assert "isSecureContext" in entry
    # 判定逻辑内联在入口块内，不改动 app.js 的表单/轮询段。
    assert "[data-new-form]" not in entry
    assert "[data-live-poll]" not in entry


def test_i1_row1_fills_full_width_when_recording_flag_is_off(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """I1（完工审 r1）：开关关闭时行1 单列整行，上传块与第二行链接行同宽，无空白格。"""
    http, _data = client
    monkeypatch.delenv("TRANSCRIBER_FEAT_RECORDING", raising=False)
    page_html = http.get("/").text
    assert "data-record-entry" not in page_html
    assert "is-dual" not in page_html.split('class="composer-row1"')[1][:40]

    with chromium_page(color_scheme="light", width=1280, height=800) as page:
        page.set_content(_inline_stylesheet(page_html), wait_until="domcontentloaded")
        measured = page.evaluate(_MEASURE_JS)
    assert isinstance(measured, dict)
    boxes = _boxes(measured)
    dropzone, url = boxes["dropzone"], boxes["url"]
    assert boxes["record"] is None, "开关关闭时不该有录音入口"
    assert dropzone is not None and url is not None

    # 上传块与第二行链接行同宽、左边界对齐 —— 行1 没有留半格空白。
    assert abs(dropzone["width"] - url["width"]) <= 1, (
        f"关闭态上传块 {dropzone['width']} 与链接行 {url['width']} 不等宽"
    )
    assert dropzone["left"] == url["left"], "关闭态上传块没与链接行左对齐"
    # 行1 容器宽度 == 上传块宽度：第一行内没有别的格占位。
    row1 = measured["row1"]
    assert row1 is not None
    assert abs(row1["width"] - dropzone["width"]) <= 1, (
        f"行1 容器 {row1['width']} 比上传块 {dropzone['width']} 宽，有空白格"
    )
    # 行2 仍在上传块之下，且整页无横向溢出。
    assert url["top"] > dropzone["bottom"], "关闭态链接行没在上传块之下"
    assert measured["doc"]["scrollWidth"] <= measured["doc"]["clientWidth"]


def test_s3_insecure_context_hides_record_link_and_shows_note(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """S3（完工审 r1）：非安全上下文时提示出现、开始录音不可用；安全上下文反之。"""
    http, _data = client
    monkeypatch.setenv("TRANSCRIBER_FEAT_RECORDING", "1")
    page_html = http.get("/").text
    assert "data-record-entry" in page_html
    inline_html = _inline_stylesheet(page_html)

    probe_js = """
    () => {
      const link = document.querySelector("[data-record-link]");
      const note = document.querySelector("[data-record-note]");
      return {
        secure: window.isSecureContext,
        linkHidden: link ? link.hidden : null,
        noteHidden: note ? note.hidden : null,
        linkClickable: link ? !link.hidden : false,
        noteText: note ? note.textContent.trim() : "",
      };
    }
    """


    def read_at(origin: str) -> dict[str, Any]:
        # isSecureContext 是只读属性，add_init_script 覆盖不生效（实测仍是 false）；
        # 用真实 origin 判定：http:// 非安全上下文，https:// 安全上下文，页面内容同一份。
        with chromium_page(color_scheme="light", width=1280, height=800) as page:
            page.route(
                "**/*",
                lambda route: route.fulfill(
                    status=200, content_type="text/html", body=inline_html
                ),
            )
            page.goto(origin, wait_until="domcontentloaded")
            data_read = page.evaluate(probe_js)
        assert isinstance(data_read, dict)
        return data_read

    # 非安全上下文：链接隐藏（开始录音不可用）、提示出现。
    insecure = read_at("http://example.test/")
    assert insecure["secure"] is False
    assert insecure["linkHidden"] is True, "非安全上下文下开始录音仍可点"
    assert insecure["linkClickable"] is False
    assert insecure["noteHidden"] is False, "非安全上下文下提示没出现"
    assert "当前不是安全上下文" in str(insecure["noteText"])

    # 安全上下文（对照）：链接可用、提示收起。
    secure = read_at("https://example.test/")
    assert secure["secure"] is True
    assert secure["linkHidden"] is False, "安全上下文下开始录音不可点"
    assert secure["linkClickable"] is True
    assert secure["noteHidden"] is True, "安全上下文下仍显示非安全上下文提示"
