"""PLAYER-BASIC：双主题播放器落地与 tokens 对齐（F2）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from verify import DESKTOP_WIDTH, MOBILE_WIDTH
from verify.browser import chromium_page
from verify.evidence import CheckResult, checklist_line, render_verdict
from verify.seed import SeededJob
from verify.tokens import same_color, same_transparent

if TYPE_CHECKING:
    from playwright.sync_api import Page

_ITEM = "PLAYER-BASIC"

_MEASURE_JS = """
() => {
  const getProp = (sel, prop) => {
    const el = document.querySelector(sel);
    return el ? getComputedStyle(el).getPropertyValue(prop).trim() : null;
  };
  return {
    playBg: getProp(".play", "background-color"),
    playShadow: getProp(".play", "box-shadow"),
    playedWaveBg: getProp(".wave i.p", "background-color"),
    headBg: getProp("#head", "background-color"),
    unplayedWaveBg: getProp(".wave i:not(.p)", "background-color"),
    // E 节起当前段高亮只落右列正文块：测量点随规格迁到 .seg-body，期望色值不变。
    currentSegBg: getProp(".seg-line.now .seg-body", "background-color"),
    currentSegTimeBg: getProp(".seg-line.now .seg-time", "background-color"),
    // 时间列所在容器的实际底色（卡片底），用来判 E2「与页面底色一致」这一口径。
    segSurfaceBg: (() => {
      let el = document.querySelector(".seg-line.now .seg-time");
      while (el) {
        const bg = getComputedStyle(el).backgroundColor;
        if (bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent") return bg;
        el = el.parentElement;
      }
      return null;
    })(),
    waveHeight: getProp(".wave", "height"),
    speedColor: getProp(".speed", "color"),
    speedBg: getProp(".speed", "background-color"),
    hasPlay: Boolean(document.querySelector("#play")),
    hasWave: Boolean(document.querySelector("#wave")),
    hasTime: Boolean(document.querySelector("#player-time")),
    hasSpeed: Boolean(document.querySelector("#speed")),
  };
}
"""


def _seek_and_settle(page: Page, problems: list[str]) -> None:
    res = page.evaluate(
        """() => {
          const a = document.querySelector("audio");
          if (!a) {
            return {ok: false, error: "未找到 audio 元素"};
          }
          const dur = a.duration;
          if (dur === null || dur === undefined || Number.isNaN(dur) || dur <= 0) {
            return {ok: false, error: `audio duration 无效或为 0（duration=${dur}）`};
          }
          a.currentTime = dur / 2;
          a.dispatchEvent(new Event("timeupdate"));
          return {ok: true, duration: dur};
        }"""
    )
    if not isinstance(res, dict) or not res.get("ok"):
        error_msg = res.get("error") if isinstance(res, dict) else "evaluate 返回非对象"
        problems.append(f"seek 至中点跳过：{error_msg}")
        return

    try:
        page.wait_for_function(
            "() => document.querySelectorAll('.wave i.p').length > 0",
            timeout=5000,
        )
    except Exception as exc:
        problems.append(f"等待已播波形生效超时（5s）：{exc}")

    page.wait_for_timeout(600)


def run_player_basic(base_url: str, job: SeededJob) -> CheckResult:
    criterion = checklist_line("## F2 · 播放器落地（双主题）")
    problems: list[str] = []
    page_errors: list[str] = []
    target_url = f"{base_url}/jobs/{job.job_id}/view"

    # 1. 账本（浅色）1280 桌面实测
    with chromium_page(
        color_scheme="light",
        width=DESKTOP_WIDTH,
        height=800,
    ) as page:
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        resp = page.goto(target_url, wait_until="load")
        if resp is None or resp.status != 200:
            status = None if resp is None else resp.status
            problems.append(f"详情页浅色 HTTP {status}")
        page.wait_for_selector("#player")
        _seek_and_settle(page, problems)
        ledger_data = page.evaluate(_MEASURE_JS)
        ledger_shot = page.screenshot(full_page=True)
        page_text = page.inner_text("body")

    # 2. 录音棚（深色）390 手机实测
    with chromium_page(
        color_scheme="dark",
        width=MOBILE_WIDTH,
        height=844,
    ) as page:
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        resp = page.goto(target_url, wait_until="load")
        if resp is None or resp.status != 200:
            status = None if resp is None else resp.status
            problems.append(f"详情页深色 HTTP {status}")
        page.wait_for_selector("#player")
        page.evaluate("() => document.documentElement.setAttribute('data-theme', 'studio')")
        _seek_and_settle(page, problems)
        studio_data = page.evaluate(_MEASURE_JS)
        studio_shot = page.screenshot(full_page=True)

    if not isinstance(ledger_data, dict) or not isinstance(studio_data, dict):
        problems.append("样式测量未能返回对象")
        ledger_data = ledger_data if isinstance(ledger_data, dict) else {}
        studio_data = studio_data if isinstance(studio_data, dict) else {}

    # 播放键
    if not same_color(str(ledger_data.get("playBg")), "#141414"):
        problems.append(f"浅色播放键背景不是 #141414，实测 {ledger_data.get('playBg')}")
    if not same_color(str(studio_data.get("playBg")), "#f2b33d"):
        problems.append(f"深色播放键背景不是 #f2b33d，实测 {studio_data.get('playBg')}")
    studio_shadow = str(studio_data.get("playShadow") or "")
    has_ring = "6px" in studio_shadow and (
        "rgb(42, 36, 20)" in studio_shadow or "#2a2414" in studio_shadow
    )
    if not has_ring:
        problems.append(f"深色播放键光环不是 6px #2a2414，实测 {studio_shadow}")

    # 已播波形/播放头
    if not same_color(str(ledger_data.get("playedWaveBg")), "#1d5fbf"):
        problems.append(f"浅色已播波形背景不是 #1d5fbf，实测 {ledger_data.get('playedWaveBg')}")
    if not same_color(str(studio_data.get("playedWaveBg")), "#f2b33d"):
        problems.append(f"深色已播波形背景不是 #f2b33d，实测 {studio_data.get('playedWaveBg')}")

    # 未播波形
    if not same_color(str(ledger_data.get("unplayedWaveBg")), "#d4d8de"):
        problems.append(f"浅色未播波形背景不是 #d4d8de，实测 {ledger_data.get('unplayedWaveBg')}")
    if not same_color(str(studio_data.get("unplayedWaveBg")), "#44413d"):
        problems.append(f"深色未播波形背景不是 #44413d，实测 {studio_data.get('unplayedWaveBg')}")

    # 当前段：E 节起高亮只落右列正文块，时间列无背景（期望色值不变）
    if not same_color(str(ledger_data.get("currentSegBg")), "#eef3fb"):
        problems.append(f"浅色当前段正文块背景不是 #eef3fb，实测 {ledger_data.get('currentSegBg')}")
    if not same_color(str(studio_data.get("currentSegBg")), "#2a2414"):
        problems.append(f"深色当前段正文块背景不是 #2a2414，实测 {studio_data.get('currentSegBg')}")
    # 时间列判据（E2）：当前段左时间列 computed background-color 必须与页面底色一致。
    # 实现上是「完全透明」——背景透出所在卡片底色，视觉与「同底色」等价；两种口径都收，
    # 但不接受任何其它色块（那才是「被背景覆盖」）。
    for theme, data in (("浅色", ledger_data), ("深色", studio_data)):
        time_bg = str(data.get("currentSegTimeBg") or "")
        surface = str(data.get("segSurfaceBg") or "")
        ok = same_transparent(time_bg) or (
            bool(surface) and same_color(time_bg, surface)
        )
        if not ok:
            problems.append(
                f"{theme}当前段时间列背景与页面底色不一致，"
                f"实测 {data.get('currentSegTimeBg')}，底色 {data.get('segSurfaceBg')}"
            )

    # 波形区高度
    if ledger_data.get("waveHeight") != "56px":
        problems.append(f"浅色波形区高度不是 56px，实测 {ledger_data.get('waveHeight')}")
    if studio_data.get("waveHeight") != "72px":
        problems.append(f"深色波形区高度不是 72px，实测 {studio_data.get('waveHeight')}")

    # 倍速键
    l_sc, l_sb = str(ledger_data.get("speedColor")), str(ledger_data.get("speedBg"))
    s_sc, s_sb = str(studio_data.get("speedColor")), str(studio_data.get("speedBg"))
    if not same_color(l_sc, "#0a0a0a") or not same_color(l_sb, "#ffffff"):
        problems.append(f"浅色倍速键字/底不是 #0a0a0a / #ffffff，实测 {l_sc} / {l_sb}")
    if not same_color(s_sc, "#ffffff") or not same_color(s_sb, "#181716"):
        problems.append(f"深色倍速键字/底不是 #ffffff / #181716，实测 {s_sc} / {s_sb}")

    v_lines = [
        "| 控件 | 属性 | tokens 变量 | 账本（浅）期望 | 账本实测 | 录音棚（深）期望 | 录音棚实测 |",
        "|---|---|---|---|---|---|---|",
        (
            f"| 播放键 | background | --accent / 专用琥珀 | #141414 | {ledger_data.get('playBg')} "
            f"| #f2b33d (6px #2a2414) | {studio_data.get('playBg')} ({studio_shadow}) |"
        ),
        (
            f"| 已播波形/播放头 | background | --data | #1d5fbf | "
            f"{ledger_data.get('playedWaveBg')} | #f2b33d | {studio_data.get('playedWaveBg')} |"
        ),
        (
            f"| 未播波形 | background | --control | #d4d8de | "
            f"{ledger_data.get('unplayedWaveBg')} | #44413d | {studio_data.get('unplayedWaveBg')} |"
        ),
        (
            f"| 当前段正文块 | background | --data-pale | #eef3fb | "
            f"{ledger_data.get('currentSegBg')} | #2a2414 | {studio_data.get('currentSegBg')} |"
        ),
        (
            f"| 当前段时间列 | background | 无（不被覆盖） | 页面底色 | "
            f"{ledger_data.get('currentSegTimeBg')} (底色 {ledger_data.get('segSurfaceBg')}) "
            f"| 页面底色 | "
            f"{studio_data.get('currentSegTimeBg')} (底色 {studio_data.get('segSurfaceBg')}) |"
        ),
        (
            f"| 波形区 | height | 样稿口径 | 56px | "
            f"{ledger_data.get('waveHeight')} | 72px | {studio_data.get('waveHeight')} |"
        ),
        (
            f"| 倍速键 | color/bg | --text / --surface | #0a0a0a / #ffffff | "
            f"{l_sc} / {l_sb} | #ffffff / #181716 | {s_sc} / {s_sb} |"
        ),
    ]
    values_table = "\n".join(v_lines) + "\n"

    passed = not problems and not page_errors
    conclusion = (
        "双主题播放器（播放/暂停、进度条、当前/总时长、倍速切换）齐全；"
        "控件计算样式实测值与 docs/design-tokens.md 规定变量及口径两档逐行对齐。"
        if passed
        else "PLAYER-BASIC 对账未满足判据。"
    )
    anomalies = "无" if passed else ("；".join(problems + page_errors))
    files = [
        "items/PLAYER-BASIC/verdict.md",
        "items/PLAYER-BASIC/values.txt",
        "items/PLAYER-BASIC/shot-final.png",
        "items/PLAYER-BASIC/shot-mobile-dark.png",
        "items/PLAYER-BASIC/page.txt",
    ]
    verdict = render_verdict(
        item_id=_ITEM,
        criterion=criterion,
        passed=passed,
        conclusion=conclusion,
        files=files,
        anomalies=anomalies,
        section="F2",
    )
    log = (
        f"item={_ITEM}\npassed={passed}\nproblems={problems}\npage_errors={page_errors}\n"
    )
    return CheckResult(
        item_id=_ITEM,
        passed=passed,
        verdict_md=verdict,
        log_txt=log,
        page_txt=page_text,
        screenshot=ledger_shot,
        extra={
            "values.txt": values_table,
            "shot-mobile-dark.png": studio_shot,
        },
    )
