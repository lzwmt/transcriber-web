from __future__ import annotations

import ast
import re
from pathlib import Path

from app.config import LLM_PROVIDERS, TIER_CONFIG

ROOT = Path(__file__).resolve().parent.parent
_SECRET = re.compile(
    r"(sk-[A-Za-z0-9]{10,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})"
)
_SKIP_DIRS = {
    ".git",
    ".venv",
    "data",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
}
_TEXT_SUFFIXES = {".py", ".md", ".yml", ".yaml", ".toml", ".sh", ".txt"}


def test_b17_docs_forbid_public_exposure() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "禁止公网暴露" in agents
    assert "Tailscale" in readme
    assert "内网" in readme
    assert "鉴权" in readme


def test_b18_repo_has_no_secret_literals() -> None:
    offenders: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix not in _TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _SECRET.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def _parametrize_cases(node: ast.FunctionDef) -> int:
    """一条参数化测试按 pytest 展开后的用例数计。"""
    cases = 1
    for dec in node.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        func = dec.func
        if not isinstance(func, ast.Attribute) or func.attr != "parametrize":
            continue
        if len(dec.args) < 2 or not isinstance(dec.args[1], ast.List | ast.Tuple):
            raise AssertionError(f"无法静态统计参数化用例: {node.name}")
        cases *= len(dec.args[1].elts)
    return cases


def test_b19_baseline_matches_collected_tests() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    match = re.search(r"测试基线：(\d+)", agents)
    assert match is not None
    registered = int(match.group(1))
    collected = 0
    for path in (ROOT / "tests").rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                collected += _parametrize_cases(node)
    assert registered == collected
    gates = (ROOT / "transcriber.yaml").read_text(encoding="utf-8")
    assert "uv run pytest" in gates
    assert "uv run mypy app" in gates
    assert "uv run ruff check ." in gates
    assert "build: 无" in gates


def test_b20_model_ids_live_only_in_config() -> None:
    ids: list[str] = []
    for cfg in TIER_CONFIG.values():
        for key in ("model", "vad_model", "spk_model", "compute_type"):
            if key in cfg:
                ids.append(str(cfg[key]))
    for cfg in LLM_PROVIDERS.values():
        # 只收集非空值：custom 的 model/base_url 默认为空字符串，
        # 空串做「不出现在 app/」断言恒假（C1 修订口径，r1 B1）。
        for key in ("model", "base_url"):
            value = str(cfg[key])
            if value:
                ids.append(value)
    assert "iic/SenseVoiceSmall" in ids
    assert "fsmn-vad" in ids
    assert "cam++" in ids
    assert "medium" in ids
    assert "int8" in ids
    assert any("stepfun" in item for item in ids)
    blob_parts: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        if path.name == "config.py":
            continue
        blob_parts.append(path.read_text(encoding="utf-8"))
    blob = "\n".join(blob_parts)
    for ident in ids:
        assert ident not in blob, ident
    engines = {str(cfg["engine"]) for cfg in TIER_CONFIG.values()}
    assert engines == {"funasr", "faster_whisper"}
