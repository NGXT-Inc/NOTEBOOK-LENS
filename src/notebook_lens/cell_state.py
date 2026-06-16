"""Shared semantic helpers for notebook cells."""

from __future__ import annotations

import hashlib

from nbformat import NotebookNode

from .notebooks import META_KEY

SOURCE_HASH_ALGORITHM = "sha256"


def cell_source(cell: NotebookNode) -> str:
    source = cell.get("source", "")
    if isinstance(source, list):
        return "".join(source)
    return str(source)


def source_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def cell_source_hash(cell: NotebookNode) -> str:
    return source_hash(cell_source(cell))


def executed_source_hash(cell: NotebookNode) -> str:
    return str(_meta(cell).get("executed_source_hash") or "")


def source_changed_since_execution(cell: NotebookNode) -> bool:
    if cell.get("cell_type") != "code":
        return False
    executed_hash = executed_source_hash(cell)
    return bool(executed_hash) and executed_hash != cell_source_hash(cell)


def cell_status(cell: NotebookNode) -> str:
    meta = _meta(cell)
    if meta.get("stale"):
        return "stale"
    if source_changed_since_execution(cell):
        return "stale"
    if meta.get("status"):
        return str(meta["status"])
    if _has_error_output(cell):
        return "error"
    if cell.get("execution_count") is not None:
        return "ok"
    return "pending"


def cell_failed(cell: NotebookNode) -> bool:
    meta = _meta(cell)
    return meta.get("status") in {"error", "timeout"} or _has_error_output(cell)


def cell_stale(cell: NotebookNode) -> bool:
    return bool(_meta(cell).get("stale")) or source_changed_since_execution(cell)


def dirty_cell_indices(nb: NotebookNode) -> list[int]:
    return [index for index, cell in enumerate(nb.cells) if cell_is_dirty(cell)]


def dirty_from_cells(nb: NotebookNode) -> int | None:
    dirty = dirty_cell_indices(nb)
    return min(dirty) if dirty else None


def cell_is_dirty(cell: NotebookNode) -> bool:
    return cell_stale(cell) or cell_failed(cell)


def failed_cell_indices(nb: NotebookNode, indices: list[int]) -> list[int]:
    return [index for index in indices if cell_failed(nb.cells[index])]


def stale_groups(nb: NotebookNode, indices: list[int]) -> tuple[list[int], list[int]]:
    code_indices: list[int] = []
    markdown_indices: list[int] = []
    for index in indices:
        cell = nb.cells[index]
        if not cell_stale(cell):
            continue
        if cell.get("cell_type") == "code":
            code_indices.append(index)
        elif cell.get("cell_type") == "markdown":
            markdown_indices.append(index)
    return code_indices, markdown_indices


def _meta(cell: NotebookNode) -> dict:
    return cell.get("metadata", {}).get(META_KEY, {})


def _has_error_output(cell: NotebookNode) -> bool:
    return any(output.get("output_type") == "error" for output in cell.get("outputs", []))
