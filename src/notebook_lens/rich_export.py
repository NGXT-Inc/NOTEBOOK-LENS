"""Explicit filesystem export for saved notebook rich outputs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .rich_output import EXPORTABLE_MIME_EXTENSIONS, TEXT_EXTENSIONS, decode_base64
from .text_render import text_value


@dataclass(frozen=True)
class ExportedRichOutput:
    """One rich notebook output explicitly exported to a caller-owned file."""

    path: str
    absolute_path: str
    mimetype: str
    size: int
    cell_index: int
    cell_id: str
    output_index: int


@dataclass(frozen=True)
class SkippedRichOutput:
    """One rich notebook output MIME skipped during explicit export."""

    mimetype: str
    reason: str
    cell_index: int
    cell_id: str
    output_index: int


def export_rich_outputs(
    outputs: list[Any],
    output_dir: Path,
    *,
    cell_index: int,
    cell_id: str,
    output_index: int | None = None,
) -> tuple[list[ExportedRichOutput], list[SkippedRichOutput]]:
    """Export supported rich outputs to ``output_dir`` when explicitly requested."""

    exported: list[ExportedRichOutput] = []
    skipped: list[SkippedRichOutput] = []
    for current_index, output in enumerate(outputs):
        if output_index is not None and current_index != output_index:
            continue
        if output.get("output_type") not in {"display_data", "execute_result"}:
            continue

        data = output.get("data", {})
        exportable_items = _exportable_items(data)
        if not exportable_items:
            skipped.extend(
                SkippedRichOutput(
                    mimetype=mimetype,
                    reason="unsupported",
                    cell_index=cell_index,
                    cell_id=cell_id,
                    output_index=current_index,
                )
                for mimetype in sorted(data)
            )
            continue

        multi_exportable_bundle = len(exportable_items) > 1
        for mimetype, value in exportable_items:
            raw = _rich_output_bytes(mimetype, value)
            if raw is None:
                skipped.append(
                    SkippedRichOutput(
                        mimetype=mimetype,
                        reason="could not decode",
                        cell_index=cell_index,
                        cell_id=cell_id,
                        output_index=current_index,
                    )
                )
                continue

            path = output_dir / _rich_output_filename(
                cell_index,
                current_index,
                mimetype,
                include_mimetype=multi_exportable_bundle,
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            exported.append(
                ExportedRichOutput(
                    path=str(path),
                    absolute_path=str(path.resolve(strict=False)),
                    mimetype=mimetype,
                    size=len(raw),
                    cell_index=cell_index,
                    cell_id=cell_id,
                    output_index=current_index,
                )
            )
    return exported, skipped


def _exportable_items(data: dict[str, Any]) -> list[tuple[str, Any]]:
    return [
        (mimetype, value)
        for mimetype, value in sorted(data.items())
        if mimetype in EXPORTABLE_MIME_EXTENSIONS
    ]


def _rich_output_filename(
    cell_index: int,
    output_index: int,
    mimetype: str,
    *,
    include_mimetype: bool,
) -> str:
    suffix = _mime_path_suffix(mimetype) if include_mimetype else ""
    stem = f"cell{cell_index}_out{output_index}" + (f"_{suffix}" if suffix else "")
    return stem + EXPORTABLE_MIME_EXTENSIONS[mimetype]


def _rich_output_bytes(mimetype: str, value: Any) -> bytes | None:
    text = text_value(value)
    if mimetype == "image/svg+xml":
        return _standalone_svg(text).encode("utf-8")
    if mimetype in TEXT_EXTENSIONS:
        return text.encode("utf-8")
    encoded = "".join(text.split())
    return decode_base64(encoded)


def _standalone_svg(text: str) -> str:
    stripped = text.lstrip()
    if not stripped.startswith("<svg"):
        return text
    open_tag_end = stripped.find(">")
    if open_tag_end == -1 or "xmlns=" in stripped[:open_tag_end]:
        return text
    prefix_len = len(text) - len(stripped)
    prefix = text[:prefix_len]
    return prefix + stripped.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)


def _mime_path_suffix(mimetype: str) -> str:
    cleaned = "".join(
        char if char.isalnum() or char in {"-", "_", "."} else "_"
        for char in mimetype.replace("/", "_").replace("+", "_")
    )
    cleaned = cleaned.strip("._")
    return cleaned or "output"
