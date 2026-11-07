"""逐字稿 Markdown。speaker 为空时不写说话人前缀。"""

from __future__ import annotations

from app.engines import Segment


def format_mmss(seconds: float) -> str:
    total = int(max(0.0, seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def render_transcript(
    *,
    source_name: str,
    duration: float,
    tier: str,
    generated_at: str,
    segments: list[Segment],
) -> str:
    lines = [
        "# 逐字稿",
        "",
        f"- 文件名：{source_name}",
        f"- 时长：{duration:.1f} 秒",
        f"- 档位：{tier}",
        f"- 生成时间：{generated_at}",
        "",
    ]
    for segment in segments:
        stamp = format_mmss(segment["start"])
        text = " ".join(segment["text"].split())
        speaker = segment["speaker"]
        if speaker:
            lines.append(f"[{stamp}] {speaker}：{text}")
        else:
            lines.append(f"[{stamp}] {text}")
    lines.append("")
    return "\n".join(lines)
