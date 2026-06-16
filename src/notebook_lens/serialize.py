"""Serialize notebooks for CLI JSON payloads."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nbformat import NotebookNode

from .cell_state import (
    cell_source,
    cell_source_hash,
    cell_stale,
    cell_status,
    executed_source_hash,
    source_changed_since_execution,
)
from .notebooks import META_KEY
from .rich_output import image_summary
from .text_render import clean_stream_text, text_value, trim_blank_lines


def notebook_payload_from_loaded(
    nb: NotebookNode,
    rel: str,
    *,
    digest: str | None = None,
) -> dict[str, Any]:
    """Return a serialized notebook payload from an already-loaded notebook."""

    return {
        "path": rel,
        "hash": digest,
        "title": Path(rel).name,
        "cell_count": len(nb.cells),
        "cells": [_cell_payload(index, cell) for index, cell in enumerate(nb.cells)],
    }


def _cell_payload(index: int, cell: NotebookNode) -> dict[str, Any]:
    meta = cell.get("metadata", {}).get(META_KEY, {})
    outputs = cell.get("outputs", []) if cell.get("cell_type") == "code" else []
    cell_id = str(cell.get("id") or "")
    return {
        "index": index,
        "id": cell_id,
        "cell_id": cell_id,
        "type": cell.get("cell_type", "unknown"),
        "source": cell_source(cell),
        "description": str(meta.get("description") or ""),
        "status": cell_status(cell),
        "stale": cell_stale(cell),
        "source_hash": cell_source_hash(cell),
        "executed_source_hash": executed_source_hash(cell),
        "source_changed_since_execution": source_changed_since_execution(cell),
        "stale_reason": str(meta.get("stale_reason") or ""),
        "execution_count": cell.get("execution_count"),
        "outputs": [_output_payload(output) for output in outputs],
    }


def _output_payload(output: NotebookNode) -> dict[str, Any]:
    kind = output.get("output_type", "unknown")
    if kind == "stream":
        return {
            "type": "stream",
            "name": output.get("name", "stdout"),
            "text": trim_blank_lines(clean_stream_text(_text(output.get("text", "")))),
        }
    if kind == "error":
        traceback = output.get("traceback") or []
        return {
            "type": "error",
            "ename": output.get("ename", "Error"),
            "evalue": output.get("evalue", ""),
            "traceback": "\n".join(str(line) for line in traceback),
        }
    if kind in {"display_data", "execute_result"}:
        data = output.get("data", {})
        return {
            "type": kind,
            "items": [
                _mime_payload(mimetype, value) for mimetype, value in data.items()
            ],
        }
    return {"type": kind, "raw": str(output)}


def _mime_payload(mimetype: str, value: Any) -> dict[str, Any]:
    text = _text(value)
    if mimetype.startswith("image/"):
        return {
            "mimetype": mimetype,
            "kind": "image",
            "summary": image_summary(mimetype, value),
        }
    if mimetype == "text/html":
        return {"mimetype": mimetype, "kind": "html", "html": text}
    return {"mimetype": mimetype, "kind": "text", "text": text}


def _text(value: Any) -> str:
    return text_value(value)
