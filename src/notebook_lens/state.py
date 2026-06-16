"""Notebook state rendering for agent context."""

from __future__ import annotations

import html
import re

from nbformat import NotebookNode

from .cell_state import cell_source, cell_status, source_changed_since_execution
from .notebooks import META_KEY, load_notebook
from .rich_output import exportable_mime_summaries, image_summaries, json_preview, rich_mime_summary
from .text_render import (
    clean_stream_text,
    strip_ansi,
    text_value,
    trim_blank_lines,
    truncate_head_tail,
)

CHARS_PER_TOKEN = 4
MAX_CODE_CHARS = 1200
MAX_STREAM_CHARS = 1200
MAX_ERROR_LINES = 12


def render_state(
    path,
    *,
    max_tokens: int = 6000,
    full_source: bool = False,
    outputs: str = "summary",
) -> str:
    nb = load_notebook(path)
    return render_loaded_state(
        nb,
        title=str(path),
        max_tokens=max_tokens,
        full_source=full_source,
        outputs=outputs,
    )


def render_loaded_state(
    nb: NotebookNode,
    *,
    title: str,
    max_tokens: int = 6000,
    full_source: bool = False,
    outputs: str = "summary",
) -> str:
    text = _render(nb, title=title, full_source=full_source, outputs=outputs)
    limit = max_tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text
    return _render_compressed(
        nb,
        title=title,
        limit=limit,
        full_source=full_source,
        outputs=outputs,
    )


def render_cell(
    path,
    index: int,
    *,
    full_source: bool = True,
    outputs: str = "full",
    max_chars: int | None = None,
) -> str:
    nb = load_notebook(path)
    return render_loaded_cell(
        nb,
        title=str(path),
        index=index,
        full_source=full_source,
        outputs=outputs,
        max_chars=max_chars,
    )


def render_loaded_cell(
    nb: NotebookNode,
    *,
    title: str,
    index: int,
    full_source: bool = True,
    outputs: str = "full",
    max_chars: int | None = None,
) -> str:
    if index < 0:
        raise IndexError(f"cell index out of range: {index}")
    try:
        cell = nb.cells[index]
    except IndexError as exc:
        raise IndexError(f"cell index out of range: {index}") from exc
    lines = [f"## {title}", ""]
    lines.extend(
        _render_cell(
            index,
            cell,
            full=True,
            full_source=full_source,
            outputs=outputs,
            output_limit=max_chars,
        )
    )
    return "\n".join(lines).rstrip() + "\n"


def _render(nb: NotebookNode, *, title: str, full_source: bool, outputs: str) -> str:
    lines = [f"## {title}", ""]
    if not nb.cells:
        lines.append("*No cells yet.*")
        return "\n".join(lines).rstrip() + "\n"

    detail_full = _show_source(full_source=full_source, outputs=outputs)
    for index, cell in enumerate(nb.cells):
        lines.extend(
            _render_cell(index, cell, full=detail_full, full_source=full_source, outputs=outputs)
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_compressed(
    nb: NotebookNode,
    *,
    title: str,
    limit: int,
    full_source: bool,
    outputs: str,
) -> str:
    lines = [f"## {title}", ""]
    if not nb.cells:
        lines.append("*No cells yet.*")
        return "\n".join(lines).rstrip() + "\n"

    recent_start = max(0, len(nb.cells) - 8)
    failed = [
        index for index, cell in enumerate(nb.cells) if cell_status(cell) in {"error", "timeout"}
    ]
    stale = [index for index, cell in enumerate(nb.cells) if cell_status(cell) == "stale"]
    recent = list(range(recent_start, len(nb.cells)))
    repair_context = failed if failed else stale[:8]
    important = _ordered_unique([*repair_context, *recent])

    if failed:
        lines.append("### Failed Cells")
        for index in failed:
            lines.append(_outline_line(index, nb.cells[index], include_issue=True))
        lines.append("")

    lines.append("### Cell Map")
    lines.extend(_compact_map_lines(nb))
    lines.append("")

    lines.append("### Important Cells")
    for index in important:
        lines.append(_outline_line(index, nb.cells[index], include_issue=True))
    lines.append("")

    if _show_source(full_source=full_source, outputs=outputs):
        lines.append("### Important Details")
        for index in important:
            lines.extend(
                _render_cell(index, nb.cells[index], full=True, full_source=full_source, outputs=outputs)
            )
            lines.append("")
    return _fit_lines(lines, limit)


def _render_cell(
    index: int,
    cell: NotebookNode,
    *,
    full: bool,
    full_source: bool,
    outputs: str,
    output_limit: int | None = MAX_STREAM_CHARS,
) -> list[str]:
    status = cell_status(cell)
    desc = _description(cell)
    cell_type = cell.get("cell_type", "unknown")
    lines = [f"[{index}] {status} {cell_type} - {desc}"]
    if cell.get("id"):
        lines.append(f"cell_id: {cell['id']}")
    meta = cell.get("metadata", {}).get(META_KEY, {})
    if meta.get("stale_reason"):
        lines.append(f"Reason: {meta['stale_reason']}")
    if source_changed_since_execution(cell):
        lines.append(
            "Warning: source changed since this cell was last executed; stored outputs may be stale"
        )
    if meta.get("provisional_reason"):
        lines.append(f"Provisional: {meta['provisional_reason']}")

    if cell.get("cell_type") != "code":
        if full:
            lines.append("")
            _append_fenced(
                lines,
                _truncate(cell_source(cell), None if full_source else MAX_CODE_CHARS),
                "markdown",
            )
        return lines

    source = cell_source(cell)
    if full:
        lines.append("")
        _append_fenced(lines, _truncate(source, None if full_source else MAX_CODE_CHARS), "python")
    else:
        first = next((line.strip() for line in source.splitlines() if line.strip()), "(empty)")
        lines.append(f"code: {_truncate(first, 120)}")

    output_lines = _render_outputs(
        cell.get("outputs", []),
        full=full,
        mode=outputs,
        output_limit=output_limit,
    )
    if output_lines:
        lines.extend(output_lines)
    return lines


def _show_source(*, full_source: bool, outputs: str) -> bool:
    return full_source or outputs != "none"


def _outline_line(index: int, cell: NotebookNode, *, include_issue: bool) -> str:
    status = cell_status(cell)
    cell_type = cell.get("cell_type", "unknown")
    line = f"[{index}] {status} {cell_type} - {_description(cell)}"
    cell_id = cell.get("id")
    if cell_id:
        line += f" id={cell_id}"
    if include_issue:
        issue = _issue_excerpt(cell)
        if issue:
            line += f" | {issue}"
    return line


def _ordered_unique(indices: list[int]) -> list[int]:
    seen = set()
    ordered = []
    for index in indices:
        if index in seen:
            continue
        seen.add(index)
        ordered.append(index)
    return ordered


def _compact_map_lines(nb: NotebookNode) -> list[str]:
    entries = [
        f"{index}:{_status_token(cell_status(cell))}-{_type_token(cell.get('cell_type', 'unknown'))}"
        for index, cell in enumerate(nb.cells)
    ]
    lines: list[str] = []
    current = ""
    for entry in entries:
        addition = entry if not current else f" | {entry}"
        if current and len(current) + len(addition) > 100:
            lines.append(current)
            current = entry
        else:
            current += addition
    if current:
        lines.append(current)
    return lines or ["(empty)"]


def _status_token(status: str) -> str:
    return {
        "error": "err",
        "timeout": "timeout",
        "stale": "stale",
        "ok": "ok",
        "pending": "pend",
    }.get(status, status[:7] or "?")


def _type_token(cell_type: str) -> str:
    return {
        "code": "code",
        "markdown": "md",
    }.get(cell_type, cell_type[:4] or "?")


def _issue_excerpt(cell: NotebookNode) -> str:
    meta = cell.get("metadata", {}).get(META_KEY, {})
    stale_reason = str(meta.get("stale_reason") or "").strip()
    if stale_reason:
        return f"reason={_truncate(_one_line(stale_reason), 180)}"
    if source_changed_since_execution(cell):
        return "source changed since execution"
    provisional_reason = str(meta.get("provisional_reason") or "").strip()
    if provisional_reason:
        return f"provisional={_truncate(_one_line(provisional_reason), 180)}"
    for output in cell.get("outputs", []):
        if output.get("output_type") != "error":
            continue
        traceback = output.get("traceback") or []
        text = "\n".join(traceback[-2:]) if traceback else output.get("evalue", "")
        text = _truncate(_one_line(strip_ansi(text)), 180)
        if text:
            return f"error={text}"
    return ""


def _render_outputs(
    outputs: list[NotebookNode],
    *,
    full: bool,
    mode: str,
    output_limit: int | None = MAX_STREAM_CHARS,
) -> list[str]:
    if mode == "none":
        return []
    lines: list[str] = []
    for output in outputs:
        kind = output.get("output_type")
        if kind == "stream":
            name = output.get("name", "stdout")
            text = trim_blank_lines(clean_stream_text(_text(output.get("text", ""))))
            if not text:
                continue
            lines.append(f"{name}:")
            _append_fenced(lines, _truncate(text, output_limit if full else 240))
        elif kind == "error":
            traceback = output.get("traceback") or []
            if traceback and full and output_limit is None:
                text = "\n".join(traceback)
            else:
                text = "\n".join(traceback[-MAX_ERROR_LINES:]) if traceback else output.get("evalue", "")
            lines.append("Error:")
            _append_fenced(lines, _truncate(strip_ansi(text), output_limit))
        elif kind in {"display_data", "execute_result"}:
            data = output.get("data", {})
            rich_summary = rich_mime_summary(data)
            exportable = exportable_mime_summaries(data)
            if rich_summary:
                action = (
                    "export-output or open the notebook to view"
                    if exportable
                    else "open the notebook to view"
                )
                lines.append(f"rich output: {rich_summary} saved in notebook; {action}")
            html_preview = _html_preview(data.get("text/html"))
            latex_preview = _text(data.get("text/latex", "")).strip()
            text_plain = _text(data.get("text/plain", ""))
            object_repr = text_plain.startswith("<IPython.core.display.")
            rendered = False
            if html_preview and (mode == "full" or object_repr or not text_plain):
                lines.append("html preview:")
                _append_fenced(lines, _truncate(html_preview, output_limit if full else 240))
                rendered = True
            if latex_preview and (mode == "full" or object_repr or not text_plain):
                lines.append("latex preview:")
                _append_fenced(
                    lines,
                    _truncate(latex_preview, output_limit if full else 240),
                    "tex",
                )
                rendered = True
            if "application/json" in data:
                lines.append("json:")
                _append_fenced(
                    lines,
                    _truncate(
                        json_preview(data.get("application/json")),
                        output_limit if full else 240,
                    ),
                    "json",
                )
                rendered = True
            images = image_summaries(data)
            if images:
                lines.append("images:")
                lines.extend(f"- {item} saved in notebook" for item in images)
                rendered = True
            if "text/plain" in data and full and not object_repr:
                lines.append("result:")
                _append_fenced(lines, _truncate(text_plain, output_limit))
                rendered = True
            if not rendered and not rich_summary:
                lines.append(f"rich output: {rich_mime_summary(data)} saved in notebook")
    return lines


def _description(cell: NotebookNode) -> str:
    meta = cell.get("metadata", {}).get(META_KEY, {})
    desc = str(meta.get("description") or "").strip()
    if desc:
        return desc
    source = cell_source(cell)
    first = next((line.strip() for line in source.splitlines() if line.strip()), "")
    return _truncate(first or "(empty)", 80)


def _text(value) -> str:
    return text_value(value)


def _one_line(text: str) -> str:
    return " ".join(text.strip().split())


def _truncate(text: str, limit: int | None) -> str:
    return truncate_head_tail(text, limit)


def _html_preview(value) -> str:
    text = _text(value or "")
    if not text:
        return ""
    stripped = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    stripped = re.sub(r"(?i)</tr\s*>", "\n", stripped)
    stripped = re.sub(r"(?i)</t[dh]\s*>", "\t", stripped)
    stripped = re.sub(r"(?i)<br\s*/?>", "\n", stripped)
    stripped = re.sub(r"(?s)<[^>]+>", " ", stripped)
    stripped = html.unescape(stripped)
    lines = []
    for line in stripped.splitlines():
        cells = [re.sub(r"[ \t]+", " ", cell).strip() for cell in line.split("\t")]
        cells = [cell for cell in cells if cell]
        if cells:
            lines.append(" | ".join(cells))
    if lines:
        return "\n".join(lines)
    return re.sub(r"\s+", " ", stripped).strip()


def _append_fenced(lines: list[str], text: str, info: str = "") -> None:
    fence = _fence_for(text)
    suffix = info if info else ""
    lines.append(f"{fence}{suffix}")
    lines.append(text)
    lines.append(fence)


def _fence_for(text: str) -> str:
    runs = [len(match.group(0)) for match in re.finditer(r"`+", text)]
    return "`" * max([3, *(length + 1 for length in runs)])


def _fit_lines(lines: list[str], limit: int) -> str:
    marker = "[state truncated]"
    kept: list[str] = []
    total = 0
    fence: str | None = None
    for line in lines:
        addition = len(line) + 1
        closing = [fence, ""] if fence else []
        reserved = sum(len(item) + 1 for item in closing) + len(marker) + 1
        if total + addition + reserved > limit:
            if fence:
                kept.append(fence)
                kept.append("")
            if kept and kept[-1]:
                kept.append("")
            kept.append(marker)
            break
        kept.append(line)
        total += addition
        fence = _active_fence_after(line, fence)
    return "\n".join(kept).rstrip() + "\n"


def _active_fence_after(line: str, fence: str | None) -> str | None:
    stripped = line.strip()
    match = re.match(r"^(`{3,})(?:\w+)?$", stripped)
    if not match:
        return fence
    marker = match.group(1)
    if fence is None:
        return marker
    if marker == fence:
        return None
    return fence
