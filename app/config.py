"""档位映射、纪要 LLM 与上限。模型名与 provider 地址只允许出现在本文件。"""

from __future__ import annotations

import os
import re
from pathlib import Path

# 单条音频不超过 1 小时；上传不超过 2GB。等于上限允许，超过拒绝。
MAX_DURATION_SECONDS_DEFAULT = 3600.0
MAX_UPLOAD_BYTES_DEFAULT = 2 * 1024 * 1024 * 1024
MINUTES_CHUNK_CHARS_DEFAULT = 6000
LLM_TIMEOUT_SECONDS_DEFAULT = 600
_SECRETS_FILE = Path.home() / ".transcriber" / "transcriber.env"


def secrets_file() -> Path:
    """secrets 文件路径。默认 `~/.transcriber/transcriber.env`。

    可用 `TRANSCRIBER_SECRETS_FILE` 覆盖（测试气密用：指向不存在或空的文件，
    免得宿主机 secrets 把键注入测试进程环境）。
    """
    raw = os.environ.get("TRANSCRIBER_SECRETS_FILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return _SECRETS_FILE


Setting = str | int | float | bool

# 更换引擎或模型只改这张表。engines / pipeline 按 engine 键读取，不写模型名。
TIER_CONFIG: dict[str, dict[str, Setting]] = {
    "fast": {
        "engine": "funasr",
        "model": "iic/SenseVoiceSmall",
        "vad_model": "fsmn-vad",
        "spk_model": "cam++",
        "device": "cpu",
        "hub": "ms",
        "language": "zh",
        "use_itn": True,
        "batch_size_s": 60,
        "merge_vad": True,
        "merge_length_s": 15,
        "vad_max_single_segment_time_ms": 30000,
        "disable_update": True,
        # SenseVoice 没有词级时间戳，说话人按 VAD 段聚类。
        "spk_mode": "vad_segment",
    },
    "precise": {
        "engine": "faster_whisper",
        "model": "medium",
        "device": "cpu",
        "compute_type": "int8",
        "language": "zh",
        "vad_filter": True,
        "initial_prompt": "以下是普通话的简体中文转写。",
        "opencc": "t2s",
    },
}

VALID_TIERS = frozenset(TIER_CONFIG)

# 更换纪要通道只改这张表和 TRANSCRIBER_LLM_PROVIDER。minutes / pipeline 不写模型名。
LLM_PROVIDERS: dict[str, dict[str, Setting]] = {
    "nous": {
        "kind": "openai_compat",
        "model": "stepfun/step-3.7-flash:free",
        "base_url": "https://inference-api.nousresearch.com/v1",
        "api_key_env": "TRANSCRIBER_LLM_API_KEY",
        "require_api_key": True,
    },
    "ollama": {
        "kind": "openai_compat",
        "model": "qwen2.5:7b",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key_env": "TRANSCRIBER_LLM_API_KEY",
        "require_api_key": False,
    },
    "spark": {
        "kind": "openai_compat",
        "model": "spark-x2.6",
        "base_url": "https://maas-api.cn-huabei-1.xf-yun.com/v2",
        "api_key_env": "TRANSCRIBER_LLM_API_KEY",
        "require_api_key": True,
    },
    # 自定义 OpenAI 兼容通道：地址与模型没有默认值，未填即报错（校验见 minutes）。
    # 不要求密钥：无密钥端点（本地网关等）不做前置拦截；带密钥时照常发 Bearer。
    "custom": {
        "kind": "openai_compat",
        "model": "",
        "base_url": "",
        "api_key_env": "TRANSCRIBER_LLM_API_KEY",
        "require_api_key": False,
    },
}

DEFAULT_LLM_PROVIDER = "nous"
VALID_LLM_PROVIDERS = frozenset(LLM_PROVIDERS)


def max_duration_seconds() -> float:
    raw = os.environ.get("TRANSCRIBER_MAX_DURATION_SECONDS", "").strip()
    if not raw:
        return MAX_DURATION_SECONDS_DEFAULT
    return float(raw)


def max_upload_bytes() -> int:
    raw = os.environ.get("TRANSCRIBER_MAX_UPLOAD_BYTES", "").strip()
    if not raw:
        return MAX_UPLOAD_BYTES_DEFAULT
    return int(raw)


def data_dir() -> Path:
    return Path(os.environ.get("TRANSCRIBER_DATA_DIR", "data")).resolve()


def stub_enabled() -> bool:
    return os.environ.get("TRANSCRIBER_STUB", "") == "1"


def stub_sleep_seconds() -> float:
    raw = os.environ.get("TRANSCRIBER_STUB_SLEEP", "").strip()
    if not raw:
        return 0.0
    return float(raw)


def engine_marker_path() -> str:
    return os.environ.get("TRANSCRIBER_ENGINE_MARKER", "").strip()


# R9 功能开关：env 约定由规格冻结为 TRANSCRIBER_FEAT_<NAME>（NAME = name 转大写），
# 与 verify 的 flag 子命令共用同一约定。
_FEATURE_NAME_RE = re.compile(r"[a-z0-9_]+")
_FEATURE_ON_VALUES = frozenset({"1", "true", "on", "yes"})


def feature_enabled(name: str) -> bool:
    """读 R9 功能开关。值先 strip 再按 1/true/on/yes（不分大小写）判开。

    未设与其余值均为关（默认关）。name 只允许 [a-z0-9_]，空或含非法字符抛 ValueError。
    """
    if not name or _FEATURE_NAME_RE.fullmatch(name) is None:
        raise ValueError(f"非法功能开关名：{name!r}，只允许 [a-z0-9_]")
    raw = os.environ.get(f"TRANSCRIBER_FEAT_{name.upper()}", "").strip().lower()
    return raw in _FEATURE_ON_VALUES


def settings_for_engine(engine: str) -> dict[str, Setting]:
    found = [cfg for cfg in TIER_CONFIG.values() if cfg.get("engine") == engine]
    if len(found) != 1:
        raise RuntimeError(f"引擎配置不唯一：{engine}")
    return found[0]


def load_secret_file() -> None:
    """把 secrets 文件里尚未出现的键写入环境。已有环境变量优先。不记录键值。"""
    path = secrets_file()
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _strip_secret_value(value)


def _strip_secret_value(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def llm_stub_enabled() -> bool:
    raw = os.environ.get("TRANSCRIBER_STUB_LLM", "").strip()
    if raw == "1":
        return True
    if raw == "0":
        return False
    return stub_enabled()


def llm_stub_error_enabled() -> bool:
    if os.environ.get("TRANSCRIBER_STUB_LLM_ERROR", "").strip() == "1":
        return True
    path = os.environ.get("TRANSCRIBER_STUB_LLM_ERROR_PATH", "").strip()
    return bool(path) and Path(path).is_file()


def llm_stub_text() -> str:
    return os.environ.get("TRANSCRIBER_STUB_LLM_TEXT", "").strip()


def llm_provider_name() -> str:
    raw = os.environ.get("TRANSCRIBER_LLM_PROVIDER", "").strip()
    if not raw:
        return DEFAULT_LLM_PROVIDER
    if raw not in LLM_PROVIDERS:
        raise RuntimeError(f"未知纪要 provider：{raw}")
    return raw


def llm_settings() -> dict[str, Setting]:
    settings = dict(LLM_PROVIDERS[llm_provider_name()])
    model = os.environ.get("TRANSCRIBER_LLM_MODEL", "").strip()
    if model:
        settings["model"] = model
    base_url = os.environ.get("TRANSCRIBER_LLM_BASE_URL", "").strip()
    if base_url:
        settings["base_url"] = base_url.rstrip("/")
    return settings


def llm_api_key() -> str:
    load_secret_file()
    settings = llm_settings()
    env_name = str(settings.get("api_key_env") or "TRANSCRIBER_LLM_API_KEY")
    return os.environ.get(env_name, "").strip()


def minutes_chunk_chars() -> int:
    raw = os.environ.get("TRANSCRIBER_MINUTES_CHUNK_CHARS", "").strip()
    if not raw:
        return MINUTES_CHUNK_CHARS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return MINUTES_CHUNK_CHARS_DEFAULT
    if value <= 0:
        return MINUTES_CHUNK_CHARS_DEFAULT
    return value


def llm_timeout_seconds() -> int:
    raw = os.environ.get("TRANSCRIBER_LLM_TIMEOUT_S", "").strip()
    if not raw:
        return LLM_TIMEOUT_SECONDS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return LLM_TIMEOUT_SECONDS_DEFAULT
    if value <= 0:
        return LLM_TIMEOUT_SECONDS_DEFAULT
    return value


# --- 在线录音（LZW-62） -------------------------------------------------------
# 上限与阈值只经服务端环境变量注入：请求参数带同名值一律不生效。
RECORD_MAX_SECONDS_DEFAULT = 3600.0
RECORD_WARN_SECONDS_DEFAULT = (300.0, 60.0)
RECORD_PART_SECONDS_DEFAULT = 5.0
RECORD_PART_BYTES_DEFAULT = 5 * 1024 * 1024
RECORD_MAX_PARTS_DEFAULT = 720
RECORD_SESSION_TTL_SECONDS_DEFAULT = 7 * 24 * 3600
# 拼合/校验容差（秒）：与 ffprobe 误差口径一致，固定 2 秒，不走 env。
RECORD_DURATION_TOLERANCE_SECONDS = 2.0
# assembling 状态的存活上限（秒）：超龄视为崩溃残留，退回待补传（I6）。
RECORD_ASSEMBLING_TIMEOUT_SECONDS = 300


def _positive_float(raw: str, default: float) -> float:
    try:
        value = float(raw)
    except ValueError:
        return default
    if value <= 0 or not value < float("inf"):
        return default
    return value


def _positive_int(raw: str, default: int) -> int:
    try:
        value = int(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


def record_max_seconds() -> float:
    """单条录音时长上限（秒）。生产口径 3600。"""
    raw = os.environ.get("TRANSCRIBER_RECORD_MAX_SECONDS", "").strip()
    if not raw:
        return RECORD_MAX_SECONDS_DEFAULT
    return _positive_float(raw, RECORD_MAX_SECONDS_DEFAULT)


def record_warn_seconds() -> tuple[float, ...]:
    """剩余时长提示阈值（秒），从大到小、去重。默认 300 与 60。

    可用 `TRANSCRIBER_RECORD_WARN_SECONDS` 给逗号分隔的两项覆盖；非法项丢弃，
    全部非法时回到默认两档。
    """
    raw = os.environ.get("TRANSCRIBER_RECORD_WARN_SECONDS", "").strip()
    if not raw:
        return RECORD_WARN_SECONDS_DEFAULT
    values: list[float] = []
    for piece in raw.split(","):
        text = piece.strip()
        if not text:
            continue
        try:
            parsed = float(text)
        except ValueError:
            continue
        if parsed <= 0 or parsed != parsed:
            continue
        if parsed not in values:
            values.append(parsed)
    if not values:
        return RECORD_WARN_SECONDS_DEFAULT
    return tuple(sorted(values, reverse=True))


def record_part_seconds() -> float:
    """每片录音时长（秒）。默认 5。"""
    raw = os.environ.get("TRANSCRIBER_RECORD_PART_SECONDS", "").strip()
    if not raw:
        return RECORD_PART_SECONDS_DEFAULT
    return _positive_float(raw, RECORD_PART_SECONDS_DEFAULT)


def record_part_bytes() -> int:
    """单片大小上限（字节）。默认 5MB。"""
    raw = os.environ.get("TRANSCRIBER_RECORD_PART_BYTES", "").strip()
    if not raw:
        return RECORD_PART_BYTES_DEFAULT
    return _positive_int(raw, RECORD_PART_BYTES_DEFAULT)


def record_max_parts() -> int:
    """单会话总片数上限。默认 720（= 1 小时 / 5 秒）。"""
    raw = os.environ.get("TRANSCRIBER_RECORD_MAX_PARTS", "").strip()
    if not raw:
        return RECORD_MAX_PARTS_DEFAULT
    return _positive_int(raw, RECORD_MAX_PARTS_DEFAULT)


def record_session_ttl_seconds() -> int:
    """未完成会话的存活时间（秒）。默认 7 天。"""
    raw = os.environ.get("TRANSCRIBER_RECORD_SESSION_TTL_SECONDS", "").strip()
    if not raw:
        return RECORD_SESSION_TTL_SECONDS_DEFAULT
    return _positive_int(raw, RECORD_SESSION_TTL_SECONDS_DEFAULT)


def recording_enabled() -> bool:
    """录音开关。R9 唯一入口 feature_enabled("recording") 的薄封装。"""
    return feature_enabled("recording")
