"""全仓唯一的脱敏实现：URL 结构化掩码先行，再走关键词与长串规则。"""

from __future__ import annotations

import re
import urllib.parse

# URL 只认 RFC 3986 里常见的 ASCII，避免把后面的中文标点和说明一起吞掉。
_URL_RE = re.compile(
    r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
    re.IGNORECASE,
)
_URL_TRAILING = ".,;:!?)]"

# key= / token= / Authorization 的值，以及 Bearer、云厂商密钥前缀。值不含空白和中文。
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|key)\b"
    r"\s*[:=]\s*(?:bearer\s+)?[A-Za-z0-9._\-+/=]+"
)
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]+|Bearer\s+[A-Za-z0-9._\-+/=]+|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]+)",
    re.IGNORECASE,
)
# 没有前缀的长串字母数字，按疑似密钥去掉。短英文单词留着。
_LONG_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9](?:[A-Za-z0-9_\-]{19,})(?![A-Za-z0-9_\-])"
)


def strip_urls(text: str) -> str:
    """把文本中每个 http(s) URL 整体替换为字面量 <url>。"""
    if not text:
        return text
    return _URL_RE.sub(lambda m: _strip_url(m.group(0)), text)


def _strip_url(raw: str) -> str:
    i = len(raw)
    while i > 0 and raw[i - 1] in _URL_TRAILING:
        i -= 1
    return f"<url>{raw[i:]}"


def mask_url(raw: str) -> str:
    """对单个 URL 结构化掩码：query 各值与 userinfo 一律替换为 ***。

    保留 path；fragment 中的 k=v 也掩码。
    """
    i = len(raw)
    while i > 0 and raw[i - 1] in _URL_TRAILING:
        i -= 1
    trimmed = raw[:i]
    trailing = raw[i:]
    if "://" not in trimmed:
        return raw
    try:
        parsed = urllib.parse.urlsplit(trimmed)
    except Exception:
        return f"<url>{trailing}"

    if parsed.scheme.lower() not in ("http", "https"):
        return raw
    if not parsed.netloc:
        return raw

    # 1. userinfo: user:pass@host:port -> ***@host:port
    netloc = parsed.netloc
    if "@" in netloc:
        _, _, host_port = netloc.rpartition("@")
        netloc = f"***@{host_port}"

    # 2. query: key=value -> key=***
    query = parsed.query
    if query:
        parts = query.split("&")
        masked_parts: list[str] = []
        for part in parts:
            if not part:
                continue
            if "=" in part:
                k, _ = part.split("=", 1)
                masked_parts.append(f"{k}=***")
            else:
                masked_parts.append(f"{part}=***")
        query = "&".join(masked_parts)

    # 3. fragment: k=v -> k=***
    fragment = parsed.fragment
    if fragment and "=" in fragment:
        fragment = re.sub(r"([^&=?;]+)=([^&;]*)", r"\1=***", fragment)

    masked = urllib.parse.urlunsplit(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            query,
            fragment,
        )
    )
    return masked + trailing


def mask_urls(text: str) -> str:
    """对文本中出现的所有 http(s) URL 进行结构化掩码。"""
    if not text:
        return text
    return _URL_RE.sub(lambda m: mask_url(m.group(0)), text)


def mask_source_name(name: str) -> str:
    """展示名（source_name）脱敏：只套 URL 规则、禁止套 _LONG_TOKEN_RE，防止误删长路径。"""
    if not name:
        return name
    return mask_urls(name)


def redact_sensitive(text: str) -> str:
    """全仓唯一的完整脱敏实现。

    顺序严格写死：
    1. 先对文本中出现的 http(s) URL 做结构化掩码（query 各值与 userinfo 一律替换为 ***）；
    2. 再走既有关键词赋值与密钥前缀规则；
    3. 最后走无前缀长串规则。
    """
    if not text:
        return text
    # 1. URL 结构化掩码先行
    text = mask_urls(text)
    # 2. 既有关键词/长串规则
    text = _SECRET_ASSIGN_RE.sub("[redacted]", text)
    text = _SECRET_RE.sub("[redacted]", text)
    text = _LONG_TOKEN_RE.sub("[redacted]", text)
    return text
