"""Jupyter kernel session management."""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jupyter_client import BlockingKernelClient
from jupyter_client.connect import write_connection_file

from .env import LensConfig, NotebookPath
from .errors import KernelUnavailableError
from .notebooks import now_iso, to_json

KERNEL_READY_TIMEOUT = 30
KERNEL_START_ATTEMPTS = 3
KERNEL_READY_RETRY_DELAY = 1.0


@dataclass
class KernelSession:
    """A live or restartable kernel session for one notebook."""

    config: LensConfig
    notebook: NotebookPath

    @property
    def session_dir(self) -> Path:
        return self.config.sessions_dir / self.notebook.key

    @property
    def connection_file(self) -> Path:
        return self.session_dir / "kernel.json"

    @property
    def state_file(self) -> Path:
        return self.session_dir / "session.json"

    def read_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            return {}

    def write_state(self, **updates: Any) -> dict[str, Any]:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        state = self.read_state()
        state.update(updates)
        state.setdefault("notebook", self.notebook.rel)
        state.setdefault("notebook_path", str(self.notebook.path))
        state.setdefault("notebook_key", self.notebook.key)
        state.setdefault("host", socket.gethostname())
        state.setdefault("cwd", str(self.config.experiment_dir))
        state.setdefault("interpreter", self.config.kernel_python)
        _atomic_write_text(self.state_file, to_json(state) + "\n")
        return state

    def clear_execution_marker(self, **updates: Any) -> dict[str, Any]:
        return self.write_state(
            execution_in_progress=False,
            executing_command=None,
            executing_cell=None,
            **updates,
        )

    def pid(self) -> int:
        return int(self.read_state().get("kernel_pid") or 0)

    def is_alive(self) -> bool:
        pid = self.pid()
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def client(
        self,
        *,
        start: bool = True,
        timeout: float = KERNEL_READY_TIMEOUT,
    ) -> BlockingKernelClient:
        if self.connection_file.exists() and self.is_alive():
            client = self._connect()
            if self._ready(client, timeout=timeout):
                return client
            client.stop_channels()

        if not start:
            raise KernelUnavailableError(f"kernel is not running: {self.notebook.rel}")
        return self._start(timeout=timeout)

    def _connect(self) -> BlockingKernelClient:
        client = BlockingKernelClient(connection_file=str(self.connection_file))
        client.load_connection_file()
        client.start_channels()
        return client

    def _ready(self, client: BlockingKernelClient, *, timeout: float) -> bool:
        try:
            client.wait_for_ready(timeout=timeout)
            return True
        except Exception:
            return False

    def _start(self, *, timeout: float) -> BlockingKernelClient:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.session_dir / "kernel.log"

        for attempt in range(1, KERNEL_START_ATTEMPTS + 1):
            kernel_id = str(uuid.uuid4())
            env = dict(os.environ)
            # The CLI is short-lived, but the kernel is the long-lived notebook
            # state. jupyter_client normally asks ipykernel to watch the parent
            # process; disable that parent poller so reconnecting on the next CLI
            # call works without adding a daemon.
            env["JPY_PARENT_PID"] = "1"
            command = [
                self.config.kernel_python,
                "-m",
                "ipykernel_launcher",
                "-f",
                str(self.connection_file),
                "--IPKernelApp.parent_handle=1",
            ]
            log_handle = None
            process = None
            try:
                write_connection_file(
                    fname=str(self.connection_file),
                    ip="127.0.0.1",
                    key=secrets.token_hex(32).encode("ascii"),
                    kernel_name="python3",
                )
                log_handle = log_path.open("ab")
                process = subprocess.Popen(
                    command,
                    cwd=str(self.config.experiment_dir),
                    env=env,
                    stdout=log_handle,
                    stderr=log_handle,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
            except Exception as exc:
                self._record_start_failure(
                    f"failed to start kernel: {type(exc).__name__}",
                    log_path=log_path,
                )
                raise KernelUnavailableError(f"failed to start kernel: {exc}") from exc
            finally:
                if log_handle is not None:
                    try:
                        log_handle.close()
                    except Exception:
                        pass

            pid = process.pid
            self.write_state(
                kernel_alive=True,
                kernel_session_id=kernel_id,
                kernel_pid=pid,
                kernel_start_attempt=attempt,
                kernel_start_attempts=KERNEL_START_ATTEMPTS,
                kernel_log_path=str(log_path),
                connection_file=str(self.connection_file),
                command=command,
                host=socket.gethostname(),
                cwd=str(self.config.experiment_dir),
                interpreter=self.config.kernel_python,
                notebook_path=str(self.notebook.path),
                notebook_key=self.notebook.key,
                started_at=now_iso(),
                last_seen_at=now_iso(),
                kernel_unsafe=False,
                live_state_lost=False,
            )
            try:
                client = self._connect()
            except Exception as exc:
                self._terminate_started_process(process)
                self._record_start_failure(
                    f"failed to connect to kernel: {type(exc).__name__}",
                    log_path=log_path,
                )
                raise KernelUnavailableError(f"failed to connect to kernel: {exc}") from exc
            if self._ready(client, timeout=timeout):
                return client
            client.stop_channels()
            self._terminate_started_process(process)
            if attempt < KERNEL_START_ATTEMPTS:
                time.sleep(KERNEL_READY_RETRY_DELAY)

        reason = f"kernel started but did not become ready after {KERNEL_START_ATTEMPTS} attempts"
        self._record_start_failure(reason, log_path=log_path)
        raise KernelUnavailableError(reason)

    def _record_start_failure(self, unsafe_reason: str, *, log_path: Path | None = None) -> None:
        self.clear_execution_marker(
            kernel_alive=False,
            kernel_pid=0,
            kernel_start_attempts=KERNEL_START_ATTEMPTS,
            kernel_log_path=str(log_path) if log_path is not None else None,
            kernel_unsafe=True,
            live_state_lost=True,
            unsafe_reason=unsafe_reason,
        )
        try:
            self.connection_file.unlink()
        except FileNotFoundError:
            pass

    def _terminate_started_process(self, process: subprocess.Popen) -> None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        except ProcessLookupError:
            pass

    def interrupt(self) -> bool:
        pid = self.pid()
        if pid <= 0:
            return False
        try:
            os.kill(pid, signal.SIGINT)
            return True
        except ProcessLookupError:
            return False

    def reset(self) -> bool:
        pid = self.pid()
        stopped = False
        if pid > 0:
            try:
                os.kill(pid, signal.SIGTERM)
                stopped = True
            except ProcessLookupError:
                stopped = True
            except PermissionError:
                stopped = False
            deadline = time.time() + 5
            while stopped and time.time() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                if stopped:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        self.clear_execution_marker(
            kernel_alive=False,
            kernel_pid=0,
            reset_at=now_iso(),
            live_state_lost=True,
            kernel_unsafe=False,
        )
        try:
            self.connection_file.unlink()
        except FileNotFoundError:
            pass
        return stopped


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            tmp_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
