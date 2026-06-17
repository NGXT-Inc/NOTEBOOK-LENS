"""Notebook Lens exception types."""

from __future__ import annotations


class NotebookLensError(Exception):
    """Base class for user-facing Notebook Lens errors."""


class PathOutsideProjectError(NotebookLensError):
    """Raised when a requested notebook path escapes the configured project root."""


class NotebookChangedError(NotebookLensError):
    """Raised when a notebook changed while a command was running."""


class NotebookLockError(NotebookLensError):
    """Raised when a notebook lock cannot be acquired."""


class KernelUnavailableError(NotebookLensError):
    """Raised when a live kernel cannot be reached or started."""


class LiveStateLostError(NotebookLensError):
    """Raised when incremental execution would rely on lost live state."""


class StaleNotebookError(NotebookLensError):
    """Raised when appending would rely on stale downstream state."""


class CellExecutionError(NotebookLensError):
    """Raised when execution cannot complete cleanly."""
