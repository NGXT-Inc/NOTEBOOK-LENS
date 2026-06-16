"""Helpers for summarizing notebook rich outputs."""

from __future__ import annotations

import base64
import json
from typing import Any

from .text_render import text_value

IMAGE_EXTENSIONS = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}
TEXT_EXTENSIONS = {
    "text/html": ".html",
    "text/latex": ".tex",
    "text/markdown": ".md",
}
EXPORTABLE_MIME_EXTENSIONS = {
    **IMAGE_EXTENSIONS,
    **TEXT_EXTENSIONS,
}


def rich_mime_summary(data: dict[str, Any]) -> str:
    """Return a concise agent-facing summary of a rich output mimebundle."""

    return ", ".join(mime_summary(mimetype, data[mimetype]) for mimetype in sorted(data)) or "unknown"


def mime_summary(mimetype: str, value: Any) -> str:
    """Return a concise summary for one rich MIME value."""

    if mimetype.startswith("image/"):
        if mimetype == "image/svg+xml":
            return _text_mime_summary(mimetype, value)
        return image_summary(mimetype, value)
    if mimetype == "application/json":
        return "application/json payload"
    return _text_mime_summary(mimetype, value)


def exportable_mime_summaries(data: dict[str, Any]) -> list[str]:
    return [
        mime_summary(mimetype, value)
        for mimetype, value in sorted(data.items())
        if mimetype in EXPORTABLE_MIME_EXTENSIONS
    ]


def json_preview(value: Any) -> str:
    """Return pretty JSON when possible, preserving invalid JSON strings as-is."""

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ""
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
    try:
        return json.dumps(value, indent=2, sort_keys=True)
    except TypeError:
        return text_value(value)


def image_summaries(data: dict[str, Any]) -> list[str]:
    return [
        image_summary(mimetype, value)
        for mimetype, value in sorted(data.items())
        if mimetype.startswith("image/")
    ]


def image_summary(mimetype: str, value: Any) -> str:
    if mimetype == "image/svg+xml":
        return _text_mime_summary(mimetype, value)
    encoded = "".join(text_value(value).split())
    raw = decode_base64(encoded)
    parts = [mimetype]
    if raw is not None:
        parts.append(f"{len(raw)} bytes")
        dimensions = _image_dimensions(mimetype, raw)
        if dimensions:
            width, height = dimensions
            parts.append(f"{width}x{height}px")
    else:
        parts.append(f"{len(encoded)} base64 chars")
    return f"{parts[0]} ({', '.join(parts[1:])})"


def decode_base64(encoded: str) -> bytes | None:
    try:
        return base64.b64decode(encoded, validate=False)
    except Exception:
        return None


def _image_dimensions(mimetype: str, raw: bytes) -> tuple[int, int] | None:
    if mimetype == "image/png":
        return _png_dimensions(raw)
    if mimetype == "image/gif":
        return _gif_dimensions(raw)
    if mimetype == "image/jpeg":
        return _jpeg_dimensions(raw)
    return None


def _text_mime_summary(mimetype: str, value: Any) -> str:
    text = text_value(value)
    parts = [mimetype, f"{len(text.encode('utf-8'))} bytes"]
    if mimetype in EXPORTABLE_MIME_EXTENSIONS:
        parts.append("exportable")
    return f"{parts[0]} ({', '.join(parts[1:])})"


def _png_dimensions(raw: bytes) -> tuple[int, int] | None:
    if len(raw) < 24 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")


def _gif_dimensions(raw: bytes) -> tuple[int, int] | None:
    if len(raw) < 10 or raw[:6] not in {b"GIF87a", b"GIF89a"}:
        return None
    return int.from_bytes(raw[6:8], "little"), int.from_bytes(raw[8:10], "little")


def _jpeg_dimensions(raw: bytes) -> tuple[int, int] | None:
    if len(raw) < 4 or not raw.startswith(b"\xff\xd8"):
        return None
    offset = 2
    while offset + 9 < len(raw):
        if raw[offset] != 0xFF:
            offset += 1
            continue
        marker = raw[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(raw):
            return None
        length = int.from_bytes(raw[offset : offset + 2], "big")
        if length < 2 or offset + length > len(raw):
            return None
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            height = int.from_bytes(raw[offset + 3 : offset + 5], "big")
            width = int.from_bytes(raw[offset + 5 : offset + 7], "big")
            return width, height
        offset += length
    return None
