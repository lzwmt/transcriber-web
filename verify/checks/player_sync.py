"""PLAYER-SYNC：逐字稿与播放器联动（F3）。"""

from __future__ import annotations

from verify import DESKTOP_WIDTH
from verify.browser import chromium_page
from verify.evidence import CheckResult, checklist_line, render_verdict
from verify.seed import SeededJob

_ITEM = "PLAYER-SYNC"


def run_player_sync(base_url: str, job: SeededJob) -> CheckResult:
    criterion = checklist_line("## F3 · 逐字稿联动")
    problems: list[str] = []
    page_errors: list[str] = []
    target_url = f"{base_url}/jobs/{job.job_id}/view"
    snapshots: list[str] = []

    with chromium_page(
        color_scheme="light",
        width=DESKTOP_WIDTH,
        height=800,
    ) as page:
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        resp = page.goto(target_url, wait_until="load")
        resp_status = resp.status if resp is not None else None
        resp_status_text = str(resp_status) if resp_status is not None else "None"
        if resp_status != 200:
            problems.append(f"详情页 HTTP {resp_status_text}")
        page.wait_for_selector("#player")
        initial_shot = page.screenshot(full_page=True)
        page_text = page.inner_text("body")

        # 检查逐字稿段带 start/end 数值
        seg_data = page.evaluate(
            """() => Array.from(document.querySelectorAll('.seg-line')).map((el, i) => ({
                index: i,
                start: el.getAttribute('data-start'),
                end: el.getAttribute('data-end'),
                hasSeek: Boolean(el.querySelector('[data-seek]')),
                isNow: el.classList.contains('now'),
            }))"""
        )
        if not isinstance(seg_data, list) or len(seg_data) < 2:
            count = len(seg_data) if isinstance(seg_data, list) else 0
            problems.append(f"逐字稿段落数不足 2 条，实测 {count}")
        elif isinstance(seg_data, list):
            for item in seg_data:
                if not isinstance(item, dict):
                    continue
                if item.get("start") is None or item.get("end") is None:
                    problems.append(f"段落 {item.get('index')} 缺少 data-start 或 data-end")

        # 观察点 1：初始加载（currentTime、高亮段索引）
        init_current_time = float(
            page.evaluate("() => document.querySelector('audio').currentTime")
        )
        init_active_idx = -1
        if isinstance(seg_data, list):
            for item in seg_data:
                if isinstance(item, dict) and item.get("isNow"):
                    init_active_idx = int(item["index"])
                    break
        if init_active_idx == -1:
            init_active_idx = page.evaluate(
                """() => Array.from(document.querySelectorAll('.seg-line')).findIndex(
                    el => el.classList.contains('now')
                )"""
            )
        snapshots.append(
            f"1. 初始加载: currentTime={init_current_time:.3f}, highlight_index={init_active_idx}"
        )

        # 1. 点第 2 段时间戳（start=4.0）
        ts_btn = page.locator(".seg-line[data-start='4.0'] .ts")
        ts_found = ts_btn.count() > 0
        if not ts_found:
            problems.append("未找到 start=4.0 的时间戳元素")
        else:
            ts_btn.click()

        current_time = float(
            page.evaluate("() => document.querySelector('audio').currentTime")
        )
        time_diff = abs(current_time - 4.0)
        if time_diff > 0.5:
            problems.append(
                f"点击 start=4.0 时间戳后 currentTime 为 {current_time}，误差 {time_diff}s > 0.5s"
            )

        active_idx_1 = page.evaluate(
            """() => Array.from(document.querySelectorAll('.seg-line')).findIndex(
                el => el.classList.contains('now')
            )"""
        )
        if active_idx_1 != 1:
            problems.append(f"点击第 2 段时间戳后当前高亮段索引应为 1，实测 {active_idx_1}")

        # 观察点 2：点第 2 段时间戳后（currentTime、高亮段索引）
        snapshots.append(
            f"2. 点第 2 段时间戳后: currentTime={current_time:.3f}, highlight_index={active_idx_1}"
        )

        # 2. 模拟播放推进到 8.5s（落在第 3 段 start=8.0..12.0）
        page.evaluate(
            """() => {
                const a = document.querySelector('audio');
                a.currentTime = 8.5;
                a.dispatchEvent(new Event('timeupdate'));
            }"""
        )
        active_idx_2 = page.evaluate(
            """() => Array.from(document.querySelectorAll('.seg-line')).findIndex(
                el => el.classList.contains('now')
            )"""
        )
        if active_idx_2 != 2:
            problems.append(f"播放推进至 8.5s 后当前高亮段索引应为 2，实测 {active_idx_2}")

        # 观察点 3：推进到 8.5 后（高亮段索引）
        snapshots.append(f"3. 推进到 8.5 后: highlight_index={active_idx_2}")

        # 3. 暂停保留高亮
        page.evaluate(
            """() => {
                const a = document.querySelector('audio');
                a.pause();
            }"""
        )
        active_idx_paused = page.evaluate(
            """() => Array.from(document.querySelectorAll('.seg-line')).findIndex(
                el => el.classList.contains('now')
            )"""
        )
        if active_idx_paused != 2:
            problems.append(f"暂停后高亮未保留在段 2，实测 {active_idx_paused}")

        # 观察点 4：暂停后（高亮段索引）
        snapshots.append(f"4. 暂停后: highlight_index={active_idx_paused}")

        final_shot = page.screenshot(full_page=True)

    values_txt = (
        f"target_segment_start=4.0\n"
        f"actual_current_time={current_time:.3f}\n"
        f"time_delta_seconds={time_diff:.3f}\n"
        f"time_delta_allowed_le=0.5\n"
        f"post_click_highlight_index={active_idx_1}\n"
        f"post_advance_highlight_index={active_idx_2}\n"
        f"post_pause_highlight_index={active_idx_paused}\n"
        f"pause_retains_highlight={'yes' if active_idx_paused == 2 else 'no'}\n"
    )

    snapshots_txt = "\n".join(["# PLAYER-SYNC 时序快照", *snapshots])

    seg_table_lines: list[str] = []
    seg_attrs_ok = True
    seg_count = len(seg_data) if isinstance(seg_data, list) else 0
    if isinstance(seg_data, list):
        for s in seg_data:
            if isinstance(s, dict):
                idx = s.get("index")
                st = s.get("start")
                ed = s.get("end")
                sk = s.get("hasSeek")
                seg_table_lines.append(f"index={idx} | start={st} | end={ed} | hasSeek={sk}")
                if st is None or ed is None:
                    seg_attrs_ok = False
            else:
                seg_attrs_ok = False
    else:
        seg_attrs_ok = False

    http_ok = resp_status == 200
    seg_count_ok = seg_count >= 2
    time_diff_ok = time_diff <= 0.5
    active_1_ok = active_idx_1 == 1
    active_2_ok = active_idx_2 == 2
    pause_ok = active_idx_paused == 2

    step_assertions = [
        (
            f"1. 详情页响应: 预期 HTTP 200 | 实测 HTTP {resp_status_text} | "
            f"结论 {'PASS' if http_ok else 'FAIL'}"
        ),
        (
            f"2. 逐字稿段落数: 预期 >= 2 | 实测 {seg_count} | "
            f"结论 {'PASS' if seg_count_ok else 'FAIL'}"
        ),
        (
            f"3. 段落 start/end 属性: 预期全部段落含 start/end | "
            f"实测 {'全部具备' if seg_attrs_ok else '存在缺失'} | "
            f"结论 {'PASS' if seg_attrs_ok else 'FAIL'}"
        ),
        (
            f"4. 找到第 2 段时间戳元素: 预期元素存在 | "
            f"实测 {'找到' if ts_found else '未找到'} | "
            f"结论 {'PASS' if ts_found else 'FAIL'}"
        ),
        (
            f"5. 点击时间戳跳转 currentTime: 预期 4.0 (误差<=0.5s) | "
            f"实测 {current_time:.3f} (误差 {time_diff:.3f}s) | "
            f"结论 {'PASS' if time_diff_ok else 'FAIL'}"
        ),
        (
            f"6. 点击时间戳高亮段索引: 预期 1 | 实测 {active_idx_1} | "
            f"结论 {'PASS' if active_1_ok else 'FAIL'}"
        ),
        (
            f"7. 推进播放至 8.5s 高亮段索引: 预期 2 | 实测 {active_idx_2} | "
            f"结论 {'PASS' if active_2_ok else 'FAIL'}"
        ),
        (
            f"8. 暂停播放保留高亮段索引: 预期 2 | 实测 {active_idx_paused} | "
            f"结论 {'PASS' if pause_ok else 'FAIL'}"
        ),
    ]

    run_txt = "\n".join(
        [
            "# 基本信息",
            f"item={_ITEM}",
            f"target_url={target_url}",
            f"job_id={job.job_id}",
            "",
            "# 逐字稿段表（seg_data）",
            *(seg_table_lines if seg_table_lines else ["无"]),
            "",
            "# 断言预期与实测与单步结论",
            *step_assertions,
        ]
    )

    passed = not problems and not page_errors
    conclusion = (
        "详情视图每段带 start/end 数值进 data 属性；点时间戳 currentTime 跳至该段 start "
        f"（误差 {time_diff:.3f}s ≤ 0.5s）；播放推进时高亮段平滑切换；暂停时高亮保留。"
        if passed
        else "PLAYER-SYNC 联动检查未满足判据。"
    )
    anomalies = "无" if passed else "；".join(problems + page_errors)
    files = [
        "items/PLAYER-SYNC/verdict.md",
        "items/PLAYER-SYNC/values.txt",
        "items/PLAYER-SYNC/shot-initial.png",
        "items/PLAYER-SYNC/shot-final.png",
        "items/PLAYER-SYNC/page.txt",
        "items/PLAYER-SYNC/snapshots.txt",
        "items/PLAYER-SYNC/run.txt",
    ]
    verdict = render_verdict(
        item_id=_ITEM,
        criterion=criterion,
        passed=passed,
        conclusion=conclusion,
        files=files,
        anomalies=anomalies,
        section="F3",
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
        screenshot=final_shot,
        extra={
            "values.txt": values_txt,
            "shot-initial.png": initial_shot,
            "snapshots.txt": snapshots_txt,
            "run.txt": run_txt,
        },
    )
