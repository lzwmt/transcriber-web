from __future__ import annotations

from app.engines import (
    assign_speakers,
    normalize_segments,
    segments_from_funasr,
    segments_from_whisper,
    strip_sensevoice_tags,
)
from app.transcript import render_transcript
from opencc import OpenCC


def test_strip_sensevoice_tags() -> None:
    raw = "<|zh|><|NEUTRAL|><|Speech|><|withitn|>你好世界 <|HAPPY|>"
    assert strip_sensevoice_tags(raw) == "你好世界"
    assert "<|" not in strip_sensevoice_tags(raw)


def test_funasr_speaker_order_and_milliseconds() -> None:
    result = [
        {
            "sentence_info": [
                {"text": "<|zh|><|NEUTRAL|>第二个人", "start": 2000, "end": 3500, "spk": 5},
                {"text": "<|zh|>第一个人", "start": 0, "end": 1500, "spk": 2},
                {"text": "<|zh|>第一个人再说", "start": 1500, "end": 2000, "spk": 2},
            ]
        }
    ]
    segments = segments_from_funasr(result)
    assert [(item["start"], item["end"], item["speaker"], item["text"]) for item in segments] == [
        (0.0, 1.5, "说话人1", "第一个人"),
        (1.5, 2.0, "说话人1", "第一个人再说"),
        (2.0, 3.5, "说话人2", "第二个人"),
    ]
    assert "<|" not in "".join(item["text"] for item in segments)


def test_funasr_fallback_strips_tags_when_no_sentences() -> None:
    segments = segments_from_funasr([{"text": "<|zh|>整段文本", "sentence_info": []}])
    assert segments == [{"start": 0.0, "end": 0.01, "speaker": None, "text": "整段文本"}]


def test_normalize_removes_overlap() -> None:
    segments = normalize_segments(
        [
            {"start": 1.0, "end": 3.0, "speaker": "说话人2", "text": "乙"},
            {"start": 0.0, "end": 2.0, "speaker": "说话人1", "text": "甲"},
        ]
    )
    assert segments == [
        {"start": 0.0, "end": 2.0, "speaker": "说话人1", "text": "甲"},
        {"start": 2.0, "end": 3.0, "speaker": "说话人2", "text": "乙"},
    ]


class _Piece:
    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end


def test_assign_speakers_by_max_overlap_keeps_null_without_overlap() -> None:
    whisper = [
        {"start": 0.0, "end": 2.0, "speaker": None, "text": "甲"},
        {"start": 2.0, "end": 4.0, "speaker": None, "text": "乙"},
        {"start": 10.0, "end": 11.0, "speaker": None, "text": "无人"},
    ]
    spans = [
        {"start": 0.0, "end": 1.5, "speaker": "说话人1", "text": ""},
        {"start": 1.8, "end": 4.2, "speaker": "说话人2", "text": ""},
    ]
    result = assign_speakers(whisper, spans)
    assert result[0]["speaker"] == "说话人1"
    assert result[1]["speaker"] == "说话人2"
    assert result[2]["speaker"] is None
    assert result[0]["text"] == "甲"


def test_whisper_simplified_and_null_speaker() -> None:
    segments = segments_from_whisper(
        [_Piece("繁體中文 Hello", 0.2, 1.4)],
        OpenCC("t2s").convert,
    )
    assert segments == [
        {"start": 0.2, "end": 1.4, "speaker": None, "text": "繁体中文 Hello"},
    ]


def test_transcript_header_and_optional_speaker() -> None:
    fast = render_transcript(
        source_name="demo.wav",
        duration=65.4,
        tier="fast",
        generated_at="2026-10-25T01:02:03Z",
        segments=[
            {"start": 0.2, "end": 1.0, "speaker": "说话人1", "text": "你好"},
            {"start": 62.0, "end": 64.0, "speaker": "说话人2", "text": "下一段"},
        ],
    )
    assert "文件名：demo.wav" in fast
    assert "时长：65.4 秒" in fast
    assert "档位：fast" in fast
    assert "生成时间：2026-10-25T01:02:03Z" in fast
    assert "[00:00] 说话人1：你好" in fast
    assert "[01:02] 说话人2：下一段" in fast

    precise = render_transcript(
        source_name="mix.wav",
        duration=3.0,
        tier="precise",
        generated_at="2026-10-25T01:02:03Z",
        segments=[{"start": 1.0, "end": 2.0, "speaker": None, "text": "今天天气不错"}],
    )
    assert "档位：precise" in precise
    assert "[00:01] 今天天气不错" in precise
    assert "说话人" not in precise
