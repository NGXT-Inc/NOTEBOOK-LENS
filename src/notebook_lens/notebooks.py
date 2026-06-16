"""Notebook load/save helpers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import nbformat
from nbformat import NotebookNode

from .errors import CellExecutionError, NotebookChangedError

META_KEY = "notebook_lens"
CHECKPOINT_DIR_NAME = ".ipynb_checkpoints"


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def new_notebook() -> NotebookNode:
    nb = nbformat.v4.new_notebook()
    nb.metadata["kernelspec"] = {
        "display_name": "Python 3",
        "language": "python",
        "name": "python3",
    }
    nb.metadata["language_info"] = {"name": "python", "pygments_lexer": "ipython3"}
    nb.metadata[META_KEY] = {"created_by": "notebook-lens", "created_at": now_iso()}
    return nb


def load_notebook(path: Path) -> NotebookNode:
    if not path.exists():
        return new_notebook()
    with path.open("r", encoding="utf-8") as handle:
        return nbformat.read(handle, as_version=4)


def notebook_files(root: Path) -> list[Path]:
    """Return user-facing notebook files under root, excluding Jupyter checkpoints."""

    if not root.exists():
        return []
    return [
        path
        for path in sorted(root.rglob("*.ipynb"))
        if CHECKPOINT_DIR_NAME not in path.parts
    ]


def ensure_cell_ids(nb: NotebookNode) -> bool:
    """Ensure every cell has a unique standard Jupyter cell id."""

    changed = False
    seen: set[str] = set()
    for cell in nb.cells:
        raw = str(cell.get("id") or "").strip()
        if not raw or raw in seen:
            raw = _new_cell_id(seen)
            cell["id"] = raw
            changed = True
        seen.add(raw)
    return changed


def resolve_cell_index(
    nb: NotebookNode, *, index: int | None = None, cell_id: str | None = None
) -> int:
    """Resolve a cell target by numeric index or stable cell id."""

    if cell_id and index is not None:
        raise CellExecutionError("provide either a cell index or --id, not both")
    if cell_id:
        wanted = cell_id.strip()
        if not wanted:
            raise CellExecutionError("empty cell id")
        matches = [offset for offset, cell in enumerate(nb.cells) if cell.get("id") == wanted]
        if not matches:
            raise CellExecutionError(f"cell id not found: {wanted}")
        if len(matches) > 1:
            raise CellExecutionError(f"cell id is not unique: {wanted}")
        return matches[0]
    if index is None:
        raise CellExecutionError("cell index or --id is required")
    if index < 0:
        raise CellExecutionError(f"cell index out of range: {index}")
    if index >= len(nb.cells):
        raise CellExecutionError(f"cell index out of range: {index}")
    return index


def atomic_save_notebook(
    nb: NotebookNode,
    path: Path,
    *,
    expected_hash: str | None = None,
    expect_missing: bool = False,
) -> str:
    """Validate and atomically save a notebook.

    If ``expected_hash`` is provided and the target changed, this refuses to
    overwrite it. That protects users who save from JupyterLab while a cell is
    executing through the CLI.
    """

    current_hash = file_hash(path)
    if expect_missing and current_hash is not None:
        raise NotebookChangedError(f"notebook was created during command: {path}")
    if expected_hash is not None and current_hash != expected_hash:
        raise NotebookChangedError(f"notebook changed during command: {path}")

    path.parent.mkdir(parents=True, exist_ok=True)
    nbformat.validate(nb)

    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            tmp_name = handle.name
            nbformat.write(nb, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        current_hash = file_hash(path)
        if expect_missing and current_hash is not None:
            raise NotebookChangedError(f"notebook was created during command: {path}")
        if expected_hash is not None and current_hash != expected_hash:
            raise NotebookChangedError(f"notebook changed during command: {path}")
        os.replace(tmp_name, path)
        try:
            dir_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    return file_hash(path) or ""


def cell_meta(cell: NotebookNode) -> dict[str, Any]:
    meta = cell.metadata.setdefault(META_KEY, {})
    if not isinstance(meta, dict):
        meta = {}
        cell.metadata[META_KEY] = meta
    return meta


def make_code_cell(source: str, description: str = "") -> NotebookNode:
    cell = nbformat.v4.new_code_cell(source=source)
    meta = cell_meta(cell)
    meta.update(
        {
            "description": description,
            "created_by": "notebook-lens",
            "created_at": now_iso(),
            "status": "pending",
            "stale": False,
        }
    )
    return cell


def make_markdown_cell(source: str, description: str = "") -> NotebookNode:
    cell = nbformat.v4.new_markdown_cell(source=source)
    meta = cell_meta(cell)
    meta.update(
        {
            "description": description,
            "created_by": "notebook-lens",
            "created_at": now_iso(),
            "status": "ok",
            "stale": False,
        }
    )
    return cell


def ensure_markdown_cell(nb: NotebookNode, index: int) -> NotebookNode:
    if index < 0:
        raise CellExecutionError(f"cell index out of range: {index}")
    try:
        cell = nb.cells[index]
    except IndexError as exc:
        raise CellExecutionError(f"cell index out of range: {index}") from exc
    if cell.get("cell_type") != "markdown":
        raise CellExecutionError(f"cell is not a markdown cell: {index}")
    return cell


def mark_cell_status(
    cell: NotebookNode, *, status: str, stale: bool = False, reason: str = ""
) -> None:
    meta = cell_meta(cell)
    meta["status"] = status
    meta["stale"] = stale
    meta["executed_at"] = now_iso()
    if reason:
        meta["stale_reason"] = reason
    elif not stale:
        meta.pop("stale_reason", None)


def mark_downstream_stale(nb: NotebookNode, start_index: int, reason: str) -> list[int]:
    stale_indices = []
    for offset, cell in enumerate(nb.cells[start_index + 1 :], start=start_index + 1):
        cell_type = cell.get("cell_type")
        if cell_type == "code":
            if cell.get("execution_count") is None and not cell.get("outputs"):
                continue
        elif cell_type == "markdown":
            source = cell.get("source", "")
            if isinstance(source, list):
                source = "".join(source)
            if not str(source).strip():
                continue
        else:
            continue
        meta = cell_meta(cell)
        meta["stale"] = True
        meta["status"] = "stale"
        meta["stale_reason"] = reason
        stale_indices.append(offset)
    return stale_indices


def to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def _new_cell_id(existing: set[str]) -> str:
    while True:
        candidate = uuid.uuid4().hex[:8]
        if candidate not in existing:
            return candidate
