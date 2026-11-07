"""A2 失败原因与展示名脱敏测试。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from app.jobs import JobStore
from app.main import create_app
from app.pipeline import PipelineError, _tail, run_job, summarize_error
from app.redact import mask_source_name, mask_url, mask_urls, redact_sensitive
from app.service import (
    combined_payload,
    create_url_job,
    job_detail_for_page,
    list_jobs_for_page,
    minutes_payload,
    public_job,
    recent_failure_banner,
)
from fastapi.testclient import TestClient


def test_mask_url_query_values_and_userinfo() -> None:
    # 关键词 query
    assert mask_url("https://example.com/audio.mp3?token=SECRET") == "https://example.com/audio.mp3?token=***"
    # 非关键词 query
    assert mask_url("https://example.com/video?sig=XYZ") == "https://example.com/video?sig=***"
    # 多参数
    multi = mask_url("https://example.com/path?a=1&b=2&sig=XYZ&token=SECRET")
    assert multi == "https://example.com/path?a=***&b=***&sig=***&token=***"
    # userinfo 掩码
    userinfo = mask_url("https://alice:pass123@example.com:8080/audio.mp3?sig=XYZ")
    assert userinfo == "https://***@example.com:8080/audio.mp3?sig=***"
    # 尾部标点保留
    assert mask_url("https://example.com/test?sig=XYZ.") == "https://example.com/test?sig=***."
    assert mask_urls("https://example.com/test?sig=XYZ，") == "https://example.com/test?sig=***，"


def test_mask_url_fragment_key_value() -> None:
    # S3: 对 fragment 中的 k=v 也掩码
    assert (
        mask_url("https://example.com/path#token=SECRET&sig=XYZ")
        == "https://example.com/path#token=***&sig=***"
    )
    assert mask_url("https://example.com/path#token=SECRET") == "https://example.com/path#token=***"
    assert mask_url("https://example.com/path#section-1") == "https://example.com/path#section-1"
    assert (
        mask_url("https://example.com/path?a=1&b=2#token=SECRET&k=v")
        == "https://example.com/path?a=***&b=***#token=***&k=***"
    )


def test_mask_url_urlsplit_exception_fallback() -> None:
    # S4: urlsplit 抛异常时整段替换为 <url>，不许原样返回
    bad_url = "http://[::1/path?token=SECRET"
    assert mask_url(bad_url) == "<url>"
    assert mask_url("http://[::1/path?token=SECRET.") == "<url>."
    text = "访问失败：http://[::1/path?token=SECRET，请重试"
    redacted = redact_sensitive(text)
    assert "SECRET" not in redacted
    assert "<url>" in redacted


def test_mask_source_name_forbids_long_token_re() -> None:
    # 链接路径中包含超过 20 位字母数字的段落，不能被 _LONG_TOKEN_RE 误伤改掉
    long_path_url = "https://example.com/media/abcdef0123456789abcdef0123456789/stream.mp3?token=SECRET"
    masked = mask_source_name(long_path_url)
    assert "abcdef0123456789abcdef0123456789" in masked
    assert "[redacted]" not in masked
    assert "token=***" in masked
    assert "SECRET" not in masked


def test_redact_sensitive_order_and_rules() -> None:
    # URL 规则先行，抓取非关键词 query 如 sig=
    text1 = "下载失败：https://example.com/stream?sig=MY_SIG_XYZ，请检查网络"
    res1 = redact_sensitive(text1)
    assert "sig=***" in res1
    assert "MY_SIG_XYZ" not in res1

    # 既有关键词规则
    text2 = "认证报错：Authorization: Bearer my-secret-token"
    res2 = redact_sensitive(text2)
    assert "[redacted]" in res2
    assert "my-secret-token" not in res2

    # 既有密钥前缀
    secret_key = "sk-" + "1234567890abcdef123456"
    text3 = f"调用失败：{secret_key}"
    res3 = redact_sensitive(text3)
    assert "[redacted]" in res3
    assert "sk-" not in res3

    # 无前缀长串
    text4 = "错误代码：ABCDEF0123456789ABCDEF012345"
    res4 = redact_sensitive(text4)
    assert "[redacted]" in res4
    assert "ABCDEF0123456789ABCDEF012345" not in res4


def test_redact_normal_error_unmodified() -> None:
    normal_errors = [
        "音频格式不支持，找不到音频流",
        "无法读取音频时长：Invalid data found when processing input",
        "上传文件缺失",
        "音频预处理失败：ffmpeg exited with 1",
    ]
    for err in normal_errors:
        assert redact_sensitive(err) == err


def test_pipeline_tail_and_summarize_error_redaction() -> None:
    raw_tail = "ERROR: failed to open https://example.com/stream.m3u8?token=SECRET_VALUE&sig=SIG_VALUE."
    tailed = _tail(raw_tail)
    assert "SECRET_VALUE" not in tailed
    assert "SIG_VALUE" not in tailed
    assert "token=***" in tailed
    assert "sig=***" in tailed

    exc = PipelineError(
        "链接抓取失败：https://example.com/audio.mp3?token=SECRET_TOKEN&sig=SIG_TOKEN"
    )
    summarized = summarize_error(exc)
    assert "SECRET_TOKEN" not in summarized
    assert "SIG_TOKEN" not in summarized
    assert "example.com" not in summarized
    assert "/audio.mp3" not in summarized
    assert "<url>" in summarized

    # GFIX2: summarize_error 对含 URL 的异常文本：输出不含主机与路径
    exc_chat = Exception("LLM call failed at https://api.example.com/v1/chat?key=sk-xxxxx")
    sum_chat = summarize_error(exc_chat)
    assert "api.example.com" not in sum_chat
    assert "/v1/chat" not in sum_chat
    assert "sk-xxxxx" not in sum_chat
    assert "<url>" in sum_chat


def test_url_job_failure_five_places_no_secret_and_sig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """带 ?token=SECRET 与 ?sig=XYZ 的任务失败：
    库内 error、日志行、详情页、列表页、失败条五处均不见原文。
    """
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    app = create_app(data)

    target_url = "https://example.com/audio.mp3?token=SUPER_SECRET_TOKEN&sig=SUPER_SIG_XYZ"
    resolver_mock = MagicMock(return_value=["93.184.216.34"])

    status, body = create_url_job(store, data, target_url, "fast", resolver=resolver_mock)
    assert status == 200
    job_id = body["job_id"]

    # 1. 建任务时标题是「未命名N」（LZW-60 B：URL 不再作标题），原始 url 字段保持原文。
    #    标题位不含 URL，也就不会漏出 token/sig；脱敏仍靠 error 与页面读路径。
    stored_job = store.get(job_id)
    assert stored_job is not None
    assert stored_job["source_name"].startswith("未命名")
    assert "SUPER_SECRET_TOKEN" not in stored_job["source_name"]
    assert "SUPER_SIG_XYZ" not in stored_job["source_name"]
    assert "http" not in stored_job["source_name"]
    assert stored_job["url"] == target_url

    # 模拟流水线下载失败
    def fail_download(url: str, directory: Path) -> Path:
        raise PipelineError(f"链接抓取失败：yt-dlp error on {url}")

    monkeypatch.setattr("app.pipeline._download_audio", fail_download)

    claimed = store.claim_next()
    assert claimed is not None

    with caplog.at_level(logging.ERROR):
        run_job(store, data, claimed)

    # 验证 ①：库内 error 字段
    updated_job = store.get(job_id)
    assert updated_job is not None
    assert updated_job["status"] == "error"
    error_field = str(updated_job["error"])
    assert "SUPER_SECRET_TOKEN" not in error_field
    assert "SUPER_SIG_XYZ" not in error_field
    assert "example.com" not in error_field
    assert "/audio.mp3" not in error_field
    assert "<url>" in error_field

    # 验证 ②：日志行
    log_text = caplog.text
    assert "SUPER_SECRET_TOKEN" not in log_text
    assert "SUPER_SIG_XYZ" not in log_text
    assert "example.com" not in log_text
    assert "<url>" in log_text

    # 验证 ③：详情页（通过 HTTP 客户端）
    with TestClient(app) as client:
        detail_resp = client.get(f"/jobs/{job_id}/view")
        assert detail_resp.status_code == 200
        detail_html = detail_resp.text
        assert "SUPER_SECRET_TOKEN" not in detail_html
        assert "SUPER_SIG_XYZ" not in detail_html

        # 验证 ④：列表页（工作台）
        list_resp = client.get("/")
        assert list_resp.status_code == 200
        list_html = list_resp.text
        assert "SUPER_SECRET_TOKEN" not in list_html
        assert "SUPER_SIG_XYZ" not in list_html

    # 验证 ⑤：最近失败条 recent_failure_banner
    banner = recent_failure_banner(store, datetime.now(UTC))
    assert banner is not None
    assert banner["count"] >= 1
    assert "SUPER_SECRET_TOKEN" not in banner["latest_name"]
    assert "SUPER_SIG_XYZ" not in banner["latest_name"]
    assert "SUPER_SECRET_TOKEN" not in banner["latest_error"]
    assert "SUPER_SIG_XYZ" not in banner["latest_error"]


def test_historical_job_read_path_fallback(tmp_path: Path) -> None:
    """历史任务不回填改库：列表卡、详情、失败条的展示读路径对 source_name 再过一遍 URL 规则兜底。"""
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    app = create_app(data)

    # 直接往库中写入未脱敏的原始 source_name 与 error / minutes_error
    raw_source = "https://example.com/old_audio/abc12345678901234567890.mp3?token=OLD_SECRET&sig=OLD_SIG"
    raw_error = "历史失败：https://api.example.com/v1/chat?key=OLD_SECRET"
    raw_minutes_error = "历史纪要失败：https://api.example.com/v1/chat?key=OLD_SECRET"
    job_id = "0123456789abcdef0123456789abcdef"
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    conn = store._connect()
    try:
        conn.execute(
            """
            INSERT INTO jobs (
                id, status, tier, source_name, source_kind, source_path, url,
                created_at, finished_at, error, minutes_error
            )
            VALUES (?, 'error', 'fast', ?, 'url', NULL, ?, ?, ?, ?, ?)
            """,
            (job_id, raw_source, raw_source, now_str, now_str, raw_error, raw_minutes_error),
        )
        conn.commit()
    finally:
        conn.close()

    # 验证读时兜底：
    # 1. list_jobs_for_page
    listed = list_jobs_for_page(store)
    cards = [c for c in listed["jobs"] if c["id"] == job_id]
    assert len(cards) == 1
    card_source = cards[0]["source_name"]
    assert "OLD_SECRET" not in card_source
    assert "OLD_SIG" not in card_source
    assert "token=***" in card_source
    assert "sig=***" in card_source
    assert "abc12345678901234567890" in card_source  # 长路径段不被误删
    card_error = cards[0]["error_short"]
    assert "OLD_SECRET" not in card_error
    assert "api.example.com" not in card_error
    assert "/v1/chat" not in card_error
    assert "<url>" in card_error

    # 2. job_detail_for_page
    detail = job_detail_for_page(store, data, job_id)
    assert detail is not None
    assert "OLD_SECRET" not in detail["source_name"]
    assert "OLD_SIG" not in detail["source_name"]
    assert "token=***" in detail["source_name"]
    assert "sig=***" in detail["source_name"]
    assert "abc12345678901234567890" in detail["source_name"]
    detail_min_err = str(detail["minutes_error"])
    assert "OLD_SECRET" not in detail_min_err
    assert "api.example.com" not in detail_min_err
    assert "/v1/chat" not in detail_min_err
    assert "<url>" in detail_min_err

    # 3. recent_failure_banner
    banner = recent_failure_banner(store, datetime.now(UTC))
    assert banner is not None
    assert "OLD_SECRET" not in banner["latest_name"]
    assert "OLD_SIG" not in banner["latest_name"]
    assert "token=***" in banner["latest_name"]
    assert "sig=***" in banner["latest_name"]
    banner_err = str(banner["latest_error"])
    assert "OLD_SECRET" not in banner_err
    assert "api.example.com" not in banner_err
    assert "/v1/chat" not in banner_err
    assert "<url>" in banner_err

    # 4. public_job (API)
    pub = public_job(store, job_id)
    assert pub is not None
    assert "OLD_SECRET" not in pub["source_name"]
    assert "OLD_SIG" not in pub["source_name"]
    assert "token=***" in pub["source_name"]

    # 5. HTTP 端点
    with TestClient(app) as client:
        detail_html = client.get(f"/jobs/{job_id}/view").text
        assert "OLD_SECRET" not in detail_html
        assert "OLD_SIG" not in detail_html
        assert "token=***" in detail_html

        list_html = client.get("/").text
        assert "OLD_SECRET" not in list_html
        assert "OLD_SIG" not in list_html
        assert "token=***" in list_html


def test_historical_read_paths_transcript_and_api_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """历史 source_name 写入逐字稿、历史 error 经 API 与任务卡读出时零命中。"""
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    app = create_app(data)
    raw_source = (
        "https://cdn.example.com/old_audio/abc12345678901234567890.mp3"
        "?token=OLD_SECRET&sig=OLD_SIG"
    )
    raw_token = "sk-" + "OLDSECRETKEYVALUE"
    raw_error = f"历史失败：https://api.example.com/v1/chat?key=OLD_SECRET {raw_token}"
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    error_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    transcript_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    source_file = tmp_path / "source.bin"
    source_file.write_bytes(b"audio")

    conn = store._connect()
    try:
        conn.execute(
            """
            INSERT INTO jobs (
                id, status, tier, source_name, source_kind, source_path, url,
                created_at, finished_at, error
            )
            VALUES (?, 'error', 'fast', ?, 'url', NULL, ?, ?, ?, ?)
            """,
            (error_id, raw_source, raw_source, now_str, now_str, raw_error),
        )
        conn.execute(
            """
            INSERT INTO jobs (
                id, status, tier, source_name, source_kind, source_path, url,
                created_at
            )
            VALUES (?, 'queued', 'fast', ?, 'upload', ?, NULL, ?)
            """,
            (transcript_id, raw_source, str(source_file), now_str),
        )
        conn.commit()
    finally:
        conn.close()

    pub = public_job(store, error_id)
    assert pub is not None
    assert "OLD_SECRET" not in str(pub["error"])
    assert "OLDSECRETKEYVALUE" not in str(pub["error"])
    assert "api.example.com" not in str(pub["error"])
    assert "<url>" in str(pub["error"])

    detail = job_detail_for_page(store, data, error_id)
    assert detail is not None
    assert "OLD_SECRET" not in str(detail["error"])
    assert "api.example.com" not in str(detail["error"])
    assert "<url>" in str(detail["error"])

    def _fake_wav(_source: Path, destination: Path) -> None:
        destination.write_bytes(b"RIFF")

    monkeypatch.setattr("app.pipeline._to_wav", _fake_wav)
    monkeypatch.setattr("app.pipeline.probe_duration", lambda _path: 1.0)
    monkeypatch.setattr(
        "app.pipeline.transcribe",
        lambda _path, _tier, **_kw: [
            {"start": 0.0, "end": 1.0, "speaker": None, "text": "你好"}
        ],
    )
    from app.pipeline import _run

    claimed = store.get(transcript_id)
    assert claimed is not None
    _run(store, data, claimed)
    transcript = (data / "jobs" / transcript_id / "transcript.md").read_text(encoding="utf-8")
    assert "OLD_SECRET" not in transcript
    assert "OLD_SIG" not in transcript
    assert "token=***" in transcript
    assert "sig=***" in transcript
    assert "abc12345678901234567890" in transcript

    (data / "jobs" / transcript_id / "minutes.md").write_text("# 纪要\n", encoding="utf-8")
    conn = store._connect()
    try:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'done', finished_at = ?, minutes_status = 'done'
            WHERE id = ?
            """,
            (now_str, transcript_id),
        )
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as client:
        api_error = client.get(f"/jobs/{error_id}").json()["error"]
        assert "OLD_SECRET" not in api_error
        assert "OLDSECRETKEYVALUE" not in api_error
        assert "<url>" in api_error
        downloaded = client.get(f"/jobs/{transcript_id}/transcript").text
        combined = client.get(f"/jobs/{transcript_id}/combined").text
        assert "OLD_SECRET" not in downloaded
        assert "token=***" in downloaded
        assert "OLD_SECRET" not in combined
        assert "token=***" in combined


def test_historical_minutes_error_download_payloads_redacted(tmp_path: Path) -> None:
    """纪要失败的历史 minutes_error 经 minutes 与 combined 的 404 读出时零命中。"""
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    raw_token = "sk-" + "OLDSECRETKEYVALUE"
    raw_minutes_error = (
        f"历史纪要失败：https://api.example.com/v1/chat?key=OLD_SECRET {raw_token}"
    )
    job_id = "cccccccccccccccccccccccccccccccc"
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    conn = store._connect()
    try:
        conn.execute(
            """
            INSERT INTO jobs (
                id, status, tier, source_name, source_kind, source_path, url,
                created_at, finished_at, minutes_status, minutes_error
            )
            VALUES (?, 'done', 'fast', 'old.wav', 'upload', NULL, NULL, ?, ?, 'error', ?)
            """,
            (job_id, now_str, now_str, raw_minutes_error),
        )
        conn.commit()
    finally:
        conn.close()

    for payload_of in (minutes_payload, combined_payload):
        status, body = payload_of(store, data, job_id)
        assert status == 404
        assert isinstance(body, dict)
        text = str(body["error"])
        assert "OLD_SECRET" not in text
        assert "OLDSECRETKEYVALUE" not in text
        assert "api.example.com" not in text
        assert "/v1/chat" not in text
        assert "<url>" in text
        assert "[redacted]" in text
