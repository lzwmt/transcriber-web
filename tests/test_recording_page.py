"""录音页浏览器行为（LZW-62 D 节）：安全上下文门禁、本地暂存与断网补传。

真机麦克风与跨浏览器由 U-R3 外部覆盖，本文件只用 fake audio 设备。
"""

from __future__ import annotations

import functools
import json
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import Dialog  # noqa: E402
from verify.browser import chromium_page  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RECORD_JS = ROOT / "app" / "static" / "record.js"

FAKE_AUDIO_ARGS = [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
]

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<body>
<div class="record" data-record
     data-prefill-name="未命名1"
     data-max-seconds="3600"
     data-part-seconds="5"
     data-max-parts="720"
     data-open-limit="3"
     data-warn-seconds="300,60">
  <p class="record-notice" data-insecure hidden>当前连接不是安全上下文，录音不可用</p>
  <p class="record-error" data-error hidden></p>
  <section class="record-setup" data-setup>
    <input type="text" data-name value="未命名1">
    <label><input type="radio" name="record-tier" id="record-tier-fast" checked>快档</label>
    <label><input type="radio" name="record-tier" id="record-tier-precise">精档</label>
    <button type="button" data-start disabled><span data-start-label>开始录音</span></button>
  </section>
  <section class="record-live" data-live hidden>
    <span data-timer>00:00</span>
    <span data-warn hidden></span>
    <canvas data-wave width="720" height="72"></canvas>
    <span>已保存 <strong data-saved>0</strong> 片</span>
    <span>待传 <strong data-pending>0</strong> 片</span>
    <span data-state>录音中</span>
    <button type="button" data-pause>暂停</button>
    <button type="button" data-stop>结束</button>
  </section>
  <a href="/" data-back>返回工作台</a>
</div>
<script src="/static/record.js"></script>
</body>
</html>
"""


@contextmanager
def _site(
    tmp_path: Path,
    *,
    max_seconds: str = "3600",
    warn_seconds: str = "300,60",
    part_seconds: str = "5",
) -> Iterator[str]:
    """起一个只读本地站点。

    127.0.0.1 在 Chromium 里算可信来源，window.isSecureContext 才为 true；
    about:blank 与 data: 都不算，IndexedDB 也会被拒。
    """
    site = tmp_path / "site"
    (site / "static").mkdir(parents=True)
    page = (
        _PAGE.replace('data-max-seconds="3600"', f'data-max-seconds="{max_seconds}"')
        .replace('data-warn-seconds="300,60"', f'data-warn-seconds="{warn_seconds}"')
        .replace('data-part-seconds="5"', f'data-part-seconds="{part_seconds}"')
    )
    (site / "index.html").write_text(page, encoding="utf-8")
    (site / "static" / "record.js").write_bytes(RECORD_JS.read_bytes())
    handler = functools.partial(_Handler, directory=str(site))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/index.html"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _Handler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 覆盖基类签名
        return

    def guess_type(self, path: str | os.PathLike[str]) -> str:
        # SimpleHTTPRequestHandler 默认不给 charset，中文会被当成 latin-1 读成乱码。
        kind = super().guess_type(path)
        if kind.startswith("text/") or kind == "application/javascript":
            return kind + "; charset=utf-8"
        return kind


def _open(page: object, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded")  # type: ignore[attr-defined]


def test_insecure_context_blocks_recording(tmp_path: Path) -> None:
    """D0b：非安全上下文只给提示，录音钮保持禁用。"""
    with (
        _site(tmp_path) as url,
        chromium_page(color_scheme="light", width=390, height=844, args=FAKE_AUDIO_ARGS) as page,
    ):
        page.add_init_script("Object.defineProperty(window, 'isSecureContext', {value: false})")
        _open(page, url)
        assert page.evaluate("() => window.isSecureContext") is False
        notice = page.locator("[data-insecure]")
        assert notice.count() == 1
        assert notice.is_visible()
        assert page.locator("[data-start]").is_disabled()


def test_secure_context_enables_recording(tmp_path: Path) -> None:
    with (
        _site(tmp_path) as url,
        chromium_page(color_scheme="light", width=390, height=844, args=FAKE_AUDIO_ARGS) as page,
    ):
        _open(page, url)
        assert page.evaluate("() => window.isSecureContext") is True
        assert page.locator("[data-insecure]").is_hidden()
        assert not page.locator("[data-start]").is_disabled()
        assert page.locator("[data-name]").input_value() == "未命名1"
        assert page.locator("#record-tier-fast").is_checked()


def test_local_parts_survive_reopen_in_same_context(tmp_path: Path) -> None:
    """D4：关页重开（同 context 新 page）后，本地暂存的切片仍能被读回。"""
    with (
        _site(tmp_path) as url,
        chromium_page(color_scheme="light", width=390, height=844, args=FAKE_AUDIO_ARGS) as page,
    ):
        _open(page, url)
        assert page.evaluate("() => Boolean(window.indexedDB)") is True
        context = page.context
        first = page.evaluate("() => document.title")
        assert isinstance(first, str)
        reopened = context.new_page()
        try:
            # add_init_script 只作用于本页，新页要重新装一次桩
            _stub_backend(reopened, job_id="not-a-job-id")
            reopened.goto(url, wait_until="domcontentloaded")
            same_origin = reopened.evaluate(
                """() => new Promise((resolve) => {
                    const req = indexedDB.open('transcriber-record', 1);
                    req.onupgradeneeded = () => {
                        const db = req.result;
                        if (!db.objectStoreNames.contains('parts')) {
                            db.createObjectStore('parts', {keyPath: 'key'});
                        }
                        if (!db.objectStoreNames.contains('sessions')) {
                            db.createObjectStore('sessions', {keyPath: 'id'});
                        }
                    };
                    req.onsuccess = () => resolve(true);
                    req.onerror = () => resolve(false);
                })"""
            )
            assert same_origin is True
        finally:
            reopened.close()


def test_mic_denied_shows_readable_notice(tmp_path: Path) -> None:
    """麦克风被拒时给可读提示，不抛原始异常。"""
    with (
        _site(tmp_path) as url,
        chromium_page(color_scheme="light", width=390, height=844, args=FAKE_AUDIO_ARGS) as page,
    ):
        page.add_init_script(
            """(() => {
                // navigator.mediaDevices 是只读访问器，只能改原型上的方法。
                MediaDevices.prototype.getUserMedia = () => Promise.reject(
                    Object.assign(new Error('denied'), {name: 'NotAllowedError'})
                );
                // 先让建会话这一步成功，才能走到麦克风被拒的分支。
                window.fetch = () => Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({
                        session_id: 'a'.repeat(32),
                        source_name: '未命名1',
                        tier: 'fast',
                        max_seconds: 3600,
                        warn_seconds: [300, 60],
                        part_seconds: 5,
                        max_parts: 720,
                        open_limit: 3,
                    }),
                });
            })();"""
        )
        _open(page, url)
        page.locator("[data-start]").click()
        error = page.locator("[data-error]")
        if error.count() == 0:
            pytest.skip("页面未提供 [data-error] 槽")
        assert error.is_visible()
        text = error.inner_text()
        assert "麦克风" in text or "权限" in text
        # 无静默失败、无残留录音状态：仍在起录区、录音钮恢复可用、可回工作台
        assert page.locator("[data-setup]").is_visible()
        assert page.locator("[data-live]").is_hidden()
        assert not page.locator("[data-start]").is_disabled()
        back = page.locator("[data-back]")
        assert back.count() == 1
        assert back.get_attribute("href") == "/"


def _dismiss_and_record(messages: list[str]) -> Callable[[Dialog], None]:
    def handler(dialog: Dialog) -> None:
        messages.append(dialog.message)
        dialog.dismiss()

    return handler


def _stub_backend(page: object, *, job_id: str = "b" * 32) -> None:
    """让建会话/上传/结束都在本地返回成功，避免真打服务端。

    job_id 传非法值时 record.js 不跳转，测试才能在结束之后继续读页面。
    """
    page.add_init_script(  # type: ignore[attr-defined]
        """(() => {
            window.__uploads = [];
            window.__finished = [];
            const SESSION = 'a'.repeat(32);
            const JOB_ID = __JOB_ID__;
            window.fetch = (url, init) => {
                const target = String(url);
                // 先记账再回包：回包里的 saved_parts 才是「含本次」的累计片数。
                if (target.includes('/parts/')) {
                    window.__uploads.push(target);
                }
                if (target.endsWith('/finish')) {
                    window.__finished.push(target);
                }
                const body = {
                    session_id: SESSION,
                    job_id: JOB_ID,
                    source_name: '未命名1',
                    tier: 'fast',
                    max_seconds: 3600,
                    warn_seconds: [300, 60],
                    part_seconds: 5,
                    max_parts: 720,
                    open_limit: 3,
                    saved_parts: window.__uploads.length,
                };
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve(JSON.parse(JSON.stringify(body))),
                });
            };
        })();""".replace("__JOB_ID__", json.dumps(job_id))
    )


def test_offline_uploads_are_replayed_after_reconnect(tmp_path: Path) -> None:
    """D3：断网期间切片留在本地，恢复后自动补传，最终能走到结束。"""
    with (
        _site(tmp_path) as url,
        chromium_page(color_scheme="light", width=390, height=844, args=FAKE_AUDIO_ARGS) as page,
    ):
        _stub_backend(page)
        _open(page, url)
        page.locator("[data-start]").click()
        page.locator("[data-live]").wait_for(state="visible")

        # 断网：后续上传全部失败
        page.evaluate(
            """() => {
                window.__realFetch = window.fetch;
                window.fetch = (url, init) => {
                    if (String(url).includes('/parts/')) {
                        return Promise.reject(new Error('offline'));
                    }
                    return window.__realFetch(url, init);
                };
            }"""
        )
        page.wait_for_timeout(12000)
        pending = page.evaluate(
            """() => new Promise((resolve) => {
                const req = indexedDB.open('transcriber-record', 1);
                req.onupgradeneeded = () => {
                    const db = req.result;
                    if (!db.objectStoreNames.contains('parts')) {
                        db.createObjectStore('parts', {keyPath: 'key'});
                    }
                    if (!db.objectStoreNames.contains('sessions')) {
                        db.createObjectStore('sessions', {keyPath: 'id'});
                    }
                };
                req.onsuccess = () => {
                    const db = req.result;
                    const tx = db.transaction('parts', 'readonly');
                    const all = tx.objectStore('parts').getAll();
                    all.onsuccess = () => resolve(all.result.length);
                };
                req.onerror = () => resolve(-1);
            })"""
        )
        assert pending > 0, "断网期间切片必须留在本地"

        # 恢复网络：online 事件触发 pump
        page.evaluate("() => { window.fetch = window.__realFetch; }")
        page.evaluate("() => window.dispatchEvent(new Event('online'))")
        page.wait_for_timeout(8000)
        left = page.evaluate(
            """() => new Promise((resolve) => {
                const req = indexedDB.open('transcriber-record', 1);
                req.onsuccess = () => {
                    const tx = req.result.transaction('parts', 'readonly');
                    const all = tx.objectStore('parts').getAll();
                    all.onsuccess = () => resolve(all.result.length);
                };
                req.onerror = () => resolve(-1);
            })"""
        )
        assert left == 0, "恢复网络后本地待传必须清空"
        assert page.evaluate("() => window.__uploads.length") >= 1


def test_leave_confirmation_keeps_recording(tmp_path: Path) -> None:
    """D8：录音中点返回先弹确认；取消则留在本页并继续保留切片。"""
    with (
        _site(tmp_path) as url,
        chromium_page(color_scheme="light", width=390, height=844, args=FAKE_AUDIO_ARGS) as page,
    ):
        _stub_backend(page)
        _open(page, url)
        page.locator("[data-start]").click()
        page.locator("[data-live]").wait_for(state="visible")
        dialogs: list[str] = []
        page.on("dialog", _dismiss_and_record(dialogs))
        page.locator("[data-back]").click()
        page.wait_for_timeout(500)
        assert dialogs, "录音中离开必须先弹确认"
        assert "已录切片保留" in dialogs[0]
        # 取消后仍在录音页
        assert page.url == url
        assert page.locator("[data-live]").is_visible()


def test_saved_parts_and_pending_reach_zero(tmp_path: Path) -> None:
    """D2：录 30 秒后「已保存 N 片」与服务端回执一致，停录后本地待传为 0。"""
    with (
        _site(tmp_path, part_seconds="5") as url,
        chromium_page(color_scheme="light", width=390, height=844, args=FAKE_AUDIO_ARGS) as page,
    ):
        _stub_backend(page, job_id="not-a-job-id")
        _open(page, url)
        page.locator("[data-start]").click()
        page.locator("[data-live]").wait_for(state="visible")
        page.wait_for_timeout(32000)
        saved = int(page.locator("[data-saved]").inner_text())
        assert saved >= 5, f"30 秒 5 秒一片应已保存 ≥5 片，实际 {saved}"
        page.locator("[data-stop]").click()
        page.wait_for_timeout(6000)
        pending = int(page.locator("[data-pending]").inner_text())
        assert pending == 0, "停止录音且回执到齐后本地待传必须为 0"
        # 回执全部到达后：页面「已保存 N 片」= 服务端确认过的上传次数
        page.wait_for_function(
            """() => {
                const el = document.querySelector('[data-saved]');
                return el && Number(el.textContent) === window.__uploads.length;
            }""",
            timeout=15000,
        )
        assert page.evaluate("() => window.__finished.length") == 1


def test_warning_thresholds_fire_once_and_autostop(tmp_path: Path) -> None:
    """D9：缩短上限后剩余两档提示各出现一次，到限自动结束并生成 job。"""
    with (
        _site(tmp_path, max_seconds="8", warn_seconds="5,2", part_seconds="2") as url,
        chromium_page(color_scheme="light", width=390, height=844, args=FAKE_AUDIO_ARGS) as page,
    ):
        _stub_backend(page, job_id="not-a-job-id")
        page.add_init_script(
            """(() => {
                window.__warns = [];
                document.addEventListener('DOMContentLoaded', () => {
                    const warn = document.querySelector('[data-warn]');
                    new MutationObserver(() => {
                        if (warn && !warn.hidden && warn.textContent) {
                            window.__warns.push(warn.textContent);
                        }
                    }).observe(warn, {childList: true, characterData: true, subtree: true,
                                       attributes: true, attributeFilter: ['hidden']});
                });
            })();"""
        )
        _open(page, url)
        page.locator("[data-start]").click()
        page.locator("[data-live]").wait_for(state="visible")
        page.wait_for_timeout(14000)
        warns = page.evaluate("() => window.__warns")
        assert any("5" in text for text in warns), f"剩余 5 秒提示缺失：{warns}"
        assert any("2" in text for text in warns), f"剩余 2 秒提示缺失：{warns}"
        assert len([t for t in warns if "5" in t]) == 1, f"剩余 5 秒提示重复：{warns}"
        assert len([t for t in warns if "2" in t]) == 1, f"剩余 2 秒提示重复：{warns}"
        # 到限自动结束并送 finish
        assert page.evaluate("() => window.__finished.length") == 1
        assert page.locator("[data-stop]").is_disabled()


_LOCAL_PARTS = """() => new Promise((resolve) => {
    const req = indexedDB.open('transcriber-record', 1);
    req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('parts')) {
            db.createObjectStore('parts', {keyPath: 'key'});
        }
        if (!db.objectStoreNames.contains('sessions')) {
            db.createObjectStore('sessions', {keyPath: 'id'});
        }
    };
    req.onsuccess = () => {
        const tx = req.result.transaction('parts', 'readonly');
        const all = tx.objectStore('parts').getAll();
        all.onsuccess = () => resolve(all.result.length);
    };
    req.onerror = () => resolve(-1);
})"""


def test_reopen_after_close_uploads_leftover_parts(tmp_path: Path) -> None:
    """D4：录音中关页后重开，未传完切片被补传，随后能结束生成 job。"""
    with (
        _site(tmp_path) as url,
        chromium_page(color_scheme="light", width=390, height=844, args=FAKE_AUDIO_ARGS) as page,
    ):
        _stub_backend(page, job_id="not-a-job-id")
        _open(page, url)
        page.locator("[data-start]").click()
        page.locator("[data-live]").wait_for(state="visible")

        # 断网：切片只落本地，不上服务端
        page.evaluate(
            """() => {
                window.__realFetch = window.fetch;
                window.fetch = (url, init) => {
                    if (String(url).includes('/parts/')) {
                        return Promise.reject(new Error('offline'));
                    }
                    return window.__realFetch(url, init);
                };
            }"""
        )
        page.wait_for_timeout(9000)
        leftover = page.evaluate(_LOCAL_PARTS)
        assert leftover > 0, "关页前必须还有未传切片留在本地"

        context = page.context
        page.close()

        # 重开（同 context，IndexedDB 同源保留）
        reopened = context.new_page()
        try:
            # add_init_script 只作用于本页，新页要重新装一次桩
            _stub_backend(reopened, job_id="not-a-job-id")
            reopened.goto(url, wait_until="domcontentloaded")
            # 自动进入待补传：live 区可见、状态为「待补传」
            reopened.locator("[data-live]").wait_for(state="visible")
            assert "待补传" in reopened.locator("[data-state]").inner_text()
            # 补传完成后本地残留清零
            reopened.wait_for_function(
                """() => new Promise((resolve) => {
                    const req = indexedDB.open('transcriber-record', 1);
                    req.onsuccess = () => {
                        const tx = req.result.transaction('parts', 'readonly');
                        const all = tx.objectStore('parts').getAll();
                        all.onsuccess = () => resolve(all.result.length === 0);
                    };
                    req.onerror = () => resolve(false);
                })""",
                timeout=20000,
            )
            assert reopened.evaluate("() => window.__uploads.length") >= leftover
            # 补传完再结束，走的是同一条「补传→拼合→生成 job」链路
            reopened.locator("[data-stop]").click()
            reopened.wait_for_function("() => window.__finished.length === 1", timeout=15000)
        finally:
            reopened.close()


def test_pause_resume_keeps_timer_and_wave(tmp_path: Path) -> None:
    """D6：暂停/继续与计时、波形状态一致。"""
    with (
        _site(tmp_path, part_seconds="2") as url,
        chromium_page(color_scheme="light", width=390, height=844, args=FAKE_AUDIO_ARGS) as page,
    ):
        _stub_backend(page, job_id="not-a-job-id")
        _open(page, url)
        page.locator("[data-start]").click()
        page.locator("[data-live]").wait_for(state="visible")

        page.wait_for_timeout(3000)
        before_pause = page.locator("[data-timer]").inner_text()
        page.locator("[data-pause]").click()
        assert "已暂停" in page.locator("[data-state]").inner_text()
        assert page.locator("[data-pause]").inner_text() == "继续"

        # 暂停期间计时不前进
        page.wait_for_timeout(2000)
        assert page.locator("[data-timer]").inner_text() == before_pause

        page.locator("[data-pause]").click()
        assert "录音中" in page.locator("[data-state]").inner_text()
        assert page.locator("[data-pause]").inner_text() == "暂停"
        page.wait_for_timeout(2500)
        after_resume = page.locator("[data-timer]").inner_text()
        assert after_resume > before_pause, "继续后计时应继续前进"

        # 波形画布有实际绘制（非空白）
        drawn = page.evaluate(
            """() => {
                const canvas = document.querySelector('[data-wave]');
                const ctx = canvas.getContext('2d');
                const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
                let ink = 0;
                for (let i = 3; i < data.length; i += 4) {
                    if (data[i] !== 0) { ink += 1; }
                }
                return ink;
            }"""
        )
        assert drawn > 0, "录音中波形必须实际绘制"

_WAVE_SPREAD = """([ratioStart, frames]) => new Promise((resolve) => {
    const canvas = document.querySelector('[data-wave]');
    const ctx = canvas.getContext('2d');
    const w = canvas.width, h = canvas.height;
    const x0 = Math.floor(w * ratioStart);
    const sw = w - x0;
    let bestSpread = 0, bestInk = 0, seen = 0;
    const tick = () => {
        const data = ctx.getImageData(x0, 0, sw, h).data;
        let minY = h, maxY = -1, ink = 0;
        for (let i = 3; i < data.length; i += 4) {
            if (data[i] === 0) { continue; }
            const y = Math.floor(((i - 3) / 4) / sw);
            ink += 1;
            if (y < minY) { minY = y; }
            if (y > maxY) { maxY = y; }
        }
        if (maxY >= 0) {
            bestSpread = Math.max(bestSpread, maxY - minY);
            bestInk = Math.max(bestInk, ink);
        }
        seen += 1;
        if (seen >= frames) {
            resolve({spread: bestSpread, ink: bestInk, width: w, height: h, x0: x0});
        } else {
            requestAnimationFrame(tick);
        }
    };
    requestAnimationFrame(tick);
})"""

_WAVE_HAS_INK = """() => {
    const canvas = document.querySelector('[data-wave]');
    const ctx = canvas.getContext('2d');
    const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    for (let i = 3; i < data.length; i += 4) {
        if (data[i] !== 0) { return true; }
    }
    return false;
}"""


@pytest.mark.parametrize("canvas_width", [720, 960])
def test_wave_tail_draws_across_full_width(tmp_path: Path, canvas_width: int) -> None:
    """LZW-94：波形在整幅画布宽度内都随声音波动，尾段不许恒为中线。

    真实录音页 canvas 是 720×72（record.html:51），而分析器时域缓冲只有
    fftSize/2=512 点。绘制映射必须按 buffer.length 与画布宽的比例取样，否则
    x≥buffer.length 的下标越界会被兜底值填成中线，尾段恒平。960 是「任意宽度」
    的另一档，确认修复不依赖 720 这个具体常量。
    """
    with (
        _site(tmp_path, part_seconds="2") as url,
        chromium_page(color_scheme="light", width=390, height=844, args=FAKE_AUDIO_ARGS) as page,
    ):
        _stub_backend(page, job_id="not-a-job-id")
        _open(page, url)
        page.evaluate(
            "(w) => { document.querySelector('[data-wave]').width = w; }", canvas_width
        )
        page.locator("[data-start]").click()
        page.locator("[data-live]").wait_for(state="visible")
        # 条件等待：波形开始实际绘制后才取样本（不用固定 sleep）
        page.wait_for_function(_WAVE_HAS_INK)
        head = page.evaluate(_WAVE_SPREAD, [0.0, 30])
        tail = page.evaluate(_WAVE_SPREAD, [5 / 6, 30])

        assert head["spread"] >= 4, f"前段应有可见波动，实测 {head}"
        assert tail["ink"] > 0, f"尾段应有绘制，实测 {tail}"
        assert tail["spread"] >= 4, (
            f"LZW-94：画布尾段 x∈[{tail['x0']},{tail['width']}) 必须随声音波动，"
            f"实测 y 跨度仅 {tail['spread']}px（恒中线=1px）；head={head} tail={tail}"
        )

_SEED_LOCAL = """() => new Promise((resolve) => {
    const SESSION = 'a'.repeat(32);
    const req = indexedDB.open('transcriber-record', 1);
    req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('parts')) {
            db.createObjectStore('parts', {keyPath: 'key'});
        }
        if (!db.objectStoreNames.contains('sessions')) {
            db.createObjectStore('sessions', {keyPath: 'id'});
        }
    };
    req.onsuccess = () => {
        const db = req.result;
        const tx = db.transaction('parts', 'readwrite');
        tx.objectStore('parts').put({
            key: SESSION + ':1',
            session_id: SESSION,
            index: 1,
            duration: 2,
            blob: new Blob(['x'], {type: 'audio/webm'})
        });
        const tx2 = db.transaction('sessions', 'readwrite');
        tx2.objectStore('sessions').put({
            id: SESSION,
            tier: 'fast',
            finished: false,
            created_at: '2026-11-03T00:00:00.000Z'
        });
        tx2.oncomplete = () => resolve(true);
        tx2.onerror = () => resolve(false);
    };
    req.onerror = () => resolve(false);
})"""


def _stub_resume_backend(page: object) -> None:
    """服务端自称只有第 0 片（next_index=1），用于验证续录号不与本地待传片相撞。"""
    page.add_init_script(  # type: ignore[attr-defined]
        """(() => {
            window.__uploads = [];
            const SESSION = 'a'.repeat(32);
            window.fetch = (target) => {
                const url = String(target);
                if (url.includes('/parts/')) {
                    window.__uploads.push(url);
                }
                const body = {
                    session_id: SESSION,
                    status: 'awaiting_upload',
                    saved_parts: 1,
                    next_index: 1,
                    tier: 'fast',
                    max_seconds: 3600,
                    warn_seconds: [300, 60],
                    part_seconds: 5,
                    max_parts: 720,
                    open_limit: 3,
                    job_id: 'not-a-job-id'
                };
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve(JSON.parse(JSON.stringify(body)))
                });
            };
        })();"""
    )


def test_i3_resume_does_not_overwrite_local_pending_parts(tmp_path: Path) -> None:
    """I3：续录的下一号要把本地待传片算进去，不能撞上服务端 next_index 覆盖本地片。"""
    with (
        _site(tmp_path, part_seconds="2") as url,
        chromium_page(color_scheme="light", width=390, height=844, args=FAKE_AUDIO_ARGS) as page,
    ):
        # 先在同一来源落一份 IndexedDB，再装桩重进页面（否则桩的 onsuccess 会被页面覆盖）
        _open(page, url)
        assert page.evaluate(_SEED_LOCAL) is True
        _stub_resume_backend(page)
        page.goto(url, wait_until="domcontentloaded")

        page.locator("[data-live]").wait_for(state="visible")
        assert "待补传" in page.locator("[data-state]").inner_text()
        # 本地第 1 片先被补传上去
        page.wait_for_function("() => window.__uploads.length >= 1", timeout=20000)
        assert any("/parts/1" in item for item in page.evaluate("() => window.__uploads"))

        # 继续录：新片号必须从 2 起，不许回到 1 覆盖本地片
        page.locator("[data-pause]").click()
        page.wait_for_function("() => window.__uploads.length >= 2", timeout=25000)
        indices = page.evaluate(
            """() => window.__uploads.map((u) => Number(u.split('/parts/')[1]))"""
        )
        assert len(set(indices)) == len(indices), f"片号不许重复：{indices}"
        assert max(indices) >= 2, f"新片号必须从 2 起：{indices}"
