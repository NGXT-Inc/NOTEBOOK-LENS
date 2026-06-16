"""Single-cell execution through a standard Jupyter kernel."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

import nbformat
from jupyter_client import BlockingKernelClient
from nbformat import NotebookNode

from .env_policy import SYNC_ENV_PREFIXES, synced_env_snapshot
from .errors import CellExecutionError
from .session import KernelSession

MAX_STREAM_CHARS = 100_000


@dataclass
class ExecutionResult:
    """Result of one cell execution."""

    status: str
    outputs: list[NotebookNode]
    execution_count: int | None = None
    timed_out: bool = False
    interrupted: bool = False
    kernel_reset: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def execute_code(
    session: KernelSession,
    source: str,
    *,
    timeout: float = 120,
    sync_env: bool = True,
) -> ExecutionResult:
    """Execute code in the notebook's live kernel and return notebook outputs."""

    client = session.client(start=True)
    try:
        if sync_env:
            setup_error = _sync_kernel_env(client, timeout=min(timeout, 10))
            if setup_error:
                return ExecutionResult(
                    status="error",
                    outputs=[
                        nbformat.v4.new_output(
                            "error",
                            ename="NotebookLensEnvSyncError",
                            evalue=setup_error,
                            traceback=[f"NotebookLensEnvSyncError: {setup_error}"],
                        )
                    ],
                    error=setup_error,
                )
        return _execute_with_client(client, session, source, timeout=timeout)
    finally:
        client.stop_channels()
        session.write_state(last_seen_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def _sync_kernel_env(client: BlockingKernelClient, *, timeout: float) -> str:
    values = synced_env_snapshot(os.environ)
    code = "\n".join(
        [
            "import os as __notebook_lens_os",
            f"__notebook_lens_env = {json.dumps(values, sort_keys=True)}",
            f"__notebook_lens_prefixes = {json.dumps(SYNC_ENV_PREFIXES)}",
            "for __notebook_lens_key in list(__notebook_lens_os.environ):",
            "    if __notebook_lens_key.startswith(tuple(__notebook_lens_prefixes)):",
            "        __notebook_lens_os.environ.pop(__notebook_lens_key, None)",
            "__notebook_lens_os.environ.update(__notebook_lens_env)",
            (
                "del __notebook_lens_os, __notebook_lens_env, "
                "__notebook_lens_prefixes, __notebook_lens_key"
            ),
        ]
    )
    msg_id = client.execute(
        code,
        allow_stdin=False,
        silent=True,
        store_history=False,
        stop_on_error=True,
    )
    deadline = time.monotonic() + timeout
    shell_error = ""
    while time.monotonic() < deadline:
        try:
            msg = client.get_iopub_msg(timeout=0.2)
        except Exception:
            shell = _drain_shell_reply(client, msg_id)
            if shell and shell.get("status") == "error":
                shell_error = shell.get("evalue") or shell.get("ename") or "env sync failed"
            continue
        if msg.get("parent_header", {}).get("msg_id") != msg_id:
            continue
        msg_type = msg.get("header", {}).get("msg_type")
        content = msg.get("content", {})
        if msg_type == "error":
            shell_error = content.get("evalue") or content.get("ename") or "env sync failed"
        if msg_type == "status" and content.get("execution_state") == "idle":
            shell = _drain_shell_reply(client, msg_id)
            if shell and shell.get("status") == "error":
                shell_error = shell.get("evalue") or shell.get("ename") or "env sync failed"
            return shell_error
    return f"kernel env sync timed out after {timeout:g}s"


def _execute_with_client(
    client: BlockingKernelClient,
    session: KernelSession,
    source: str,
    *,
    timeout: float,
) -> ExecutionResult:
    msg_id = client.execute(
        source,
        allow_stdin=False,
        store_history=True,
        stop_on_error=True,
    )
    deadline = time.monotonic() + timeout
    outputs: list[NotebookNode] = []
    display_by_id: dict[str, int] = {}
    stream_chars: dict[str, int] = {}
    truncated_streams: set[str] = set()
    execution_count: int | None = None
    shell_status = "ok"
    shell_error = ""

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            interrupted = session.interrupt()
            settled = _wait_for_idle(client, msg_id, timeout=5)
            error = f"cell timed out after {timeout:g}s"
            kernel_reset = False
            if not settled:
                session.reset()
                kernel_reset = True
                error += "; kernel did not settle after interrupt and was reset"
            outputs.append(
                nbformat.v4.new_output(
                    "error",
                    ename="TimeoutError",
                    evalue=error,
                    traceback=[f"TimeoutError: {error}"],
                )
            )
            return ExecutionResult(
                status="timeout",
                outputs=outputs,
                execution_count=execution_count,
                timed_out=True,
                interrupted=interrupted,
                kernel_reset=kernel_reset,
                error=error,
            )

        try:
            msg = client.get_iopub_msg(timeout=min(remaining, 1.0))
        except Exception:
            _drain_shell_reply(client, msg_id)
            continue

        if msg.get("parent_header", {}).get("msg_id") != msg_id:
            continue

        msg_type = msg["header"]["msg_type"]
        content = msg["content"]

        if msg_type == "status" and content.get("execution_state") == "idle":
            shell = _drain_shell_reply(client, msg_id)
            if shell:
                shell_status = shell.get("status", shell_status)
                if shell_status == "error":
                    shell_error = shell.get("evalue") or shell.get("ename") or ""
            break
        if msg_type == "execute_input":
            execution_count = content.get("execution_count")
            continue
        if msg_type == "stream":
            _append_stream(
                outputs,
                name=content.get("name", "stdout"),
                text=content.get("text", ""),
                stream_chars=stream_chars,
                truncated_streams=truncated_streams,
            )
            continue
        if msg_type in {"display_data", "execute_result"}:
            out = nbformat.v4.new_output(
                msg_type,
                data=content.get("data", {}),
                metadata=content.get("metadata", {}),
            )
            if msg_type == "execute_result":
                out["execution_count"] = content.get("execution_count")
            display_id = content.get("transient", {}).get("display_id")
            if display_id:
                display_by_id[display_id] = len(outputs)
            outputs.append(out)
            continue
        if msg_type == "update_display_data":
            display_id = content.get("transient", {}).get("display_id")
            out = nbformat.v4.new_output(
                "display_data",
                data=content.get("data", {}),
                metadata=content.get("metadata", {}),
            )
            if display_id and display_id in display_by_id:
                outputs[display_by_id[display_id]] = out
            else:
                outputs.append(out)
            continue
        if msg_type == "clear_output":
            outputs.clear()
            display_by_id.clear()
            continue
        if msg_type == "error":
            outputs.append(
                nbformat.v4.new_output(
                    "error",
                    ename=content.get("ename", "Error"),
                    evalue=content.get("evalue", ""),
                    traceback=content.get("traceback", []),
                )
            )
            shell_status = "error"
            shell_error = content.get("evalue", "") or content.get("ename", "")

    if shell_status == "error":
        if not any(output.get("output_type") == "error" for output in outputs):
            outputs.append(
                nbformat.v4.new_output(
                    "error",
                    ename="CellExecutionError",
                    evalue=shell_error,
                    traceback=[shell_error],
                )
            )
        return ExecutionResult(
            status="error",
            outputs=outputs,
            execution_count=execution_count,
            error=shell_error,
        )

    return ExecutionResult(status="ok", outputs=outputs, execution_count=execution_count)


def _drain_shell_reply(
    client: BlockingKernelClient, msg_id: str
) -> dict[str, Any] | None:
    """Fetch the shell reply for an execute request if available."""

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            msg = client.get_shell_msg(timeout=0.2)
        except Exception:
            return None
        if msg.get("parent_header", {}).get("msg_id") == msg_id:
            return dict(msg.get("content") or {})
    return None


def _append_stream(
    outputs: list[NotebookNode],
    *,
    name: str,
    text: str,
    stream_chars: dict[str, int],
    truncated_streams: set[str],
) -> None:
    if name in truncated_streams:
        return
    current = stream_chars.get(name, 0)
    remaining = MAX_STREAM_CHARS - current
    if len(text) > remaining:
        text = text[: max(0, remaining)]
        text += f"\n...[notebook-lens truncated {name} after {MAX_STREAM_CHARS} chars]\n"
        truncated_streams.add(name)
    stream_chars[name] = current + len(text)
    if not text:
        return
    if outputs and outputs[-1].get("output_type") == "stream" and outputs[-1].get("name") == name:
        outputs[-1]["text"] = str(outputs[-1].get("text", "")) + text
        return
    outputs.append(nbformat.v4.new_output("stream", name=name, text=text))


def _wait_for_idle(client: BlockingKernelClient, msg_id: str, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msg = client.get_iopub_msg(timeout=0.2)
        except Exception:
            continue
        if msg.get("parent_header", {}).get("msg_id") != msg_id:
            continue
        if (
            msg.get("header", {}).get("msg_type") == "status"
            and msg.get("content", {}).get("execution_state") == "idle"
        ):
            _drain_shell_reply(client, msg_id)
            return True
    return False


def ensure_code_cell(nb: NotebookNode, index: int) -> NotebookNode:
    if index < 0:
        raise CellExecutionError(f"cell index out of range: {index}")
    try:
        cell = nb.cells[index]
    except IndexError as exc:
        raise CellExecutionError(f"cell index out of range: {index}") from exc
    if cell.get("cell_type") != "code":
        raise CellExecutionError(f"cell is not a code cell: {index}")
    return cell
