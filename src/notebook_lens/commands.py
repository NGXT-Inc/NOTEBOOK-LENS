"""High-level Notebook Lens command implementations."""

from __future__ import annotations

import time
from pathlib import Path

from .artifacts import (
    artifact_changes,
    artifact_inventory,
    artifact_scopes,
    artifact_snapshot,
)
from .cell_state import (
    SOURCE_HASH_ALGORITHM,
    cell_failed,
    cell_source,
    dirty_cell_indices,
    dirty_from_cells,
    failed_cell_indices,
    source_hash,
    stale_groups,
)
from .command_render import (
    CommandResult,
    cell_command_result,
    cell_status_result,
    env_command_result,
    export_output_command_result,
    execution_summary_lines,
    list_command_result,
    notebook_status_result,
    run_clean_command_result,
)
from .env import LensConfig, resolve_notebook_path
from .env_policy import safe_env_lines
from .errors import (
    CellExecutionError,
    KernelUnavailableError,
    LiveStateLostError,
    StaleNotebookError,
)
from .executor import ExecutionResult, ensure_code_cell, execute_code
from .guidance import (
    cell_refs,
    dirty_summary,
    failed_cell_next_action,
    failed_cells_next_action,
    stale_next_action,
)
from .locks import notebook_lock
from .notebooks import (
    atomic_save_notebook,
    cell_meta,
    ensure_cell_ids,
    ensure_markdown_cell,
    file_hash,
    load_notebook,
    make_code_cell,
    make_markdown_cell,
    mark_cell_status,
    mark_downstream_stale,
    new_notebook,
    notebook_files,
    now_iso,
    resolve_cell_index,
)
from .rich_export import export_rich_outputs
from .session import KernelSession
from .serialize import notebook_payload_from_loaded
from .state import render_loaded_cell, render_loaded_state


def cmd_new(path: str, config: LensConfig) -> CommandResult:
    config.ensure_dirs()
    notebook = resolve_notebook_path(path, config)
    with notebook_lock(config, notebook, "new"):
        if notebook.path.exists():
            return notebook_status_result(path, notebook, "exists")
        nb = new_notebook()
        new_hash = atomic_save_notebook(
            nb,
            notebook.path,
            expected_hash=None,
            expect_missing=True,
        )
        _session(config, notebook).write_state(
            notebook_hash_at_last_exec=new_hash,
            dirty_from_cell=None,
            created_at=now_iso(),
        )
    return notebook_status_result(path, notebook, "created")


def cmd_run_cell(
    path: str,
    config: LensConfig,
    *,
    source: str,
    description: str = "",
    timeout: float = 120,
) -> CommandResult:
    _require_source(source, "code")
    config.ensure_dirs()
    notebook = resolve_notebook_path(path, config)
    artifacts_before = artifact_snapshot(config, notebook)
    with notebook_lock(config, notebook, "add-code"):
        session = _session(config, notebook)
        expected_hash = file_hash(notebook.path)
        expect_missing = expected_hash is None
        nb = load_notebook(notebook.path)
        ensure_cell_ids(nb)
        _assert_incremental_state_safe(session, expected_hash, nb)
        _assert_append_allowed(session, nb)
        cell = make_code_cell(source, description)
        nb.cells.append(cell)
        index = len(nb.cells) - 1
        _mark_execution_in_progress(session, command="add-code", cell_index=index)
        result = execute_code(session, source, timeout=timeout)
        _apply_execution(cell, result)
        new_hash = _save_after_execution(
            session,
            nb,
            notebook.path,
            expected_hash=expected_hash,
            expect_missing=expect_missing,
        )
        dirty_from_cell = index if not result.ok else dirty_from_cells(nb)
        session.write_state(
            notebook_hash_at_last_exec=new_hash,
            dirty_from_cell=dirty_from_cell,
            execution_in_progress=False,
            live_state_lost=result.kernel_reset,
            kernel_unsafe=result.kernel_reset,
            unsafe_reason=_kernel_reset_reason(result),
        )
    artifacts_after = artifact_snapshot(config, notebook)
    changes = artifact_changes(artifacts_before, artifacts_after)
    scopes = artifact_scopes(config, notebook)
    next_action = failed_cell_next_action(cell) if not result.ok else None
    return cell_command_result(
        path,
        notebook,
        index,
        cell,
        result,
        config=config,
        artifact_changes=changes,
        artifact_scope=scopes,
        next_action=next_action,
    )


def cmd_update_cell(
    path: str,
    index: int | None,
    config: LensConfig,
    *,
    cell_id: str | None = None,
    source: str,
    description: str = "",
    timeout: float = 120,
) -> CommandResult:
    _require_source(source, "code")
    config.ensure_dirs()
    notebook = resolve_notebook_path(path, config)
    artifacts_before = artifact_snapshot(config, notebook)
    with notebook_lock(config, notebook, "update-code"):
        session = _session(config, notebook)
        expected_hash = file_hash(notebook.path)
        _require_existing_notebook(notebook, expected_hash)
        nb = load_notebook(notebook.path)
        ensure_cell_ids(nb)
        _assert_incremental_state_safe(session, expected_hash, nb)
        index = resolve_cell_index(nb, index=index, cell_id=cell_id)
        cell = ensure_code_cell(nb, index)
        previous_failed = cell_failed(cell)
        cell["source"] = source
        if description:
            cell_meta(cell)["description"] = description
        _mark_execution_in_progress(session, command="update-code", cell_index=index)
        result = execute_code(session, source, timeout=timeout)
        _apply_execution(cell, result)
        stale_indices = mark_downstream_stale(
            nb,
            index,
            reason=f"earlier cell {index} was updated after this output was produced",
        )
        stale_code_indices = [
            item for item in stale_indices if nb.cells[item].get("cell_type") == "code"
        ]
        stale_markdown_indices = [
            item for item in stale_indices if nb.cells[item].get("cell_type") == "markdown"
        ]
        if result.ok and stale_indices:
            meta = cell_meta(cell)
            meta["provisional"] = True
            meta["provisional_reason"] = (
                "updated before downstream stale cells were repaired; "
                "run-clean stale code and update stale markdown before treating this output as evidence"
            )
        if result.ok and previous_failed:
            meta = cell_meta(cell)
            meta["stale"] = True
            meta["stale_reason"] = (
                "repaired a previously failed cell in a kernel that may still contain "
                "pre-error side effects; run-clean before appending code or treating "
                "the notebook as evidence"
            )
        new_hash = _save_after_execution(
            session,
            nb,
            notebook.path,
            expected_hash=expected_hash,
        )
        dirty_from_cell = (
            index
            if not result.ok
            else min(stale_indices)
            if stale_indices
            else dirty_from_cells(nb)
        )
        session.write_state(
            notebook_hash_at_last_exec=new_hash,
            dirty_from_cell=dirty_from_cell,
            execution_in_progress=False,
            live_state_lost=result.kernel_reset,
            kernel_unsafe=result.kernel_reset,
            unsafe_reason=_kernel_reset_reason(result),
    )
    changes = artifact_changes(artifacts_before, artifact_snapshot(config, notebook))
    scopes = artifact_scopes(config, notebook)
    next_action = failed_cell_next_action(cell) if not result.ok else None
    if result.ok and stale_indices:
        next_action = stale_next_action(nb, stale_code_indices, stale_markdown_indices)
    if result.ok and previous_failed:
        next_action = (
            f"run-clean to recompute repaired failed cell {cell_refs(nb, [index])} "
            "from a fresh kernel "
            "before appending code or treating the notebook as evidence."
        )
    return cell_command_result(
        path,
        notebook,
        index,
        cell,
        result,
        config=config,
        artifact_changes=changes,
        artifact_scope=scopes,
        stale_indices=stale_indices,
        stale_code_indices=stale_code_indices,
        stale_markdown_indices=stale_markdown_indices,
        stale_refs=cell_refs(nb, stale_indices),
        stale_code_refs=cell_refs(nb, stale_code_indices),
        stale_markdown_refs=cell_refs(nb, stale_markdown_indices),
        provisional=result.ok and bool(stale_indices),
        next_action=next_action,
    )


def cmd_add_markdown(
    path: str,
    config: LensConfig,
    *,
    source: str,
    description: str = "",
) -> CommandResult:
    _require_source(source, "markdown")
    config.ensure_dirs()
    notebook = resolve_notebook_path(path, config)
    with notebook_lock(config, notebook, "add-markdown"):
        session = _session(config, notebook)
        expected_hash = file_hash(notebook.path)
        expect_missing = expected_hash is None
        _mark_file_divergence_for_state(session, expected_hash)
        nb = load_notebook(notebook.path)
        ensure_cell_ids(nb)
        cell = make_markdown_cell(source, description)
        nb.cells.append(cell)
        index = len(nb.cells) - 1
        new_hash = atomic_save_notebook(
            nb,
            notebook.path,
            expected_hash=expected_hash,
            expect_missing=expect_missing,
        )
        session.write_state(notebook_hash_at_last_exec=new_hash)
    return cell_status_result(path, notebook, index, cell, "added")


def cmd_update_markdown(
    path: str,
    index: int | None,
    config: LensConfig,
    *,
    cell_id: str | None = None,
    source: str,
    description: str = "",
) -> CommandResult:
    _require_source(source, "markdown")
    config.ensure_dirs()
    notebook = resolve_notebook_path(path, config)
    with notebook_lock(config, notebook, "update-markdown"):
        session = _session(config, notebook)
        expected_hash = file_hash(notebook.path)
        _require_existing_notebook(notebook, expected_hash)
        _mark_file_divergence_for_state(session, expected_hash)
        nb = load_notebook(notebook.path)
        ensure_cell_ids(nb)
        index = resolve_cell_index(nb, index=index, cell_id=cell_id)
        cell = ensure_markdown_cell(nb, index)
        cell["source"] = source
        meta = cell_meta(cell)
        if description:
            meta["description"] = description
        meta["status"] = "ok"
        meta["stale"] = False
        meta["updated_at"] = now_iso()
        meta.pop("stale_reason", None)
        new_hash = atomic_save_notebook(nb, notebook.path, expected_hash=expected_hash)
        session.write_state(
            notebook_hash_at_last_exec=new_hash,
            dirty_from_cell=dirty_from_cells(nb),
        )
    remaining = dirty_cell_indices(nb)
    next_action = None
    if remaining:
        code_indices, markdown_indices = stale_groups(nb, remaining)
        next_action = stale_next_action(nb, code_indices, markdown_indices)
    return cell_status_result(
        path,
        notebook,
        index,
        cell,
        "updated",
        remaining_stale_cell_indices=remaining,
        next_action=next_action,
    )


def cmd_state(
    path: str,
    config: LensConfig,
    *,
    max_tokens: int = 6000,
    full_source: bool = False,
    outputs: str = "summary",
) -> CommandResult:
    outputs = _normalize_output_mode(outputs)
    notebook = resolve_notebook_path(path, config)
    with notebook_lock(config, notebook, "state"):
        session = _session(config, notebook)
        current_hash = file_hash(notebook.path)
        _require_existing_notebook(notebook, current_hash)
        _mark_file_divergence_for_state(session, current_hash)
        state = session.read_state()
        nb = load_notebook(notebook.path)
    warnings = _inspection_warnings(state)
    live_state_untrusted = bool(state.get("live_state_lost") or state.get("kernel_unsafe"))
    dirty_indices = dirty_cell_indices(nb)
    if state.get("dirty_from_cell") is not None or dirty_indices:
        code_indices, markdown_indices = stale_groups(nb, dirty_indices)
        failed_indices = failed_cell_indices(nb, dirty_indices)
        if dirty_indices:
            warnings.append(
                "warning: stale cells need repair before appending code: "
                    f"{dirty_summary(nb, dirty_indices)}."
            )
            if failed_indices:
                if live_state_untrusted:
                    warnings.append(
                        "next_action: run-clean first to re-establish a trustworthy kernel; "
                        "if it stops at failed cells, "
                        f"{failed_cells_next_action(nb, failed_indices, markdown_indices)}"
                    )
                else:
                    warnings.append(
                        f"next_action: {failed_cells_next_action(nb, failed_indices, markdown_indices)}"
                    )
            else:
                if live_state_untrusted:
                    warnings.append(
                        "next_action: run-clean first to re-establish a trustworthy kernel; "
                        f"then {stale_next_action(nb, code_indices, markdown_indices)}"
                    )
                else:
                    warnings.append(f"next_action: {stale_next_action(nb, code_indices, markdown_indices)}")
        else:
            warnings.append(
                f"warning: cell {state['dirty_from_cell']} needs repair before appending code."
            )
    text = render_loaded_state(
        nb,
        title=str(notebook.path),
        max_tokens=max_tokens,
        full_source=full_source,
        outputs=outputs,
    )
    if warnings:
        text = "\n".join(warnings) + "\n\n" + text
    payload = notebook_payload_from_loaded(
        nb,
        str(notebook.rel),
        digest=current_hash,
    )
    return CommandResult(
        text,
        data={
            "kind": "state",
            "notebook": _notebook_json_data(notebook, payload),
            "cell_count": payload["cell_count"],
            "cells": payload["cells"],
            "warnings": warnings,
            "render_options": {
                "max_tokens": max_tokens,
                "full_source": full_source,
                "outputs": outputs,
            },
            "truncated": "[state truncated]" in text,
        },
    )


def cmd_show_cell(
    path: str,
    index: int | None,
    config: LensConfig,
    *,
    cell_id: str | None = None,
    outputs: str = "full",
    max_chars: int | None = None,
) -> CommandResult:
    outputs = _normalize_output_mode(outputs)
    notebook = resolve_notebook_path(path, config)
    try:
        with notebook_lock(config, notebook, "show-cell"):
            session = _session(config, notebook)
            current_hash = file_hash(notebook.path)
            _require_existing_notebook(notebook, current_hash)
            _mark_file_divergence_for_state(session, current_hash)
            state = session.read_state()
            nb = load_notebook(notebook.path)
            ensure_cell_ids(nb)
            index = resolve_cell_index(nb, index=index, cell_id=cell_id)
            text = render_loaded_cell(
                nb,
                title=str(notebook.path),
                index=index,
                full_source=True,
                outputs=outputs,
                max_chars=max_chars,
            )
    except (IndexError, CellExecutionError) as exc:
        raise CellExecutionError(str(exc)) from exc
    warnings = _inspection_warnings(state)
    if warnings:
        text = "\n".join(warnings) + "\n\n" + text
    payload = notebook_payload_from_loaded(
        nb,
        str(notebook.rel),
        digest=current_hash,
    )
    return CommandResult(
        text,
        data={
            "kind": "show_cell",
            "notebook": _notebook_json_data(notebook, payload),
            "cell_index": index,
            "cell_id": payload["cells"][index]["id"],
            "cell": payload["cells"][index],
            "warnings": warnings,
            "render_options": {
                "outputs": outputs,
                "max_chars": max_chars,
            },
        },
    )


def cmd_export_output(
    path: str,
    index: int | None,
    config: LensConfig,
    *,
    cell_id: str | None = None,
    output_dir: str,
    output_index: int | None = None,
) -> CommandResult:
    if output_index is not None and output_index < 0:
        raise CellExecutionError(f"output index out of range: {output_index}")
    output_path = _resolve_output_dir(output_dir)
    config.ensure_dirs()
    notebook = resolve_notebook_path(path, config)
    with notebook_lock(config, notebook, "export-output"):
        current_hash = file_hash(notebook.path)
        _require_existing_notebook(notebook, current_hash)
        nb = load_notebook(notebook.path)
        ensure_cell_ids(nb)
        index = resolve_cell_index(nb, index=index, cell_id=cell_id)
        cell = nb.cells[index]
        outputs = cell.get("outputs", []) if cell.get("cell_type") == "code" else []
        exported, skipped = export_rich_outputs(
            outputs,
            output_path,
            cell_index=index,
            cell_id=cell.get("id", ""),
            output_index=output_index,
        )
    return export_output_command_result(
        path,
        notebook,
        index,
        cell,
        output_path,
        exported,
        skipped,
        output_index=output_index,
    )


def _resolve_output_dir(raw: str) -> Path:
    if not raw.strip():
        raise CellExecutionError("output directory is empty")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def _normalize_output_mode(outputs: str) -> str:
    if outputs == "compact":
        return "summary"
    if outputs in {"summary", "full", "none"}:
        return outputs
    raise CellExecutionError(
        f"invalid outputs mode: {outputs}; choose summary, compact, full, or none"
    )


def _notebook_json_data(notebook, payload: dict) -> dict[str, object]:
    return {
        "rel": payload["path"],
        "title": payload["title"],
        "absolute_path": str(notebook.path),
        "hash": payload["hash"],
    }


def cmd_reset(path: str, config: LensConfig) -> CommandResult:
    config.ensure_dirs()
    notebook = resolve_notebook_path(path, config)
    with notebook_lock(config, notebook, "reset-kernel"):
        stopped = _session(config, notebook).reset()
    status = "reset" if stopped else "no-live-kernel"
    return notebook_status_result(path, notebook, status)


def cmd_list(config: LensConfig) -> CommandResult:
    notebook_rels = []
    for path in notebook_files(config.notebook_dir):
        try:
            rel = path.resolve(strict=False).relative_to(config.notebook_dir).as_posix()
        except ValueError:
            continue
        notebook_rels.append(rel)
    return list_command_result(config, notebook_rels)


def cmd_env(config: LensConfig, path: str | None = None) -> CommandResult:
    notebook = resolve_notebook_path(path, config) if path else None
    artifact_scope = artifact_scopes(config, notebook)[0]
    return env_command_result(
        config,
        artifact_scope,
        notebook=notebook,
        path_provided=path is not None,
    )


def cmd_run_clean(path: str, config: LensConfig, *, timeout: float = 120) -> CommandResult:
    config.ensure_dirs()
    notebook = resolve_notebook_path(path, config)
    started = time.perf_counter()
    artifacts_before = artifact_snapshot(config, notebook)
    with notebook_lock(config, notebook, "run-clean"):
        expected_hash = file_hash(notebook.path)
        _require_existing_notebook(notebook, expected_hash)
        nb = load_notebook(notebook.path)
        ensure_cell_ids(nb)
        session = _session(config, notebook)
        session.reset()
        first_error: tuple[int, ExecutionResult] | None = None
        executed_results: list[tuple[int, object, ExecutionResult]] = []
        total_code_cells = sum(1 for cell in nb.cells if cell.get("cell_type") == "code")
        executed_cells = 0
        env_synced = False

        for index, cell in enumerate(nb.cells):
            if cell.get("cell_type") != "code":
                continue
            cell["outputs"] = []
            cell["execution_count"] = None
            _mark_execution_in_progress(session, command="run-clean", cell_index=index)
            result = execute_code(
                session,
                cell_source(cell),
                timeout=timeout,
                sync_env=not env_synced,
            )
            env_synced = True
            executed_cells += 1
            _apply_execution(cell, result)
            executed_results.append((index, cell, result))
            if not result.ok:
                first_error = (index, result)
                break

        if first_error is None:
            for cell in nb.cells:
                if cell.get("cell_type") == "code":
                    meta = cell_meta(cell)
                    meta["stale"] = False
                    meta.pop("stale_reason", None)
                    meta.pop("provisional", None)
                    meta.pop("provisional_reason", None)
            status = "ok"
            exit_code = 0
        else:
            failed_index, failed_result = first_error
            mark_downstream_stale(
                nb,
                failed_index,
                reason=f"clean run stopped at failing cell {failed_index}",
            )
            status = failed_result.status
            exit_code = 1

        new_hash = _save_after_execution(
            session,
            nb,
            notebook.path,
            expected_hash=expected_hash,
        )
        reset_during_clean = bool(first_error and first_error[1].kernel_reset)
        session.write_state(
            notebook_hash_at_last_exec=new_hash,
            dirty_from_cell=dirty_from_cells(nb) if status == "ok" else failed_index,
            last_clean_run=now_iso() if status == "ok" else None,
            execution_in_progress=False,
            live_state_lost=reset_during_clean,
            kernel_unsafe=reset_during_clean,
            unsafe_reason=_kernel_reset_reason(failed_result) if first_error else None,
        )

    elapsed = time.perf_counter() - started
    artifacts_after = artifact_snapshot(config, notebook)
    changes = artifact_changes(artifacts_before, artifacts_after)
    scopes = artifact_scopes(config, notebook)
    env_lines = safe_env_lines()
    output_lines = execution_summary_lines(executed_results, input_path=path)
    remaining = dirty_cell_indices(nb) if status == "ok" else []
    remaining_next_action = None
    if remaining:
        code_indices, markdown_indices = stale_groups(nb, remaining)
        remaining_next_action = stale_next_action(nb, code_indices, markdown_indices)
    failed_cell_index = None
    failed_cell_id = None
    failed_error = None
    failed_next_action = None
    if first_error is not None:
        index, result = first_error
        failed_cell_index = index
        failed_cell_id = nb.cells[index].get("id", "")
        failed_error = result.error
        failed_next_action = failed_cell_next_action(nb.cells[index])
    return run_clean_command_result(
        path,
        notebook,
        status=status,
        executed_cells=executed_cells,
        total_code_cells=total_code_cells,
        elapsed_seconds=elapsed,
        exit_code=exit_code,
        env_lines=env_lines,
        output_lines=output_lines,
        preexisting_artifact_count=len(artifacts_before),
        config=config,
        artifact_scope=scopes,
        artifact_changes=changes,
        artifact_inventory=artifact_inventory(artifacts_after),
        executed_results=executed_results,
        remaining_stale_cell_indices=remaining,
        remaining_next_action=remaining_next_action,
        failed_cell_index=failed_cell_index,
        failed_cell_id=failed_cell_id,
        error=failed_error,
        failed_next_action=failed_next_action,
    )


def _require_source(source: str, kind: str) -> None:
    if source.strip():
        return
    if kind == "code":
        raise CellExecutionError(
            "code source is empty; the selected file, stdin, or --code value contained "
            "no non-whitespace source. next_action: write the cell source to a small "
            "file and rerun with --file <path>, or pass short local source with "
            "--code <python>. When running through remote SSH wrappers, do not rely on "
            "--file - unless stdin is known to be forwarded."
        )
    raise CellExecutionError(
        f"{kind} source is empty; the selected file or stdin contained no non-whitespace "
        "source. next_action: write the cell source to a small file and rerun with "
        "--file <path>. When running through remote SSH wrappers, do not rely on "
        "--file - unless stdin is known to be forwarded."
    )


def _require_existing_notebook(notebook, digest: str | None) -> None:
    if digest is not None:
        return
    raise CellExecutionError(
        f"notebook not found: {notebook.rel}; create it with notebook-lens new "
        f"{notebook.rel} or append a first cell with add-code/add-markdown."
    )


def _session(config: LensConfig, notebook) -> KernelSession:
    return KernelSession(config=config, notebook=notebook)


def _assert_incremental_state_safe(session: KernelSession, current_hash: str | None, nb) -> None:
    state = session.read_state()
    last_hash = state.get("notebook_hash_at_last_exec")
    has_executed_cells = _has_executed_cells(nb)
    if state.get("execution_in_progress"):
        _mark_live_state_lost(
            session,
            unsafe_reason="previous execution did not finish safely",
            reset=True,
        )
        session.write_state(execution_in_progress=False)
        raise LiveStateLostError(
            "previous execution did not finish safely; live kernel was reset. "
            "Run state or run-clean before continuing."
        )
    if last_hash and session.is_alive() and current_hash != last_hash:
        _mark_live_state_lost(
            session,
            unsafe_reason="notebook file changed outside the live kernel",
            reset=True,
        )
        from .errors import NotebookChangedError

        raise NotebookChangedError(
            "notebook changed outside Notebook Lens since the live kernel last "
            "matched it; kernel was reset. Run state or run-clean before continuing."
        )
    if not has_executed_cells or not last_hash:
        return

    if state.get("kernel_unsafe") or state.get("live_state_lost"):
        raise LiveStateLostError(
            "live kernel state is not trustworthy; run-clean before incremental execution."
        )

    if state.get("kernel_pid") and not _kernel_ready(session):
        _mark_live_state_lost(
            session,
            unsafe_reason="previous live kernel is no longer reachable",
            reset=False,
        )
        raise LiveStateLostError(
            "previous live kernel is no longer reachable; run-clean before incremental execution."
        )


def _assert_append_allowed(session: KernelSession, nb=None) -> None:
    state = session.read_state()
    dirty_from_cell = state.get("dirty_from_cell")
    if dirty_from_cell is not None:
        try:
            dirty_index = int(dirty_from_cell)
        except (TypeError, ValueError):
            dirty_index = None
        if dirty_index is not None and nb is not None and 0 <= dirty_index < len(nb.cells):
            cell = nb.cells[dirty_index]
            cell_id = cell.get("id", "")
            status = cell_meta(cell).get("status")
            if status in {"error", "timeout"}:
                raise StaleNotebookError(
                    f"cell {dirty_index} (id {cell_id}) failed; appending code is blocked. "
                    f"Inspect with show-cell --id {cell_id} --outputs full, then repair with "
                    f"update-code --id {cell_id} or run-clean after repair."
                )
        raise StaleNotebookError(
            f"stale cells need repair before appending code: "
            f"{dirty_summary(nb, dirty_cell_indices(nb))}. "
            f"{stale_next_action(nb, *stale_groups(nb, dirty_cell_indices(nb)))}"
        )


def _mark_file_divergence_for_state(session: KernelSession, current_hash: str | None) -> None:
    state = session.read_state()
    last_hash = state.get("notebook_hash_at_last_exec")
    if last_hash and current_hash != last_hash:
        _mark_live_state_lost(
            session,
            unsafe_reason="notebook file changed outside the live kernel",
            reset=session.is_alive(),
        )


def _mark_live_state_lost(
    session: KernelSession, *, unsafe_reason: str, reset: bool
) -> None:
    session.write_state(
        kernel_unsafe=True,
        live_state_lost=True,
        unsafe_reason=unsafe_reason,
    )
    if reset:
        session.reset()
        session.write_state(
            kernel_unsafe=True,
            live_state_lost=True,
            unsafe_reason=unsafe_reason,
        )


def _kernel_ready(session: KernelSession) -> bool:
    try:
        client = session.client(start=False, timeout=1)
    except KernelUnavailableError:
        return False
    try:
        return True
    finally:
        client.stop_channels()


def _has_executed_cells(nb) -> bool:
    return any(
        cell.get("cell_type") == "code"
        and (cell.get("execution_count") is not None or bool(cell.get("outputs")))
        for cell in nb.cells
    )


def _save_after_execution(
    session: KernelSession,
    nb,
    path: Path,
    *,
    expected_hash: str | None,
    expect_missing: bool = False,
) -> str:
    try:
        return atomic_save_notebook(
            nb,
            path,
            expected_hash=expected_hash,
            expect_missing=expect_missing,
        )
    except Exception:
        session.write_state(
            kernel_unsafe=True,
            live_state_lost=True,
            execution_in_progress=False,
            unsafe_reason="notebook save conflict after kernel execution",
        )
        session.reset()
        session.write_state(
            kernel_unsafe=True,
            live_state_lost=True,
            execution_in_progress=False,
            unsafe_reason="notebook save conflict after kernel execution",
        )
        raise


def _mark_execution_in_progress(
    session: KernelSession, *, command: str, cell_index: int
) -> None:
    session.write_state(
        execution_in_progress=True,
        executing_command=command,
        executing_cell=cell_index,
        kernel_unsafe=True,
        live_state_lost=False,
        unsafe_reason=f"{command} is executing cell {cell_index}",
    )


def _apply_execution(cell, result: ExecutionResult) -> None:
    cell["outputs"] = result.outputs
    cell["execution_count"] = result.execution_count
    executed_hash = source_hash(cell_source(cell))
    meta = cell_meta(cell)
    meta.pop("provisional", None)
    meta.pop("provisional_reason", None)
    mark_cell_status(cell, status=result.status, stale=False)
    meta = cell_meta(cell)
    meta["executed_source_hash"] = executed_hash
    meta["executed_source_hash_algorithm"] = SOURCE_HASH_ALGORITHM


def _inspection_warnings(state: dict) -> list[str]:
    warnings = []
    if state.get("execution_in_progress"):
        warnings.append(
            "warning: previous execution did not finish safely; run-clean before incremental work."
        )
    if state.get("live_state_lost"):
        warnings.append(
            "warning: live kernel state was reset or lost; run-clean before incremental work or evidence use."
        )
    if state.get("kernel_unsafe"):
        warnings.append("warning: previous kernel state was unsafe and should not be trusted.")
    unsafe_reason = str(state.get("unsafe_reason") or "").strip()
    if unsafe_reason:
        warnings.append(f"unsafe_reason: {unsafe_reason}")
    return warnings


def _kernel_reset_reason(result: ExecutionResult | None) -> str | None:
    if result and result.kernel_reset:
        return "kernel was reset while handling the previous execution"
    return None
