"""页面与工作台测试。

仓库无 JS 运行环境，app.js 与 CSS 只能文本对账，行为由清单 U 条实测兜底。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from app import service
from app.jobs import JobStore
from app.main import create_app
from fastapi.testclient import TestClient
from tests.support import post_file, synth_wav, wait_job, wait_minutes

ROOT = Path(__file__).resolve().parent.parent


def test_c11_page_routes_have_no_sql() -> None:
    text = (ROOT / "app" / "web.py").read_text(encoding="utf-8")
    assert "SELECT " not in text
    assert "INSERT " not in text
    assert "UPDATE " not in text
    assert "DELETE " not in text
    assert "sqlite" not in text.lower()
    assert "execute(" not in text


def test_u1_list_empty_and_with_job(client: tuple[TestClient, Path], tmp_path: Path) -> None:
    http, _data = client
    empty = http.get("/")
    assert empty.status_code == 200
    assert "上传或贴链接开始第一次转写" in empty.text
    assert "还没有任务" not in empty.text
    assert "＋ 新建" not in empty.text
    assert "本月 0 分钟 · 精档 0 次" in empty.text
    wav = tmp_path / "talk.wav"
    synth_wav(wav, seconds=0.2)
    job_id = post_file(http, wav, tier="fast").json()["job_id"]
    assert wait_job(http, job_id)["status"] == "done"
    wait_minutes(http, job_id)
    listed = http.get("/")
    assert "talk.wav" in listed.text
    assert "快档" in listed.text
    assert "已完成" in listed.text
    assert "有纪要" in listed.text
    assert "仅显示最近 100 条" not in listed.text


def test_u2_new_form_creates_job_and_redirects(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    http, _data = client
    jumped = http.get("/new", follow_redirects=False)
    assert jumped.status_code == 302
    assert jumped.headers["location"] == "/"
    page = http.get("/new")
    assert page.status_code == 200
    assert "上传音视频" in page.text
    assert "贴链接" in page.text
    assert "快档" in page.text
    assert "精档" in page.text
    assert "data-new-form" in page.text
    assert "data-error" in page.text
    wav = tmp_path / "meet.wav"
    synth_wav(wav, seconds=0.2)
    with wav.open("rb") as handle:
        response = http.post(
            "/new",
            data={"tier": "fast"},
            files={"file": (wav.name, handle, "audio/wav")},
            follow_redirects=False,
        )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/jobs/")
    assert location.endswith("/view")
    empty = http.post("/new", data={"tier": "fast"})
    assert empty.status_code == 400
    assert "请上传文件或粘贴链接" in empty.text
    assert 'data-error' in empty.text
    assert 'id="usage"' in empty.text
    assert "新建转写" not in empty.text


def test_u3_detail_has_minutes_and_speakers(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    http, _data = client
    wav = tmp_path / "talk.wav"
    synth_wav(wav, seconds=0.2)
    job_id = post_file(http, wav, tier="fast").json()["job_id"]
    assert wait_job(http, job_id)["status"] == "done"
    wait_minutes(http, job_id)
    page = http.get(f"/jobs/{job_id}/view")
    assert page.status_code == 200
    assert "纪要" in page.text
    assert "一句话摘要" in page.text
    assert "逐字稿" in page.text
    assert "说话人1" in page.text
    # 说话人自 E 节起是横排小标签，不再是 <strong>：断言落到标签结构本身。
    assert 'class="seg-speaker spk-' in page.text
    assert "00:00" in page.text


def test_u4_download_entries_include_combined(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    http, _data = client
    wav = tmp_path / "talk.wav"
    synth_wav(wav, seconds=0.2)
    job_id = post_file(http, wav, tier="fast").json()["job_id"]
    assert wait_job(http, job_id)["status"] == "done"
    wait_minutes(http, job_id)
    page = http.get(f"/jobs/{job_id}/view")
    assert "下载合并版" in page.text
    assert f"/jobs/{job_id}/combined" in page.text
    assert f"/jobs/{job_id}/transcript" in page.text
    assert f"/jobs/{job_id}/minutes" in page.text


def test_u5_delete_button_has_confirm(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    http, _data = client
    wav = tmp_path / "talk.wav"
    synth_wav(wav, seconds=0.2)
    job_id = post_file(http, wav, tier="fast").json()["job_id"]
    assert wait_job(http, job_id)["status"] == "done"
    page = http.get(f"/jobs/{job_id}/view")
    assert "删除任务" in page.text
    assert "confirm(" in page.text
    cancel_still_there = http.get(f"/jobs/{job_id}")
    assert cancel_still_there.status_code == 200


def test_u7_processing_page_auto_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSCRIBER_STUB", "1")
    monkeypatch.setenv("TRANSCRIBER_STUB_SLEEP", "3")
    data = tmp_path / "data"
    wav = tmp_path / "slow.wav"
    synth_wav(wav, seconds=0.2)
    with TestClient(create_app(data)) as http:
        job_id = post_file(http, wav, tier="fast").json()["job_id"]
        page = http.get(f"/jobs/{job_id}/view")
        assert page.status_code == 200
        assert "处理中" in page.text
        assert 'http-equiv="refresh"' in page.text
        assert 'content="5"' in page.text
        assert "data-live-poll" not in page.text
        listed = http.get("/")
        assert 'http-equiv="refresh"' not in listed.text
        assert 'id="live"' in listed.text
        assert 'id="usage"' in listed.text
        assert "data-live-poll" in listed.text


def test_f1_pages_show_shanghai_time_eight_hours_ahead_of_stored_utc(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    """列表页和详情页显示 Asia/Shanghai 墙钟，比库里的 UTC 早 8 小时。"""
    http, data = client
    wav = tmp_path / "tz.wav"
    synth_wav(wav, seconds=0.2)
    job_id = post_file(http, wav, tier="fast").json()["job_id"]
    assert wait_job(http, job_id)["status"] == "done"
    stored_created = "2026-01-15T16:30:45.123456Z"
    _set_stored_created(data / "jobs.db", job_id, stored_created)

    displayed = "2026-01-16 00:30"
    listed = http.get("/")
    detail = http.get(f"/jobs/{job_id}/view")
    assert listed.status_code == 200
    assert detail.status_code == 200
    assert displayed in listed.text
    assert displayed in detail.text
    stored_wall = datetime.strptime(stored_created[:16], "%Y-%m-%dT%H:%M")
    shown_wall = datetime.strptime(displayed, "%Y-%m-%d %H:%M")
    assert shown_wall - stored_wall == timedelta(hours=8)
    assert _stored_created(data / "jobs.db", job_id) == stored_created


def _set_stored_created(db: Path, job_id: str, created_at: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (created_at, job_id))
        conn.commit()
    finally:
        conn.close()


def _stored_created(db: Path, job_id: str) -> str:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT created_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def _insert_job(
    db: Path,
    *,
    source_name: str,
    created_at: str,
    status: str = "done",
    tier: str = "fast",
    source_kind: str = "upload",
    finished_at: str | None = None,
    error: str | None = None,
    duration: float | None = None,
    minutes_status: str = "done",
    url: str | None = None,
) -> str:
    JobStore(db)
    job_id = uuid4().hex
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """
            INSERT INTO jobs (
                id, status, tier, source_name, source_kind, source_path, url,
                created_at, finished_at, error, duration, minutes_status
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                status,
                tier,
                source_name,
                source_kind,
                url,
                created_at,
                finished_at,
                error,
                duration,
                minutes_status,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return job_id


def test_w2_monthly_usage_counts_statuses_and_formats_duration(tmp_path: Path) -> None:
    """用量时长写法（本单定死）：

    - 秒数先 int 向 0 截断，再按整分钟向 0 取整。
    - 0 秒、以及不足 1 分钟（45 秒）→「0 分钟」。秒数仍计入 total_seconds。
    - 不足 1 小时 →「Y 分钟」（170 秒 →「2 分钟」，60 秒 →「1 分钟」）。
    - 满小时且余分钟为 0 →「X 小时」，不写「0 分钟」（3600 秒、7200 秒）。
    - 满小时且有余分钟 →「X 小时 Y 分钟」（3661 秒 →「1 小时 1 分钟」）。
    当月含失败、排队和空 duration；范围外的任务不计入。
    """
    now = datetime(2026, 6, 15, 4, 0, tzinfo=UTC)
    june = "2026-06-01T04:00:00.000000Z"

    empty = JobStore(tmp_path / "empty.db")
    none = service.monthly_usage(empty, now)
    assert none["text"] == "本月 0 分钟 · 精档 0 次"
    assert none["total_seconds"] == 0
    assert none["precise_count"] == 0

    seconds = tmp_path / "seconds.db"
    _insert_job(seconds, source_name="short.wav", created_at=june, duration=45)
    short = service.monthly_usage(JobStore(seconds), now)
    assert short["text"] == "本月 0 分钟 · 精档 0 次"
    assert short["total_seconds"] == 45

    one_minute = tmp_path / "minute.db"
    _insert_job(one_minute, source_name="minute.wav", created_at=june, duration=60)
    assert service.monthly_usage(JobStore(one_minute), now)["text"] == "本月 1 分钟 · 精档 0 次"

    mixed = tmp_path / "mixed.db"
    _insert_job(mixed, source_name="two-min.wav", created_at=june, duration=120, status="done")
    _insert_job(
        mixed,
        source_name="failed-precise.wav",
        created_at=june,
        duration=None,
        status="error",
        tier="precise",
        error="引擎失败",
    )
    _insert_job(
        mixed,
        source_name="queued.wav",
        created_at=june,
        duration=50,
        status="queued",
        minutes_status="pending",
    )
    _insert_job(
        mixed,
        source_name="last-month.wav",
        created_at="2026-05-15T00:00:00.000000Z",
        duration=30,
        tier="precise",
    )
    mixed_usage = service.monthly_usage(JobStore(mixed), now)
    assert mixed_usage["total_seconds"] == 170
    assert mixed_usage["precise_count"] == 1
    assert mixed_usage["text"] == "本月 2 分钟 · 精档 1 次"

    hours = tmp_path / "hours.db"
    _insert_job(hours, source_name="hour.wav", created_at=june, duration=3661)
    hour_text = service.monthly_usage(JobStore(hours), now)["text"]
    assert hour_text == "本月 1 小时 1 分钟 · 精档 0 次"

    one_hour = tmp_path / "one-hour.db"
    _insert_job(one_hour, source_name="one-hour.wav", created_at=june, duration=3600)
    assert service.monthly_usage(JobStore(one_hour), now)["text"] == "本月 1 小时 · 精档 0 次"

    exact = tmp_path / "exact.db"
    _insert_job(exact, source_name="two-hours.wav", created_at=june, duration=7200)
    assert service.monthly_usage(JobStore(exact), now)["text"] == "本月 2 小时 · 精档 0 次"


def test_w2_shanghai_month_boundary_at_utc_1630(tmp_path: Path) -> None:
    """UTC 2026-01-31 16:30 = 上海 2026-02-01 00:30，计入 2 月，不计入 1 月。

    对照行 UTC 15:59 = 上海 1 月 31 日 23:59，只计入 1 月。上海无夏令时，月界按 UTC+8 手算。
    """
    db = tmp_path / "boundary.db"
    _insert_job(
        db,
        source_name="january.wav",
        created_at="2026-01-31T15:59:59.000000Z",
        duration=10,
        tier="precise",
        status="error",
        error="失败也算",
    )
    _insert_job(
        db,
        source_name="february.wav",
        created_at="2026-01-31T16:30:00.000000Z",
        duration=5,
        tier="fast",
        status="queued",
        minutes_status="pending",
    )
    store = JobStore(db)
    january = service.monthly_usage(store, datetime(2026, 1, 15, 8, 0, tzinfo=UTC))
    february = service.monthly_usage(store, datetime(2026, 2, 10, 8, 0, tzinfo=UTC))
    assert january["text"] == "本月 0 分钟 · 精档 1 次"
    assert january["total_seconds"] == 10
    assert february["text"] == "本月 0 分钟 · 精档 0 次"
    assert february["total_seconds"] == 5
    assert february["precise_count"] == 0


def test_recent_failure_banner_uses_finished_at_window(tmp_path: Path) -> None:
    db = tmp_path / "fails.db"
    store = JobStore(db)
    now = datetime(2026, 4, 10, 8, 0, tzinfo=UTC)
    assert service.recent_failure_banner(store, now) is None

    def stamp(moment: datetime) -> str:
        return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    _insert_job(
        db,
        source_name="空完成.wav",
        created_at=stamp(now),
        status="error",
        error="没有完成时间",
        finished_at=None,
    )
    _insert_job(
        db,
        source_name="太旧.wav",
        created_at=stamp(now),
        status="error",
        error="二十五小时前",
        finished_at=stamp(now - timedelta(hours=25)),
    )
    _insert_job(
        db,
        source_name="界外.wav",
        created_at=stamp(now),
        status="error",
        error="差一微秒",
        finished_at=stamp(now - timedelta(hours=24, microseconds=1)),
    )
    _insert_job(
        db,
        source_name="边界.wav",
        created_at=stamp(now),
        status="error",
        error="刚好二十四小时",
        finished_at=stamp(now - timedelta(hours=24)),
    )
    _insert_job(
        db,
        source_name="最新.wav",
        created_at=stamp(now),
        status="error",
        error="甲" * 80,
        finished_at=stamp(now - timedelta(hours=1)),
    )
    _insert_job(
        db,
        source_name="成功.wav",
        created_at=stamp(now),
        status="done",
        finished_at=stamp(now - timedelta(minutes=30)),
    )
    banner = service.recent_failure_banner(store, now)
    assert banner is not None
    assert banner["count"] == 2
    assert banner["latest_name"] == "最新.wav"
    assert banner["latest_error"] == ("甲" * 59) + "…"
    assert len(banner["latest_error"]) == 60


def test_recent_failures_breaks_ties_by_later_row(tmp_path: Path) -> None:
    """finished_at 相同则后插入的那条（更大的 rowid）算最近一条。"""
    db = tmp_path / "tie.db"
    now = datetime(2026, 4, 10, 8, 0, tzinfo=UTC)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _insert_job(
        db,
        source_name="先.wav",
        created_at=stamp,
        status="error",
        error="先插入",
        finished_at=stamp,
    )
    _insert_job(
        db,
        source_name="后.wav",
        created_at=stamp,
        status="error",
        error="后插入",
        finished_at=stamp,
    )
    banner = service.recent_failure_banner(JobStore(db), now)
    assert banner is not None
    assert banner["count"] == 2
    assert banner["latest_name"] == "后.wav"
    assert banner["latest_error"] == "后插入"


def test_w1_new_template_removed() -> None:
    assert not (ROOT / "app" / "templates" / "new.html").exists()
    web = (ROOT / "app" / "web.py").read_text(encoding="utf-8")
    assert "new.html" not in web
    assert "def new_page" not in web


def test_w2_home_renders_service_usage_text(client: tuple[TestClient, Path]) -> None:
    http, data = client
    db = data / "jobs.db"
    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _insert_job(
        db,
        source_name="hour.wav",
        created_at=created,
        duration=3661,
        tier="precise",
        status="error",
        error="失败仍计入精档",
        finished_at=created,
    )
    page = http.get("/")
    expected = service.monthly_usage(JobStore(db), datetime.now(UTC))["text"]
    assert page.status_code == 200
    assert expected in page.text
    assert "精档 1 次" in expected
    assert 'id="usage"' in page.text


def test_w3_workbench_keeps_submit_hooks(client: tuple[TestClient, Path]) -> None:
    """页面保留提交钩子；拦截文案只对 app.js 源码。

    源码对账理由见模块头。
    """
    http, _data = client
    page = http.get("/")
    assert page.status_code == 200
    assert "data-new-form" in page.text
    assert "data-error" in page.text
    assert 'name="file"' in page.text
    assert 'name="url"' in page.text
    assert 'name="tier"' in page.text
    assert 'value="fast"' in page.text
    assert 'value="precise"' in page.text
    assert 'action="/new"' in page.text
    assert 'enctype="multipart/form-data"' in page.text
    js = _static_text(client, "app.js")
    head, separator, _tail = js.partition("})();")
    assert separator == "})();"
    assert "文件和链接只能提交一种" in head
    assert "请上传文件或粘贴链接" in head


def test_w7_idle_workbench_has_targets_without_polling(client: tuple[TestClient, Path]) -> None:
    http, _data = client
    page = http.get("/")
    assert page.status_code == 200
    assert 'http-equiv="refresh"' not in page.text
    assert 'id="live"' in page.text
    assert 'id="usage"' in page.text
    assert 'id="fail-slot"' in page.text
    match = re.search(r'<div\b[^>]*\bid=["\']fail-slot["\'][^>]*>', page.text)
    assert match is not None, "未找到 id=fail-slot 的插槽 div"
    tag = match.group(0)
    assert re.search(r'\bclass=["\'][^"\']*fail-slot', tag)
    assert re.search(r"\bhidden\b", tag)
    assert "data-live-poll" not in page.text
    assert "单文件 ≤ 2GB · 单条最长 1 小时" in page.text
    assert "约 1/5 时长出稿" in page.text
    assert "区分说话人 · 生成纪要" in page.text
    assert "YouTube · B 站" in page.text
    assert "开始转写" in page.text
    assert 'href="/#live"' in page.text
    assert 'class="brand" href="/">转写</a>' in page.text


_FORBIDDEN_HTML_WRITES = ("insertAdjacentHTML", "innerHTML", "outerHTML")
_ALLOWED_WRITE_RECEIVERS = frozenset({"currentBanner", "slot", "currentRecent", "usage"})
_METHOD_WRITE_RE = re.compile(
    r"\b([a-zA-Z_$][a-zA-Z0-9_$]*)\s*\.\s*(?:replaceChildren|replaceWith|remove|appendChild|insertAdjacentHTML|insertAdjacentElement|insertAdjacentText)\b"
)
_ASSIGN_WRITE_RE = re.compile(
    r"\b([a-zA-Z_$][a-zA-Z0-9_$]*)\s*\.\s*(?:textContent|innerText|innerHTML|outerHTML)\s*(=|\+=)"
)


def _poll_section(iife: str) -> str:
    """轮询段：从取得 #live 的那句到本 IIFE 末尾。前面的拖拽区不算轮询。"""
    marker = 'getElementById("live")'
    start = iife.find(marker)
    assert start != -1, marker
    return iife[iife.rfind("\n", 0, start) + 1 :]


def _assert_no_forbidden_html_writes(source: str) -> None:
    offenders = [api for api in _FORBIDDEN_HTML_WRITES if api in source]
    assert not offenders, f"发现被禁的 HTML 写入 API: {offenders}"


def _assert_poll_dom_writes_whitelisted(poll_source: str) -> None:
    """白名单断言：检查轮询代码中的 DOM 写入安全且合规。

    1. 全文三禁词（insertAdjacentHTML, innerHTML, outerHTML）零命中；
    2. 覆盖 innerText 与 appendChild 写入路径（轮询中禁止使用）；
    3. 写入接收者（receiver）严格限定在白名单集合 {currentBanner, slot, currentRecent, usage} 内；
    4. 轮询插槽更新路径必须包含 usage.textContent 赋值。
    """
    _assert_no_forbidden_html_writes(poll_source)
    assert "innerText" not in poll_source, "轮询代码禁止使用 innerText（只许 textContent）"
    assert "appendChild" not in poll_source, (
        "轮询代码禁止使用 appendChild（固定插槽与列表更新只许 replaceChildren 等）"
    )
    receivers = {m.group(1) for m in _METHOD_WRITE_RE.finditer(poll_source)} | {
        m.group(1) for m in _ASSIGN_WRITE_RE.finditer(poll_source)
    }
    forbidden = receivers - _ALLOWED_WRITE_RECEIVERS
    assert not forbidden, f"写入目标超出白名单: {forbidden}"
    assert not ({"live", "current", "composer"} & receivers), (
        f"禁止向 live/composer 写入: {receivers}"
    )
    assert re.search(r"\busage\.textContent\s*=", poll_source), (
        "轮询代码缺少 usage.textContent 更新路径"
    )


def test_w7_poll_whitelist_rejects_forbidden_writes() -> None:
    """白名单反例与正例自检：构造违规 JS 片段断言报错并区分原因；另断言合法片段不报错。

    替代原先手写扫描器的 _assert_poll_scanner_catches_forbidden_writes。
    若检查器抓不住任何反例或误伤正例，本测试立即 FAIL。
    """
    # 正例：合法片段（含接收者白名单内的写入与 usage.textContent 赋值）断言白名单检查不报错
    valid_sample = (
        'slot.replaceChildren(); '
        'currentBanner.replaceChildren(); '
        'currentRecent.replaceChildren(); '
        'usage.textContent = "已就绪";'
    )
    _assert_poll_dom_writes_whitelisted(valid_sample)

    forbidden_samples: list[tuple[str, str]] = [
        # 三禁词各一条反例（均带合法 usage.textContent 以免被最后一道断言假绿掩盖）
        (
            'var live = document.getElementById("live"); live.innerHTML = ""; '
            'usage.textContent = "1";',
            r"发现被禁的 HTML 写入 API",
        ),
        (
            'document.getElementById("live").outerHTML = "<div></div>"; '
            'usage.textContent = "1";',
            r"发现被禁的 HTML 写入 API",
        ),
        (
            'composer.insertAdjacentHTML("beforebegin", "<p></p>"); '
            'usage.textContent = "1";',
            r"发现被禁的 HTML 写入 API",
        ),
        # innerText 写入反例
        ('slot.innerText = "失败"; usage.textContent = "1";', r"轮询代码禁止使用 innerText"),
        # appendChild 写入反例
        (
            'live.appendChild(document.createElement("div")); usage.textContent = "1";',
            r"轮询代码禁止使用 appendChild",
        ),
        # 缺少 textContent 更新反例（唯独本条不提供 usage.textContent 赋值）
        ('slot.replaceChildren();', r"轮询代码缺少 usage\.textContent 更新路径"),
        # 4 类写入目标限定反例（B1）
        ('composer.textContent = "x"; usage.textContent = "1";', r"写入目标超出白名单"),
        (
            'var composer = document.querySelector(".composer"); composer.remove(); '
            'usage.textContent = "1";',
            r"写入目标超出白名单",
        ),
        (
            'var live = document.getElementById("live"); live.replaceChildren(); '
            'usage.textContent = "1";',
            r"写入目标超出白名单",
        ),
        (
            'var live = document.getElementById("live"); live.replaceWith(live); '
            'usage.textContent = "1";',
            r"写入目标超出白名单",
        ),
    ]
    for sample, match_pattern in forbidden_samples:
        with pytest.raises(AssertionError, match=match_pattern):
            _assert_poll_dom_writes_whitelisted(sample)


def test_w7_poll_script_is_guarded_and_leaves_the_form_alone(
    client: tuple[TestClient, Path],
) -> None:
    """轮询替换范围和失败退避只对 app.js 源码。

    源码对账理由见模块头。
    输入卡不在替换集合内；文件、链接、档位不丢由 U7 ① 行为实测兜底。
    抓回的 HTML 没有 #live 时走失败退避，不把 delay 打回 5 秒。
    写入断言覆盖整段轮询代码，含 schedule、syncLive、tick 及其回调。
    """
    js = _static_text(client, "app.js")
    parts = js.split("})();")
    assert len(parts) >= 4
    form_iife, theme_iife, poll_iife = parts[0], parts[1], parts[2]
    assert "[data-new-form]" in form_iife
    assert "data-theme-toggle" in theme_iife
    assert "[data-new-form]" not in poll_iife
    assert "input[name=url]" not in poll_iife
    assert 'getElementById("live")' in poll_iife
    assert 'getElementById("usage")' in poll_iife
    assert "data-reupload" in poll_iife
    assert "data-dropzone" in poll_iife
    assert "is-hot" in poll_iife
    assert "is-highlight" in poll_iife
    assert "scrollIntoView" in poll_iife
    guard = poll_iife.find('hasAttribute("data-live-poll")')
    first_delay = poll_iife.find("5000")
    assert guard != -1
    assert first_delay != -1
    assert guard < first_delay
    assert "if (!response.ok)" in poll_iife
    assert "delay * 2" in poll_iife
    assert "60000" in poll_iife
    assert poll_iife.find("if (!response.ok)") < poll_iife.find("schedule(false)")
    missing_at = poll_iife.find("if (!nextLive)")
    assert missing_at != -1
    missing_return = poll_iife.find("return;", missing_at)
    assert missing_return != -1
    missing_branch = poll_iife[missing_at:missing_return]
    assert "schedule(false)" in missing_branch
    assert "schedule(true)" not in missing_branch
    assert "delay = 5000" not in missing_branch
    success_at = poll_iife.rfind("schedule(true)")
    assert success_at != -1
    success_prelude = poll_iife[max(0, success_at - 160) : success_at]
    assert 'hasAttribute("data-live-poll")' in success_prelude
    assert "nextLive.innerHTML" not in poll_iife
    assert 'querySelector(".fail-banner")' in poll_iife
    assert 'querySelector("#fail-slot")' in poll_iife
    assert 'querySelector(".recent")' in poll_iife

    # ① 全文三禁词零命中检查
    _assert_no_forbidden_html_writes(js)

    # 轮询段限定：上传区与输入卡选择器不得出现在轮询段
    poll = _poll_section(poll_iife)
    assert "data-dropzone" not in poll
    assert "data-file-name" not in poll
    assert ".composer" not in poll

    # ② 插槽更新路径 textContent 断言 + ③ 覆盖 innerText/appendChild + 写入目标限定
    _assert_poll_dom_writes_whitelisted(poll)
    assert "usage.textContent = nextUsage.textContent" in poll
    assert "currentBanner.replaceWith" in poll
    assert "slot.replaceChildren" in poll
    assert "currentRecent.replaceChildren" in poll


def _assert_workbench_error(html: str, message: str) -> None:
    assert re.search(rf"data-error[^>]*>{re.escape(message)}<", html)
    assert 'id="live"' in html
    assert 'id="usage"' in html
    assert "新建转写" not in html


def _static_text(client: tuple[TestClient, Path], name: str) -> str:
    http, _data = client
    response = http.get(f"/static/{name}")
    assert response.status_code == 200
    return response.text


def _input_tag(html: str, pattern: str) -> str:
    match = re.search(pattern, html)
    assert match is not None, pattern
    return match.group(0)


def test_w8_post_errors_land_on_workbench(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    http, _data = client
    wav = tmp_path / "one.wav"
    synth_wav(wav, seconds=0.2)
    job_id = post_file(http, wav, tier="fast").json()["job_id"]
    assert wait_job(http, job_id)["status"] == "done"

    empty = http.post("/new", data={"tier": "fast"})
    assert empty.status_code == 400
    _assert_workbench_error(empty.text, "请上传文件或粘贴链接")
    assert "one.wav" in empty.text
    assert "本月" in empty.text

    with wav.open("rb") as handle:
        both = http.post(
            "/new",
            data={"tier": "fast", "url": "https://example.com/a"},
            files={"file": (wav.name, handle, "audio/wav")},
        )
    assert both.status_code == 400
    _assert_workbench_error(both.text, "文件和链接只能提交一种")
    assert "one.wav" in both.text

    bad = http.post("/new", data={"tier": "precise", "url": "ftp://example.com/a"})
    assert bad.status_code == 400
    _assert_workbench_error(bad.text, "url 必须是 http 或 https 链接")
    assert "one.wav" in bad.text


def test_w8_error_refills_url_and_tier(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    """报错重绘工作台时回填链接和档位。文件选择器不能回填。"""
    http, _data = client
    home = http.get("/")
    assert 'value=""' in _input_tag(home.text, r'<input\b[^>]*name="url"[^>]*>')
    assert "checked" in _input_tag(home.text, r'<input\b[^>]*id="tier-fast"[^>]*>')
    assert "checked" not in _input_tag(home.text, r'<input\b[^>]*id="tier-precise"[^>]*>')

    empty = http.post("/new", data={"tier": "precise"})
    assert empty.status_code == 400
    _assert_workbench_error(empty.text, "请上传文件或粘贴链接")
    assert 'value=""' in _input_tag(empty.text, r'<input\b[^>]*name="url"[^>]*>')
    assert "checked" not in _input_tag(empty.text, r'<input\b[^>]*id="tier-fast"[^>]*>')
    assert "checked" in _input_tag(empty.text, r'<input\b[^>]*id="tier-precise"[^>]*>')

    wav = tmp_path / "one.wav"
    synth_wav(wav, seconds=0.2)
    with wav.open("rb") as handle:
        both = http.post(
            "/new",
            data={"tier": "fast", "url": "https://example.com/a"},
            files={"file": (wav.name, handle, "audio/wav")},
        )
    assert both.status_code == 400
    assert 'value="https://example.com/a"' in _input_tag(
        both.text, r'<input\b[^>]*name="url"[^>]*>'
    )
    assert "checked" in _input_tag(both.text, r'<input\b[^>]*id="tier-fast"[^>]*>')
    assert "checked" not in _input_tag(both.text, r'<input\b[^>]*id="tier-precise"[^>]*>')

    bad = http.post("/new", data={"tier": "precise", "url": "ftp://example.com/a"})
    assert bad.status_code == 400
    _assert_workbench_error(bad.text, "url 必须是 http 或 https 链接")
    assert 'value="ftp://example.com/a"' in _input_tag(
        bad.text, r'<input\b[^>]*name="url"[^>]*>'
    )
    assert "checked" not in _input_tag(bad.text, r'<input\b[^>]*id="tier-fast"[^>]*>')
    assert "checked" in _input_tag(bad.text, r'<input\b[^>]*id="tier-precise"[^>]*>')

    invalid = http.post("/new", data={"tier": "studio", "url": "https://example.com/v"})
    assert invalid.status_code == 400
    _assert_workbench_error(invalid.text, "tier 必须是 fast 或 precise")
    assert 'value="https://example.com/v"' in _input_tag(
        invalid.text, r'<input\b[^>]*name="url"[^>]*>'
    )
    assert "checked" in _input_tag(invalid.text, r'<input\b[^>]*id="tier-fast"[^>]*>')
    assert "checked" not in _input_tag(invalid.text, r'<input\b[^>]*id="tier-precise"[^>]*>')


def test_w8_oversize_upload_lands_on_workbench(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSCRIBER_STUB", "1")
    monkeypatch.setenv("TRANSCRIBER_MAX_UPLOAD_BYTES", "32")
    data = tmp_path / "data"
    blob = tmp_path / "big.bin"
    blob.write_bytes(b"x" * 64)
    with TestClient(create_app(data)) as http:
        with blob.open("rb") as handle:
            response = http.post(
                "/new",
                data={"tier": "fast"},
                files={"file": ("big.bin", handle, "application/octet-stream")},
            )
    assert response.status_code == 413
    _assert_workbench_error(response.text, "上传文件超过大小上限 32 字节")
    assert "本月 0 分钟 · 精档 0 次" in response.text


def test_pending_minutes_marks_the_workbench_live(
    client: tuple[TestClient, Path],
) -> None:
    """已完成但纪要未结束也要轮询。纪要结束后不再轮询。"""
    http, data = client
    db = data / "jobs.db"
    job_id = _insert_job(
        db,
        source_name="待纪要.wav",
        created_at="2026-06-01T00:00:00.000000Z",
        status="done",
        minutes_status="pending",
    )
    pending = http.get("/")
    assert pending.status_code == 200
    assert "data-live-poll" in pending.text
    assert "待纪要.wav" in pending.text

    store = JobStore(db)
    store.set_minutes_status(job_id, "running")
    running = http.get("/")
    assert "data-live-poll" in running.text

    store.set_minutes_status(job_id, "done")
    finished = http.get("/")
    assert "data-live-poll" not in finished.text
    assert "待纪要.wav" in finished.text


def test_workbench_shell_is_1200_and_detail_stays_720(
    client: tuple[TestClient, Path],
) -> None:
    """工作台页顶栏和内容同在 1200 容器里。详情页和缺页仍是 720。

    工作台容器左右 padding 取 var(--pad)。详情页 .wrap 仍是 16px。
    源码对账理由见模块头。
    """
    http, data = client
    home = http.get("/")
    assert home.status_code == 200
    assert 'class="wrap wrap-workbench"' in home.text
    job_id = _insert_job(
        data / "jobs.db",
        source_name="详情.wav",
        created_at="2026-04-01T00:00:00.000000Z",
        status="done",
    )
    detail = http.get(f"/jobs/{job_id}/view")
    missing = http.get("/jobs/not-a-real-id/view")
    assert detail.status_code == 200
    assert "wrap-workbench" not in detail.text
    assert 'class="wrap"' in detail.text
    assert missing.status_code == 404
    assert "wrap-workbench" not in missing.text
    css = http.get("/static/style.css")
    assert css.status_code == 200
    assert re.search(r"\.wrap\s*\{[^}]*max-width:\s*720px;", css.text)
    assert re.search(
        r"\.wrap\.wrap-workbench\s*\{[^}]*max-width:\s*1200px;",
        css.text,
    )
    workbench_wrap = re.search(r"\.wrap\.wrap-workbench\s*\{([^}]*)\}", css.text)
    assert workbench_wrap is not None
    assert "padding-left: var(--pad);" in workbench_wrap.group(1)
    assert "padding-right: var(--pad);" in workbench_wrap.group(1)
    detail_wrap = re.search(r"\.wrap\s*\{([^}]*)\}", css.text)
    assert detail_wrap is not None
    assert "padding: 16px;" in detail_wrap.group(1)
    workbench = re.search(r"\.workbench\s*\{([^}]*)\}", css.text)
    assert workbench is not None
    assert "translateX" not in workbench.group(1)
    assert "1200px" not in workbench.group(1)


def _brace_body(text: str, open_at: int) -> str:
    assert text[open_at] == "{"
    depth = 0
    for index in range(open_at, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[open_at + 1 : index]
    raise AssertionError("unclosed brace")


def test_workbench_padding_is_16_below_860(client: tuple[TestClient, Path]) -> None:
    """860px 以下工作台左右 padding 回到 16。桌面端仍用 var(--pad)。

    源码对账理由见模块头。
    """
    css = _static_text(client, "style.css")
    header = "@media (max-width: 860px)"
    start = css.find(header)
    assert start != -1
    assert css.find(header, start + len(header)) == -1
    open_at = css.find("{", start)
    assert open_at != -1
    media = _brace_body(css, open_at)
    rule_at = media.find(".wrap.wrap-workbench")
    assert rule_at != -1
    rule_open = media.find("{", rule_at)
    assert rule_open != -1
    rule = _brace_body(media, rule_open)
    assert "padding-left: 16px;" in rule
    assert "padding-right: 16px;" in rule
    desktop = re.search(r"\.wrap\.wrap-workbench\s*\{([^}]*)\}", css[:start])
    assert desktop is not None
    assert "padding-left: var(--pad);" in desktop.group(1)
    assert "padding-right: var(--pad);" in desktop.group(1)


def test_workbench_tab_order_matches_visual_order(
    client: tuple[TestClient, Path],
) -> None:
    """失败条、输入卡、表格按源码顺序排列，Tab 顺序与画面一致。

    源码对账理由见模块头。
    """
    http, data = client
    db = data / "jobs.db"
    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _insert_job(
        db,
        source_name="失败.wav",
        created_at=created,
        status="error",
        error="引擎失败",
        finished_at=created,
        minutes_status="error",
    )
    page = http.get("/")
    assert page.status_code == 200
    html = page.text
    live_at = html.find('id="live"')
    banner_at = html.find("重新上传失败文件")
    url_at = html.find('name="url"')
    submit_at = html.find(">开始转写<")
    open_at = html.find(">打开</a>")
    composer_at = html.find('class="composer"')
    recent_at = html.find('class="recent"')
    assert live_at != -1
    assert live_at < banner_at < url_at < submit_at < open_at
    assert live_at < composer_at < recent_at
    css = http.get("/static/style.css")
    assert css.status_code == 200
    section = css.text.split("/* workbench */", 1)[1]
    assert re.search(r"(?<![\w-])order\s*:", section) is None
    assert "display: contents" not in section


def test_workbench_caps_recent_jobs_at_100(client: tuple[TestClient, Path]) -> None:
    http, data = client
    db = data / "jobs.db"
    base = datetime(2026, 5, 1, tzinfo=UTC)
    for index in range(101):
        stamp = (base + timedelta(seconds=index)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        _insert_job(db, source_name=f"clip-{index:03d}.wav", created_at=stamp, duration=1)
    page = http.get("/")
    assert page.status_code == 200
    assert "clip-100.wav" in page.text
    assert "clip-001.wav" in page.text
    assert "clip-000.wav" not in page.text
    assert "共 101 条，仅显示最近 100 条" in page.text


def test_workbench_escapes_truncated_failure_and_shows_banner(
    client: tuple[TestClient, Path],
) -> None:
    http, data = client
    db = data / "jobs.db"
    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    raw = "<b>坏</b>" + ("乙" * 55)
    assert len(raw) == 63
    _insert_job(
        db,
        source_name="坏文件.wav",
        created_at=created,
        status="error",
        error=raw,
        finished_at=created,
        minutes_status="error",
    )
    page = http.get("/")
    assert page.status_code == 200
    assert 'id="fail-slot"' in page.text
    match = re.search(r'<div\b[^>]*\bid=["\']fail-slot["\'][^>]*>', page.text)
    assert match is not None, "未找到 id=fail-slot 的插槽 div"
    tag = match.group(0)
    assert re.search(r'\bclass=["\'][^"\']*fail-slot', tag)
    assert not re.search(r"\bhidden\b", tag)
    assert "<b>坏</b>" not in page.text
    shown = "&lt;b&gt;坏&lt;/b&gt;" + ("乙" * 51) + "…"
    assert shown in page.text
    assert "乙" * 52 not in page.text
    assert "乙" * 55 not in page.text
    assert "1 条转写失败：坏文件.wav" in page.text
    assert "重新上传失败文件" in page.text


def test_workbench_minutes_marks_and_running_label(tmp_path: Path) -> None:
    db = tmp_path / "marks.db"
    rows = [
        ("2026-06-01T00:00:01.000000Z", "done", "done", "有纪要.wav", "已完成", "有纪要"),
        ("2026-06-01T00:00:02.000000Z", "done", "running", "生成中.wav", "已完成", "生成中"),
        ("2026-06-01T00:00:03.000000Z", "done", "error", "纪要失败.wav", "已完成", "纪要失败"),
        ("2026-06-01T00:00:04.000000Z", "queued", "pending", "排队.wav", "排队", "—"),
        ("2026-06-01T00:00:05.000000Z", "running", "pending", "处理中.wav", "处理中", "—"),
    ]
    for created_at, status, minutes_status, name, _label, _mark in rows:
        _insert_job(
            db,
            source_name=name,
            created_at=created_at,
            status=status,
            minutes_status=minutes_status,
        )
    listed = service.list_jobs_for_page(JobStore(db))
    assert listed["total"] == 5
    by_name = {card["source_name"]: card for card in listed["jobs"]}
    for _created_at, _status, _minutes_status, name, label, mark in rows:
        assert by_name[name]["status_text"] == label
        assert by_name[name]["minutes_mark"] == mark
        assert len(by_name[name]["error_short"]) <= 60


def test_detail_page_has_no_usage_or_poll_marker(client: tuple[TestClient, Path]) -> None:
    http, data = client
    db = data / "jobs.db"
    done_id = _insert_job(
        db,
        source_name="keep.wav",
        created_at="2026-04-01T00:00:00.000000Z",
        status="done",
    )
    running_id = _insert_job(
        db,
        source_name="wait.wav",
        created_at="2026-04-01T00:00:01.000000Z",
        status="running",
        minutes_status="pending",
    )
    done = http.get(f"/jobs/{done_id}/view")
    queued = http.get(f"/jobs/{running_id}/view")
    assert done.status_code == 200
    assert "keep.wav" in done.text
    assert 'id="usage"' not in done.text
    assert "data-live-poll" not in done.text
    assert 'http-equiv="refresh"' not in done.text
    assert "data-theme-toggle" in done.text
    assert queued.status_code == 200
    assert 'http-equiv="refresh"' in queued.text
    assert 'content="5"' in queued.text
    assert "data-live-poll" not in queued.text
    assert 'id="usage"' not in queued.text
    missing = http.get("/jobs/not-a-real-id/view")
    assert missing.status_code == 404
    assert 'id="usage"' not in missing.text


def test_workbench_css_type_scale_and_metrics(client: tuple[TestClient, Path]) -> None:
    """工作台字号和行高只对 style.css 文本。

    源码对账理由见模块头。
    """
    css = _static_text(client, "style.css")
    marker = "/* workbench */"
    assert marker in css
    section = css.split(marker, 1)[1]
    sizes = set(re.findall(r"font-size:\s*([^;]+);", section))
    assert sizes <= {"12px", "14px", "16px", "20px", "24px", "32px"}
    assert sizes
    assert "height: 52px;" in section
    assert "height: 64px;" in section
    assert "font-size: 24px;" in section
    assert "font-size: 20px;" in section
    assert "border-radius: 16px;" in section
    assert "860px" in section
    # C 节新布局：行1 等宽 1fr 1fr，行2/行3 整行（design-tokens.md 已同步改写）。
    assert "7fr 5fr" not in section
    assert ".workbench .composer-row1 {" in section
    assert "grid-template-columns: 1fr 1fr;" in section
    assert "http://" not in section
    assert "https://" not in section
    assert "@import" not in section
    assert re.findall(r"(--[A-Za-z0-9-]+)\s*:", section) == []


def test_f2_f3_detail_player_and_segments_sync(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    http, data = client
    job_id = _insert_job(
        data / "jobs.db",
        source_name="has_audio.wav",
        created_at="2026-04-01T00:00:00.000000Z",
        status="done",
    )
    job_dir = data / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "audio.wav").write_bytes(b"RIFFWAVEDATA")
    segments_payload = [
        {"start": 0.0, "end": 2.5, "speaker": "A", "text": "第一段话"},
        {"start": 2.5, "end": 5.0, "speaker": "B", "text": "第二段话"},
    ]
    (job_dir / "segments.json").write_text(json.dumps(segments_payload), encoding="utf-8")

    resp = http.get(f"/jobs/{job_id}/view")
    assert resp.status_code == 200
    html = resp.text
    # 播放器元素落地 (F2)
    assert 'id="player"' in html
    assert f'src="/jobs/{job_id}/audio"' in html
    assert 'id="play"' in html
    assert 'id="speed"' in html
    assert 'id="wave"' in html
    assert 'id="head"' in html
    assert 'id="player-time"' in html
    # 逐字稿 data 属性携带数值 (F3)
    assert 'data-start="0.0"' in html
    assert 'data-end="2.5"' in html
    assert 'data-seek="0.0"' in html
    assert 'data-start="2.5"' in html
    assert 'data-end="5.0"' in html
    assert '第一段话' in html
    assert '第二段话' in html


def test_non_numeric_segment_bounds_render_as_zero(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    """start/end 为非数字字符串时详情页仍 200，按缺失口径记成 0。"""
    http, data = client
    job_id = _insert_job(
        data / "jobs.db",
        source_name="bad-bounds.wav",
        created_at="2026-04-01T00:00:00.000000Z",
        status="done",
    )
    job_dir = data / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "segments.json").write_text(
        json.dumps(
            [
                {"start": "not-a-number", "end": "F", "speaker": "A", "text": "坏时间"},
                {"start": 1.5, "end": 3.0, "speaker": None, "text": "正常段"},
            ]
        ),
        encoding="utf-8",
    )

    resp = http.get(f"/jobs/{job_id}/view")
    assert resp.status_code == 200
    assert 'data-start="0.0"' in resp.text
    assert 'data-end="0.0"' in resp.text
    assert "坏时间" in resp.text
    assert 'data-start="1.5"' in resp.text
    assert 'data-end="3.0"' in resp.text


def test_non_finite_segment_bounds_render_as_zero(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    """start/end 为 nan、inf、-inf 时按缺失口径记 0，不再透传非有限值。"""
    http, data = client
    job_id = _insert_job(
        data / "jobs.db",
        source_name="nonfinite-bounds.wav",
        created_at="2026-04-01T00:00:00.000000Z",
        status="done",
    )
    job_dir = data / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "segments.json").write_text(
        json.dumps(
            [
                {"start": "nan", "end": "inf", "speaker": "A", "text": "非有限"},
                {"start": "-inf", "end": 4.0, "speaker": None, "text": "负无穷起点"},
                {"start": 1.5, "end": 3.0, "speaker": None, "text": "正常段"},
            ]
        ),
        encoding="utf-8",
    )

    resp = http.get(f"/jobs/{job_id}/view")
    assert resp.status_code == 200
    assert "非有限" in resp.text
    assert "负无穷起点" in resp.text
    assert 'data-start="nan"' not in resp.text
    assert 'data-end="inf"' not in resp.text
    assert 'data-start="-inf"' not in resp.text
    assert 'data-start="0.0"' in resp.text
    assert 'data-end="0.0"' in resp.text
    assert 'data-end="4.0"' in resp.text
    assert "正常段" in resp.text
    assert 'data-start="1.5"' in resp.text
    assert 'data-end="3.0"' in resp.text


def test_f4_detail_player_empty_state(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    http, data = client
    # 1. 未完成任务：占位态，无可交互播放器
    j_running = _insert_job(
        data / "jobs.db",
        source_name="running.wav",
        created_at="2026-04-01T00:00:00.000000Z",
        status="running",
    )
    resp_running = http.get(f"/jobs/{j_running}/view")
    assert resp_running.status_code == 200
    assert 'class="card player-empty"' in resp_running.text
    assert "任务尚未完成" in resp_running.text
    assert 'id="player"' not in resp_running.text
    assert 'id="play"' not in resp_running.text
    assert 'id="wave"' not in resp_running.text

    # 2. 已完成但缺失音频文件任务：明确占位态
    j_missing = _insert_job(
        data / "jobs.db",
        source_name="missing_audio.wav",
        created_at="2026-04-01T00:00:00.000000Z",
        status="done",
    )
    resp_missing = http.get(f"/jobs/{j_missing}/view")
    assert resp_missing.status_code == 200
    assert 'class="card player-empty"' in resp_missing.text
    assert "音频文件不存在" in resp_missing.text
    assert 'id="player"' not in resp_missing.text


def test_detail_player_empty_failed_and_pending_copy(
    client: tuple[TestClient, Path],
) -> None:
    """failed 显示失败文案；pending 仍显示尚未完成。"""
    http, data = client
    j_failed = _insert_job(
        data / "jobs.db",
        source_name="failed.wav",
        created_at="2026-04-01T00:00:00.000000Z",
        status="failed",
    )
    resp_failed = http.get(f"/jobs/{j_failed}/view")
    assert resp_failed.status_code == 200
    assert 'class="card player-empty"' in resp_failed.text
    assert "任务已失败，暂无可播放音频" in resp_failed.text
    assert "任务尚未完成" not in resp_failed.text

    j_pending = _insert_job(
        data / "jobs.db",
        source_name="pending.wav",
        created_at="2026-04-01T00:00:00.000000Z",
        status="pending",
    )
    resp_pending = http.get(f"/jobs/{j_pending}/view")
    assert resp_pending.status_code == 200
    assert "任务尚未完成，暂无可播放音频" in resp_pending.text
    assert "任务已失败" not in resp_pending.text


def test_i1_detail_dual_source_and_single_source(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    http, data = client

    # 1. 原始文件与 wav 并存（两者非同一文件）：渲染两个 source，原始在前，?fmt=wav 在后
    j_dual = _insert_job(
        data / "jobs.db",
        source_name="meeting.mp3",
        created_at="2026-04-01T00:00:00.000000Z",
        status="done",
    )
    j_dual_dir = data / "jobs" / j_dual
    j_dual_dir.mkdir(parents=True, exist_ok=True)
    (j_dual_dir / "source.mp3").write_bytes(b"MP3DATA")
    (j_dual_dir / "audio.wav").write_bytes(b"WAVDATA")

    resp_dual = http.get(f"/jobs/{j_dual}/view")
    assert resp_dual.status_code == 200
    html_dual = resp_dual.text
    src_orig = f'src="/jobs/{j_dual}/audio"'
    src_wav = f'src="/jobs/{j_dual}/audio?fmt=wav"'
    assert src_orig in html_dual
    assert src_wav in html_dual
    idx_orig = html_dual.find(src_orig)
    idx_wav = html_dual.find(src_wav)
    assert idx_orig < idx_wav, "两个 source 顺序必须为原始在前、fmt=wav 在后"

    # 2. 只有 wav 时维持单 source
    j_single = _insert_job(
        data / "jobs.db",
        source_name="single.wav",
        created_at="2026-04-01T00:00:00.000000Z",
        status="done",
    )
    j_single_dir = data / "jobs" / j_single
    j_single_dir.mkdir(parents=True, exist_ok=True)
    (j_single_dir / "audio.wav").write_bytes(b"WAVDATA")

    resp_single = http.get(f"/jobs/{j_single}/view")
    assert resp_single.status_code == 200
    html_single = resp_single.text
    assert f'src="/jobs/{j_single}/audio"' in html_single
    assert f'src="/jobs/{j_single}/audio?fmt=wav"' not in html_single
    player_html = html_single.split('id="player"')[1].split('</div>')[0]
    assert player_html.count("<source ") == 1


