"""Shared policy for env vars mirrored into notebook kernels."""

from __future__ import annotations

import os
from collections.abc import Mapping

SYNC_ENV_PREFIXES = ("HF_", "HUGGINGFACE_", "ML_", "NL_", "NOTEBOOK_LENS_")
SECRET_NAME_PARTS = ("TOKEN", "SECRET", "KEY", "PASSWORD")


def is_synced_env_key(key: str) -> bool:
    return key.startswith(SYNC_ENV_PREFIXES)


def synced_env_snapshot(env: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if env is None else env
    return {key: value for key, value in sorted(source.items()) if is_synced_env_key(key)}


def looks_secret(name: str) -> bool:
    upper = name.upper()
    return any(part in upper for part in SECRET_NAME_PARTS)


def safe_env_lines(env: Mapping[str, str] | None = None, *, value_limit: int = 160) -> list[str]:
    lines = []
    for key, value in synced_env_snapshot(env).items():
        rendered = "<set>" if looks_secret(key) else _truncate(_one_line(value), value_limit)
        lines.append(f"- {key}={rendered}")
    return lines


def _one_line(text: str) -> str:
    return " ".join(text.strip().split())


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text.rstrip()
    return text[:limit].rstrip() + "\n...[truncated]"
