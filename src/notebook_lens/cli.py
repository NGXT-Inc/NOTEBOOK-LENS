"""Notebook Lens command-line interface."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .commands import (
    cmd_add_markdown,
    cmd_env,
    cmd_export_output,
    cmd_list,
    cmd_new,
    cmd_reset,
    cmd_run_cell,
    cmd_run_clean,
    cmd_show_cell,
    cmd_state,
    cmd_update_cell,
    cmd_update_markdown,
)
from .command_render import CommandResult, user_error
from .env import LensConfig
from .source_io import read_source


class CliParseError(Exception):
    """argparse failure captured for structured CLI rendering."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class NotebookLensArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliParseError(f"{self.format_usage()}{self.prog}: error: {message}\n")

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status:
            raise CliParseError(message or "")
        super().exit(status, message)


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return parsed


PYTHON_FILE_HELP = (
    "Python file read relative to the current shell directory, or '-' for stdin. "
    "For remote SSH wrappers, prefer a synced file path because stdin may not be forwarded."
)
PYTHON_CODE_HELP = (
    "Inline Python source for short local cells. For longer cells or remote SSH wrappers, "
    "prefer --file."
)
MARKDOWN_FILE_HELP = (
    "Required Markdown file read relative to the current shell directory, or '-' for stdin. "
    "For remote SSH wrappers, prefer a synced file path because stdin may not be forwarded."
)
NOTEBOOK_PATH_HELP = (
    "Notebook path relative to NL_EXPERIMENT_DIR/current project root, "
    "or absolute under that root."
)
LEGACY_COMMAND_ALIASES = {
    "run-cell": "add-code",
    "update-cell": "update-code",
    "reset": "reset-kernel",
}
JSON_SCHEMA_VERSION = 1


def build_parser() -> argparse.ArgumentParser:
    parser = NotebookLensArgumentParser(
        prog="notebook-lens",
        description="Agent-facing CLI for standard Jupyter notebooks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Core agent loop:\n"
            "  add-code -> state -> show-cell -> update-code -> run-clean\n\n"
            "Narrative cells:\n"
            "  add-markdown, update-markdown\n\n"
            "Support commands:\n"
            "  new, env, list, export-output, reset-kernel"
        ),
    )
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=NotebookLensArgumentParser,
    )

    new = sub.add_parser("new", help="create a notebook")
    new.add_argument("path", help=NOTEBOOK_PATH_HELP)
    _add_json_flag(new)

    add_code = sub.add_parser("add-code", help="append and execute a code cell")
    add_code.add_argument("path", help=NOTEBOOK_PATH_HELP)
    add_code.add_argument("--desc", default="", help="cell description")
    add_code_source = add_code.add_mutually_exclusive_group(required=True)
    add_code_source.add_argument(
        "--file",
        help=PYTHON_FILE_HELP,
    )
    add_code_source.add_argument("--code", help=PYTHON_CODE_HELP)
    add_code.add_argument("--timeout", type=_positive_float, default=120, help="seconds before interrupting the cell")
    _add_json_flag(add_code)

    state = sub.add_parser("state", help="render notebook state for agent context")
    state.add_argument("path", help=NOTEBOOK_PATH_HELP)
    state.add_argument("--max-tokens", type=_positive_int, default=6000, help="approximate budget for rendered state")
    state.add_argument("--full-source", action="store_true", help="do not truncate cell source")
    state.add_argument(
        "--outputs",
        choices=["summary", "compact", "full", "none"],
        default="summary",
        help=(
            "'none' for structure only, 'summary'/'compact' for capped previews, "
            "'full' for more output detail"
        ),
    )
    _add_json_flag(state)

    show_cell = sub.add_parser("show-cell", help="render one notebook cell")
    show_cell.add_argument("path", help=NOTEBOOK_PATH_HELP)
    show_cell.add_argument("index", type=int, nargs="?", help="zero-based cell index")
    show_cell.add_argument("--id", dest="cell_id", help="stable Jupyter cell id; preferred after edits")
    show_cell.add_argument(
        "--outputs",
        choices=["summary", "compact", "full", "none"],
        default="full",
        help=(
            "'none' for source only, 'summary'/'compact' for capped previews, "
            "'full' for full cell output"
        ),
    )
    show_cell.add_argument(
        "--max-chars",
        type=_positive_int,
        default=None,
        help="maximum characters per rendered output; default is unbounded",
    )
    _add_json_flag(show_cell)

    export_output = sub.add_parser(
        "export-output",
        help="export saved rich cell outputs to caller-owned files",
        description=(
            "Extract supported rich outputs from a saved notebook cell into an explicit "
            "directory. Files are written only because this command was requested; "
            "the caller is responsible for removing them when no longer needed."
        ),
    )
    export_output.add_argument("path", help=NOTEBOOK_PATH_HELP)
    export_output.add_argument("index", type=int, nargs="?", help="zero-based cell index")
    export_output.add_argument("--id", dest="cell_id", help="stable Jupyter cell id; preferred after edits")
    export_output.add_argument(
        "--dir",
        required=True,
        dest="output_dir",
        help="directory where exported output files should be written",
    )
    export_output.add_argument(
        "--output-index",
        type=_nonnegative_int,
        default=None,
        help="optional zero-based notebook output index to export",
    )
    _add_json_flag(export_output)

    update_code = sub.add_parser("update-code", help="replace and execute an existing code cell")
    update_code.add_argument("path", help=NOTEBOOK_PATH_HELP)
    update_code.add_argument("index", type=int, nargs="?", help="zero-based cell index")
    update_code.add_argument("--id", dest="cell_id", help="stable Jupyter cell id; preferred after edits")
    update_code.add_argument("--desc", default="", help="cell description")
    update_code_source = update_code.add_mutually_exclusive_group(required=True)
    update_code_source.add_argument(
        "--file",
        help=PYTHON_FILE_HELP,
    )
    update_code_source.add_argument("--code", help=PYTHON_CODE_HELP)
    update_code.add_argument("--timeout", type=_positive_float, default=120, help="seconds before interrupting the cell")
    _add_json_flag(update_code)

    run_clean = sub.add_parser(
        "run-clean",
        help="rerun code cells from a fresh kernel",
        description=(
            "Rerun all code cells top-to-bottom from a fresh kernel. "
            "This rewrites notebook cell outputs but does not manage external files."
        ),
    )
    run_clean.add_argument("path", help=NOTEBOOK_PATH_HELP)
    run_clean.add_argument("--timeout", type=_positive_float, default=120, help="seconds before interrupting each cell")
    _add_json_flag(run_clean)

    markdown = sub.add_parser("add-markdown", help="append a markdown cell")
    markdown.add_argument("path", help=NOTEBOOK_PATH_HELP)
    markdown.add_argument("--desc", default="", help="cell description")
    markdown.add_argument(
        "--file",
        required=True,
        help=MARKDOWN_FILE_HELP,
    )
    _add_json_flag(markdown)

    update_markdown = sub.add_parser("update-markdown", help="replace an existing markdown cell")
    update_markdown.add_argument("path", help=NOTEBOOK_PATH_HELP)
    update_markdown.add_argument("index", type=int, nargs="?", help="zero-based cell index")
    update_markdown.add_argument("--id", dest="cell_id", help="stable Jupyter cell id; preferred after edits")
    update_markdown.add_argument("--desc", default="", help="cell description")
    update_markdown.add_argument(
        "--file",
        required=True,
        help=MARKDOWN_FILE_HELP,
    )
    _add_json_flag(update_markdown)

    env_cmd = sub.add_parser(
        "env",
        help="print resolved Notebook Lens paths",
        description=(
            "Print resolved paths from NL_EXPERIMENT_DIR, NL_RUNTIME_DIR, "
            "and NL_KERNEL_PYTHON."
        ),
    )
    _add_json_flag(env_cmd)

    list_cmd = sub.add_parser("list", help="list notebooks")
    _add_json_flag(list_cmd)

    reset_kernel = sub.add_parser("reset-kernel", help="stop the live kernel for a notebook")
    reset_kernel.add_argument("path", help=NOTEBOOK_PATH_HELP)
    _add_json_flag(reset_kernel)

    return parser


def _normalize_legacy_command(argv: list[str]) -> list[str]:
    if argv and argv[0] in LEGACY_COMMAND_ALIASES:
        normalized = list(argv)
        normalized[0] = LEGACY_COMMAND_ALIASES[argv[0]]
        return normalized
    return argv


def _wants_json(argv: list[str]) -> bool:
    return "--json" in argv


def _command_name(argv: list[str]) -> str | None:
    for token in argv:
        if token == "--json" or token.startswith("-"):
            continue
        return LEGACY_COMMAND_ALIASES.get(token, token)
    return None


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a JSON object with exit_code, text, and structured data where available",
    )


def _code_source(args: argparse.Namespace) -> str:
    if args.code is not None:
        return args.code
    return read_source(args.file)


def main(argv: list[str] | None = None) -> int:
    """Run the Notebook Lens CLI."""

    parser = build_parser()
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    wants_json = _wants_json(raw_argv)
    fallback_command = _command_name(raw_argv)
    try:
        args = parser.parse_args(_normalize_legacy_command(raw_argv))
    except CliParseError as exc:
        result = CommandResult(
            exc.text,
            exit_code=2,
            data={"kind": "error", "error": exc.text.strip(), "error_type": "CliParseError"},
        )
        _write_result(result, json_output=wants_json, command=fallback_command)
        return result.exit_code
    config = LensConfig.from_env()

    try:
        if args.command == "new":
            result = cmd_new(args.path, config)
        elif args.command == "add-code":
            result = cmd_run_cell(
                args.path,
                config,
                source=_code_source(args),
                description=args.desc,
                timeout=args.timeout,
            )
        elif args.command == "add-markdown":
            result = cmd_add_markdown(
                args.path,
                config,
                source=read_source(args.file),
                description=args.desc,
            )
        elif args.command == "update-markdown":
            result = cmd_update_markdown(
                args.path,
                args.index,
                config,
                cell_id=args.cell_id,
                source=read_source(args.file),
                description=args.desc,
            )
        elif args.command == "update-code":
            result = cmd_update_cell(
                args.path,
                args.index,
                config,
                cell_id=args.cell_id,
                source=_code_source(args),
                description=args.desc,
                timeout=args.timeout,
            )
        elif args.command == "state":
            result = cmd_state(
                args.path,
                config,
                max_tokens=args.max_tokens,
                full_source=args.full_source,
                outputs=args.outputs,
            )
        elif args.command == "show-cell":
            result = cmd_show_cell(
                args.path,
                args.index,
                config,
                cell_id=args.cell_id,
                outputs=args.outputs,
                max_chars=args.max_chars,
            )
        elif args.command == "export-output":
            result = cmd_export_output(
                args.path,
                args.index,
                config,
                cell_id=args.cell_id,
                output_dir=args.output_dir,
                output_index=args.output_index,
            )
        elif args.command == "reset-kernel":
            result = cmd_reset(args.path, config)
        elif args.command == "list":
            result = cmd_list(config)
        elif args.command == "env":
            result = cmd_env(config)
        elif args.command == "run-clean":
            result = cmd_run_clean(args.path, config, timeout=args.timeout)
        else:
            parser.error(f"unknown command: {args.command}")
            return 2
    except Exception as exc:  # CLI boundary: convert all errors to readable text.
        result = user_error(exc)

    _write_result(result, json_output=getattr(args, "json", False), command=args.command)
    return result.exit_code


def _write_result(result: CommandResult, *, json_output: bool, command: str | None) -> None:
    stream = sys.stderr if result.exit_code >= 2 else sys.stdout
    if json_output:
        payload = {
            "command": command,
            "data": result.data or {},
            "exit_code": result.exit_code,
            "notebook_lens_version": __version__,
            "schema_version": JSON_SCHEMA_VERSION,
            "text": result.text,
        }
        stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        stream.write(result.text)


if __name__ == "__main__":
    raise SystemExit(main())
