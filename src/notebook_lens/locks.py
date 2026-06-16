"""Advisory notebook locks."""

from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .env import LensConfig, NotebookPath
from .errors import NotebookLockError

STALE_LOCK_SECONDS = 60 * 60


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _lock_stale(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return path.stat().st_mtime < time.time() - STALE_LOCK_SECONDS

    host = data.get("host")
    pid = int(data.get("pid") or 0)
    if host == socket.gethostname():
        return not _pid_alive(pid)
    started = float(data.get("started_at") or 0)
    return bool(started and started < time.time() - STALE_LOCK_SECONDS)


@contextmanager
def notebook_lock(
    config: LensConfig, notebook: NotebookPath, command: str
) -> Iterator[None]:
    """Acquire a coarse advisory lock for a notebook command."""

    config.locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = config.locks_dir / f"{notebook.key}.lock"
    payload = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started_at": time.time(),
        "command": command,
        "notebook": notebook.rel,
    }

    acquired = False
    for _ in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if _lock_stale(lock_path):
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise NotebookLockError(
                f"notebook is locked: {notebook.rel}; another Notebook Lens command "
                "is active for this notebook. Retry after it finishes."
            )
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
            acquired = True
            break

    if not acquired:
        raise NotebookLockError(
            f"could not acquire lock: {notebook.rel}; another Notebook Lens command "
            "may still be active. Retry after it finishes."
        )

    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
