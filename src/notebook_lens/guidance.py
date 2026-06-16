"""Agent-facing repair guidance for dirty notebook cells."""

from __future__ import annotations

from nbformat import NotebookNode

from .cell_state import cell_failed, cell_stale


def dirty_summary(nb: NotebookNode, indices: list[int]) -> str:
    if not indices:
        return "none"
    if len(indices) > 8:
        errors = []
        stale_code = []
        stale_markdown = []
        other = []
        for index in indices:
            cell = nb.cells[index]
            if cell_failed(cell):
                errors.append(index)
            elif cell_stale(cell) and cell.get("cell_type") == "code":
                stale_code.append(index)
            elif cell_stale(cell) and cell.get("cell_type") == "markdown":
                stale_markdown.append(index)
            else:
                other.append(index)
        parts = []
        if errors:
            parts.append(f"errors {compact_indices(errors)}")
        if stale_markdown:
            parts.append(f"stale markdown {compact_indices(stale_markdown)}")
        if stale_code:
            parts.append(f"stale code {compact_indices(stale_code)}")
        if other:
            parts.append(f"other {compact_indices(other)}")
        return "; ".join(parts)
    return ", ".join(f"{index} {nb.cells[index].get('cell_type', 'unknown')}" for index in indices)


def compact_indices(indices: list[int]) -> str:
    if not indices:
        return "none"
    sorted_indices = sorted(indices)
    ranges = []
    start = previous = sorted_indices[0]
    for index in sorted_indices[1:]:
        if index == previous + 1:
            previous = index
            continue
        ranges.append(_range_text(start, previous))
        start = previous = index
    ranges.append(_range_text(start, previous))
    return ",".join(ranges)


def stale_next_action(
    nb: NotebookNode,
    code_indices: list[int],
    markdown_indices: list[int],
) -> str:
    actions = []
    repaired_failed = _repaired_failed_stale_indices(nb, code_indices)
    repaired_failed_set = set(repaired_failed)
    remaining_code = [index for index in code_indices if index not in repaired_failed_set]
    if repaired_failed:
        subject = "cell" if len(repaired_failed) == 1 else "cells"
        actions.append(
            f"run-clean to recompute repaired failed {subject} "
            f"{cell_refs(nb, repaired_failed)} from a fresh kernel"
        )
    if remaining_code:
        actions.append("run-clean to recompute stale code cells " f"{cell_refs(nb, remaining_code)}")
    if markdown_indices:
        actions.append("update-markdown for stale narrative " f"{cell_refs(nb, markdown_indices)}")
    if not actions:
        return "inspect dirty cells with show-cell --id before appending."
    return "; ".join(actions) + " before appending code or treating the notebook as evidence."


def failed_cells_next_action(
    nb: NotebookNode,
    indices: list[int],
    markdown_indices: list[int] | None = None,
) -> str:
    action = (
        "inspect failed cells with show-cell --id "
        f"{cell_refs(nb, indices)}; repair with update-code --id, then run-clean "
        "before appending."
    )
    if markdown_indices:
        action += (
            " After failed cells are repaired and run-clean succeeds, update-markdown "
            f"for stale narrative {cell_refs(nb, markdown_indices)} before treating "
            "the notebook as evidence."
        )
    return action


def failed_cell_next_action(cell: NotebookNode) -> str:
    cell_id = cell.get("id", "")
    if cell_id:
        return (
            f"inspect with show-cell --id {cell_id} --outputs full; repair with "
            f"update-code --id {cell_id}; then run-clean before appending code."
        )
    return "inspect the failed cell, repair it with update-code, then run-clean before appending."


def cell_refs(nb: NotebookNode, indices: list[int]) -> str:
    refs = []
    for index in indices:
        cell_id = nb.cells[index].get("id", "")
        refs.append(f"{index}" + (f" ({cell_id})" if cell_id else ""))
    return ", ".join(refs)


def _range_text(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"


def _repaired_failed_stale_indices(nb: NotebookNode, code_indices: list[int]) -> list[int]:
    repaired = []
    for index in code_indices:
        meta = nb.cells[index].get("metadata", {}).get("notebook_lens", {})
        stale_reason = str(meta.get("stale_reason") or "")
        if stale_reason.startswith("repaired a previously failed cell"):
            repaired.append(index)
    return repaired
