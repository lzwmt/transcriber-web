"""两档转写。函数签名统一为 transcribe(wav_path) -> segments。

模型名从 config 读取。本文件不写模型 id。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, TypedDict

from app import config

_TAG = re.compile(r"<\|.*?\|>")

_funasr_model: Any = None
_whisper_model: Any = None
_opencc_converter: Any = None


class Segment(TypedDict):
    start: float
    end: float
    speaker: str | None
    text: str


def strip_sensevoice_tags(text: str) -> str:
    """去掉 SenseVoice 的 <|...|> 标签。"""
    cleaned = _TAG.sub("", text)
    return " ".join(cleaned.split())


def _meaningful(text: str) -> str:
    """丢掉只有标点的片段，避免空说话人占一个编号。"""
    cleaned = " ".join(text.split())
    if not any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in cleaned):
        return ""
    return cleaned


def normalize_segments(segments: list[Segment]) -> list[Segment]:
    """按 start 升序，并消掉重叠，保证 start < end。"""
    ordered = sorted(segments, key=lambda item: (item["start"], item["end"]))
    fixed: list[Segment] = []
    for segment in ordered:
        text = _meaningful(segment["text"])
        if not text:
            continue
        start = float(segment["start"])
        end = float(segment["end"])
        if fixed and start < fixed[-1]["end"]:
            start = fixed[-1]["end"]
        if end <= start:
            end = start + 0.01
        fixed.append(
            {
                "start": start,
                "end": end,
                "speaker": segment["speaker"],
                "text": text,
            }
        )
    return fixed


def segments_from_funasr(result: Any) -> list[Segment]:
    """解析 FunASR 返回。sentence_info 的 start/end 单位是毫秒。

    说话人按时间上首次出现的顺序编号为「说话人1」「说话人2」……
    """
    if not result:
        return []
    item = result[0]
    if not isinstance(item, dict):
        return []
    sentences = item.get("sentence_info") or []
    drafted: list[tuple[float, float, object | None, str]] = []
    for sentence in sentences:
        if not isinstance(sentence, dict):
            continue
        raw = sentence.get("text")
        if raw is None:
            raw = sentence.get("sentence")
        text = _meaningful(strip_sensevoice_tags(str(raw or "")))
        if not text:
            continue
        start_ms = float(sentence.get("start") or 0)
        end_ms = float(sentence.get("end") or 0)
        drafted.append((start_ms / 1000.0, end_ms / 1000.0, sentence.get("spk"), text))
    drafted.sort(key=lambda row: (row[0], row[1]))
    speaker_of: dict[object, str] = {}
    segments: list[Segment] = []
    for start, end, spk, text in drafted:
        speaker: str | None
        if spk is None:
            speaker = None
        else:
            if spk not in speaker_of:
                speaker_of[spk] = f"说话人{len(speaker_of) + 1}"
            speaker = speaker_of[spk]
        segments.append({"start": start, "end": end, "speaker": speaker, "text": text})
    if segments:
        return normalize_segments(segments)
    fallback = strip_sensevoice_tags(str(item.get("text") or ""))
    if not fallback:
        return []
    return normalize_segments(
        [{"start": 0.0, "end": 0.01, "speaker": None, "text": fallback}]
    )


def overlap_seconds(
    left_start: float, left_end: float, right_start: float, right_end: float
) -> float:
    return max(0.0, min(left_end, right_end) - max(left_start, right_start))


def assign_speakers(segments: list[Segment], spans: list[Segment]) -> list[Segment]:
    """按最大时间重叠把说话人时间段贴到 ASR 段上。无重叠保持原 speaker（通常为 null）。"""
    assigned: list[Segment] = []
    for segment in segments:
        best_speaker = segment["speaker"]
        best_overlap = 0.0
        for span in spans:
            speaker = span["speaker"]
            if not speaker:
                continue
            overlap = overlap_seconds(segment["start"], segment["end"], span["start"], span["end"])
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = speaker
        assigned.append(
            {
                "start": segment["start"],
                "end": segment["end"],
                "speaker": best_speaker if best_overlap > 0 else None,
                "text": segment["text"],
            }
        )
    return assigned


def speaker_spans(wav_path: str) -> list[Segment]:
    """同一 wav 上跑 FunASR 说话人时间段。模型名从 config 读。"""
    return transcribe_funasr(wav_path)


def segments_from_whisper(raw_segments: Any, convert: Any) -> list[Segment]:
    """faster-whisper 片段。speaker 先留空，文本做繁转简。"""
    segments: list[Segment] = []
    for piece in raw_segments:
        converted = convert(str(piece.text))
        text = _meaningful(converted)
        if not text:
            continue
        segments.append(
            {
                "start": float(piece.start),
                "end": float(piece.end),
                "speaker": None,
                "text": text,
            }
        )
    return normalize_segments(segments)


def transcribe_funasr(wav_path: str) -> list[Segment]:
    settings = config.settings_for_engine("funasr")
    model = _load_funasr(settings)
    result = model.generate(
        input=wav_path,
        cache={},
        language=str(settings["language"]),
        use_itn=bool(settings["use_itn"]),
        batch_size_s=int(settings["batch_size_s"]),
        merge_vad=bool(settings["merge_vad"]),
        merge_length_s=int(settings["merge_length_s"]),
    )
    return segments_from_funasr(result)


def transcribe_faster_whisper(wav_path: str) -> list[Segment]:
    settings = config.settings_for_engine("faster_whisper")
    model = _load_whisper(settings)
    converter = _load_opencc(str(settings["opencc"]))
    raw_segments, _info = model.transcribe(
        wav_path,
        language=str(settings["language"]),
        vad_filter=bool(settings["vad_filter"]),
        initial_prompt=str(settings["initial_prompt"]),
    )
    segments = segments_from_whisper(raw_segments, converter.convert)
    return assign_speakers(segments, speaker_spans(wav_path))


def transcribe(
    wav_path: str, tier: str, *, job_id: str | None = None
) -> list[Segment]:
    """流水线入口。测试可设 TRANSCRIBER_ENGINE_MARKER，调用即落盘。

    桩模式另可设 TRANSCRIBER_STUB_TRACE：每次桩转写追加 start/end 区间记录
    （job 标识由调用方传入，见 LZW-71 勘误 #1）。非桩路径零记录。
    """
    marker = config.engine_marker_path()
    if marker:
        Path(marker).write_text(tier, encoding="utf-8")
    if config.stub_enabled():
        return _stub_transcribe(tier, job_id)
    settings = config.TIER_CONFIG[tier]
    engine = str(settings["engine"])
    if engine == "funasr":
        return transcribe_funasr(wav_path)
    if engine == "faster_whisper":
        return transcribe_faster_whisper(wav_path)
    raise RuntimeError(f"未知引擎：{engine}")


def _stub_trace_path() -> str:
    """桩模式区间记录的落点；未设 TRANSCRIBER_STUB_TRACE 时返回空串（零记录）。

    scoped 例外（LZW-71 勘误 #1，owner 批准）：本变量只在桩分支内直读 env，
    不进 app/config.py，以守住本单的文件范围。
    """
    return os.environ.get("TRANSCRIBER_STUB_TRACE", "").strip()


def _stub_trace_write(
    trace: str, event: str, job_id: str | None, mono: float, wall: float
) -> None:
    """追加一行事件（start/end）。trace 为空即整段跳过，生产路径不产生任何写入。"""
    if not trace:
        return
    record = {"event": event, "job_id": job_id, "mono": mono, "wall": wall}
    with Path(trace).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _stub_transcribe(tier: str, job_id: str | None = None) -> list[Segment]:
    trace = _stub_trace_path()
    started_mono = time.monotonic()
    started_wall = time.time()
    _stub_trace_write(trace, "start", job_id, started_mono, started_wall)
    delay = config.stub_sleep_seconds()
    if delay > 0:
        time.sleep(delay)
    if tier == "precise":
        segments: list[Segment] = [
            {"start": 0.0, "end": 1.5, "speaker": None, "text": "今天天气不错"},
        ]
    else:
        segments = [
            {"start": 0.0, "end": 1.2, "speaker": "说话人1", "text": "你好"},
            {"start": 1.2, "end": 2.5, "speaker": "说话人2", "text": "大家好"},
        ]
    _stub_trace_write(trace, "end", job_id, time.monotonic(), time.time())
    return segments


def _load_funasr(settings: dict[str, config.Setting]) -> Any:
    global _funasr_model
    if _funasr_model is None:
        from funasr import AutoModel

        _funasr_model = AutoModel(
            model=str(settings["model"]),
            vad_model=str(settings["vad_model"]),
            vad_kwargs={
                "max_single_segment_time": int(settings["vad_max_single_segment_time_ms"]),
            },
            spk_model=str(settings["spk_model"]),
            spk_mode=str(settings["spk_mode"]),
            device=str(settings["device"]),
            hub=str(settings["hub"]),
            disable_update=bool(settings["disable_update"]),
            disable_pbar=True,
        )
    return _funasr_model


def _load_whisper(settings: dict[str, config.Setting]) -> Any:
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        _whisper_model = WhisperModel(
            str(settings["model"]),
            device=str(settings["device"]),
            compute_type=str(settings["compute_type"]),
        )
    return _whisper_model


def _load_opencc(config_name: str) -> Any:
    global _opencc_converter
    if _opencc_converter is None:
        from opencc import OpenCC

        _opencc_converter = OpenCC(config_name)
    return _opencc_converter
