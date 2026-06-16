"""Environment and path handling."""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import PathOutsideNotebookDirError


@dataclass(frozen=True)
class LensConfig:
    """Resolved Notebook Lens directories."""

    experiment_dir: Path
    notebook_dir: Path
    runtime_dir: Path
    kernel_python: str

    @classmethod
    def from_env(cls, cwd: Path | None = None) -> "LensConfig":
        base_cwd = Path(cwd or Path.cwd()).resolve()
        experiment_dir = Path(os.environ.get("NL_EXPERIMENT_DIR", base_cwd)).resolve()
        notebook_dir = Path(
            os.environ.get("NL_NOTEBOOK_DIR", experiment_dir / "notebooks")
        ).resolve()
        runtime_dir = Path(
            os.environ.get("NL_RUNTIME_DIR", experiment_dir / ".notebook_lens")
        ).resolve()
        kernel_python = os.environ.get("NL_KERNEL_PYTHON", sys.executable)
        return cls(
            experiment_dir=experiment_dir,
            notebook_dir=notebook_dir,
            runtime_dir=runtime_dir,
            kernel_python=kernel_python,
        )

    @property
    def sessions_dir(self) -> Path:
        return self.runtime_dir / "sessions"

    @property
    def locks_dir(self) -> Path:
        return self.runtime_dir / "locks"

    def ensure_dirs(self) -> None:
        self.notebook_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class NotebookPath:
    """A notebook path constrained to the configured notebook directory."""

    path: Path
    rel: str
    key: str


def resolve_notebook_path(raw: str, config: LensConfig) -> NotebookPath:
    """Resolve a user path and require it to stay under ``config.notebook_dir``.

    Relative paths can be passed either from the experiment root
    (``notebooks/explore.ipynb``) or from the notebook directory
    (``explore.ipynb``).
    """

    if not raw:
        raise PathOutsideNotebookDirError("empty notebook path")

    user_path = Path(raw).expanduser()
    if user_path.is_absolute():
        candidate = user_path
    else:
        parts = user_path.parts
        if parts and parts[0] == config.notebook_dir.name:
            candidate = config.experiment_dir / user_path
        else:
            candidate = config.notebook_dir / user_path

    resolved = candidate.resolve(strict=False)
    notebook_root = config.notebook_dir.resolve(strict=False)
    try:
        rel_path = resolved.relative_to(notebook_root)
    except ValueError as exc:
        raise PathOutsideNotebookDirError(
            f"notebook path must stay under {notebook_root}: {raw}"
        ) from exc

    if any(part == ".." for part in rel_path.parts):
        raise PathOutsideNotebookDirError(f"invalid notebook path: {raw}")
    if resolved.suffix != ".ipynb":
        raise PathOutsideNotebookDirError("notebook path must end with .ipynb")

    rel = rel_path.as_posix()
    key = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    return NotebookPath(path=resolved, rel=rel, key=key)
