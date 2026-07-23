"""Deterministic order resolution and one-environment lock lifecycle."""

from __future__ import annotations

import copy
import os
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import tomlkit

from . import __version__
from .preset_registry import (
    CANONICALIZATION,
    PresetDataError,
    component_set_sha256,
    evaluate_lifecycle,
    load_catalog_policy,
    load_compatibility_records,
    load_preset,
    load_preset_catalog,
    load_profile,
    semantic_sha256,
    validate_bundled_schema,
)
from .toml_project import ProjectOrderError, load_order

LOCK_NAME = "mc-remote.lock.toml"
SOURCE_PRECEDENCE = ["profile", "preset", "order", "override"]


class ResolutionError(ValueError):
    """Stable, fail-closed diagnostic for resolve and lock operations."""

    def __init__(self, reason: str, path: str, message: str) -> None:
        self.reason = reason
        self.path = path
        super().__init__(f"{reason}: {path}: {message}")


@dataclass(frozen=True)
class ResolveResult:
    status: str
    lock_identity: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class LockInspection:
    status: str
    current_lock_identity: str | None
    candidate_lock_identity: str | None


@dataclass(frozen=True)
class _Candidate:
    lock: dict[str, Any]
    warnings: tuple[str, ...]


def _fail(reason: str, path: object, message: str) -> None:
    raise ResolutionError(reason, str(path), message)


def _translate_source_error(exc: PresetDataError | ProjectOrderError) -> None:
    _fail(exc.reason, exc.path, str(exc))


def _canonical_resolved_at(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if not value.endswith("Z"):
        _fail("resolved_at_invalid", "resolved_at", "timestamp must be an explicit UTC value ending in Z")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        _fail("resolved_at_invalid", "resolved_at", str(exc))
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        _fail("resolved_at_invalid", "resolved_at", "timestamp must use UTC")
    return value


def _validate_profile_preset_environment(
    order: dict[str, Any],
    profile: dict[str, Any],
    preset: dict[str, Any],
) -> None:
    provided_capabilities = set(profile["capabilities"]["provided"])
    required_capabilities = set(preset["requirements"]["profile_capabilities"])
    missing_capabilities = sorted(required_capabilities - provided_capabilities)
    if missing_capabilities:
        _fail(
            "profile_incompatible",
            "requirements.profile_capabilities",
            f"profile lacks capabilities: {', '.join(missing_capabilities)}",
        )

    component_roles = {component["role"] for component in preset["components"]}
    required_roles = set(profile["capabilities"]["required_component_roles"])
    missing_roles = sorted(required_roles - component_roles)
    if missing_roles:
        _fail(
            "profile_incompatible",
            "capabilities.required_component_roles",
            f"preset lacks component roles: {', '.join(missing_roles)}",
        )

    environment = order["environment"]
    constraints = profile["environment"]
    checks = (
        ("channel", constraints["allowed_channels"]),
        ("exposure", constraints["allowed_exposures"]),
        ("purpose", constraints["allowed_purposes"]),
    )
    for key, allowed in checks:
        if environment[key] not in allowed:
            _fail(
                "unsupported_environment_combination",
                f"environment.{key}",
                f"{environment[key]} is not allowed by the selected profile",
            )
    if environment["channel"] not in preset["requirements"]["allowed_channels"]:
        _fail(
            "unsupported_environment_combination",
            "environment.channel",
            f"{environment['channel']} is not allowed by the selected preset",
        )

    assigned_volume_roles = {assignment["role"] for assignment in order["runtime"]["volumes"]}
    declared_volume_roles = {volume_role["id"] for volume_role in profile["volume_roles"]}
    if assigned_volume_roles != declared_volume_roles:
        missing = sorted(declared_volume_roles - assigned_volume_roles)
        extra = sorted(assigned_volume_roles - declared_volume_roles)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if extra:
            details.append(f"extra: {', '.join(extra)}")
        _fail(
            "profile_incompatible",
            "runtime.volumes",
            f"volume assignments must exactly match profile roles ({'; '.join(details)})",
        )

    for instance_field in profile["policy"]["instance_fields"]:
        current: object = order
        for segment in instance_field.split("."):
            if not isinstance(current, dict) or segment not in current:
                _fail(
                    "profile_incompatible",
                    "policy.instance_fields",
                    f"order does not provide required instance field: {instance_field}",
                )
            current = current[segment]

    if not order["agreements"]["minecraft_eula"]:
        _fail(
            "minecraft_eula_not_accepted",
            "agreements.minecraft_eula",
            "accept the Minecraft EULA explicitly before resolving",
        )


def _compatibility_projection(
    *,
    data_root: Traversable,
    profile_ref: str,
    profile_sha256: str,
    preset_ref: str,
    preset_sha256: str,
    preset: dict[str, Any],
) -> dict[str, Any]:
    required_claims = sorted(set(preset["requirements"]["required_claims"]))
    component_digest = component_set_sha256(preset)
    covered_claims: set[str] = set()
    projected_records: list[dict[str, Any]] = []
    for record in load_compatibility_records(data_root=data_root):
        subject = record.data["subject"]
        if (
            subject["profile_ref"] != profile_ref
            or subject["profile_sha256"] != profile_sha256
            or subject["preset_ref"] != preset_ref
            or subject["preset_sha256"] != preset_sha256
            or subject["component_set_sha256"] != component_digest
        ):
            continue
        claims = sorted(
            {
                claim["id"]
                for claim in record.data["claims"]
                if claim["constraint"] == "all" and claim["id"] in required_claims
            }
        )
        if not claims:
            continue
        covered_claims.update(claims)
        projected_records.append(
            {
                "id": record.ref,
                "content_sha256": record.content_sha256,
                "claims": claims,
                "evidence": copy.deepcopy(record.data["evidence"]),
            }
        )
    return {
        "status": "verified" if set(required_claims).issubset(covered_claims) else "unverified",
        "required_claims_sha256": semantic_sha256(required_claims),
        "component_set_sha256": component_digest,
        "records": projected_records,
    }


def _identity_payload(lock: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in lock.items()
        if key not in {"lock_identity", "resolved_at"}
    }


def _calculate_lock_identity(lock: dict[str, Any]) -> str:
    return f"sha256:{semantic_sha256(_identity_payload(lock))}"


def _build_candidate(
    project_root: Path,
    *,
    data_root: Traversable,
    resolved_at: str,
    allow_unverified: bool,
    allow_eol: bool,
    enforce_one_shot_acknowledgements: bool,
) -> _Candidate:
    order_record = load_order(project_root)
    order = order_record.order
    profile_ref = order["deployment"]["profile"]
    preset_ref = order["environment"]["preset"]
    profile_record = load_profile(profile_ref, data_root=data_root)
    preset_record = load_preset(preset_ref, data_root=data_root)

    load_preset_catalog(data_root=data_root)
    policy = load_catalog_policy(data_root=data_root)
    lifecycle = evaluate_lifecycle(policy, preset_ref)
    _validate_profile_preset_environment(order, profile_record.data, preset_record.data)
    compatibility = _compatibility_projection(
        data_root=data_root,
        profile_ref=profile_ref,
        profile_sha256=profile_record.content_sha256,
        preset_ref=preset_ref,
        preset_sha256=preset_record.content_sha256,
        preset=preset_record.data,
    )

    acknowledgements = order["acknowledgements"]
    if enforce_one_shot_acknowledgements and lifecycle.requires_eol_ack:
        if not (acknowledgements["allow_eol"] and allow_eol):
            _fail(
                "preset_eol",
                "environment.preset",
                "EOL resolution requires an order reason and the one-shot --allow-eol acknowledgement",
            )
    if enforce_one_shot_acknowledgements and compatibility["status"] == "unverified":
        if not (acknowledgements["allow_unverified"] and allow_unverified):
            _fail(
                "unverified_not_acknowledged",
                "environment.preset",
                "unverified resolution requires an order reason and the one-shot --allow-unverified acknowledgement",
            )

    environment = order["environment"]
    profile = profile_record.data
    preset = preset_record.data
    render_payload = {
        "adapter": profile["renderer"]["name"],
        "adapter_revision": profile["renderer"]["revision"],
        "deployment": order["deployment"],
        "environment": environment,
        "runtime": order["runtime"],
        "world": order["world"],
        "network": order["network"],
        "agreements": order["agreements"],
        "services": profile["services"],
        "volume_roles": profile["volume_roles"],
        "required_security_controls": sorted(profile["policy"]["required_security_controls"]),
        "components": preset["components"],
        "artifacts": preset["artifacts"],
    }
    render_plan = {
        "adapter": profile["renderer"]["name"],
        "adapter_revision": profile["renderer"]["revision"],
        "semantic_sha256": semantic_sha256(render_payload),
        "services": copy.deepcopy(profile["services"]),
        "volume_roles": copy.deepcopy(profile["volume_roles"]),
        "required_security_controls": sorted(profile["policy"]["required_security_controls"]),
    }
    lifecycle_projection: dict[str, Any] = {"status": lifecycle.status}
    if lifecycle.warning:
        lifecycle_projection["warning"] = lifecycle.warning

    identity_payload: dict[str, Any] = {
        "schema_version": 1,
        "resolver": {
            "name": "mcrctl",
            "version": __version__,
            "lock_schema": 1,
            "canonicalization": CANONICALIZATION,
        },
        "input": {
            "order": {
                "semantic_sha256": semantic_sha256(order),
            },
            "profile": {
                "ref": profile_ref,
                "content_sha256": profile_record.content_sha256,
            },
            "preset": {
                "ref": preset_ref,
                "content_sha256": preset_record.content_sha256,
            },
        },
        "deployment": copy.deepcopy(order["deployment"]),
        "environment": {
            "identity": environment["identity"],
            "channel": environment["channel"],
            "exposure": environment["exposure"],
            "purpose": environment["purpose"],
        },
        "runtime": copy.deepcopy(order["runtime"]),
        "world": copy.deepcopy(order["world"]),
        "network": copy.deepcopy(order["network"]),
        "agreements": copy.deepcopy(order["agreements"]),
        "source_precedence": SOURCE_PRECEDENCE,
        "selection": {
            "kind": "preset",
        },
        "preset_lifecycle": lifecycle_projection,
        "compatibility": compatibility,
        "acknowledgements": copy.deepcopy(acknowledgements),
        "components": copy.deepcopy(preset["components"]),
        "artifacts": copy.deepcopy(preset["artifacts"]),
        "render_plan": render_plan,
        "secret_references": [],
        "scope": {
            "secret_values": "excluded",
            "secret_injected_bytes": "excluded",
            "runtime_owned_state": "excluded",
        },
    }
    lock = {
        "schema_version": 1,
        "lock_identity": f"sha256:{semantic_sha256(identity_payload)}",
        "resolved_at": resolved_at,
        **{key: value for key, value in identity_payload.items() if key != "schema_version"},
    }
    validate_bundled_schema(
        lock,
        "lock.schema.json",
        source=project_root / LOCK_NAME,
        data_root=data_root,
    )

    warnings: list[str] = []
    if lifecycle.warning:
        warnings.append(f"preset {lifecycle.status}: {lifecycle.warning}")
    if compatibility["status"] == "unverified":
        warnings.append("compatibility evidence does not cover all required claims")
    return _Candidate(lock=lock, warnings=tuple(warnings))


def _serialize_lock(lock: dict[str, Any]) -> bytes:
    return tomlkit.dumps(lock).encode("utf-8")


def _atomic_write_lock(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        mode = path.stat().st_mode & 0o7777 if path.exists() else 0o644
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _load_lock(project_root: Path, *, data_root: Traversable) -> dict[str, Any]:
    path = project_root.resolve() / LOCK_NAME
    if not path.is_file():
        _fail("lock_missing", path, "resolve the project before plan or render")
    try:
        source_bytes = path.read_bytes()
    except OSError as exc:
        _fail("lock_read_failed", path, str(exc))
    if source_bytes.startswith(b"\xef\xbb\xbf"):
        _fail("lock_parse_failed", path, "UTF-8 BOM is forbidden")
    try:
        lock = tomllib.loads(source_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        _fail("lock_parse_failed", path, str(exc))
    try:
        validate_bundled_schema(
            lock,
            "lock.schema.json",
            source=path,
            data_root=data_root,
        )
    except PresetDataError as exc:
        _translate_source_error(exc)
    calculated = _calculate_lock_identity(lock)
    if lock["lock_identity"] != calculated:
        _fail(
            "lock_identity_mismatch",
            path,
            f"recorded {lock['lock_identity']} does not match calculated {calculated}",
        )
    return lock


def load_lock(
    project_root: Path,
    *,
    data_root: Traversable,
) -> dict[str, Any]:
    """Load and self-verify one machine-owned environment lock."""

    return _load_lock(project_root, data_root=data_root)


def resolve_project(
    project_root: Path,
    *,
    data_root: Traversable,
    allow_unverified: bool = False,
    allow_eol: bool = False,
    resolved_at: str | None = None,
) -> ResolveResult:
    """Resolve exact bundled inputs and atomically create or replace the lock."""

    project_root = project_root.resolve()
    timestamp = _canonical_resolved_at(resolved_at)
    try:
        candidate = _build_candidate(
            project_root,
            data_root=data_root,
            resolved_at=timestamp,
            allow_unverified=allow_unverified,
            allow_eol=allow_eol,
            enforce_one_shot_acknowledgements=True,
        )
    except (PresetDataError, ProjectOrderError) as exc:
        _translate_source_error(exc)

    lock_path = project_root / LOCK_NAME
    if lock_path.exists():
        existing = _load_lock(project_root, data_root=data_root)
        if existing["lock_identity"] == candidate.lock["lock_identity"]:
            return ResolveResult(
                status="unchanged",
                lock_identity=existing["lock_identity"],
                warnings=candidate.warnings,
            )
        status = "replaced"
    else:
        status = "created"
    _atomic_write_lock(lock_path, _serialize_lock(candidate.lock))
    return ResolveResult(
        status=status,
        lock_identity=candidate.lock["lock_identity"],
        warnings=candidate.warnings,
    )


def inspect_lock(
    project_root: Path,
    *,
    data_root: Traversable,
) -> LockInspection:
    """Report missing, unchanged, or stale without writing or requiring one-shot acknowledgements."""

    project_root = project_root.resolve()
    lock_path = project_root / LOCK_NAME
    if not lock_path.exists():
        return LockInspection(
            status="missing",
            current_lock_identity=None,
            candidate_lock_identity=None,
        )
    existing = _load_lock(project_root, data_root=data_root)
    try:
        candidate = _build_candidate(
            project_root,
            data_root=data_root,
            resolved_at=existing["resolved_at"],
            allow_unverified=False,
            allow_eol=False,
            enforce_one_shot_acknowledgements=False,
        )
    except (PresetDataError, ProjectOrderError) as exc:
        _translate_source_error(exc)
    status = "unchanged" if existing["lock_identity"] == candidate.lock["lock_identity"] else "stale"
    return LockInspection(
        status=status,
        current_lock_identity=existing["lock_identity"],
        candidate_lock_identity=candidate.lock["lock_identity"],
    )
