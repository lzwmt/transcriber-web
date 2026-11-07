"""压测脚本的纯函数。不联网、不调 docker、不跑引擎。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_STRESS_PATH = Path(__file__).resolve().parent.parent / "scripts" / "stress.py"


def _load_stress() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stress_script", _STRESS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {_STRESS_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stress = _load_stress()


def test_parse_docker_mem_mebibytes() -> None:
    assert stress.parse_docker_mem_mb("512MiB / 26GiB") == 512


def test_parse_docker_mem_gibibytes() -> None:
    assert stress.parse_docker_mem_mb("1.5GiB / 26GiB") == 1536


def test_last_timestamp_without_hours() -> None:
    text = "[01:02:03] 早段\n[120:30] 末段\n"
    assert stress.last_segment_end_s(text) == 7230


def test_last_timestamp_with_hours() -> None:
    text = "[120:30] 早段\n[02:05:07] 末段\n"
    assert stress.last_segment_end_s(text) == 7507


@pytest.mark.parametrize(
    ("last_s", "audio_s", "expected"),
    [
        (95.0, 100.0, 5.0),
        (110.0, 100.0, 10.0),
        (100.0, 100.0, 0.0),
    ],
)
def test_coverage_deviation_pct(last_s: float, audio_s: float, expected: float) -> None:
    assert stress.coverage_deviation_pct(last_s, audio_s) == expected


_MINUTES_COMPLETE = """# 纪要

## 一句话摘要
测

## 议题
- 一项

## 决定
无

## 待办
- [ ] 办事（负责人：待定）

## 未定事项
无
"""

_MINUTES_MISSING_TODO = """# 纪要

## 一句话摘要
测

## 议题
讨论了待办但没有单独成节

## 决定
无

## 未定事项
无
"""


def test_minutes_sections_ok_when_all_five_headings_present() -> None:
    assert stress.minutes_sections_ok(_MINUTES_COMPLETE) is True


def test_minutes_sections_ok_false_when_heading_missing() -> None:
    assert stress.minutes_sections_ok(_MINUTES_MISSING_TODO) is False


# 末段 [00:01:36] = 96 秒。音频 100 秒时偏差是 4%，低于 5% 判据。
_TRANSCRIPT_WITHIN_COVERAGE = "[00:00:01] 起\n[00:01:36] 止\n".encode()


def test_artifact_checks_keeps_transcript_fields_when_minutes_missing() -> None:
    """纪要与合并稿都拿不到时，转写侧仍按逐字稿和时长填写，纪要侧为 false。"""
    checks = stress.artifact_checks(_TRANSCRIPT_WITHIN_COVERAGE, b"", b"", 100.0)
    assert checks == {
        "transcript_nonempty": True,
        "last_segment_end_s": 96.0,
        "coverage_deviation_pct": 4.0,
        "coverage_ok": True,
        "minutes_sections_ok": False,
        "combined_nonempty": False,
    }


def test_artifact_checks_passes_when_transcript_minutes_and_combined_present() -> None:
    transcript = _TRANSCRIPT_WITHIN_COVERAGE
    minutes = _MINUTES_COMPLETE.encode()
    combined = (_MINUTES_COMPLETE + "\n\n---\n\n[00:01:36] 止\n").encode()
    checks = stress.artifact_checks(transcript, minutes, combined, 100.0)
    assert checks == {
        "transcript_nonempty": True,
        "last_segment_end_s": 96.0,
        "coverage_deviation_pct": 4.0,
        "coverage_ok": True,
        "minutes_sections_ok": True,
        "combined_nonempty": True,
    }


def test_artifact_checks_coverage_not_ok_at_five_percent() -> None:
    # [01:35] = 95 秒。偏差正好 5%，判据是严格小于 5%。
    transcript = "[01:35] 止\n".encode()
    checks = stress.artifact_checks(
        transcript,
        _MINUTES_COMPLETE.encode(),
        "合并".encode(),
        100.0,
    )
    assert checks == {
        "transcript_nonempty": True,
        "last_segment_end_s": 95.0,
        "coverage_deviation_pct": 5.0,
        "coverage_ok": False,
        "minutes_sections_ok": True,
        "combined_nonempty": True,
    }
