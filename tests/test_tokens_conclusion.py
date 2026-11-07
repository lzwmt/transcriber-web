"""TOKENS 结论文案的变量个数随输入字典变化。"""

import pytest

pytest.importorskip("playwright")

from verify.checks.tokens import _counts_phrase  # noqa: E402


def test_counts_phrase_follows_input_sizes() -> None:
    md = {
        "ledger": {"a": "1", "b": "2", "c": "3"},
        "studio": {str(i): "x" for i in range(5)},
    }
    css = {
        "ledger": {"a": "1", "b": "2", "c": "9"},
        "studio": {str(i): "y" for i in range(5)},
    }
    text = _counts_phrase(md, css)
    assert "3" in text
    assert "5" in text
    assert "24" not in text
