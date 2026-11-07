"""写出单条证据目录。整包 manifest 不在本单。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from verify import CHECKLIST_PATH, ITEMS_DIR, ROOT


@dataclass
class CheckResult:
    item_id: str
    passed: bool
    verdict_md: str
    log_txt: str
    page_txt: str
    screenshot: bytes
    extra: dict[str, str | bytes] = field(default_factory=dict)


def checklist_line(prefix: str) -> str:
    candidates = [
        CHECKLIST_PATH,
        ROOT / ".handoff" / "BACKLOG-SWEEP-CHECKLIST.md",
        ROOT / "docs" / "BACKLOG-SWEEP-CHECKLIST.md",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(prefix):
                return line.strip()
    raise RuntimeError(f"清单没有以 {prefix!r} 开头的行")


def render_verdict(
    *,
    item_id: str,
    criterion: str,
    passed: bool,
    conclusion: str,
    files: list[str],
    anomalies: str,
    section: str = "V2a",
) -> str:
    status = "PASS" if passed else "FAIL"
    pointers = "\n".join(f"- {path}" for path in files)
    checklist_doc = (
        "docs/BACKLOG-SWEEP-CHECKLIST.md"
        if section.startswith("F")
        else "docs/VERIFY-PILOT-CHECKLIST.md"
    )
    return (
        f"# {item_id}\n\n"
        f"判据（{checklist_doc} {section}）：\n"
        f"> {criterion}\n\n"
        f"判定：{status}\n\n"
        f"{conclusion}\n\n"
        "证据：\n"
        f"{pointers}\n\n"
        f"异常：{anomalies}\n"
    )


def write_item(result: CheckResult) -> Path:
    if not result.screenshot.startswith(b"\x89PNG"):
        raise RuntimeError(f"{result.item_id} 的截图不是 PNG")
    directory = ITEMS_DIR / result.item_id
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    _text(directory / "verdict.md", result.verdict_md)
    _text(directory / "log.txt", result.log_txt)
    _text(directory / "page.txt", result.page_txt)
    (directory / "shot-final.png").write_bytes(result.screenshot)
    for name, content in result.extra.items():
        target = directory / name
        if isinstance(content, str):
            _text(target, content)
        else:
            target.write_bytes(content)
    return directory


def _text(path: Path, content: str) -> None:
    if not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")
