"""Verification for the immutable Scratch runtime-config handoff."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath
from typing import Any

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError


@dataclass(frozen=True)
class ScratchContractError(ValueError):
    reason: str
    path: str
    detail: str

    def __str__(self) -> str:
        return f"{self.reason}: {self.path}: {self.detail}"


def _fail(reason: str, path: object, detail: str) -> None:
    raise ScratchContractError(reason, str(path), detail)


def _git_tree_identity(root: Traversable) -> str:
    entries: list[tuple[str, bool, bytes]] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name.encode("utf-8")):
        if child.is_dir():
            identity = bytes.fromhex(_git_tree_identity(child))
            entries.append((child.name, True, identity))
        elif child.is_file():
            source = child.read_bytes()
            header = b"blob " + str(len(source)).encode("ascii") + b"\0"
            identity = hashlib.sha1(header + source).digest()  # noqa: S324 - Git identity
            entries.append((child.name, False, identity))
        else:
            _fail("scratch_contract_tree_invalid", child, "entry is not a regular file or directory")
    body = b"".join(
        (b"40000" if is_directory else b"100644")
        + b" "
        + name.encode("utf-8")
        + b"\0"
        + identity
        for name, is_directory, identity in entries
    )
    header = b"tree " + str(len(body)).encode("ascii") + b"\0"
    return hashlib.sha1(header + body).hexdigest()  # noqa: S324 - Git identity


def load_runtime_config_schema(
    contract: dict[str, Any], *, data_root: Traversable | None = None
) -> dict[str, Any]:
    """Verify the complete bundled handoff and return its schema."""

    root = data_root or files("mc_remote_stack").joinpath("data")
    contract_root = root.joinpath("scratch-contracts", contract["source_commit"])
    try:
        tree_identity = _git_tree_identity(contract_root)
    except (FileNotFoundError, OSError) as exc:
        _fail("scratch_contract_read_failed", contract_root, str(exc))
    if tree_identity != contract["directory_tree_sha"]:
        _fail(
            "scratch_contract_tree_mismatch",
            contract_root,
            "bundled directory differs from the locked Scratch Git tree",
        )

    schema_resource = contract_root.joinpath("schema.json")
    try:
        schema_source = schema_resource.read_bytes()
    except OSError as exc:
        _fail("scratch_contract_read_failed", schema_resource, str(exc))
    if hashlib.sha256(schema_source).hexdigest() != contract["schema_sha256"]:
        _fail("scratch_contract_digest_mismatch", schema_resource, "schema digest differs")
    try:
        schema = json.loads(schema_source)
        Draft7Validator.check_schema(schema)
    except (UnicodeDecodeError, json.JSONDecodeError, SchemaError) as exc:
        _fail("scratch_contract_schema_invalid", schema_resource, str(exc))
    if not isinstance(schema, dict):
        _fail("scratch_contract_schema_invalid", schema_resource, "schema root must be an object")

    accepted = set(contract["accepted_fixtures"])
    rejected = set(contract["rejected_fixtures"])
    fixture_sha256 = contract["fixture_sha256"]
    if accepted & rejected or accepted | rejected != set(fixture_sha256):
        _fail(
            "scratch_contract_fixture_set_mismatch",
            contract_root.joinpath("fixtures"),
            "accept/reject sets must exactly equal the locked fixture digest set",
        )
    validator = Draft7Validator(schema)
    for relative, expected_sha256 in sorted(fixture_sha256.items()):
        resource = contract_root.joinpath(*PurePosixPath(relative).parts)
        try:
            source = resource.read_bytes()
            document = json.loads(source)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            _fail("scratch_contract_fixture_invalid", resource, str(exc))
        if hashlib.sha256(source).hexdigest() != expected_sha256:
            _fail("scratch_contract_fixture_digest_mismatch", resource, "fixture digest differs")
        errors = list(validator.iter_errors(document))
        if relative in accepted and errors:
            _fail("scratch_contract_fixture_result_mismatch", resource, "accepted fixture was rejected")
        if relative in rejected and not errors:
            _fail("scratch_contract_fixture_result_mismatch", resource, "rejected fixture was accepted")
    return schema


def validate_runtime_config(document: object, schema: dict[str, Any]) -> None:
    errors = sorted(Draft7Validator(schema).iter_errors(document), key=lambda error: error.json_path)
    if errors:
        first = errors[0]
        _fail("scratch_runtime_schema_invalid", first.json_path, first.message)
