"""Packaged schemas for Notebook Lens machine-readable outputs."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

CLI_JSON_ENVELOPE_SCHEMA_RESOURCE = "cli-json-envelope-v1.schema.json"

__all__ = ["CLI_JSON_ENVELOPE_SCHEMA_RESOURCE", "load_cli_json_envelope_schema"]


def load_cli_json_envelope_schema() -> dict[str, Any]:
    """Load the packaged JSON Schema for CLI ``--json`` envelopes."""

    text = (
        resources.files(__package__)
        .joinpath(CLI_JSON_ENVELOPE_SCHEMA_RESOURCE)
        .read_text(encoding="utf-8")
    )
    return json.loads(text)
