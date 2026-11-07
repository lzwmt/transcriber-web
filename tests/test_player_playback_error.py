"""详情页播放器失败处理：终局才定罪、定了也可重试（非销毁式）。

本文件按 SPEC-PLAYER-FIX.md 的新规格改写，锁死以下行为：

- 失败呈现是非销毁式的：播放器容器、audio 元素与播放按钮一律保留，只在播放器内追加
  一条 ``[data-player-failure]`` 提示与 ``[data-player-retry]`` 重试按钮；旧的
  ``player-empty`` 占位态与清空子节点的做法在失败路径上不再出现。
- 只有终局证据才呈现失败：全部 source 已失败且判定时刻仍无可用源（audio.error 为
  MEDIA_ERR_ABORTED 时不定罪），或 play() 被非瞬时名义（NotSupportedError）拒绝。
- 瞬时信号一律不定罪：单个 source 的 error、audio 自身单个 error、初始化读到的中间值、
  AbortError（play 承诺被打断与 MEDIA_ERR_ABORTED 两形态）、NotAllowedError（无手势）、
  瞬断后可达。
- 失败可恢复：点重试重新加载后可播即清除；canplay/playing 到达也自动清除，不留闩锁。

负例走真实加载时序（清单 P3）：页面与真实详情页一样由 ``<script src="/static/app.js">``
加载脚本（不是事后注入），次源为仓内真实 wav 经路由拦截提供的真实 HTTP 抓取（不用
data URI），AbortError 由真实 load() 打断产生，NotAllowedError 由无用户手势的页面脚本
点击在真实自动播放策略下产生，竞速负例由路由延迟 ≥1 秒的真实慢失败构造。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import Page, Route  # noqa: E402
from verify.browser import chromium_page  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "app" / "static" / "app.js"
WAV_FIXTURE = ROOT / "tests" / "fixtures" / "s2_60.wav"
FAILURE_COPY = "音频文件不存在或格式不可用"
BASE = "http://player.test"

_PLAYER_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<body>
<div class="card player" id="player" data-audio-player data-duration="60">
  <audio id="audio" preload="{preload}"{src_attr}>{sources}</audio>
  <div class="p-top">
    <button class="play" id="play" aria-label="播放" type="button">播放</button>
    <button class="skip" id="skip-back" type="button" data-skip="-15">后退</button>
    <button class="skip" id="skip-fwd" type="button" data-skip="15">前进</button>
    <div class="time num" id="player-time">
      <span class="curr">0:00</span> <span class="total">/ 1:00</span>
    </div>
  </div>
  <div class="track" id="player-track">
    <div class="wave" id="wave"></div>
    <div class="head" id="head"></div>
  </div>
</div>
<p class="seg-line" data-start="0" data-end="30"><button class="ts" type="button">0:00</button></p>
<p class="seg-line" data-start="30" data-end="60"><button class="ts" type="button">0:30</button></p>
<script src="/static/app.js"></script>
{tail}
</body>
</html>
"""


def _serve_wav(route: Route, data: bytes) -> None:
    rng = route.request.headers.get("range")
    match = re.match(r"bytes=(\d+)-(\d*)", rng) if rng else None
    if match is None:
        route.fulfill(
            status=200,
            headers={"Content-Type": "audio/wav", "Accept-Ranges": "bytes"},
            body=data,
        )
        return
    start = int(match.group(1))
    raw_end = match.group(2)
    end = int(raw_end) if raw_end else len(data) - 1
    end = min(end, len(data) - 1)
    route.fulfill(
        status=206,
        headers={
            "Content-Type": "audio/wav",
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{len(data)}",
        },
        body=data[start : end + 1],
    )


class Routes:
    """路由表：真实 wav 走 Range 协商；失败源按状态返回 404，或挂起由 release 放行。"""

    def __init__(
        self,
        *,
        sources: list[str],
        preload: str = "metadata",
        hold: tuple[str, ...] = (),
        failing: tuple[str, ...] = (),
        tail: str = "",
        src_attr: bool = False,
    ) -> None:
        self.wav = WAV_FIXTURE.read_bytes()
        self.js = APP_JS.read_bytes()
        self.sources = sources
        self.preload = preload
        self.hold = set(hold)
        self.failing = set(failing)
        self.held: dict[str, Route] = {}
        self.served: list[str] = []
        self.tail = tail
        self.src_attr = src_attr

    def install(self, page: Page) -> None:
        page.route("**/*", self.handle)

    def handle(self, route: Route) -> None:
        path = re.sub(r"^http://[^/]+", "", route.request.url).split("?")[0]
        self.served.append(path)
        if path == "/view":
            route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=_PLAYER_HTML.format(
                    preload=self.preload,
                    src_attr=(f' src="{self.sources[0]}"' if self.src_attr else ""),
                    sources=(
                        ""
                        if self.src_attr
                        else "".join(f'<source src="{src}">' for src in self.sources)
                    ),
                    tail=self.tail,
                ),
            )
        elif path == "/static/app.js":
            route.fulfill(
                status=200,
                content_type="application/javascript; charset=utf-8",
                body=self.js,
            )
        elif path in self.hold:
            self.held[path] = route
        elif path in self.failing:
            route.fulfill(status=404, body="gone")
        elif path.startswith("/wav/") or path.startswith("/rec/") or path.startswith("/auto/"):
            # 恢复型路径：不在 failing 集合内即可播（真实 wav 经 Range 协商）。
            _serve_wav(route, self.wav)
        elif path.startswith("/bad/"):
            route.fulfill(status=200, content_type="audio/mpeg", body=b"not audio")
        else:
            route.fulfill(status=404, body="gone")

    def release(self, path: str, *, ok: bool = False) -> None:
        """放行挂起请求：ok=True 时改为真实可播 wav，否则以 404 失败。"""
        held = self.held.pop(path, None)
        if held is None:
            return
        if ok:
            _serve_wav(held, self.wav)
        else:
            held.fulfill(status=404, body="gone")


def _open(page: Page, routes: Routes) -> None:
    routes.install(page)
    page.goto(f"{BASE}/view", wait_until="domcontentloaded")
    assert page.locator("#audio").count() == 1


def _failure_count(page: Page) -> int:
    return page.locator("[data-player-failure]").count()


def _assert_no_failure(page: Page) -> None:
    assert _failure_count(page) == 0


def _assert_player_intact(page: Page) -> None:
    assert page.locator("#player").count() == 1
    assert page.locator("#player.player").count() == 1
    assert page.locator("#player.player-empty").count() == 0
    assert page.locator("#audio").count() == 1
    assert page.locator("#play").count() == 1
    # 子节点未被清空：播放控件与波形仍在。
    assert page.locator("#player .p-top").count() == 1
    assert page.locator("#player .wave").count() == 1


def _assert_failure_shown(page: Page) -> None:
    note = page.locator("[data-player-failure]")
    assert note.count() == 1
    assert FAILURE_COPY in note.inner_text()
    assert page.locator("[data-player-retry]").count() == 1
    _assert_player_intact(page)


def _assert_playable(page: Page) -> None:
    """断言音频可正常播放：已在播则直接判定，未播则点播放按钮后判定。"""
    if page.evaluate("() => !document.getElementById('audio').paused") is not True:
        page.click("#play")
        page.wait_for_timeout(400)
    assert page.evaluate("() => !document.getElementById('audio').paused") is True

def _record_play_rejections(page: Page) -> None:
    """记录 play() 的真实拒绝名（包装只为观察，拒绝仍由浏览器原生产生）。"""
    page.evaluate(
        """() => {
          const audio = document.getElementById('audio');
          window.__rejections = [];
          const native = audio.play.bind(audio);
          audio.play = function () {
            const promise = native();
            promise.catch(function (err) { window.__rejections.push(err.name); });
            return promise;
          };
        }"""
    )


def test_player_failure_path_has_no_destructive_code() -> None:
    """P1 静态核查：销毁逻辑与 init 同步定罪入口已从 app.js 移除。

    双证之一：本用例证明代码里不再有清空播放器子节点、移除其标识、换占位类的做法，
    audio 的 error 监听也不直达失败呈现；另一证为 P3③ 竞速负例的窗口内无呈现断言。
    """
    js = APP_JS.read_text(encoding="utf-8")
    assert "player-empty" not in js
    assert "while (player.firstChild)" not in js
    assert 'player.removeAttribute("id")' not in js
    assert "showPlaybackFailure" not in js
    # init 路径不得同步读取 audio.error / networkState 定罪。
    assert "audio.error || audio.networkState === 3" not in js
    # audio 的 error 监听必须经终局复核，不许直达呈现。
    assert 'audio.addEventListener("error", renderPlaybackFailure)' not in js
    assert "hasTerminalAudioFailure" in js
    assert "err.code !== 1" in js
    # 非销毁式呈现所需的标识与重试入口存在。
    assert "data-player-failure" in js
    assert "data-player-retry" in js


def test_single_audio_error_does_not_show_failure() -> None:
    """P1/P2 负例：audio 自身的单个 error 事件不呈现失败（旧行为已改写）。"""
    with chromium_page(color_scheme="light", width=390, height=844) as page:
        routes = Routes(sources=["/wav/ok.wav"])
        _open(page, routes)
        page.wait_for_function(
            "() => document.getElementById('audio').readyState >= 2", timeout=15000
        )
        page.evaluate(
            "() => document.getElementById('audio').dispatchEvent(new Event('error'))"
        )
        page.wait_for_timeout(300)
        _assert_no_failure(page)
        _assert_player_intact(page)
        _assert_playable(page)


def test_both_sources_fail_shows_retryable_failure() -> None:
    """P2 正例：双 source 真实失败、协商终局后出现可重试失败提示且不销毁播放器。"""
    with chromium_page(color_scheme="light", width=390, height=844) as page:
        routes = Routes(sources=["/miss/a.mp3", "/miss/b.mp3"])
        _open(page, routes)
        page.wait_for_selector("[data-player-failure]", timeout=15000)
        _assert_failure_shown(page)
        assert page.locator(".player-empty").count() == 0




def test_fallback_to_real_wav_does_not_show_failure() -> None:
    """P3① 回落负例：首源真实失败，次源为路由提供的真实 wav，无失败呈现且可播放。"""
    with chromium_page(color_scheme="light", width=390, height=844) as page:
        routes = Routes(sources=["/miss/a.mp3", "/wav/fallback.wav"])
        _open(page, routes)
        page.wait_for_function(
            "() => document.getElementById('audio').readyState >= 2", timeout=15000
        )
        _assert_no_failure(page)
        _assert_player_intact(page)
        _assert_playable(page)
        assert any(p.startswith("/wav/") for p in routes.served)


def test_play_not_supported_shows_retryable_failure() -> None:
    """P2 正例：play() 以真实 NotSupportedError 拒绝时进入可重试呈现而非销毁。

    构造说明：Chromium 只在媒体资源本身不可解码时才让 play() 以 NotSupportedError
    拒绝，而同一资源在加载阶段也会置 audio.error（MEDIA_ERR_SRC_NOT_SUPPORTED），
    因此这里不声称「加载阶段静默」，只锁 play() 的真实拒绝名与呈现结果——可重试、
    非销毁；瞬时名义的拒绝分流见 P3 四条负例。
    """
    with chromium_page(color_scheme="light", width=390, height=844) as page:
        routes = Routes(sources=["/bad/x.mp3"], src_attr=True)
        _open(page, routes)
        _record_play_rejections(page)
        page.click("#play")
        page.wait_for_selector("[data-player-failure]", timeout=15000)
        page.wait_for_timeout(300)
        _assert_failure_shown(page)
        assert "NotSupportedError" in page.evaluate("() => window.__rejections")
        assert page.locator(".player-empty").count() == 0

def test_abort_during_play_does_not_show_failure() -> None:
    """P3② 瞬断负例：真实 load() 打断在途播放，原生抛 AbortError，不呈现失败。

    构造：媒体请求经路由挂起（协商仍打开），点击播放使 play() 承诺在途；随后 load()
    真实打断，浏览器原生以 AbortError 拒绝（不是 mock Promise.reject）。打断后放行
    请求改为真实可播 wav，加载完成后再点可正常播放，全程无失败呈现。
    """
    with chromium_page(color_scheme="light", width=390, height=844) as page:
        routes = Routes(sources=["/slow/w.wav"], preload="none", hold=("/slow/w.wav",))
        _open(page, routes)
        _record_play_rejections(page)
        page.wait_for_timeout(200)
        page.click("#play")
        page.wait_for_timeout(300)
        # 真实打断：load() 使在途 play() 承诺以 AbortError 拒绝。
        page.evaluate("() => document.getElementById('audio').load()")
        page.wait_for_timeout(400)
        assert "AbortError" in page.evaluate("() => window.__rejections")
        _assert_no_failure(page)
        _assert_player_intact(page)
        # 放行为真实可播 wav：瞬断后可达，全程不许留失败呈现。
        routes.release("/slow/w.wav", ok=True)
        page.wait_for_function(
            "() => document.getElementById('audio').readyState >= 2", timeout=15000
        )
        page.wait_for_timeout(400)
        _assert_no_failure(page)
        _assert_playable(page)

def test_slow_first_source_window_has_no_failure() -> None:
    """P3③ 竞速负例：首源延迟 ≥1 秒才失败，窗口内不呈现；窗口后回落成功。"""
    with chromium_page(color_scheme="light", width=390, height=844) as page:
        routes = Routes(
            sources=["/slow/s.mp3", "/wav/backup.wav"], hold=("/slow/s.mp3",)
        )
        _open(page, routes)
        page.wait_for_timeout(1100)
        # 协商仍打开（首源尚未失败）：窗口内不许出现失败呈现。
        _assert_no_failure(page)
        _assert_player_intact(page)
        routes.release("/slow/s.mp3")
        page.wait_for_function(
            "() => document.getElementById('audio').readyState >= 2", timeout=15000
        )
        # 窗口后期望写死为回落成功：无失败呈现、终态可播放。
        _assert_no_failure(page)
        _assert_playable(page)


def test_not_allowed_error_is_ignored() -> None:
    """P3④ 无手势负例：真实自动播放策略拒绝 NotAllowedError，不呈现也不留标记。"""
    with chromium_page(color_scheme="light", width=390, height=844) as page:
        routes = Routes(
            sources=["/wav/ok.wav"],
            tail=(
                "<script>window.addEventListener('load', function () {"
                "document.getElementById('play').click();});</script>"
            ),
        )
        _open(page, routes)
        page.wait_for_function(
            "() => document.getElementById('audio').readyState >= 2", timeout=15000
        )
        page.wait_for_timeout(400)
        _assert_no_failure(page)
        _assert_player_intact(page)
        # 之后用户手势点击可正常播放。
        _assert_playable(page)


def test_retry_reloads_and_clears_failure() -> None:
    """P4① 可恢复：真实双源失败后点重试，恢复可播即清除提示与内部标记。"""
    with chromium_page(color_scheme="light", width=390, height=844) as page:
        routes = Routes(
            sources=["/rec/x.mp3", "/rec/x.mp3"], failing=("/rec/x.mp3",)
        )
        _open(page, routes)
        page.wait_for_selector("[data-player-failure]", timeout=15000)
        _assert_failure_shown(page)
        routes.failing.discard("/rec/x.mp3")
        page.click("[data-player-retry]")
        page.wait_for_function(
            "() => document.getElementById('audio').readyState >= 2", timeout=15000
        )
        page.wait_for_timeout(400)
        _assert_no_failure(page)
        _assert_player_intact(page)
        _assert_playable(page)


def test_canplay_auto_clears_failure() -> None:
    """P4② 自动清除：恢复后不点重试，仅让加载成功，呈现与标记也必须自动清除。"""
    with chromium_page(color_scheme="light", width=390, height=844) as page:
        routes = Routes(
            sources=["/auto/x.mp3", "/auto/x.mp3"], failing=("/auto/x.mp3",)
        )
        _open(page, routes)
        page.wait_for_selector("[data-player-failure]", timeout=15000)
        _assert_failure_shown(page)
        routes.failing.discard("/auto/x.mp3")
        # 不点重试，只重新加载音频；canplay/playing 到达须自动清除。
        page.evaluate("() => document.getElementById('audio').load()")
        page.wait_for_function(
            "() => document.getElementById('audio').readyState >= 2", timeout=15000
        )
        page.wait_for_timeout(400)
        _assert_no_failure(page)
        _assert_player_intact(page)
        _assert_playable(page)


def test_normal_audio_plays_and_keeps_existing_controls() -> None:
    """P5 回归：正常可播音频全程无失败提示，播放/暂停、段落定位、快进如旧。"""
    with chromium_page(color_scheme="light", width=390, height=844) as page:
        routes = Routes(sources=["/wav/ok.wav"])
        _open(page, routes)
        page.wait_for_function(
            "() => document.getElementById('audio').readyState >= 2", timeout=15000
        )
        _assert_no_failure(page)
        _assert_playable(page)
        assert page.locator("#play").get_attribute("aria-label") == "暂停"
        page.click("#play")
        page.wait_for_timeout(300)
        assert page.evaluate("() => document.getElementById('audio').paused") is True
        assert page.locator("#play").get_attribute("aria-label") == "播放"
        # 段落点击定位。
        page.locator(".seg-line[data-start='30'] .ts").click()
        page.wait_for_timeout(300)
        current: Any = page.evaluate("() => document.getElementById('audio').currentTime")
        assert 29.0 <= float(current) <= 31.0
        # 快进 15 秒。
        before: Any = page.evaluate("() => document.getElementById('audio').currentTime")
        page.click("#skip-fwd")
        page.wait_for_timeout(300)
        after: Any = page.evaluate("() => document.getElementById('audio').currentTime")
        assert float(after) >= float(before) + 14.0
        _assert_no_failure(page)
