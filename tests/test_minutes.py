from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.config import (
    LLM_PROVIDERS,
    LLM_TIMEOUT_SECONDS_DEFAULT,
    MINUTES_CHUNK_CHARS_DEFAULT,
    llm_settings,
    llm_timeout_seconds,
    minutes_chunk_chars,
)
from app.engines import Segment
from app.jobs import JobStore
from app.main import create_app
from app.minutes import (
    MinutesError,
    normalize_minutes_headings,
    render_minutes_html,
    summarize,
)
from app.pipeline import run_minutes
from fastapi.testclient import TestClient
from tests.support import post_file, synth_wav, wait_job, wait_minutes

ROOT = Path(__file__).resolve().parent.parent

_SAMPLE_SEGMENTS: list[Segment] = [
    {"start": 0.0, "end": 1.2, "speaker": "说话人1", "text": "你好"},
    {"start": 1.2, "end": 2.5, "speaker": "说话人2", "text": "大家好"},
]


_MARKDOWN_SAMPLE = """# 纪要

## 议题

**决定**去开会

- 一项
- [ ] 待办（负责人：待定）
"""

_MARKDOWN_HTML = """<h1>纪要</h1>
<h2>议题</h2>
<p><strong>决定</strong>去开会</p>
<ul>
<li>一项</li>
<li>[ ] 待办（负责人：待定）</li>
</ul>"""

_VARIANT_HEADING_MINUTES = """# 纪要

## 一句话总结
会议讨论了待办事项的推进方式。

## 议题
- 待办事项清单

## 决策
无

## 待办事项
- [ ] 跟进（负责人：待定）

## 未决事项
无
"""


def test_normalize_minutes_headings_canonicalizes_h2_variants() -> None:
    normalized = normalize_minutes_headings(_VARIANT_HEADING_MINUTES)
    headings = [line for line in normalized.splitlines() if line.startswith("##")]
    assert headings == [
        "## 一句话摘要",
        "## 议题",
        "## 决定",
        "## 待办",
        "## 未定事项",
    ]
    assert "会议讨论了待办事项的推进方式。" in normalized
    assert "- 待办事项清单" in normalized
    assert normalize_minutes_headings("## 待定事项\n") == "## 未定事项\n"
    assert normalize_minutes_headings(normalized) == normalized


def test_render_minutes_html_escapes_raw_script() -> None:
    rendered = render_minutes_html("前文 <script>alert(1)</script> 后文")
    assert "<script" not in rendered.lower()
    assert "</script>" not in rendered.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered


def test_render_minutes_html_keeps_markdown_structure() -> None:
    assert render_minutes_html(_MARKDOWN_SAMPLE) == _MARKDOWN_HTML


@pytest.mark.parametrize("raw", ["abc", "0", "-5"])
def test_minutes_chunk_chars_invalid_falls_back(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("TRANSCRIBER_MINUTES_CHUNK_CHARS", raw)
    assert minutes_chunk_chars() == MINUTES_CHUNK_CHARS_DEFAULT


def test_llm_timeout_seconds_unset_is_600(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSCRIBER_LLM_TIMEOUT_S", raising=False)
    assert LLM_TIMEOUT_SECONDS_DEFAULT == 600
    assert llm_timeout_seconds() == 600


def test_llm_timeout_seconds_reads_900(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIBER_LLM_TIMEOUT_S", "900")
    assert llm_timeout_seconds() == 900


@pytest.mark.parametrize("raw", ["abc", "0", "-5"])
def test_llm_timeout_seconds_invalid_falls_back(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("TRANSCRIBER_LLM_TIMEOUT_S", raw)
    assert llm_timeout_seconds() == 600


def test_c1_provider_mapping_lives_only_in_config() -> None:
    minutes = (ROOT / "app" / "minutes.py").read_text(encoding="utf-8")
    pipeline = (ROOT / "app" / "pipeline.py").read_text(encoding="utf-8")
    blob = minutes + "\n" + pipeline
    assert '"nous"' not in minutes
    assert '"ollama"' not in minutes
    assert '"spark"' not in minutes
    # 带引号的通道标识形式才算字面禁令：单双引号两种形态都不许出现。
    for name in LLM_PROVIDERS:
        assert f'"{name}"' not in blob
        assert f"'{name}'" not in blob
    # model/base_url 的「不出现在 minutes/pipeline」只对非空值执行；
    # custom 的空默认值跳过本项，改断言其在通道表中确为空字符串。
    for name, settings in LLM_PROVIDERS.items():
        for key in ("model", "base_url"):
            value = str(settings[key])
            if value:
                assert value not in blob, f"{name}.{key} 不得出现在 minutes/pipeline"
            else:
                assert value == "", f"{name}.{key} 只允许以空字符串作为默认值"
    assert "nous" in LLM_PROVIDERS
    assert "ollama" in LLM_PROVIDERS
    assert "spark" in LLM_PROVIDERS
    assert "custom" in LLM_PROVIDERS
    # custom 与预置三通道并列，空默认值与不要求密钥是它的定版形状。
    custom_cfg = LLM_PROVIDERS["custom"]
    assert custom_cfg["kind"] == "openai_compat"
    assert custom_cfg["api_key_env"] == "TRANSCRIBER_LLM_API_KEY"
    assert custom_cfg["require_api_key"] is False
    assert custom_cfg["model"] == ""
    assert custom_cfg["base_url"] == ""
    # 预置三通道逐项零漂移。
    assert LLM_PROVIDERS["nous"]["model"] == "stepfun/step-3.7-flash:free"
    assert LLM_PROVIDERS["nous"]["base_url"] == "https://inference-api.nousresearch.com/v1"
    assert LLM_PROVIDERS["nous"]["require_api_key"] is True
    assert LLM_PROVIDERS["ollama"]["model"] == "qwen2.5:7b"
    assert LLM_PROVIDERS["ollama"]["base_url"] == "http://127.0.0.1:11434/v1"
    assert LLM_PROVIDERS["ollama"]["require_api_key"] is False
    assert LLM_PROVIDERS["spark"]["model"] == "spark-x2.6"
    assert LLM_PROVIDERS["spark"]["base_url"] == "https://maas-api.cn-huabei-1.xf-yun.com/v2"
    assert LLM_PROVIDERS["spark"]["require_api_key"] is True


def test_b4_spark_provider_and_missing_key_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. 验证 provider 表中 spark 条目属性
    assert "spark" in LLM_PROVIDERS
    spark_cfg = LLM_PROVIDERS["spark"]
    assert spark_cfg["kind"] == "openai_compat"
    assert spark_cfg["model"] == "spark-x2.6"
    assert spark_cfg["base_url"] == "https://maas-api.cn-huabei-1.xf-yun.com/v2"
    assert spark_cfg["require_api_key"] is True

    # 2. 模拟切到 spark 且未配 key，验证 MinutesError 报错文案
    monkeypatch.setenv("TRANSCRIBER_LLM_PROVIDER", "spark")
    monkeypatch.setenv("TRANSCRIBER_STUB_LLM", "0")
    monkeypatch.delenv("TRANSCRIBER_LLM_API_KEY", raising=False)
    monkeypatch.setattr("app.config.load_secret_file", lambda: None)

    with pytest.raises(MinutesError) as exc_info:
        summarize(_SAMPLE_SEGMENTS)
    assert "未配置纪要接口密钥，无法生成纪要" in str(exc_info.value)



def test_c4_prompt_has_speaker_timestamp_and_five_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRANSCRIBER_STUB_LLM", "1")
    captured: list[str] = []

    def fake_complete(prompt: str) -> str:
        captured.append(prompt)
        return "# 纪要\n\n## 一句话摘要\n测\n"

    monkeypatch.setattr("app.minutes._complete", fake_complete)
    summarize(_SAMPLE_SEGMENTS)
    assert captured
    prompt = captured[0]
    assert "说话人" in prompt
    assert "[00:00]" in prompt
    assert "一句话摘要" in prompt
    assert "议题" in prompt
    assert "决定" in prompt
    assert "待办" in prompt
    assert "未定事项" in prompt


def test_c5_long_transcript_map_reduce_call_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIBER_STUB_LLM", "1")
    monkeypatch.setenv("TRANSCRIBER_MINUTES_CHUNK_CHARS", "80")
    calls: list[str] = []

    def fake_complete(prompt: str) -> str:
        calls.append(prompt)
        if "各段摘要" in prompt:
            return "# 纪要\n\n## 一句话摘要\n合并结果\n"
        return "片段摘要"

    monkeypatch.setattr("app.minutes._complete", fake_complete)
    segments: list[Segment] = [
        {
            "start": float(i),
            "end": float(i) + 0.8,
            "speaker": "说话人1",
            "text": f"这是第{i}段用于撑长逐字稿的内容内容内容",
        }
        for i in range(12)
    ]
    result = summarize(segments)
    assert len(calls) >= 3
    assert result.strip()
    assert "合并结果" in result


def test_f2_stored_minutes_error_strips_url_and_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """含完整 URL 与密钥的异常落库后，主机、路径和密钥片段都不再出现。"""
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    job_id = store.create_job(
        tier="fast",
        source_name="talk.wav",
        source_kind="upload",
        source_path=None,
        url=None,
    )
    assert store.claim_next() is not None
    store.mark_done(job_id)
    directory = data / "jobs" / job_id
    directory.mkdir(parents=True)
    (directory / "segments.json").write_text("[]", encoding="utf-8")

    def explode(_segments: list[Segment]) -> str:
        raise MinutesError(
            "纪要模型请求失败：https://api.example.com/v1/chat?key=sk-xxxxx"
            "，token=s3cr3tVALUE"
            "，Authorization: Bearer sk-xxxxx"
            "，追踪号 ZZZZZZZZZZZZZZZZZZZZ"
            "，请稍后重试"
        )

    monkeypatch.setattr("app.pipeline.summarize", explode)
    run_minutes(store, data, {"id": job_id})
    stored = store.get(job_id)
    assert stored is not None
    message = str(stored["minutes_error"])
    assert stored["minutes_status"] == "error"
    assert "api.example.com" not in message
    assert "/v1/chat" not in message
    assert "sk-xxxxx" not in message
    assert "s3cr3tVALUE" not in message
    assert "ZZZZZZZZZZZZZZZZZZZZ" not in message
    assert "纪要模型请求失败" in message
    assert "追踪号" in message
    assert "请稍后重试" in message


def test_c10_nous_without_key_error_has_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIBER_STUB_LLM", "0")
    monkeypatch.setenv("TRANSCRIBER_STUB", "0")
    monkeypatch.setenv("TRANSCRIBER_LLM_PROVIDER", "nous")
    monkeypatch.delenv("TRANSCRIBER_LLM_API_KEY", raising=False)
    with pytest.raises(MinutesError) as caught:
        summarize(_SAMPLE_SEGMENTS)
    message = str(caught.value)
    assert "密钥" in message
    assert "sk-" not in message
    assert "Bearer" not in message
    assert "TRANSCRIBER_LLM_API_KEY=" not in message


def test_c2_minutes_generated_after_done(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    http, data = client
    wav = tmp_path / "talk.wav"
    synth_wav(wav, seconds=0.2)
    job_id = post_file(http, wav, tier="fast").json()["job_id"]
    body = wait_job(http, job_id)
    assert body["status"] == "done"
    minutes = wait_minutes(http, job_id)
    assert minutes["minutes_status"] == "done"
    assert "一句话摘要" in minutes["text"]
    assert (data / "jobs" / job_id / "minutes.md").is_file()


def test_c3_minutes_failure_isolated_then_regenerate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail = tmp_path / "llm-fail"
    fail.write_text("1", encoding="utf-8")
    monkeypatch.setenv("TRANSCRIBER_STUB", "1")
    monkeypatch.setenv("TRANSCRIBER_STUB_SLEEP", "0")
    monkeypatch.setenv("TRANSCRIBER_STUB_LLM_ERROR_PATH", str(fail))
    data = tmp_path / "data"
    wav = tmp_path / "talk.wav"
    synth_wav(wav, seconds=0.2)
    with TestClient(create_app(data)) as http:
        job_id = post_file(http, wav, tier="fast").json()["job_id"]
        body = wait_job(http, job_id)
        assert body["status"] == "done"
        minutes = wait_minutes(http, job_id)
        assert minutes.get("minutes_status") == "error"
        assert "stub LLM" in str(minutes.get("error"))
        public = http.get(f"/jobs/{job_id}").json()
        assert public["status"] == "done"
        fail.unlink()
        regen = http.post(f"/jobs/{job_id}/minutes/regenerate")
        assert regen.status_code == 200
        again = wait_minutes(http, job_id)
        assert again["minutes_status"] == "done"
        assert "纪要" in again["text"]


def test_c6_minutes_and_combined_order(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    http, _data = client
    wav = tmp_path / "talk.wav"
    synth_wav(wav, seconds=0.2)
    job_id = post_file(http, wav, tier="fast").json()["job_id"]
    assert wait_job(http, job_id)["status"] == "done"
    minutes = wait_minutes(http, job_id)
    assert minutes["minutes_status"] == "done"
    downloaded = http.get(f"/jobs/{job_id}/minutes")
    assert downloaded.status_code == 200
    assert "一句话摘要" in downloaded.text
    combined = http.get(f"/jobs/{job_id}/combined")
    assert combined.status_code == 200
    text = combined.text
    sep = text.find("\n---\n")
    assert sep > 0
    assert "纪要" in text[:sep]
    assert "逐字稿" in text[sep:]
    missing = http.get("/jobs/does-not-exist/minutes")
    assert missing.status_code == 404
    assert missing.status_code != 500

    # 符号链接越出 jobs 根目录的负例断言与正常正例断言
    outside_file = tmp_path / "outside_transcript.txt"
    outside_marker = "SECRET_TRANSCRIPT_ESCAPE_MARKER"
    outside_file.write_text(outside_marker, encoding="utf-8")

    symlink_job_id = "0123456789abcdef0123456789abcdef"
    store = JobStore(_data / "jobs.db")
    store.create_job(
        job_id=symlink_job_id,
        source_name="symlink.wav",
        tier="fast",
        source_kind="upload",
        source_path=None,
        url=None,
    )
    conn = sqlite3.connect(_data / "jobs.db")
    try:
        conn.execute(
            "UPDATE jobs SET status = 'done', minutes_status = 'done' WHERE id = ?",
            (symlink_job_id,),
        )
        conn.commit()
    finally:
        conn.close()

    symlink_job_dir = _data / "jobs" / symlink_job_id
    symlink_job_dir.mkdir(parents=True, exist_ok=True)
    (symlink_job_dir / "minutes.md").write_text("## 纪要\n一句话摘要", encoding="utf-8")
    (symlink_job_dir / "transcript.md").symlink_to(outside_file)

    symlink_resp = http.get(f"/jobs/{symlink_job_id}/combined")
    assert symlink_resp.status_code == 404
    assert outside_marker not in symlink_resp.text
    assert symlink_resp.json() == {
        "error": "逐字稿不存在",
        "status": "done",
        "minutes_status": "done",
    }

    # 正常 transcript.md 返回 200 且内容含逐字稿文本
    (symlink_job_dir / "transcript.md").unlink()
    (symlink_job_dir / "transcript.md").write_text("正常逐字稿文本", encoding="utf-8")
    normal_resp = http.get(f"/jobs/{symlink_job_id}/combined")
    assert normal_resp.status_code == 200
    assert "正常逐字稿文本" in normal_resp.text


def test_c6_minutes_before_done_is_not_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSCRIBER_STUB", "1")
    monkeypatch.setenv("TRANSCRIBER_STUB_SLEEP", "2")
    data = tmp_path / "data"
    wav = tmp_path / "slow.wav"
    synth_wav(wav, seconds=0.2)
    with TestClient(create_app(data)) as http:
        job_id = post_file(http, wav, tier="fast").json()["job_id"]
        response = http.get(f"/jobs/{job_id}/minutes")
        assert response.status_code == 404
        assert response.status_code != 500
        body = response.json()
        assert "error" in body
        combined = http.get(f"/jobs/{job_id}/combined")
        assert combined.status_code == 404
        assert combined.status_code != 500


def test_c9_delete_job_and_artifacts(
    client: tuple[TestClient, Path], tmp_path: Path
) -> None:
    http, data = client
    wav = tmp_path / "talk.wav"
    synth_wav(wav, seconds=0.2)
    job_id = post_file(http, wav, tier="fast").json()["job_id"]
    assert wait_job(http, job_id)["status"] == "done"
    directory = data / "jobs" / job_id
    assert directory.is_dir()
    deleted = http.delete(f"/jobs/{job_id}")
    assert deleted.status_code == 200
    assert http.get(f"/jobs/{job_id}").status_code == 404
    assert not directory.exists()
    again = http.delete(f"/jobs/{job_id}")
    assert again.status_code == 404
    missing = http.post("/jobs/does-not-exist/delete")
    assert missing.status_code == 404


# --- LZW-91 custom 纪要通道 -------------------------------------------------
# 接缝：配置层（llm_settings）、纪要生成层（summarize / run_minutes，
# 传输以替身捕获）、治理层（test_c1 / test_b20）。


class _Captured:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.payloads: list[dict[str, Any]] = []


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    captured: _Captured,
    responses: list[httpx.Response | Exception],
) -> None:
    """把 httpx.Client 换成捕获请求的替身；按顺序吐 responses（最后一个重复）。"""
    queue = list(responses)

    class _FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: object,
        ) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, Any]) -> httpx.Response:
            captured.urls.append(url)
            captured.headers.append(dict(headers))
            captured.payloads.append(dict(json))
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return item

    monkeypatch.setattr("app.minutes.httpx.Client", _FakeClient)


def _ok_response(content: str) -> httpx.Response:
    request = httpx.Request("POST", "http://stub/chat/completions")
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}]},
        request=request,
    )


def _status_response(code: int) -> httpx.Response:
    request = httpx.Request("POST", "http://stub/chat/completions")
    return httpx.Response(code, request=request)


_MINUTES_TEXT = "# 纪要\n\n## 一句话摘要\n自定义端点返回的纪要\n"


def _custom_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model: str | None = "magpie-chat",
    base_url: str | None = "https://api.example.com/v1",
    api_key: str | None = "sk-xxx",
) -> None:
    monkeypatch.setenv("TRANSCRIBER_LLM_PROVIDER", "custom")
    monkeypatch.setenv("TRANSCRIBER_STUB_LLM", "0")
    monkeypatch.setattr("app.config.load_secret_file", lambda: None)
    for name, value in (
        ("TRANSCRIBER_LLM_MODEL", model),
        ("TRANSCRIBER_LLM_BASE_URL", base_url),
        ("TRANSCRIBER_LLM_API_KEY", api_key),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_c1_unregistered_provider_still_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """负例：未注册通道名（如 custom2）仍被拒绝，不静默回退到任一通道。"""
    monkeypatch.setenv("TRANSCRIBER_LLM_PROVIDER", "custom2")
    with pytest.raises(RuntimeError) as caught:
        llm_settings()
    assert "未知纪要 provider" in str(caught.value)
    assert "custom2" in str(caught.value)


@pytest.mark.parametrize(
    "raw",
    [
        "https://api.example.com/v1/",
        "https://api.example.com/v1",
    ],
)
def test_c2_custom_request_url_and_auth(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """正例：请求打到 {base_url 去尾斜杠}/chat/completions，模型与 Bearer 头照传。"""
    captured = _Captured()
    _patch_transport(monkeypatch, captured, [_ok_response(_MINUTES_TEXT)])
    _custom_env(monkeypatch, base_url=raw)

    result = summarize(_SAMPLE_SEGMENTS)

    assert captured.urls == ["https://api.example.com/v1/chat/completions"]
    assert captured.payloads[0]["model"] == "magpie-chat"
    assert captured.headers[0]["Authorization"] == "Bearer sk-xxx"
    assert "自定义端点返回的纪要" in result


def test_c2_custom_without_api_key_has_no_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """负例：无密钥端点不做前置拦截、不发 Authorization，照常解析成功。"""
    captured = _Captured()
    _patch_transport(monkeypatch, captured, [_ok_response(_MINUTES_TEXT)])
    _custom_env(monkeypatch, api_key=None)

    result = summarize(_SAMPLE_SEGMENTS)

    assert captured.urls == ["https://api.example.com/v1/chat/completions"]
    assert "Authorization" not in captured.headers[0]
    assert "自定义端点返回的纪要" in result


@pytest.mark.parametrize(
    ("env_kwargs", "expected"),
    [
        ({"model": None}, "未配置纪要模型"),
        ({"base_url": None}, "未配置纪要接口地址"),
        ({"model": None, "base_url": None}, "未配置纪要模型"),
        ({"base_url": "ftp://x"}, "纪要接口地址无效"),
        ({"base_url": "api.example.com"}, "纪要接口地址无效"),
        ({"base_url": "http://"}, "纪要接口地址无效"),
        # fix-r1（I1/S1 收口）：畸形解析、越界端口、userinfo、query、fragment 一律判无效。
        ({"base_url": "http://[::1"}, "纪要接口地址无效"),
        ({"base_url": "http://api.example.com:99999"}, "纪要接口地址无效"),
        ({"base_url": "https://api.example.com/v1?x=1"}, "纪要接口地址无效"),
        ({"base_url": "https://api.example.com/v1#x"}, "纪要接口地址无效"),
        ({"base_url": "https://user:pass@api.example.com/v1"}, "纪要接口地址无效"),
    ],
)
def test_c3_custom_config_gaps_name_the_missing_item(
    monkeypatch: pytest.MonkeyPatch, env_kwargs: dict[str, Any], expected: str
) -> None:
    """缺项按写死顺序点名（缺 model → 缺 base_url → 地址非法 → 缺密钥）。"""
    _custom_env(monkeypatch, **env_kwargs)
    with pytest.raises(MinutesError) as caught:
        summarize(_SAMPLE_SEGMENTS)
    message = str(caught.value)
    assert expected in message
    # 文案不含所填地址原文与密钥。
    for secret in (
        "api.example.com",
        "ftp://x",
        "sk-xxx",
        "Bearer",
        "::1",
        "99999",
        "user:pass",
        "x=1",
        "#x",
    ):
        assert secret not in message
    raw = env_kwargs.get("base_url")
    if raw is not None:
        assert raw not in message  # 文案不回显地址原文（含 query/userinfo 等形态）
    if expected == "未配置纪要模型":
        assert "未配置纪要接口地址" not in message


def test_c3_custom_failure_isolated_and_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置型真失败：只置纪要 error，任务仍 done、逐字稿在；改好配置后重生成即恢复。"""
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    job_id = store.create_job(
        tier="fast",
        source_name="talk.wav",
        source_kind="upload",
        source_path=None,
        url=None,
    )
    assert store.claim_next() is not None
    store.mark_done(job_id)
    directory = data / "jobs" / job_id
    directory.mkdir(parents=True)
    (directory / "segments.json").write_text("[]", encoding="utf-8")
    (directory / "transcript.md").write_text("逐字稿原文", encoding="utf-8")

    _custom_env(monkeypatch, model=None)
    run_minutes(store, data, {"id": job_id})
    stored = store.get(job_id)
    assert stored is not None
    assert stored["minutes_status"] == "error"
    assert stored["status"] == "done"
    assert "未配置纪要模型" in str(stored["minutes_error"])
    assert (directory / "transcript.md").is_file()
    assert (directory / "segments.json").is_file()

    # 等价关系：单测改 env 重跑 ≡ 生产改配置 + 重启 + 重新生成。
    _custom_env(monkeypatch, model="magpie-chat")
    captured = _Captured()
    _patch_transport(monkeypatch, captured, [_ok_response(_MINUTES_TEXT)])
    run_minutes(store, data, {"id": job_id})
    again = store.get(job_id)
    assert again is not None
    assert again["minutes_status"] == "done"
    assert (directory / "minutes.md").is_file()


def test_c3_custom_stored_error_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端点型真失败：落库文案不含端点 URL、Authorization 与密钥原文。"""
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    job_id = store.create_job(
        tier="fast",
        source_name="talk.wav",
        source_kind="upload",
        source_path=None,
        url=None,
    )
    assert store.claim_next() is not None
    store.mark_done(job_id)
    directory = data / "jobs" / job_id
    directory.mkdir(parents=True)
    (directory / "segments.json").write_text("[]", encoding="utf-8")
    (directory / "transcript.md").write_text("逐字稿原文", encoding="utf-8")

    _custom_env(monkeypatch)
    boom = httpx.HTTPStatusError(
        "端点炸了 https://api.example.com/v1/chat?key=sk-xxx"
        " Authorization: Bearer sk-xxx",
        request=httpx.Request("POST", "https://api.example.com/v1/chat"),
        response=_status_response(500),
    )
    _patch_transport(monkeypatch, _Captured(), [boom])
    run_minutes(store, data, {"id": job_id})
    stored = store.get(job_id)
    assert stored is not None
    assert stored["minutes_status"] == "error"
    assert stored["status"] == "done"
    message = str(stored["minutes_error"])
    assert "api.example.com" not in message
    assert "sk-xxx" not in message
    assert "Bearer" not in message
    assert "HTTP 500" in message
    assert (directory / "transcript.md").is_file()


def test_c3_transient_failure_does_not_kill_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """负例：瞬时故障（首次 500、二次成功）不误杀任务、逐字稿与通道。"""
    data = tmp_path / "data"
    store = JobStore(data / "jobs.db")
    job_id = store.create_job(
        tier="fast",
        source_name="talk.wav",
        source_kind="upload",
        source_path=None,
        url=None,
    )
    assert store.claim_next() is not None
    store.mark_done(job_id)
    directory = data / "jobs" / job_id
    directory.mkdir(parents=True)
    (directory / "segments.json").write_text("[]", encoding="utf-8")
    (directory / "transcript.md").write_text("逐字稿原文", encoding="utf-8")

    _custom_env(monkeypatch)
    captured = _Captured()
    _patch_transport(monkeypatch, captured, [_status_response(500), _ok_response(_MINUTES_TEXT)])

    run_minutes(store, data, {"id": job_id})
    first = store.get(job_id)
    assert first is not None
    assert first["minutes_status"] == "error"
    assert first["status"] == "done"
    assert (directory / "transcript.md").is_file()
    assert (directory / "segments.json").is_file()

    run_minutes(store, data, {"id": job_id})
    second = store.get(job_id)
    assert second is not None
    assert second["minutes_status"] == "done"
    assert second["status"] == "done"
    assert (directory / "minutes.md").is_file()


def test_c4_ollama_readings_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """ollama 零漂移：读数与现行一致。"""
    monkeypatch.setenv("TRANSCRIBER_LLM_PROVIDER", "ollama")
    for name in ("TRANSCRIBER_LLM_MODEL", "TRANSCRIBER_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    settings = llm_settings()
    assert settings["model"] == "qwen2.5:7b"
    assert settings["base_url"] == "http://127.0.0.1:11434/v1"
    assert settings["require_api_key"] is False


def test_c4_two_step_rollback_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """两步回退：只改 provider 不算回退，BASE_URL 也得改回（全局覆盖对全通道生效）。"""
    monkeypatch.setenv("TRANSCRIBER_LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("TRANSCRIBER_LLM_PROVIDER", "custom")
    assert llm_settings()["base_url"] == "https://api.example.com/v1"

    monkeypatch.setenv("TRANSCRIBER_LLM_PROVIDER", "ollama")
    # 第一步之后仍被覆盖值劫持，不许误报为已回退。
    assert llm_settings()["base_url"] == "https://api.example.com/v1"

    monkeypatch.setenv("TRANSCRIBER_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    assert llm_settings()["base_url"] == "http://127.0.0.1:11434/v1"

    # 或清空回落通道表默认（原生部署口径）。
    monkeypatch.delenv("TRANSCRIBER_LLM_BASE_URL", raising=False)
    assert llm_settings()["base_url"] == "http://127.0.0.1:11434/v1"


def test_c4_two_step_rollback_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """MODEL 对称：只改 provider 与 BASE_URL 不算回退，MODEL 也得改回。"""
    monkeypatch.setenv("TRANSCRIBER_LLM_MODEL", "magpie-chat")
    monkeypatch.setenv("TRANSCRIBER_LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("TRANSCRIBER_LLM_PROVIDER", "custom")
    assert llm_settings()["model"] == "magpie-chat"

    monkeypatch.setenv("TRANSCRIBER_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("TRANSCRIBER_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    assert llm_settings()["model"] == "magpie-chat"

    monkeypatch.setenv("TRANSCRIBER_LLM_MODEL", "qwen2.5:7b")
    assert llm_settings()["model"] == "qwen2.5:7b"

    monkeypatch.delenv("TRANSCRIBER_LLM_MODEL", raising=False)
    assert llm_settings()["model"] == "qwen2.5:7b"


def test_c4_custom_env_does_not_move_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """负例：provider=ollama 时多设 custom 相关 env 不改变 ollama 读数。"""
    monkeypatch.setenv("TRANSCRIBER_LLM_PROVIDER", "ollama")
    monkeypatch.delenv("TRANSCRIBER_LLM_MODEL", raising=False)
    monkeypatch.delenv("TRANSCRIBER_LLM_BASE_URL", raising=False)
    baseline = dict(llm_settings())
    for name, value in (
        ("TRANSCRIBER_LLM_API_KEY", "sk-xxx"),
        ("TRANSCRIBER_CUSTOM_NOTE", "anything"),
    ):
        monkeypatch.setenv(name, value)
    assert dict(llm_settings()) == baseline


def test_c5_compose_does_not_hardcode_ollama_address() -> None:
    """compose 不写死地址，也不再在 environment 里出现该键（fix-r1 I2 勘误口径）。"""
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "11434" not in text
    # 勘误：environment 出现该键时，空串会盖掉 secrets env_file 里的同名值。
    assert "TRANSCRIBER_LLM_BASE_URL" not in text


def test_c5_compose_render_keeps_env_file_address(tmp_path: Path) -> None:
    """负例（勘误口径）：仅 env_file 给地址时，渲染结果原样保留该地址。

    原口径断言「宿主未给值时渲染为空串」，已被 fix-r1 I2 勘误取代：
    空串会盖掉 env_file，地址到不了进程。这里临时工程只经 env_file 传地址。
    """
    import shutil
    import subprocess

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker 不可用：只做文件静态断言（见 RESULT）")

    address = "http://ollama:11434/v1"
    secrets = tmp_path / "secrets.env"
    secrets.write_text(f"TRANSCRIBER_LLM_BASE_URL={address}\n", encoding="utf-8")
    project = tmp_path / "compose.yml"
    project.write_text(
        "services:\n"
        "  probe:\n"
        "    image: busybox\n"
        "    env_file:\n"
        f"      - path: \"{secrets}\"\n"
        "        required: false\n"
        "    environment:\n"
        "      TRANSCRIBER_STUB: \"${TRANSCRIBER_STUB:-}\"\n",
        encoding="utf-8",
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home())}
    rendered = subprocess.run(
        [docker, "compose", "-f", str(project), "config"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert rendered.returncode == 0, rendered.stderr
    lines = [ln.strip() for ln in rendered.stdout.splitlines()]
    assert f"TRANSCRIBER_LLM_BASE_URL: {address}" in lines
