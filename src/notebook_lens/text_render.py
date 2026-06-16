"""Shared cleanup for text rendered in CLI and notebook state output."""

from __future__ import annotations

import re
from typing import Any


def text_value(value: Any) -> str:
    if isinstance(value, list):
        return "".join(str(part) for part in value)
    return str(value)


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)


def clean_stream_text(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        if "\r" in line:
            line = line.split("\r")[-1]
        lines.append(line)
    return "\n".join(lines)


def trim_blank_lines(text: str) -> str:
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def join_stream_parts(parts: list[str]) -> str:
    cleaned = [trim_blank_lines(clean_stream_text(part)) for part in parts]
    return "\n".join(part for part in cleaned if part)


def truncate_head_tail(text: str, limit: int | None) -> str:
    """Truncate long text while preserving both the beginning and end."""

    if limit is None:
        return text
    if len(text) <= limit:
        return text.rstrip()
    if limit <= 0:
        return f"...[truncated] {len(text)} chars omitted"

    fallback_marker = f"\n...[truncated] {max(0, len(text) - limit)} chars omitted"
    if limit < 80:
        return text[:limit].rstrip() + fallback_marker

    marker = ""
    head_len = tail_len = 0
    for _ in range(3):
        keep = max(20, limit - len(marker))
        head_len = max(1, keep // 2)
        tail_len = max(1, keep - head_len)
        omitted = max(0, len(text) - head_len - tail_len)
        marker = f"\n...[truncated] {omitted} chars omitted...\n"

    keep = max(20, limit - len(marker))
    head_len = max(1, keep // 2)
    tail_len = max(1, keep - head_len)
    omitted = max(0, len(text) - head_len - tail_len)
    marker = f"\n...[truncated] {omitted} chars omitted...\n"

    head = text[:head_len].rstrip()
    tail = text[-tail_len:].lstrip()
    return f"{head}{marker}{tail}".rstrip()


def was_truncated(text: str, limit: int | None) -> bool:
    return limit is not None and len(text) > limit
