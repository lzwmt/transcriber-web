"""向本机服务提交一条 fast 任务，并打印逐字稿或失败原因。"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WAV = ROOT / "tests" / "fixtures" / "s2_60.wav"


def _request(url: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> bytes:
    method = "POST" if data else "GET"
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read()


def _post_wav(base: str, wav: Path) -> str:
    boundary = "----transcribersmoke"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="tier"\r\n\r\nfast\r\n',
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="file"; filename="{wav.name}"\r\n'
                "Content-Type: audio/wav\r\n\r\n"
            ).encode(),
            wav.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    raw = _request(
        f"{base}/jobs",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    payload = json.loads(raw.decode())
    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise SystemExit(f"提交失败：{payload}")
    return job_id


def main() -> int:
    parser = argparse.ArgumentParser(description="提交本地音频并打印 fast 档逐字稿")
    parser.add_argument("wav", nargs="?", type=Path, default=DEFAULT_WAV)
    parser.add_argument("--base", default="http://127.0.0.1:8900")
    parser.add_argument("--timeout", type=float, default=1800.0)
    args = parser.parse_args()
    wav: Path = args.wav
    if not wav.is_file():
        print(f"找不到样本：{wav}", file=sys.stderr)
        return 1
    base = str(args.base).rstrip("/")
    try:
        health = json.loads(_request(f"{base}/health").decode())
    except urllib.error.URLError as exc:
        print(f"服务不可用：{exc}", file=sys.stderr)
        return 1
    if health != {"ok": True}:
        print(f"健康检查异常：{health}", file=sys.stderr)
        return 1
    job_id = _post_wav(base, wav)
    print(f"job_id={job_id}")
    deadline = time.time() + float(args.timeout)
    status: dict[str, object] = {}
    while time.time() < deadline:
        status = json.loads(_request(f"{base}/jobs/{job_id}").decode())
        print(f"status={status.get('status')}")
        if status.get("status") in {"done", "error"}:
            break
        time.sleep(1)
    else:
        print("等待超时", file=sys.stderr)
        return 1
    if status.get("status") != "done":
        print(f"任务失败：{status.get('error')}", file=sys.stderr)
        return 1
    transcript = _request(f"{base}/jobs/{job_id}/transcript").decode()
    print(transcript)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
