"""Parsing and validation for the cross-repo release manifest (DEC 2026-09-06-04).

Each component repo (Scratch editor, McRemote, Python client) attaches one
`manifest.json` to its own GitHub Release, listing every artifact it publishes
with an exact, verifiable identity. This module only parses and validates that
document; fetching it from a release is a separate, thin step (see
`docs/release-preset-preparation-guide_ja.md`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


@dataclass(frozen=True)
class ReleaseManifestError(ValueError):
    reason: str
    path: str
    detail: str

    def __str__(self) -> str:
        return f"{self.reason}: {self.path}: {self.detail}"


def _fail(reason: str, path: object, detail: str) -> None:
    raise ReleaseManifestError(reason, str(path), detail)


def _load_schema(data_root: Traversable | None) -> dict[str, Any]:
    root = data_root or files("mc_remote_stack").joinpath("data")
    resource = root.joinpath("schemas", "release-manifest.schema.json")
    try:
        source = resource.read_bytes()
    except OSError as exc:
        _fail("release_manifest_schema_missing", resource, str(exc))
    try:
        schema = json.loads(source)
        Draft202012Validator.check_schema(schema)
    except (UnicodeDecodeError, json.JSONDecodeError, SchemaError) as exc:
        _fail("release_manifest_schema_invalid", resource, str(exc))
    return schema


def parse_release_manifest(
    source: bytes,
    *,
    path: object = "manifest.json",
    data_root: Traversable | None = None,
) -> dict[str, Any]:
    """Parse and schema-validate one release manifest, returning it as-is.

    Callers that already know the expected `release_tag` and per-artifact
    digests (from a review step) compare those values themselves; this
    function only enforces the shape, not any particular release identity.
    """

    try:
        document = json.loads(source)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("release_manifest_parse_failed", path, str(exc))
    schema = _load_schema(data_root)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda error: error.json_path,
    )
    if errors:
        first = errors[0]
        _fail("release_manifest_schema_invalid", f"{path}{first.json_path}", first.message)
    roles = [artifact["role"] for artifact in document["artifacts"]]
    if len(roles) != len(set(roles)):
        _fail("release_manifest_duplicate_role", path, "artifacts[].role must be unique")
    return document


def artifact_by_role(manifest: dict[str, Any], role: str) -> dict[str, Any]:
    matches = [artifact for artifact in manifest["artifacts"] if artifact["role"] == role]
    if len(matches) != 1:
        _fail(
            "release_manifest_role_missing",
            f"artifacts[role={role}]",
            f"expected exactly one artifact with role {role!r}, found {len(matches)}",
        )
    return matches[0]
