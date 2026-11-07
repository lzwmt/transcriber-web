"""R9 功能开关读取口 feature_enabled：六态覆盖（开/关/未设/异常值/带空格值/非法 name）。

env 约定由规格冻结：TRANSCRIBER_FEAT_<NAME>，NAME = name 转大写。
值先 strip 再按 1/true/on/yes（不分大小写）判开；未设与其余值均为关（默认关）。
"""

from __future__ import annotations

import pytest
from app.config import feature_enabled

ENV = "TRANSCRIBER_FEAT_DEMO_FLAG"


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)


def test_state_on_accepts_all_four_literal_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("1", "true", "on", "yes"):
        monkeypatch.setenv(ENV, value)
        assert feature_enabled("demo_flag") is True


def test_state_on_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("TRUE", "True", "On", "YES", "YeS"):
        monkeypatch.setenv(ENV, value)
        assert feature_enabled("demo_flag") is True


def test_state_off_for_zero_and_false(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("0", "false", "FALSE", "no", "off"):
        monkeypatch.setenv(ENV, value)
        assert feature_enabled("demo_flag") is False


def test_state_unset_defaults_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    assert feature_enabled("demo_flag") is False


def test_state_unexpected_value_is_off_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("2", "enabled", "0.0", "-1"):
        monkeypatch.setenv(ENV, value)
        assert feature_enabled("demo_flag") is False


def test_state_padded_value_is_stripped_before_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in (" on ", "  1  ", "\ttrue\n", "  YES  "):
        monkeypatch.setenv(ENV, value)
        assert feature_enabled("demo_flag") is True


def test_state_padded_off_value_is_still_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, " 0 ")
    assert feature_enabled("demo_flag") is False


@pytest.mark.parametrize("bad_name", ["", "a-b", "a b", "Demo_Flag", "a.b", "开关"])
def test_invalid_name_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, bad_name: str
) -> None:
    _clear(monkeypatch)
    with pytest.raises(ValueError):
        feature_enabled(bad_name)


def test_name_maps_to_uppercased_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    # 约定证据：只有 <NAME 转大写> 这个变量被读到，其它写法不生效。
    _clear(monkeypatch)
    monkeypatch.setenv("TRANSCRIBER_FEAT_DEMO_FLAG", "1")
    monkeypatch.setenv("TRANSCRIBER_FEAT_demo_flag", "0")
    assert feature_enabled("demo_flag") is True


def test_two_names_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(ENV, "1")
    assert feature_enabled("demo_flag") is True
    assert feature_enabled("other_flag") is False
