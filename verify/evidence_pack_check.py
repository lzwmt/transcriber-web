#!/usr/bin/env python3
"""证据包校验脚本（定稿 v1.1，2026-10-31；含 temporal/asserts_values 条件必备件检查）

按 templates/dev-workflow/evidence-pack-spec.md（evidence-pack/1）校验一个证据包目录：
缺件、哈希不对、结论三方不一致、未声明的重复截图，一律打回（exit 1）。

用法:
    evidence-pack-check.py <pack-root> [--expect items.txt]

--expect 文件为一行一个条目号，要求这些条目在 manifest 与 results.md 中齐全。
exit: 0 通过（可带 WARN）；1 校验不通过；2 用法/IO 错误。
仅用 Python 标准库。
"""
import hashlib
import json
import re
import sys
from pathlib import Path

REQUIRED_BY_TYPE = {
    "ui": ["verdict.md", "log.txt", "shot-final.png", "page.txt"],
    "behavior": ["verdict.md", "log.txt", "run.txt"],
    "code": ["verdict.md", "log.txt"],
}
VERDICT_RE = re.compile(r"\b(PASS|FAIL)\b")

errors: list[str] = []
warnings: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def first_verdict(text: str) -> str | None:
    m = VERDICT_RE.search(text)
    return m.group(1) if m else None


def parse_results(md: str) -> dict[str, str]:
    """results.md: 每条一节 `## <id>`，节内须出现字面 PASS/FAIL，取首个为结论。"""
    out: dict[str, str] = {}
    sections = re.split(r"^##\s+", md, flags=re.M)
    for sec in sections[1:]:
        lines = sec.splitlines()
        if not lines:
            continue
        item_id = lines[0].strip().split()[0] if lines[0].strip() else ""
        if not item_id:
            continue
        v = first_verdict("\n".join(lines[1:]))
        if v is None:
            err(f"results.md 条目 {item_id}: 节内无字面 PASS/FAIL")
        else:
            out[item_id] = v
    return out


def main(argv: list[str]) -> int:
    args: list[str] = []
    expect_path = None
    it = iter(range(1, len(argv)))
    for i in it:
        tok = argv[i]
        if tok == "--expect":
            if i + 1 >= len(argv):
                print("ERROR: --expect 需要一个文件路径", file=sys.stderr)
                return 2
            expect_path = Path(argv[i + 1])
            next(it, None)
        elif tok.startswith("--"):
            print(f"ERROR: 未知参数 {tok}", file=sys.stderr)
            return 2
        else:
            args.append(tok)
    if len(args) != 1:
        print(__doc__.strip().splitlines()[4], file=sys.stderr)
        return 2
    root = Path(args[0])
    if not root.is_dir():
        print(f"ERROR: 证据包目录不存在: {root}", file=sys.stderr)
        return 2

    # --- 必备顶层文件 ---
    results_file = root / "results.md"
    manifest_file = root / "manifest.json"
    if not results_file.is_file():
        err("缺 results.md")
    if not manifest_file.is_file():
        err("缺 manifest.json")
    if errors:
        return report(root)

    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        err(f"manifest.json 解析失败: {e}")
        return report(root)

    if manifest.get("schema") != "evidence-pack/1":
        err(f"manifest.schema 应为 evidence-pack/1，实际: {manifest.get('schema')!r}")
    for key in ("pack_id", "project", "producer", "created_at"):
        if not manifest.get(key):
            err(f"manifest 缺字段: {key}")
    checklist = manifest.get("checklist") or {}
    for key in ("name", "rev", "sha256"):
        if not checklist.get(key):
            err(f"manifest.checklist 缺字段: {key}")
    subject = manifest.get("subject") or {}
    for key in ("branch", "range", "base"):
        if not subject.get(key):
            err(f"manifest.subject 缺字段: {key}")

    results = parse_results(results_file.read_text(encoding="utf-8"))

    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        err("manifest.items 为空或不是数组")
        return report(root)

    seen_ids: set[str] = set()
    shot_hashes: dict[str, str] = {}  # sha256 -> item id
    registered: set[str] = set()

    for item in items:
        iid = item.get("id")
        itype = item.get("type")
        iverdict = item.get("verdict")
        if not iid:
            err("manifest.items 有条目缺 id")
            continue
        if iid in seen_ids:
            err(f"条目 {iid}: manifest 中重复出现")
        seen_ids.add(iid)
        if itype not in REQUIRED_BY_TYPE:
            err(f"条目 {iid}: type 非法: {itype!r}（应为 ui/behavior/code）")
            continue
        if iverdict not in ("PASS", "FAIL"):
            err(f"条目 {iid}: manifest verdict 非法: {iverdict!r}")

        idir = root / "items" / iid
        if not idir.is_dir():
            err(f"条目 {iid}: 缺目录 items/{iid}/")
            continue

        # 条件必备件（v1.1）：temporal -> snapshots.txt；asserts_values -> values.txt
        cond: list[str] = []
        if item.get("temporal") is True:
            cond.append("snapshots.txt")
        if item.get("asserts_values") is True:
            cond.append("values.txt")

        # 必备文件
        for name in REQUIRED_BY_TYPE[itype] + cond:
            if not (idir / name).is_file():
                err(f"条目 {iid}: 缺必备文件 items/{iid}/{name}")

        # manifest 登记与哈希
        files = item.get("files")
        if not isinstance(files, list) or not files:
            err(f"条目 {iid}: manifest files 为空")
            files = []
        listed_here: set[str] = set()
        for entry in files:
            rel = entry.get("path", "")
            want = entry.get("sha256", "")
            registered.add(rel)
            listed_here.add(Path(rel).name)
            fpath = root / rel
            if not fpath.is_file():
                err(f"条目 {iid}: manifest 登记的文件不存在: {rel}")
                continue
            actual = sha256_of(fpath)
            if actual != want:
                err(f"条目 {iid}: 哈希不一致: {rel}（manifest {want[:12]}… 实际 {actual[:12]}…）")
            if entry.get("role") == "shot":
                prev = shot_hashes.get(actual)
                if prev and prev != iid and not item.get("duplicate_of"):
                    err(f"条目 {iid}: 截图与条目 {prev} 哈希相同且未声明 duplicate_of")
                shot_hashes.setdefault(actual, iid)
        for name in REQUIRED_BY_TYPE[itype] + cond:
            if name not in listed_here:
                err(f"条目 {iid}: 必备文件 {name} 未登记进 manifest")

        # 三方结论一致：manifest / results.md / verdict.md
        vfile = idir / "verdict.md"
        if vfile.is_file():
            vv = first_verdict(vfile.read_text(encoding="utf-8", errors="replace"))
            if vv is None:
                err(f"条目 {iid}: verdict.md 无字面 PASS/FAIL")
            elif iverdict in ("PASS", "FAIL") and vv != iverdict:
                err(f"条目 {iid}: verdict.md 结论 {vv} 与 manifest {iverdict} 不一致")
        rv = results.get(iid)
        if rv is None:
            err(f"条目 {iid}: results.md 中无此条目")
        elif iverdict in ("PASS", "FAIL") and rv != iverdict:
            err(f"条目 {iid}: results.md 结论 {rv} 与 manifest {iverdict} 不一致")

    # results.md 多出的条目
    for rid in results:
        if rid not in seen_ids:
            err(f"results.md 条目 {rid} 不在 manifest.items 中")

    # 未登记文件（警告）
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            if rel not in ("results.md", "manifest.json") and rel not in registered:
                warn(f"未登记进 manifest 的文件: {rel}")

    # --expect 完整性
    if expect_path is not None:
        if not expect_path.is_file():
            print(f"ERROR: --expect 文件不存在: {expect_path}", file=sys.stderr)
            return 2
        for line in expect_path.read_text(encoding="utf-8").splitlines():
            eid = line.strip()
            if eid and eid not in seen_ids:
                err(f"--expect 条目 {eid} 不在证据包中")

    return report(root)


def report(root: Path) -> int:
    for m in errors:
        print(f"ERROR: {m}")
    for m in warnings:
        print(f"WARN: {m}")
    if errors:
        print(f"FAIL: {root} 不合规（{len(errors)} 个错误，{len(warnings)} 个警告）")
        return 1
    print(f"OK: {root} 合规（{len(warnings)} 个警告）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
