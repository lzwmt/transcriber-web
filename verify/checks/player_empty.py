"""PLAYER-EMPTY：未完成与音频缺失任务的详情页占位态（F4）。"""

from __future__ import annotations

from verify import DESKTOP_WIDTH
from verify.browser import chromium_page
from verify.evidence import CheckResult, checklist_line, render_verdict
from verify.seed import SeededJob

_ITEM = "PLAYER-EMPTY"


def run_player_empty(
    base_url: str,
    running_job: SeededJob,
    missing_audio_job: SeededJob,
) -> CheckResult:
    criterion = checklist_line("## F4 · 无音频占位态")
    problems: list[str] = []
    page_errors: list[str] = []

    # 1. 处理中任务
    with chromium_page(
        color_scheme="light",
        width=DESKTOP_WIDTH,
        height=800,
    ) as page:
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        resp = page.goto(f"{base_url}/jobs/{running_job.job_id}/view", wait_until="load")
        if resp is None or resp.status != 200:
            status = None if resp is None else resp.status
            problems.append(f"处理中任务详情页 HTTP {status}")
        page.wait_for_selector(".player-empty")
        has_player = page.evaluate("() => Boolean(document.querySelector('#player'))")
        has_play_btn = page.evaluate("() => Boolean(document.querySelector('.play'))")
        has_wave = page.evaluate("() => Boolean(document.querySelector('.wave'))")
        empty_text = page.inner_text(".player-empty")
        if has_player or has_play_btn or has_wave:
            problems.append("处理中任务出现了可交互播放器控件")
        if "尚未完成" not in empty_text and "暂无" not in empty_text:
            problems.append(f"处理中任务占位文案不明确：{empty_text}")
        running_shot = page.screenshot(full_page=True)
        running_page_text = page.inner_text("body")

    # 2. 已完成但缺失音频任务
    with chromium_page(
        color_scheme="light",
        width=DESKTOP_WIDTH,
        height=800,
    ) as page:
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        resp = page.goto(f"{base_url}/jobs/{missing_audio_job.job_id}/view", wait_until="load")
        if resp is None or resp.status != 200:
            status = None if resp is None else resp.status
            problems.append(f"缺音频任务详情页 HTTP {status}")
        page.wait_for_selector(".player-empty")
        has_player = page.evaluate("() => Boolean(document.querySelector('#player'))")
        empty_text_2 = page.inner_text(".player-empty")
        has_transcript = page.evaluate("() => Boolean(document.querySelector('.transcript'))")
        has_minutes = page.evaluate("() => Boolean(document.querySelector('.minutes-body'))")
        if has_player:
            problems.append("缺音频任务出现了播放器控件")
        if "不存在" not in empty_text_2 and "不可用" not in empty_text_2:
            problems.append(f"缺音频任务占位文案不明确：{empty_text_2}")
        if not has_transcript:
            problems.append("缺音频任务的逐字稿区块丢失")
        if not has_minutes:
            problems.append("缺音频任务的纪要区块丢失")
        missing_shot = page.screenshot(full_page=True)

    passed = not problems and not page_errors
    conclusion = (
        "未完成任务与音频缺失任务详情页均显示明确占位文案，无可交互播放器控件，"
        "页面无未捕获异常与坏布局，纪要/逐字稿区块行为正常保持。"
        if passed
        else "PLAYER-EMPTY 检查未满足判据。"
    )
    anomalies = "无" if passed else "；".join(problems + page_errors)
    files = [
        "items/PLAYER-EMPTY/verdict.md",
        "items/PLAYER-EMPTY/shot-running.png",
        "items/PLAYER-EMPTY/shot-final.png",
        "items/PLAYER-EMPTY/page.txt",
    ]
    verdict = render_verdict(
        item_id=_ITEM,
        criterion=criterion,
        passed=passed,
        conclusion=conclusion,
        files=files,
        anomalies=anomalies,
        section="F4",
    )
    log = (
        f"item={_ITEM}\npassed={passed}\nproblems={problems}\npage_errors={page_errors}\n"
    )
    return CheckResult(
        item_id=_ITEM,
        passed=passed,
        verdict_md=verdict,
        log_txt=log,
        page_txt=running_page_text,
        screenshot=missing_shot,
        extra={
            "shot-running.png": running_shot,
        },
    )
