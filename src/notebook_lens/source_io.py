"""CLI source input helpers."""

from __future__ import annotations

import sys
from pathlib import Path


def read_source(file_path: str | None) -> str:
    if file_path in (None, "-"):
        return sys.stdin.read()
    raw = Path(file_path)
    resolved = raw if raw.is_absolute() else Path.cwd() / raw
    try:
        return raw.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"source file not found: {resolved.resolve(strict=False)} "
            "(--file paths are relative to the current working directory)"
        ) from exc
