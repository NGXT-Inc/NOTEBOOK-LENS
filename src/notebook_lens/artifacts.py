"""Internal artifact scope and diff helpers for Notebook Lens commands."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .env import LensConfig

__all__ = [
    "ArtifactChange",
    "ArtifactInventoryItem",
    "ArtifactScope",
    "artifact_changes",
    "artifact_inventory",
    "artifact_scopes",
    "artifact_snapshot",
    "display_path",
]


@dataclass(frozen=True)
class ArtifactScope:
    """One artifact directory watched for a command."""

    root: Path
    source: str


@dataclass(frozen=True)
class ArtifactChange:
    """One observed artifact diff between two snapshots."""

    path: str
    status: str
    size: int | None = None


@dataclass(frozen=True)
class ArtifactInventoryItem:
    """One artifact file observed in the current snapshot."""

    path: str
    size: int


def artifact_snapshot(config: LensConfig, notebook: Any = None) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for scope in artifact_scopes(config, notebook):
        root = scope.root
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
                rel = display_path(config, path)
            except OSError:
                continue
            snapshot[rel] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def artifact_scopes(config: LensConfig, notebook: Any = None) -> list[ArtifactScope]:
    scopes: list[ArtifactScope] = []
    for env_name in ("NL_ARTIFACT_DIR", "ML_ARTIFACT_DIR"):
        raw = os.environ.get(env_name)
        if raw:
            scopes.append(ArtifactScope(_resolve_experiment_path(config, raw), env_name))
            break

    if not scopes and notebook is not None:
        rel = Path(notebook.rel)
        scopes.append(
            ArtifactScope(
                config.experiment_dir / "artifacts" / rel.with_suffix(""),
                "inferred",
            )
        )
    if not scopes:
        scopes.append(ArtifactScope(config.experiment_dir / "artifacts", "fallback"))

    unique: list[ArtifactScope] = []
    seen = set()
    for scope in scopes:
        resolved = scope.root.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(ArtifactScope(resolved, scope.source))
    return unique


def artifact_changes(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> list[ArtifactChange]:
    changes = []
    for rel, (_, size) in sorted(after.items()):
        if rel not in before:
            changes.append(ArtifactChange(rel, "new", size))
        elif before[rel] != after[rel]:
            changes.append(ArtifactChange(rel, "modified", size))
    for rel in sorted(before):
        if rel not in after:
            changes.append(ArtifactChange(rel, "deleted"))
    return changes


def artifact_inventory(snapshot: dict[str, tuple[int, int]]) -> list[ArtifactInventoryItem]:
    return [
        ArtifactInventoryItem(rel, size)
        for rel, (_, size) in sorted(snapshot.items())
    ]


def display_path(config: LensConfig, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(config.experiment_dir).as_posix()
    except ValueError:
        return str(path.resolve(strict=False))


def _resolve_experiment_path(config: LensConfig, raw: str) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return config.experiment_dir / path
