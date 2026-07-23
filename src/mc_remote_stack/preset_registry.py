"""Bundled profile, preset registry, and generated preset catalog loading."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any

import rfc8785
import tomlkit
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

EXACT_REF = re.compile(r"^(?P<name>[a-z0-9][a-z0-9-]{0,62})@(?P<revision>[1-9][0-9]*)$")
RECORD_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,126}$")
MAX_IDENTITY_INTEGER = 2**53 - 1
CATALOG_GENERATOR = "mcrctl-preset-catalog-v1"
CANONICALIZATION = "jcs-rfc8785-v1"


class PresetDataError(ValueError):
    """Stable, fail-closed diagnostic for bundled deployment data."""

    def __init__(self, reason: str, path: str, message: str) -> None:
        self.reason = reason
        self.path = path
        super().__init__(f"{reason}: {path}: {message}")


@dataclass(frozen=True)
class ImmutableRecord:
    ref: str
    content_sha256: str
    data: dict[str, Any]
    path: str


@dataclass(frozen=True)
class LifecycleDecision:
    status: str
    new_resolve_allowed: bool
    requires_eol_ack: bool
    warning: str | None


def _data_root(data_root: Traversable | None = None) -> Traversable:
    if data_root is not None:
        return data_root
    return files("mc_remote_stack").joinpath("data")


def _fail(reason: str, path: object, message: str) -> None:
    raise PresetDataError(reason, str(path), message)


def _parse_exact_ref(ref: str) -> tuple[str, str]:
    match = EXACT_REF.fullmatch(ref)
    if match is None:
        _fail(
            "mutable_selector",
            ref,
            "profile and preset selectors must use exact name@positive-revision form",
        )
    return match.group("name"), match.group("revision")


def _read_text(resource: Traversable, *, reason: str) -> str:
    try:
        return resource.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        _fail(reason, resource, str(exc))


def _read_toml(resource: Traversable, *, missing_reason: str) -> dict[str, Any]:
    if not resource.is_file():
        _fail(missing_reason, resource, "required bundled TOML record does not exist")
    source = _read_text(resource, reason="registry_record_read_failed")
    try:
        return tomllib.loads(source)
    except tomllib.TOMLDecodeError as exc:
        _fail("registry_record_parse_failed", resource, str(exc))


def _load_schema(root: Traversable, name: str) -> dict[str, Any]:
    resource = root.joinpath("schemas", name)
    if not resource.is_file():
        _fail("registry_schema_missing", resource, "bundled JSON Schema does not exist")
    source = _read_text(resource, reason="registry_schema_read_failed")
    try:
        schema = json.loads(source)
    except json.JSONDecodeError as exc:
        _fail("registry_schema_invalid", resource, str(exc))
    if not isinstance(schema, dict):
        _fail("registry_schema_invalid", resource, "schema root must be an object")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        _fail("registry_schema_invalid", resource, exc.message)
    return schema


def _validate_schema(instance: object, root: Traversable, schema_name: str, source: Traversable) -> None:
    schema = _load_schema(root, schema_name)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(instance),
        key=lambda error: (tuple(str(part) for part in error.absolute_path), error.message),
    )
    if errors:
        error = errors[0]
        logical_path = error.json_path
        _fail("registry_schema_invalid", source, f"{logical_path}: {error.message}")


def validate_bundled_schema(
    instance: object,
    schema_name: str,
    *,
    source: Traversable,
    data_root: Traversable | None = None,
) -> None:
    """Validate an instance against one schema from the selected bundled data root."""

    _validate_schema(instance, _data_root(data_root), schema_name, source)


def _validate_identity_value(value: object, logical_path: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                _fail("canonicalization_value_invalid", logical_path, "object keys must be strings")
            child_path = f"{logical_path}.{key}" if logical_path else key
            _validate_identity_value(child, child_path)
        return
    if isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _validate_identity_value(child, f"{logical_path}[{index}]")
        return
    if isinstance(value, str | bool) or value is None:
        return
    if isinstance(value, int):
        if abs(value) > MAX_IDENTITY_INTEGER:
            _fail(
                "canonicalization_value_invalid",
                logical_path,
                "integer exceeds the I-JSON interoperable range",
            )
        return
    if isinstance(value, float | date | datetime | time):
        _fail(
            "canonicalization_value_invalid",
            logical_path,
            "float and native date/time values are forbidden in identity-bearing data",
        )
    _fail("canonicalization_value_invalid", logical_path, f"unsupported value type: {type(value).__name__}")


def semantic_sha256(value: object) -> str:
    """Return the RFC 8785 SHA-256 for an identity-bearing semantic value."""

    _validate_identity_value(value, "")
    try:
        canonical = rfc8785.dumps(value)
    except rfc8785.CanonicalizationError as exc:
        _fail("canonicalization_failed", "<semantic-value>", str(exc))
    return hashlib.sha256(canonical).hexdigest()


def component_set_sha256(preset: dict[str, Any]) -> str:
    """Digest the exact component-to-artifact set independently of record layout."""

    components = sorted(preset["components"], key=lambda component: component["id"])
    artifacts = sorted(preset["artifacts"], key=lambda artifact: artifact["id"])
    return semantic_sha256(
        {
            "components": components,
            "artifacts": artifacts,
        }
    )


def _ensure_unique_ids(records: list[dict[str, Any]], logical_path: str, source: Traversable) -> set[str]:
    identifiers: set[str] = set()
    for record in records:
        identifier = record["id"]
        if identifier in identifiers:
            _fail("registry_schema_invalid", source, f"{logical_path} contains duplicate id {identifier}")
        identifiers.add(identifier)
    return identifiers


def _load_immutable_record(
    *,
    ref: str,
    data_root: Traversable | None,
    directory: str,
    filename: str,
    metadata_key: str,
    schema_name: str,
    unknown_reason: str,
) -> ImmutableRecord:
    root = _data_root(data_root)
    name, revision = _parse_exact_ref(ref)
    resource = root.joinpath(directory, name, revision, filename)
    data = _read_toml(resource, missing_reason=unknown_reason)
    _validate_schema(data, root, schema_name, resource)

    metadata = data[metadata_key]
    if metadata["name"] != name or metadata["revision"] != revision:
        _fail(
            "registry_record_tampered",
            resource,
            f"record identity {metadata['name']}@{metadata['revision']} does not match path {ref}",
        )

    if metadata_key == "profile":
        _ensure_unique_ids(data["services"], "services", resource)
        _ensure_unique_ids(data["volume_roles"], "volume_roles", resource)
    else:
        _ensure_unique_ids(data["components"], "components", resource)
        artifact_ids = _ensure_unique_ids(data["artifacts"], "artifacts", resource)
        for component in data["components"]:
            if component["artifact"] not in artifact_ids:
                _fail(
                    "component_artifact_unknown",
                    resource,
                    f"component {component['id']} references undeclared artifact {component['artifact']}",
                )

    return ImmutableRecord(
        ref=ref,
        content_sha256=semantic_sha256(data),
        data=data,
        path=str(resource),
    )


def load_profile(ref: str, *, data_root: Traversable | None = None) -> ImmutableRecord:
    """Load one exact bundled profile revision."""

    return _load_immutable_record(
        ref=ref,
        data_root=data_root,
        directory="profiles",
        filename="profile.toml",
        metadata_key="profile",
        schema_name="profile.schema.json",
        unknown_reason="unknown_profile_revision",
    )


def load_preset(ref: str, *, data_root: Traversable | None = None) -> ImmutableRecord:
    """Load one exact bundled preset revision from the preset registry."""

    return _load_immutable_record(
        ref=ref,
        data_root=data_root,
        directory="preset_registry",
        filename="preset.toml",
        metadata_key="preset",
        schema_name="preset.schema.json",
        unknown_reason="unknown_preset_revision",
    )


def load_compatibility_record(
    record_id: str,
    *,
    data_root: Traversable | None = None,
) -> ImmutableRecord:
    """Load one immutable PASS compatibility record and verify its exact subjects."""

    if RECORD_ID.fullmatch(record_id) is None:
        _fail("invalid_compatibility_record_id", record_id, "record ID must be a canonical lowercase token")
    root = _data_root(data_root)
    resource = root.joinpath("compatibility", "records", f"{record_id}.toml")
    data = _read_toml(resource, missing_reason="unknown_compatibility_record")
    _validate_schema(
        data,
        root,
        "compatibility-record.schema.json",
        resource,
    )
    if data["record"]["id"] != record_id:
        _fail(
            "registry_record_tampered",
            resource,
            f"record identity {data['record']['id']} does not match path {record_id}",
        )

    _ensure_unique_ids(data["claims"], "claims", resource)
    subject = data["subject"]
    preset = load_preset(subject["preset_ref"], data_root=root)
    profile = load_profile(subject["profile_ref"], data_root=root)
    if preset.content_sha256 != subject["preset_sha256"]:
        _fail(
            "compatibility_subject_mismatch",
            resource,
            f"preset digest does not match {preset.ref}",
        )
    if profile.content_sha256 != subject["profile_sha256"]:
        _fail(
            "compatibility_subject_mismatch",
            resource,
            f"profile digest does not match {profile.ref}",
        )
    if component_set_sha256(preset.data) != subject["component_set_sha256"]:
        _fail(
            "compatibility_subject_mismatch",
            resource,
            f"component set digest does not match {preset.ref}",
        )
    return ImmutableRecord(
        ref=record_id,
        content_sha256=semantic_sha256(data),
        data=data,
        path=str(resource),
    )


def load_compatibility_records(
    *,
    data_root: Traversable | None = None,
) -> list[ImmutableRecord]:
    """Load all bundled compatibility records in stable record-ID order."""

    root = _data_root(data_root)
    records_root = root.joinpath("compatibility", "records")
    if not records_root.is_dir():
        return []
    records: list[ImmutableRecord] = []
    for resource in sorted(records_root.iterdir(), key=lambda item: item.name):
        if not resource.is_file() or not resource.name.endswith(".toml"):
            continue
        record_id = resource.name.removesuffix(".toml")
        records.append(load_compatibility_record(record_id, data_root=root))
    return records


def load_catalog_policy(*, data_root: Traversable | None = None) -> dict[str, Any]:
    """Load human-owned preset lifecycle facts."""

    root = _data_root(data_root)
    resource = root.joinpath("preset_catalog_policy.toml")
    policy = _read_toml(resource, missing_reason="preset_catalog_policy_missing")
    _validate_schema(
        policy,
        root,
        "preset-catalog-policy.schema.json",
        resource,
    )
    seen: set[str] = set()
    for entry in policy["presets"]:
        ref = entry["ref"]
        _parse_exact_ref(ref)
        if ref in seen:
            _fail("preset_catalog_policy_invalid", resource, f"duplicate lifecycle entry for {ref}")
        seen.add(ref)
        replacement = entry.get("replacement")
        if replacement is not None:
            _parse_exact_ref(replacement)
            if replacement == ref:
                _fail("preset_catalog_policy_invalid", resource, f"{ref} cannot replace itself")
    return policy


def evaluate_lifecycle(policy: dict[str, Any], ref: str) -> LifecycleDecision:
    """Describe the new-resolve lifecycle gate without bypassing acknowledgements."""

    _parse_exact_ref(ref)
    entry = next((item for item in policy["presets"] if item["ref"] == ref), None)
    if entry is None:
        _fail("preset_not_in_catalog", ref, "preset is not currently offered for new resolution")
    status = entry["status"]
    warning = entry.get("reason") if status != "active" else None
    return LifecycleDecision(
        status=status,
        new_resolve_allowed=status != "eol",
        requires_eol_ack=status == "eol",
        warning=warning,
    )


def _ref_sort_key(ref: str) -> tuple[str, int]:
    name, revision = _parse_exact_ref(ref)
    return name, int(revision)


def _catalog_entry(
    policy_entry: dict[str, Any],
    preset: ImmutableRecord,
    compatibility_records: list[ImmutableRecord],
) -> dict[str, Any]:
    requirements = preset.data["requirements"]
    required_claims = set(requirements["required_claims"])
    covered_claims: set[str] = set()
    matching_record_ids: list[str] = []
    component_digest = component_set_sha256(preset.data)
    for record in compatibility_records:
        subject = record.data["subject"]
        if (
            subject["preset_ref"] != preset.ref
            or subject["preset_sha256"] != preset.content_sha256
            or subject["component_set_sha256"] != component_digest
        ):
            continue
        claims = {
            claim["id"]
            for claim in record.data["claims"]
            if claim["constraint"] == "all" and claim["id"] in required_claims
        }
        if not claims:
            continue
        covered_claims.update(claims)
        matching_record_ids.append(record.ref)
    entry: dict[str, Any] = {
        "ref": preset.ref,
        "content_sha256": preset.content_sha256,
        "description": preset.data["preset"]["description"],
        "status": policy_entry["status"],
        "available_since": policy_entry["available_since"],
        "required_profile_capabilities": sorted(requirements["profile_capabilities"]),
        "allowed_channels": sorted(requirements["allowed_channels"]),
        "compatibility_status": "verified" if required_claims.issubset(covered_claims) else "unverified",
        "compatibility_records": matching_record_ids,
    }
    for key in ("deprecated_since", "eol_since", "reason", "replacement"):
        if key in policy_entry:
            entry[key] = policy_entry[key]
    return entry


def _catalog_document(catalog: dict[str, Any]) -> bytes:
    document = tomlkit.document()
    document.add("schema_version", catalog["schema_version"])
    catalog_table = tomlkit.table()
    catalog_data = catalog["preset_catalog"]
    catalog_table.add("generator", catalog_data["generator"])
    catalog_table.add("canonicalization", catalog_data["canonicalization"])
    catalog_table.add("source_sha256", catalog_data["source_sha256"])

    entries = catalog_data["presets"]
    if entries:
        preset_tables = tomlkit.aot()
        for entry in entries:
            table = tomlkit.table()
            for key in (
                "ref",
                "content_sha256",
                "description",
                "status",
                "available_since",
                "deprecated_since",
                "eol_since",
                "reason",
                "replacement",
                "required_profile_capabilities",
                "allowed_channels",
                "compatibility_status",
                "compatibility_records",
            ):
                if key in entry:
                    table.add(key, entry[key])
            preset_tables.append(table)
        catalog_table.add("presets", preset_tables)
    else:
        catalog_table.add("presets", tomlkit.array())
    document.add("preset_catalog", catalog_table)
    return tomlkit.dumps(document).encode("utf-8")


def build_preset_catalog(*, data_root: Traversable | None = None) -> bytes:
    """Generate byte-stable discovery data from policy and exact registry records."""

    root = _data_root(data_root)
    policy = load_catalog_policy(data_root=root)
    policy_entries = sorted(policy["presets"], key=lambda entry: _ref_sort_key(entry["ref"]))
    records = [(entry, load_preset(entry["ref"], data_root=root)) for entry in policy_entries]
    compatibility_records = load_compatibility_records(data_root=root)
    replacement_refs = sorted(
        {entry["replacement"] for entry in policy_entries if "replacement" in entry},
        key=_ref_sort_key,
    )
    replacement_records = [load_preset(ref, data_root=root) for ref in replacement_refs]
    source_payload = {
        "policy": {
            "schema_version": policy["schema_version"],
            "presets": policy_entries,
        },
        "preset_registry": [
            {
                "ref": record.ref,
                "content_sha256": record.content_sha256,
            }
            for _, record in records
        ],
        "replacement_registry": [
            {
                "ref": record.ref,
                "content_sha256": record.content_sha256,
            }
            for record in replacement_records
        ],
        "compatibility_records": [
            {
                "id": record.ref,
                "content_sha256": record.content_sha256,
            }
            for record in compatibility_records
        ],
    }
    catalog = {
        "schema_version": 1,
        "preset_catalog": {
            "generator": CATALOG_GENERATOR,
            "canonicalization": CANONICALIZATION,
            "source_sha256": semantic_sha256(source_payload),
            "presets": [
                _catalog_entry(entry, record, compatibility_records)
                for entry, record in records
            ],
        },
    }
    resource = root.joinpath("preset_catalog.toml")
    _validate_schema(catalog, root, "preset-catalog.schema.json", resource)
    return _catalog_document(catalog)


def load_preset_catalog(
    *,
    data_root: Traversable | None = None,
    verify_generated: bool = True,
) -> dict[str, Any]:
    """Load the generated preset catalog after checking its source projection."""

    root = _data_root(data_root)
    if verify_generated:
        verify_preset_catalog(data_root=root)
    resource = root.joinpath("preset_catalog.toml")
    catalog = _read_toml(resource, missing_reason="preset_catalog_missing")
    _validate_schema(
        catalog,
        root,
        "preset-catalog.schema.json",
        resource,
    )
    return catalog


def verify_preset_catalog(*, data_root: Traversable | None = None) -> None:
    """Fail if the checked-in generated preset catalog is missing or stale."""

    root = _data_root(data_root)
    resource = root.joinpath("preset_catalog.toml")
    expected = build_preset_catalog(data_root=root)
    if not resource.is_file():
        _fail("stale_preset_catalog", resource, "generated preset catalog is missing")
    try:
        actual = resource.read_bytes()
    except OSError as exc:
        _fail("preset_catalog_read_failed", resource, str(exc))
    if actual != expected:
        _fail("stale_preset_catalog", resource, "generated bytes do not match registry and policy")


def _discover_records(
    root: Traversable,
    *,
    directory: str,
    filename: str,
    loader,
) -> dict[str, ImmutableRecord]:
    base = root.joinpath(directory)
    if not base.is_dir():
        return {}
    records: dict[str, ImmutableRecord] = {}
    for name_directory in sorted(base.iterdir(), key=lambda item: item.name):
        if not name_directory.is_dir():
            continue
        for revision_directory in sorted(name_directory.iterdir(), key=lambda item: item.name):
            if not revision_directory.is_dir() or not revision_directory.joinpath(filename).is_file():
                continue
            ref = f"{name_directory.name}@{revision_directory.name}"
            records[ref] = loader(ref, data_root=root)
    return records


def verify_append_only(*, current_root: Traversable, baseline_root: Traversable) -> None:
    """Reject edits or deletes of profile and preset revisions in a baseline tree."""

    surfaces = (
        ("profiles", "profile.toml", load_profile),
        ("preset_registry", "preset.toml", load_preset),
    )
    for directory, filename, loader in surfaces:
        baseline = _discover_records(
            baseline_root,
            directory=directory,
            filename=filename,
            loader=loader,
        )
        current = _discover_records(
            current_root,
            directory=directory,
            filename=filename,
            loader=loader,
        )
        for ref, baseline_record in baseline.items():
            current_record = current.get(ref)
            if current_record is None:
                _fail("immutable_revision_deleted", ref, f"published {directory} revision was deleted")
            if current_record.content_sha256 != baseline_record.content_sha256:
                _fail("immutable_revision_edited", ref, f"published {directory} revision was edited")
