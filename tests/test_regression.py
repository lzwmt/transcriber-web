"""真实样本回归。默认随 pytest 一起跑。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.engines import Segment, transcribe

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# S3 口播里能稳定听到的词，以及它们的繁体写法。期望来自样本，不是对实现再算一遍。
_S3_SIMPLIFIED = ("电脑", "编程", "语言", "课程")
_S3_TRADITIONAL = ("電腦", "編程", "語言", "課程")


@pytest.fixture(scope="module")
def s3_precise() -> list[Segment]:
    return transcribe(str(FIXTURES / "s3_60.wav"), "precise")


@pytest.mark.slow
def test_b10_s5_fast_has_two_speakers() -> None:
    segments = transcribe(str(FIXTURES / "s5_60.wav"), "fast")
    speakers = {segment["speaker"] for segment in segments}
    assert len(speakers) >= 2
    for speaker in speakers:
        assert speaker is not None
        assert re.fullmatch(r"说话人\d+", speaker)
    previous = -1.0
    for segment in segments:
        assert segment["start"] < segment["end"]
        assert segment["start"] >= previous
        previous = segment["end"]


@pytest.mark.slow
def test_b11_s2_fast_opening_without_tags() -> None:
    segments = transcribe(str(FIXTURES / "s2_60.wav"), "fast")
    text = "".join(segment["text"] for segment in segments)
    assert "有多少人是以父母的身份来到这个现场的" in text
    assert "<|" not in text
    assert len(text) > 40


@pytest.mark.slow
def test_b12_s3_precise_segments_nonempty(s3_precise: list[Segment]) -> None:
    assert len(s3_precise) >= 1
    assert all(segment["text"].strip() for segment in s3_precise)
    assert any("Python" in segment["text"] for segment in s3_precise)


@pytest.mark.slow
def test_b13_s3_precise_simplified(s3_precise: list[Segment]) -> None:
    """口径变更：第 3 步起精档 speaker 不再恒为 null，本条只断言繁转简。"""
    text = "".join(segment["text"] for segment in s3_precise)
    found = [word for word in _S3_SIMPLIFIED if word in text]
    assert len(found) >= 2
    for traditional in _S3_TRADITIONAL:
        assert traditional not in text


@pytest.mark.slow
def test_c7_s5_precise_has_two_speakers() -> None:
    segments = transcribe(str(FIXTURES / "s5_60.wav"), "precise")
    speakers = {segment["speaker"] for segment in segments if segment["speaker"]}
    assert len(speakers) >= 2
    for speaker in speakers:
        assert re.fullmatch(r"说话人\d+", speaker)
