from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# 测试气密（LZW-84 / FIX2）：accept 门禁会把 secrets 文件的键（如
# TRANSCRIBER_FEAT_RECORDING=1、TRANSCRIBER_LLM_PROVIDER）直接注入门禁进程的
# os.environ，而 app.config.load_secret_file() 只填「不在 os.environ 的键」，
# 所以光重定向文件挡不住已注入的键。故先中和进程里所有 TRANSCRIBER_ 前缀键，
# 再把 secrets 文件路径指到不存在的位置。**顺序不可反**：先 pop 再重定向。
# TRANSCRIBER_SECRETS_FILE 本身保留，由下一行重新设定。
for _key in [
    k for k in os.environ if k.startswith("TRANSCRIBER_") and k != "TRANSCRIBER_SECRETS_FILE"
]:
    del os.environ[_key]

# 必须在导入 app 之前设置（app.config 读该变量决定读哪个文件）。
os.environ["TRANSCRIBER_SECRETS_FILE"] = str(Path(__file__).resolve().parent / "_no_secrets.env")

from app.main import create_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Path]]:
    data = tmp_path / "data"
    monkeypatch.setenv("TRANSCRIBER_STUB", "1")
    monkeypatch.setenv("TRANSCRIBER_STUB_SLEEP", "0")
    monkeypatch.setenv("TRANSCRIBER_DATA_DIR", str(data))
    monkeypatch.delenv("TRANSCRIBER_MAX_DURATION_SECONDS", raising=False)
    monkeypatch.delenv("TRANSCRIBER_MAX_UPLOAD_BYTES", raising=False)
    monkeypatch.delenv("TRANSCRIBER_ENGINE_MARKER", raising=False)
    monkeypatch.delenv("TRANSCRIBER_STUB_LLM", raising=False)
    monkeypatch.delenv("TRANSCRIBER_STUB_LLM_ERROR", raising=False)
    monkeypatch.delenv("TRANSCRIBER_STUB_LLM_ERROR_PATH", raising=False)
    monkeypatch.delenv("TRANSCRIBER_STUB_LLM_TEXT", raising=False)
    monkeypatch.delenv("TRANSCRIBER_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("TRANSCRIBER_LLM_API_KEY", raising=False)
    monkeypatch.delenv("TRANSCRIBER_LLM_MODEL", raising=False)
    monkeypatch.delenv("TRANSCRIBER_LLM_BASE_URL", raising=False)
    # LZW-86 气密补强：录音开关按「未设=默认关」的用例前置条件 scrub。
    # conftest 导入期的 pop 挡不住「conftest 导入后才进入本进程」的 ambient
    # 注入（accept 门禁 setupenv 注入过 TRANSCRIBER_FEAT_RECORDING=1，见
    # RESULT-LZW-84.md FIX2 节）；这里按用例前置条件再清一道，把 d0b 类
    # 「默认关」判据与进程外环境解耦。用例如需开开关仍自行 setenv。
    monkeypatch.delenv("TRANSCRIBER_FEAT_RECORDING", raising=False)
    app = create_app(data)
    with TestClient(app) as test_client:
        yield test_client, data
