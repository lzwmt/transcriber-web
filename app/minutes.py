"""纪要生成。LLM 的模型名、base_url、provider 只从 config 读。"""

from __future__ import annotations

import html
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
import markdown

from app import config
from app.engines import Segment
from app.redact import redact_sensitive, strip_urls
from app.transcript import format_mmss

# LLM 常见的五节标题变体。只用于独立成行的 ## 标题，不改正文。
_HEADING_ALIASES = {
    "待办事项": "待办",
    "一句话总结": "一句话摘要",
    "决策": "决定",
    "未决事项": "未定事项",
    "待定事项": "未定事项",
}

STUB_MINUTES = """# 纪要

## 一句话摘要
这是一次简短的测试对话。

## 议题
- 测试转写与纪要流程

## 决定
无

## 待办
- [ ] 确认纪要内容（负责人：待定）

## 未定事项
无
"""

_STRUCTURE = """必须使用以下 Markdown 结构，不要输出结构之外的内容：

# 纪要

## 一句话摘要

## 议题

## 决定

## 待办

待办每条格式：`- [ ] 内容（负责人：X）`。没有负责人时写「负责人：待定」。

## 未定事项

某节没有内容时该节写「无」。"""


class MinutesError(Exception):
    """纪要失败原因。消息给任务记录看，不含密钥和堆栈。"""


class LLMProvider(Protocol):
    def complete(self, prompt: str) -> str: ...


def format_transcript_lines(segments: list[Segment]) -> str:
    lines: list[str] = []
    for segment in segments:
        stamp = format_mmss(segment["start"])
        text = " ".join(segment["text"].split())
        speaker = segment["speaker"]
        if speaker:
            lines.append(f"[{stamp}] {speaker}：{text}")
        else:
            lines.append(f"[{stamp}] {text}")
    return "\n".join(lines)


def build_minutes_prompt(transcript: str) -> str:
    return (
        "请根据下面带说话人标记与时间戳的逐字稿，生成会议纪要。\n"
        f"{_STRUCTURE}\n\n"
        "逐字稿：\n"
        f"{transcript}\n"
    )


def build_chunk_prompt(transcript: str) -> str:
    return (
        "请根据下面这一段带说话人标记与时间戳的逐字稿，写一份简短摘要，"
        "列出本段出现的议题、决定、待办和未定事项。不要输出最终纪要结构。\n\n"
        "逐字稿片段：\n"
        f"{transcript}\n"
    )


def build_merge_prompt(partials: list[str]) -> str:
    joined = "\n\n----\n\n".join(partials)
    return (
        "下面是长会议逐字稿按段生成的摘要。请合并为一份完整纪要。\n"
        f"{_STRUCTURE}\n\n"
        "各段摘要：\n"
        f"{joined}\n"
    )


def chunk_segments(segments: list[Segment], limit: int) -> list[list[Segment]]:
    """按 segment 边界切段，使每段逐字稿尽量不超过 limit 字。"""
    if not segments:
        return []
    chunks: list[list[Segment]] = []
    current: list[Segment] = []
    current_len = 0
    for segment in segments:
        line_len = len(format_transcript_lines([segment])) + 1
        if current and current_len + line_len > limit:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(segment)
        current_len += line_len
    if current:
        chunks.append(current)
    return chunks


def normalize_minutes_headings(text: str) -> str:
    """把独立成行的 ## 标题变体收成规范名。正文和其他标题原样保留。"""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        stripped_nl = line.rstrip("\r\n")
        newline = line[len(stripped_nl) :]
        if not stripped_nl.startswith("##") or stripped_nl.startswith("###"):
            out.append(line)
            continue
        title = stripped_nl[2:].strip()
        canonical = _HEADING_ALIASES.get(title)
        if canonical is None:
            out.append(line)
            continue
        out.append(f"## {canonical}{newline}")
    return "".join(out)


def summarize(segments: list[Segment]) -> str:
    transcript = format_transcript_lines(segments)
    limit = config.minutes_chunk_chars()
    if len(transcript) <= limit:
        return _minutes_output(_complete(build_minutes_prompt(transcript)))
    chunks = chunk_segments(segments, limit)
    if len(chunks) <= 1:
        return _minutes_output(_complete(build_minutes_prompt(transcript)))
    partials = [_complete(build_chunk_prompt(format_transcript_lines(chunk))) for chunk in chunks]
    return _minutes_output(_complete(build_merge_prompt(partials)))


def _minutes_output(text: str) -> str:
    return normalize_minutes_headings(text).strip() + "\n"


def _complete(prompt: str) -> str:
    return _provider().complete(prompt)


def _validated_settings(settings: dict[str, Any]) -> tuple[str, str]:
    """按写死顺序校验纪要通道配置，返回 (model, 去尾斜杠的 base_url)。

    顺序固定（规格 r1 I2）：①缺 model → ②缺 base_url → ③地址非法 →
    ④（仅 require_api_key 的通道）缺密钥。命中第一项即抛 MinutesError，
    文案只点名该项，不含地址原文、URL 与密钥。校验与通道无关：custom 的
    空默认值同样走到这里，预置通道默认值齐全，行为不变。
    """
    model = str(settings.get("model") or "").strip()
    base_url = str(settings.get("base_url") or "").strip()
    require_key = bool(settings.get("require_api_key"))
    api_key = config.llm_api_key()
    if not model:
        raise MinutesError("未配置纪要模型，无法生成纪要")
    if not base_url:
        raise MinutesError("未配置纪要接口地址，无法生成纪要")
    if not _is_valid_base_url(base_url):
        raise MinutesError("纪要接口地址无效，需为 http/https 且含主机名")
    if require_key and not api_key:
        raise MinutesError("未配置纪要接口密钥，无法生成纪要")
    return model, base_url.rstrip("/")


def _is_valid_base_url(base_url: str) -> bool:
    """地址合法性：http/https、主机非空、端口合法、无 userinfo/query/fragment（r1 I1+S1）。

    解析期与判定期的 ValueError（畸形 IPv6、越界端口等）一律归 False，
    让畸形地址落「纪要接口地址无效」口径，而不是裸 ValueError 逃逸。
    """
    try:
        parsed = urlsplit(base_url)
        # 读 port 会触发端口解析，越界（如 :99999）在这里抛 ValueError。
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and (port is None or 0 < port < 65536)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )

def _provider() -> LLMProvider:
    if config.llm_stub_enabled():
        return StubProvider()
    return OpenAICompatProvider()


class StubProvider:
    def complete(self, prompt: str) -> str:
        if config.llm_stub_error_enabled():
            raise MinutesError("stub LLM 失败")
        custom = config.llm_stub_text()
        if custom:
            return custom
        if "完整纪要" in prompt or "一句话摘要" in prompt:
            return STUB_MINUTES
        return "片段摘要：本段为测试对话。"


class OpenAICompatProvider:
    def complete(self, prompt: str) -> str:
        settings = config.llm_settings()
        model, base_url = _validated_settings(settings)
        api_key = config.llm_api_key()
        url = f"{base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        try:
            # 读/写/池超时走 TRANSCRIBER_LLM_TIMEOUT_S；连接超时仍固定 15 秒。
            timeout_s = config.llm_timeout_seconds()
            with httpx.Client(timeout=httpx.Timeout(timeout_s, connect=15.0)) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                body = response.json()
        except MinutesError:
            raise
        except httpx.HTTPStatusError as exc:
            raise MinutesError(f"纪要模型请求失败：HTTP {exc.response.status_code}") from None
        except Exception as exc:
            raise MinutesError(f"纪要模型请求失败：{_redact(exc)}") from None
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise MinutesError("纪要模型返回无法解析") from None
        if not isinstance(content, str) or not content.strip():
            raise MinutesError("纪要模型返回为空")
        return content


def render_minutes_html(text: str) -> str:
    # 纪要来自模型。先转义原文里的标签，再交给 Markdown，避免详情页把脚本当真 HTML。
    escaped = html.escape(text, quote=False)
    return markdown.markdown(escaped, extensions=["extra", "sane_lists", "nl2br"])


def sanitize_minutes_error(exc: BaseException) -> str:
    if isinstance(exc, MinutesError):
        text = str(exc)
    else:
        text = f"纪要失败：{exc}"
    text = redact_sensitive(text)
    text = strip_urls(text)
    text = " ".join(text.split())
    if len(text) > 500:
        text = text[:500]
    return text or "纪要失败"


def _redact(exc: BaseException) -> str:
    return strip_urls(redact_sensitive(str(exc)))
