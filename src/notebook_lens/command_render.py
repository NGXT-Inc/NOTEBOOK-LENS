"""Internal text rendering for Notebook Lens command results."""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from shlex import quote
from typing import Any

from .cell_state import cell_status
from .errors import NotebookLensError
from .executor import ExecutionResult
from .rich_export import ExportedRichOutput, SkippedRichOutput
from .rich_output import rich_mime_summary
from .text_render import (
    join_stream_parts,
    strip_ansi,
    text_value,
    trim_blank_lines,
    truncate_head_tail,
    was_truncated,
)

PRINT_LIMIT = 3000
FULL_OUTPUT_HINT_LIMIT = 20000
RICH_OUTPUT_CLEANUP_NOTE = "export-output writes files only when requested; remove them when done"


@dataclass
class CommandResult:
    """Text plus process exit code for a CLI command."""

    text: str
    exit_code: int = 0
    data: dict[str, Any] | None = None


def notebook_lines(input_path: str, notebook) -> list[str]:
    return [
        f"notebook: {notebook.rel}",
        f"input_path: {input_path}",
        f"absolute_path: {notebook.path}",
    ]


def notebook_status_result(input_path: str, notebook, status: str) -> CommandResult:
    lines = notebook_lines(input_path, notebook)
    lines.append(f"status: {status}")
    return CommandResult(
        "\n".join(lines) + "\n",
        data={
            "kind": "notebook_status",
            "notebook": notebook_data(input_path, notebook),
            "status": status,
        },
    )


def cell_status_result(
    input_path: str,
    notebook,
    index: int,
    cell,
    status: str,
    *,
    remaining_stale_cell_indices: list[int] | None = None,
    next_action: str | None = None,
) -> CommandResult:
    lines = notebook_lines(input_path, notebook)
    lines.extend([f"cell_index: {index}", f"cell_id: {cell.get('id', '')}", f"status: {status}"])
    if remaining_stale_cell_indices is not None:
        if remaining_stale_cell_indices:
            stale_list = ", ".join(str(item) for item in remaining_stale_cell_indices)
            lines.append(f"remaining_stale_cell_indices: {stale_list}")
            if next_action:
                lines.append(f"next_action: {next_action}")
        else:
            lines.append("remaining_stale_cell_indices: none")
    data: dict[str, Any] = {
        "kind": "cell_status",
        "notebook": notebook_data(input_path, notebook),
        "cell_index": index,
        "cell_id": cell.get("id", ""),
        "status": status,
    }
    if remaining_stale_cell_indices is not None:
        data["remaining_stale_cell_indices"] = remaining_stale_cell_indices
    if next_action:
        data["next_action"] = next_action
    return CommandResult("\n".join(lines) + "\n", data=data)


def export_output_command_result(
    input_path: str,
    notebook,
    index: int,
    cell,
    output_dir,
    exported: list[ExportedRichOutput],
    skipped: list[SkippedRichOutput],
    *,
    output_index: int | None = None,
) -> CommandResult:
    status = "exported" if exported else "none"
    lines = [
        *notebook_lines(input_path, notebook),
        f"cell_index: {index}",
        f"cell_id: {cell.get('id', '')}",
        f"output_dir: {output_dir}",
        f"status: {status}",
    ]
    if output_index is not None:
        lines.append(f"output_index: {output_index}")
    if exported:
        lines.append("exported_outputs:")
        lines.extend(
            f"- {item.absolute_path} ({item.mimetype}, {item.size} bytes)"
            for item in exported
        )
    else:
        lines.append("exported_outputs: none")
    if skipped:
        lines.append("skipped_outputs:")
        lines.extend(f"- {item.mimetype} ({item.reason})" for item in skipped)
    lines.append(f"cleanup_note: {RICH_OUTPUT_CLEANUP_NOTE}")
    data: dict[str, Any] = {
        "kind": "export_output",
        "notebook": notebook_data(input_path, notebook),
        "cell_index": index,
        "cell_id": cell.get("id", ""),
        "output_dir": str(output_dir),
        "status": status,
        "exported_outputs": [asdict(item) for item in exported],
        "skipped_outputs": [asdict(item) for item in skipped],
        "cleanup_note": RICH_OUTPUT_CLEANUP_NOTE,
    }
    if output_index is not None:
        data["output_index"] = output_index
    return CommandResult("\n".join(lines) + "\n", data=data)


def list_command_result(config, notebook_rels: list[str]) -> CommandResult:
    lines = [
        f"experiment_dir: {config.experiment_dir}",
        "notebooks:",
    ]
    if not notebook_rels:
        lines.append("- none")
    else:
        lines.extend(f"- {rel}" for rel in notebook_rels)
    return CommandResult(
        "\n".join(lines) + "\n",
        data={
            "kind": "list",
            "experiment_dir": str(config.experiment_dir),
            "notebooks": notebook_rels,
        },
    )


def env_command_result(config) -> CommandResult:
    lines = [
        f"experiment_dir: {config.experiment_dir}",
        f"runtime_dir: {config.runtime_dir}",
        f"sessions_dir: {config.sessions_dir}",
        f"kernel_python: {config.kernel_python}",
    ]
    data: dict[str, Any] = {
        "kind": "env",
        "experiment_dir": str(config.experiment_dir),
        "runtime_dir": str(config.runtime_dir),
        "sessions_dir": str(config.sessions_dir),
        "kernel_python": str(config.kernel_python),
    }
    return CommandResult("\n".join(lines) + "\n", data=data)


def notebook_data(input_path: str, notebook) -> dict[str, str]:
    return {
        "rel": str(notebook.rel),
        "input_path": input_path,
        "absolute_path": str(notebook.path),
    }


def execution_summary_lines(
    executed_results: list[tuple[int, object, ExecutionResult]],
    *,
    input_path: str = "",
) -> list[str]:
    lines = []
    for index, cell, result in executed_results:
        cell_id = cell.get("id", "") if hasattr(cell, "get") else ""
        lines.append(f"- cell_index: {index}, cell_id: {cell_id}, status: {result.status}")
        truncated = []
        if _append_excerpt(lines, "stdout_excerpt", stream_text(result.outputs, "stdout"), limit=700):
            truncated.append("stdout")
        if _append_excerpt(lines, "stderr_excerpt", stream_text(result.outputs, "stderr"), limit=700):
            truncated.append("stderr")
        errors = [
            _one_line(error_text(output))
            for output in result.outputs
            if output.get("output_type") == "error"
        ]
        if errors:
            lines.append(f"  error_excerpt: {truncate(errors[-1], 700)}")
            if was_truncated(errors[-1], 700):
                truncated.append("error")
        note = _truncated_output_note(truncated, cell_id, index)
        if note:
            lines.append(f"  {note}")
        rich = rich_outputs(result.outputs)
        if rich:
            lines.append("  rich_outputs:")
            lines.extend(f"  - {item} saved in notebook" for item in rich)
            lines.extend(_rich_output_export_lines(input_path, index, cell_id, indent="  "))
    return lines


def execution_summary_data(
    executed_results: list[tuple[int, object, ExecutionResult]],
    *,
    input_path: str = "",
) -> list[dict[str, Any]]:
    items = []
    for index, cell, result in executed_results:
        cell_id = cell.get("id", "") if hasattr(cell, "get") else ""
        stdout = stream_text(result.outputs, "stdout")
        stderr = stream_text(result.outputs, "stderr")
        errors = [
            error_text(output)
            for output in result.outputs
            if output.get("output_type") == "error"
        ]
        rich = rich_outputs(result.outputs)
        truncated = []
        if was_truncated(trim_blank_lines(stdout), 700):
            truncated.append("stdout")
        if was_truncated(trim_blank_lines(stderr), 700):
            truncated.append("stderr")
        if errors and was_truncated(_one_line(errors[-1]), 700):
            truncated.append("error")
        note = _truncated_output_note(truncated, cell_id, index)

        item: dict[str, Any] = {
            "cell_index": index,
            "cell_id": cell_id,
            "status": result.status,
            "ok": result.ok,
            "cell_status": cell_status(cell),
        }
        if stdout:
            item["stdout"] = stdout
        if stderr:
            item["stderr"] = stderr
        if errors:
            item["errors"] = errors
        if rich:
            item["rich_outputs"] = rich
            item.update(_rich_output_export_data(input_path, index, cell_id))
        if note:
            item["output_note"] = note
        items.append(item)
    return items


def cell_command_result(
    input_path: str,
    notebook,
    index: int,
    cell,
    result: ExecutionResult,
    *,
    stale_indices: list[int] | None = None,
    stale_code_indices: list[int] | None = None,
    stale_markdown_indices: list[int] | None = None,
    stale_refs: str | None = None,
    stale_code_refs: str | None = None,
    stale_markdown_refs: str | None = None,
    provisional: bool = False,
    next_action: str | None = None,
) -> CommandResult:
    lines = [
        *notebook_lines(input_path, notebook),
        f"cell_index: {index}",
        f"cell_id: {cell.get('id', '')}",
        f"status: {result.status}",
    ]
    persisted_status = cell_status(cell)
    if persisted_status != result.status:
        lines.append(f"cell_status: {persisted_status}")
    stdout = stream_text(result.outputs, "stdout")
    stderr = stream_text(result.outputs, "stderr")
    truncated = []
    if stdout:
        lines.extend(["stdout:", truncate(stdout)])
        if was_truncated(stdout, PRINT_LIMIT):
            truncated.append("stdout")
    if stderr:
        lines.extend(["stderr:", truncate(stderr)])
        if was_truncated(stderr, PRINT_LIMIT):
            truncated.append("stderr")
    for output in result.outputs:
        if output.get("output_type") == "error":
            rendered_error = error_text(output)
            lines.extend(["error:", truncate(rendered_error)])
            if was_truncated(rendered_error, PRINT_LIMIT):
                truncated.append("error")
    note = _truncated_output_note(truncated, cell.get("id", ""), index)
    if note:
        lines.append(note)
    rich = rich_outputs(result.outputs)
    if rich:
        lines.append("rich_outputs:")
        lines.extend(f"- {item} saved in notebook" for item in rich)
        lines.extend(_rich_output_export_lines(input_path, index, cell.get("id", "")))
    if stale_indices:
        lines.append(f"provisional: {'true' if provisional else 'false'}")
        lines.append(
            f"stale_downstream_cell_indices: {', '.join(str(item) for item in stale_indices)}"
        )
        if stale_refs:
            lines.append(f"stale_downstream_cell_refs: {stale_refs}")
        if stale_code_indices:
            lines.append(
                "stale_downstream_code_cell_indices: "
                f"{', '.join(str(item) for item in stale_code_indices)}"
            )
            if stale_code_refs:
                lines.append(f"stale_downstream_code_cell_refs: {stale_code_refs}")
        if stale_markdown_indices:
            lines.append(
                "stale_downstream_markdown_cell_indices: "
                f"{', '.join(str(item) for item in stale_markdown_indices)}"
            )
            if stale_markdown_refs:
                lines.append(f"stale_downstream_markdown_cell_refs: {stale_markdown_refs}")
    if next_action:
        lines.append(f"next_action: {next_action}")
    data: dict[str, Any] = {
        "kind": "cell_execution",
        "notebook": notebook_data(input_path, notebook),
        "cell_index": index,
        "cell_id": cell.get("id", ""),
        "status": result.status,
        "ok": result.ok,
    }
    if persisted_status != result.status:
        data["cell_status"] = persisted_status
    if stdout:
        data["stdout"] = stdout
    if stderr:
        data["stderr"] = stderr
    errors = [error_text(output) for output in result.outputs if output.get("output_type") == "error"]
    if errors:
        data["errors"] = errors
    if note:
        data["output_note"] = note
    if rich:
        data["rich_outputs"] = rich
        data.update(_rich_output_export_data(input_path, index, cell.get("id", "")))
    if stale_indices:
        data["stale_downstream"] = {
            "indices": stale_indices,
            "refs": stale_refs or "",
            "code_indices": stale_code_indices or [],
            "code_refs": stale_code_refs or "",
            "markdown_indices": stale_markdown_indices or [],
            "markdown_refs": stale_markdown_refs or "",
        }
        data["provisional"] = provisional
    if next_action:
        data["next_action"] = next_action
    return CommandResult(
        "\n".join(lines).rstrip() + "\n",
        exit_code=0 if result.ok else 1,
        data=data,
    )


def run_clean_command_result(
    input_path: str,
    notebook,
    *,
    status: str,
    executed_cells: int,
    total_code_cells: int,
    elapsed_seconds: float,
    exit_code: int,
    env_lines: list[str] | None = None,
    output_lines: list[str] | None = None,
    executed_results: list[tuple[int, object, ExecutionResult]] | None = None,
    remaining_stale_cell_indices: list[int] | None = None,
    remaining_next_action: str | None = None,
    failed_cell_index: int | None = None,
    failed_cell_id: str | None = None,
    error: str | None = None,
    failed_next_action: str | None = None,
) -> CommandResult:
    lines = notebook_lines(input_path, notebook)
    lines.extend(
        [
            f"status: {status}",
            f"executed_cells: {executed_cells}",
            f"total_code_cells: {total_code_cells}",
            f"elapsed_seconds: {elapsed_seconds:.3f}",
        ]
    )
    if env_lines:
        lines.append("env:")
        lines.extend(env_lines)
    if output_lines:
        lines.append("cell_outputs:")
        lines.extend(output_lines)
    if remaining_stale_cell_indices:
        stale_list = ", ".join(str(item) for item in remaining_stale_cell_indices)
        lines.append(f"remaining_stale_cell_indices: {stale_list}")
        if remaining_next_action:
            lines.append(f"next_action: {remaining_next_action}")
    if failed_cell_index is not None:
        lines.append(f"failed_cell: {failed_cell_index}")
        lines.append(f"failed_cell_id: {failed_cell_id or ''}")
        if error:
            lines.append(f"error: {truncate(error)}")
        if failed_next_action:
            lines.append(f"next_action: {failed_next_action}")
    data: dict[str, Any] = {
        "kind": "run_clean",
        "notebook": notebook_data(input_path, notebook),
        "status": status,
        "executed_cells": executed_cells,
        "total_code_cells": total_code_cells,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }
    if env_lines:
        data["env"] = env_lines
    if output_lines:
        data["cell_outputs_text"] = output_lines
    if executed_results is not None:
        data["cell_outputs"] = execution_summary_data(executed_results, input_path=input_path)
    if remaining_stale_cell_indices:
        data["remaining_stale_cell_indices"] = remaining_stale_cell_indices
        if remaining_next_action:
            data["next_action"] = remaining_next_action
    if failed_cell_index is not None:
        data["failed_cell"] = failed_cell_index
        data["failed_cell_id"] = failed_cell_id or ""
        if error:
            data["error"] = error
        if failed_next_action:
            data["next_action"] = failed_next_action
    return CommandResult("\n".join(lines) + "\n", exit_code=exit_code, data=data)


def stream_text(outputs, name: str) -> str:
    return join_stream_parts(
        [
            text_value(output.get("text", ""))
            for output in outputs
            if output.get("output_type") == "stream" and output.get("name") == name
        ]
    )


def rich_outputs(outputs) -> list[str]:
    rich: list[str] = []
    for output in outputs:
        if output.get("output_type") not in {"display_data", "execute_result"}:
            continue
        data = output.get("data", {})
        rich.append(rich_mime_summary(data))
    return rich


def error_text(output) -> str:
    traceback = output.get("traceback") or []
    if traceback:
        return strip_ansi("\n".join(traceback[-8:]))
    return strip_ansi(str(output.get("evalue") or output.get("ename") or ""))


def truncate(text: str, limit: int = PRINT_LIMIT) -> str:
    return truncate_head_tail(text, limit)


def user_error(exc: Exception) -> CommandResult:
    if isinstance(exc, NotebookLensError):
        return CommandResult(
            f"error: {exc}\n",
            exit_code=2,
            data={"kind": "error", "error": str(exc), "error_type": type(exc).__name__},
        )
    return CommandResult(
        f"error: {type(exc).__name__}: {exc}\n",
        exit_code=2,
        data={"kind": "error", "error": str(exc), "error_type": type(exc).__name__},
    )


def _one_line(text: str) -> str:
    return " ".join(text.strip().split())


def _append_excerpt(lines: list[str], label: str, text: str, *, limit: int) -> bool:
    trimmed = trim_blank_lines(text)
    excerpt = _head_tail_excerpt(trimmed, limit)
    if not excerpt:
        return False
    if "\n" not in excerpt:
        lines.append(f"  {label}: {excerpt}")
        return was_truncated(trimmed, limit)
    lines.append(f"  {label}:")
    lines.extend(f"    {line}" if line else "    " for line in excerpt.splitlines())
    return was_truncated(trimmed, limit)


def _head_tail_excerpt(text: str, limit: int) -> str:
    return truncate_head_tail(text, limit)


def _truncated_output_note(labels: list[str], cell_id: str, index: int) -> str | None:
    if not labels:
        return None
    ref = f"--id {cell_id}" if cell_id else str(index)
    label_text = ", ".join(dict.fromkeys(labels))
    return (
        f"output_note: {label_text} truncated; inspect with show-cell {ref} "
        f"--outputs full --max-chars {FULL_OUTPUT_HINT_LIMIT}"
    )


def _rich_output_export_lines(
    input_path: str,
    index: int,
    cell_id: str,
    *,
    indent: str = "",
) -> list[str]:
    return [
        f"{indent}{key}: {value}"
        for key, value in _rich_output_export_data(input_path, index, cell_id).items()
    ]


def _rich_output_export_data(input_path: str, index: int, cell_id: str) -> dict[str, str]:
    if not input_path:
        return {}
    return {
        "rich_output_export_command": rich_output_export_command(input_path, index, cell_id),
        "rich_output_cleanup_note": RICH_OUTPUT_CLEANUP_NOTE,
    }


def rich_output_export_command(input_path: str, index: int, cell_id: str) -> str:
    selector = f"--id {quote(cell_id)}" if cell_id else str(index)
    return f"notebook-lens export-output {quote(input_path)} {selector} --dir <dir>"
