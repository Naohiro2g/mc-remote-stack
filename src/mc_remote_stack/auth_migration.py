"""Resumable deployed-state migration to enforced McRemote authentication."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Protocol

import yaml
from yaml.nodes import MappingNode, ScalarNode

from .apply import (
    DOCKER_CONTEXT,
    ApplyContractError,
    CommandRunner,
    _default_runner,
    _expected_volume_labels,
    _inspect_current_container,
    _inspect_managed_volume,
    _nonempty_lines,
    _project_container_ids,
    _run,
    _service_ids,
    _single_inspect_record,
    _volume_exists,
    _volume_identities,
)
from .doctor import DoctorContractError, doctor_toml_project, probe_protocol_hello
from .render import RenderContractError, render_toml_project, verify_toml_render_output
from .resolver import ResolutionError, load_lock, resolve_project
from .toml_project import (
    LOCK_NAME,
    ORDER_NAME,
    ProjectOrderError,
    update_order_scalar,
    update_order_volume_identity,
)

MIGRATION_NAME = "auth-enforcement"
MIGRATION_SCHEMA = "mcrctl.auth-enforcement-migration"
MIGRATION_SCHEMA_VERSION = 1
MIGRATION_RELATIVE = Path(".mcrctl") / "migrations" / MIGRATION_NAME
STATE_NAME = "state.json"
PHASES = (
    "prepared",
    "target-volumes-created",
    "source-runtime-stopped",
    "desired-published",
    "auth-config-installed",
    "volumes-copied",
    "target-auth-config-installed",
    "complete",
)
PROFILE_TRANSITIONS = {
    "home-server@2": "home-server@4",
    "vps-server@4": "vps-server@5",
}


@dataclass(frozen=True)
class MigrationSpec:
    name: str
    schema: str
    relative: Path
    profile_transitions: dict[str, str]
    preset_transitions: dict[str, str]
    candidate_policy: str


AUTH_ENFORCEMENT_MIGRATION = MigrationSpec(
    name=MIGRATION_NAME,
    schema=MIGRATION_SCHEMA,
    relative=MIGRATION_RELATIVE,
    profile_transitions=PROFILE_TRANSITIONS,
    preset_transitions={},
    candidate_policy="auth-only",
)
PUBLIC_B3_MIGRATION = MigrationSpec(
    name="public-b3",
    schema="mcrctl.public-b3-migration",
    relative=Path(".mcrctl") / "migrations" / "public-b3",
    profile_transitions={"vps-server@5": "vps-server@6"},
    preset_transitions={"public-web-paper@1": "public-web-paper@2"},
    candidate_policy="public-b3",
)
_ACTIVE_MIGRATION: ContextVar[MigrationSpec] = ContextVar(
    "mc_remote_stack_active_migration",
    default=AUTH_ENFORCEMENT_MIGRATION,
)


@contextmanager
def _activate_migration(spec: MigrationSpec):
    token = _ACTIVE_MIGRATION.set(spec)
    try:
        yield
    finally:
        _ACTIVE_MIGRATION.reset(token)
SHA256_IDENTITY = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_PRESERVED_COMPOSE_FILES = 4
MAX_PRESERVED_COMPOSE_BYTES = 64 * 1024


class AuthMigrationContractError(ValueError):
    """Stable fail-closed diagnostic for the auth-enforcement migration."""

    def __init__(self, reason: str, path: object, message: str) -> None:
        self.reason = reason
        self.path = str(path)
        super().__init__(f"{reason}: {path}: {message}")


@dataclass(frozen=True)
class AuthMigrationPlan:
    project_root: Path
    output: Path
    docker_context: str
    source_lock_identity: str
    target_lock_identity: str
    source_profile: str
    target_profile: str
    deployment: str
    environment: str
    services: tuple[str, ...]
    volume_migrations: tuple[tuple[str, str, str], ...]
    preserved_compose_files: tuple[Path, ...]
    preserved_compose_sha256: tuple[str, ...]
    preserved_composition_identity: str | None
    auth_config_root: Path | None


@dataclass(frozen=True)
class AuthMigrationResult:
    status: str
    source_lock_identity: str
    target_lock_identity: str
    phase: str


class AuthMigrationHost(Protocol):
    def inspect_source(self, plan: AuthMigrationPlan) -> None: ...

    def inspect_targets_absent(self, plan: AuthMigrationPlan) -> None: ...

    def pull_target(self, plan: AuthMigrationPlan) -> None: ...

    def create_target_volumes(self, plan: AuthMigrationPlan) -> None: ...

    def stop_source(self, plan: AuthMigrationPlan, source_output: Path) -> None: ...

    def copy_volumes(self, plan: AuthMigrationPlan) -> None: ...

    def install_target_auth_config(self, plan: AuthMigrationPlan) -> None: ...

    def start_target(self, plan: AuthMigrationPlan) -> None: ...

    def verify_target(self, plan: AuthMigrationPlan) -> None: ...


def _fail(reason: str, path: object, message: str) -> None:
    raise AuthMigrationContractError(reason, path, message)


def _migration_root(project_root: Path) -> Path:
    return project_root.resolve() / _ACTIVE_MIGRATION.get().relative


def _state_path(project_root: Path) -> Path:
    return _migration_root(project_root) / STATE_NAME


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    files = [path for path in root.rglob("*") if path.is_file() and not path.is_symlink()]
    for path in files:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    directories = [path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()]
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(path)
    _fsync_directory(root)


def _atomic_write(path: Path, content: bytes, *, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        if path.exists():
            mode = path.stat().st_mode & 0o7777
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_state(project_root: Path, state: dict[str, Any]) -> None:
    source = (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    _atomic_write(_state_path(project_root), source)


def load_auth_migration_state(project_root: Path) -> dict[str, Any]:
    """Load and structurally validate one durable migration transaction."""

    path = _state_path(project_root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _fail("migration_transaction_missing", path, "no auth-enforcement transaction exists")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("migration_transaction_invalid", path, str(exc))
    if not isinstance(value, dict):
        _fail("migration_transaction_invalid", path, "transaction state must be one JSON object")
    required = {
        "schema",
        "schema_version",
        "migration",
        "phase",
        "project_root",
        "output",
        "docker_context",
        "source_lock_identity",
        "target_lock_identity",
        "source_profile",
        "target_profile",
        "deployment",
        "environment",
        "services",
        "volume_migrations",
        "preserved_compose_files",
        "preserved_composition_identity",
        "auth_config_root",
        "last_error",
    }
    if set(value) != required:
        _fail("migration_transaction_invalid", path, "transaction fields do not match schema version 1")
    if (
        value["schema"] != _ACTIVE_MIGRATION.get().schema
        or value["schema_version"] != MIGRATION_SCHEMA_VERSION
        or value["migration"] != _ACTIVE_MIGRATION.get().name
        or value["phase"] not in PHASES
        or not isinstance(value["project_root"], str)
        or not isinstance(value["output"], str)
        or not isinstance(value["docker_context"], str)
        or not isinstance(value["source_lock_identity"], str)
        or not isinstance(value["target_lock_identity"], str)
        or not SHA256_IDENTITY.fullmatch(value["source_lock_identity"])
        or not SHA256_IDENTITY.fullmatch(value["target_lock_identity"])
        or not isinstance(value["source_profile"], str)
        or not isinstance(value["target_profile"], str)
        or not isinstance(value["deployment"], str)
        or not isinstance(value["environment"], str)
        or not isinstance(value["services"], list)
        or not value["services"]
        or any(not isinstance(service, str) or not service for service in value["services"])
        or not isinstance(value["volume_migrations"], list)
        or not value["volume_migrations"]
        or not isinstance(value["preserved_compose_files"], list)
        or value["preserved_composition_identity"] is not None
        and (
            not isinstance(value["preserved_composition_identity"], str)
            or not SHA256_IDENTITY.fullmatch(value["preserved_composition_identity"])
        )
        or value["auth_config_root"] is not None
        and not isinstance(value["auth_config_root"], str)
        or value["last_error"] is not None
        and not (
            isinstance(value["last_error"], dict)
            and set(value["last_error"]) == {"reason", "path"}
            and all(isinstance(item, str) for item in value["last_error"].values())
        )
    ):
        _fail("migration_transaction_invalid", path, "transaction values do not match schema version 1")
    for item in value["volume_migrations"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"role", "source", "target"}
            or not all(isinstance(item[key], str) and item[key] for key in item)
        ):
            _fail("migration_transaction_invalid", path, "volume migration entry is invalid")
    for item in value["preserved_compose_files"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256", "snapshot"}
            or not isinstance(item["path"], str)
            or not isinstance(item["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            or not isinstance(item["snapshot"], str)
            or not re.fullmatch(r"[0-9]{2}-[A-Za-z0-9_.-]+\.ya?ml", item["snapshot"])
        ):
            _fail("migration_transaction_invalid", path, "preserved Compose entry is invalid")
    if not (
        bool(value["preserved_compose_files"])
        == (value["auth_config_root"] is not None)
        == (value["preserved_composition_identity"] is not None)
    ):
        _fail(
            "migration_transaction_invalid",
            path,
            "preserved Compose state and auth config root must be present together",
        )
    return value


def _copy_project_source(source: Path, destination: Path, output: Path) -> None:
    ignored_names = {".mcrctl"}
    if output.parent.resolve() == source.resolve():
        ignored_names.add(output.name)

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return set(names) & ignored_names

    shutil.copytree(source, destination, symlinks=False, ignore=ignore)


def _build_candidate(
    project_root: Path,
    output: Path,
    destination: Path,
    *,
    target_profile: str,
    target_preset: str | None = None,
    target_volumes: dict[str, str],
    data_root: Traversable,
    allow_unverified: bool,
    allow_eol: bool,
    resolved_at: str,
) -> tuple[dict[str, Any], Path]:
    _copy_project_source(project_root, destination, output)
    update_order_scalar(destination, ("deployment", "profile"), target_profile)
    if target_preset is not None:
        update_order_scalar(destination, ("deployment", "preset"), target_preset)
    for role, identity in sorted(target_volumes.items()):
        update_order_volume_identity(destination, role, identity)
    resolve_project(
        destination,
        data_root=data_root,
        allow_unverified=allow_unverified,
        allow_eol=allow_eol,
        resolved_at=resolved_at,
    )
    candidate_output = destination / "generated"
    render_toml_project(destination, candidate_output, data_root=data_root)
    verification = verify_toml_render_output(destination, candidate_output, data_root=data_root)
    return verification.lock, candidate_output


def _validate_transition(
    source_lock: dict[str, Any],
    target_volumes: dict[str, str],
) -> tuple[str, tuple[tuple[str, str, str], ...]]:
    source_profile = source_lock["input"]["profile"]["ref"]
    target_profile = _ACTIVE_MIGRATION.get().profile_transitions.get(source_profile)
    if target_profile is None:
        _fail(
            "migration_transition_unsupported",
            "input.profile.ref",
            f"{_ACTIVE_MIGRATION.get().name} migration does not support source profile {source_profile}",
        )
    source_volumes = {
        volume["role"]: volume["identity"]
        for volume in source_lock["runtime"]["volumes"]
    }
    if set(target_volumes) != set(source_volumes):
        _fail(
            "migration_target_volumes_invalid",
            "migration.target_volumes",
            "one explicit target identity is required for every source volume role",
        )
    if len(set(target_volumes.values())) != len(target_volumes):
        _fail(
            "migration_target_volumes_invalid",
            "migration.target_volumes",
            "target volume identities must be unique",
        )
    unchanged = sorted(
        role for role, identity in target_volumes.items() if source_volumes[role] == identity
    )
    if unchanged:
        _fail(
            "migration_target_volumes_invalid",
            "migration.target_volumes",
            f"target volume must differ from source for roles: {', '.join(unchanged)}",
        )
    migrations = tuple(
        (role, source_volumes[role], target_volumes[role])
        for role in sorted(source_volumes)
    )
    return target_profile, migrations


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _preserved_compose_service_scope(path: Path) -> frozenset[str]:
    try:
        root = yaml.compose(path.read_text(encoding="utf-8"), Loader=yaml.SafeLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        _fail(
            "migration_preserved_composition_invalid",
            path,
            f"cannot inspect preserved Compose service scope: {exc}",
        )
    if not isinstance(root, MappingNode):
        _fail(
            "migration_preserved_composition_invalid",
            path,
            "preserved Compose file must be one mapping",
        )
    top_level: dict[str, yaml.Node] = {}
    for key_node, value_node in root.value:
        if not isinstance(key_node, ScalarNode) or key_node.value in top_level:
            _fail(
                "migration_preserved_composition_invalid",
                path,
                "preserved Compose top-level keys must be unique scalar values",
            )
        top_level[key_node.value] = value_node
    if set(top_level) != {"services"} or not isinstance(
        top_level["services"], MappingNode
    ):
        _fail(
            "migration_preserved_composition_invalid",
            path,
            "service-specific preservation requires exactly one top-level services mapping",
        )
    services = []
    for service_node, _definition_node in top_level["services"].value:
        if (
            not isinstance(service_node, ScalarNode)
            or not service_node.value
            or service_node.value in services
        ):
            _fail(
                "migration_preserved_composition_invalid",
                path,
                "preserved Compose service names must be unique non-empty scalar values",
            )
        services.append(service_node.value)
    if not services:
        _fail(
            "migration_preserved_composition_invalid",
            path,
            "preserved Compose file must affect at least one service",
        )
    return frozenset(services)


def _validate_preserved_composition(
    project_root: Path,
    preserved_compose_files: tuple[Path, ...],
    auth_config_root: Path | None,
) -> tuple[tuple[Path, ...], tuple[str, ...], str | None, Path | None]:
    if bool(preserved_compose_files) != (auth_config_root is not None):
        _fail(
            "migration_preserved_composition_invalid",
            "migration.preserved_composition",
            "preserved Compose files and an auth config root must be supplied together",
        )
    if not preserved_compose_files:
        return (), (), None, None
    if len(preserved_compose_files) > MAX_PRESERVED_COMPOSE_FILES:
        _fail(
            "migration_preserved_composition_invalid",
            "migration.preserved_compose_files",
            f"at most {MAX_PRESERVED_COMPOSE_FILES} Compose files may be preserved",
        )
    if any(path.is_symlink() for path in preserved_compose_files):
        _fail(
            "migration_preserved_composition_invalid",
            "migration.preserved_compose_files",
            "preserved Compose files must not be symlinks",
        )
    resolved_files = tuple(path.resolve() for path in preserved_compose_files)
    if len(set(resolved_files)) != len(resolved_files):
        _fail(
            "migration_preserved_composition_invalid",
            "migration.preserved_compose_files",
            "preserved Compose files must be unique",
        )
    digests = []
    for path in resolved_files:
        if not path.is_relative_to(project_root) or not path.is_file():
            _fail(
                "migration_preserved_composition_invalid",
                path,
                "preserved Compose files must be regular files inside the deployment project",
            )
        if path.stat().st_size > MAX_PRESERVED_COMPOSE_BYTES:
            _fail(
                "migration_preserved_composition_invalid",
                path,
                f"preserved Compose file exceeds {MAX_PRESERVED_COMPOSE_BYTES} bytes",
            )
        _preserved_compose_service_scope(path)
        digests.append(_sha256_file(path))
    assert auth_config_root is not None
    resolved_root = auth_config_root.resolve()
    if (
        auth_config_root.is_symlink()
        or not resolved_root.is_absolute()
        or not resolved_root.is_dir()
        or resolved_root.is_relative_to(project_root)
    ):
        _fail(
            "migration_auth_config_root_invalid",
            auth_config_root,
            "auth config root must be an existing external non-symlink directory",
        )
    identity_source = json.dumps(
        {
            "auth_config_root": str(resolved_root),
            "compose": [
                {"path": str(path), "sha256": digest}
                for path, digest in zip(resolved_files, digests, strict=True)
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    identity = f"sha256:{hashlib.sha256(identity_source).hexdigest()}"
    return resolved_files, tuple(digests), identity, resolved_root


def _make_plan(
    project_root: Path,
    output: Path,
    docker_context: str,
    source_lock: dict[str, Any],
    target_lock: dict[str, Any],
    target_profile: str,
    volume_migrations: tuple[tuple[str, str, str], ...],
    preserved_compose_files: tuple[Path, ...],
    preserved_compose_sha256: tuple[str, ...],
    preserved_composition_identity: str | None,
    auth_config_root: Path | None,
) -> AuthMigrationPlan:
    return AuthMigrationPlan(
        project_root=project_root.resolve(),
        output=output.absolute(),
        docker_context=docker_context,
        source_lock_identity=source_lock["lock_identity"],
        target_lock_identity=target_lock["lock_identity"],
        source_profile=source_lock["input"]["profile"]["ref"],
        target_profile=target_profile,
        deployment=source_lock["deployment"]["name"],
        environment=source_lock["environment"]["identity"],
        services=tuple(_service_ids(target_lock)),
        volume_migrations=volume_migrations,
        preserved_compose_files=preserved_compose_files,
        preserved_compose_sha256=preserved_compose_sha256,
        preserved_composition_identity=preserved_composition_identity,
        auth_config_root=auth_config_root,
    )


def _validate_migration_candidate(
    source_lock: dict[str, Any],
    target_lock: dict[str, Any],
) -> None:
    spec = _ACTIVE_MIGRATION.get()
    if spec.candidate_policy == "public-b3":
        exact_fields = (
            "environment",
            "world",
            "network",
            "agreements",
            "acknowledgements",
            "operator_inputs",
            "secret_references",
            "scope",
        )
        source_runtime = {
            key: value for key, value in source_lock["runtime"].items() if key != "volumes"
        }
        target_runtime = {
            key: value for key, value in target_lock["runtime"].items() if key != "volumes"
        }
        source_render = source_lock["render_plan"]
        target_render = target_lock["render_plan"]
        source_profile = source_lock["input"]["profile"]["ref"]
        target_profile = target_lock["input"]["profile"]["ref"]
        source_preset = source_lock["input"]["preset"]["ref"]
        target_preset = target_lock["input"]["preset"]["ref"]
        if (
            source_profile != "vps-server@5"
            or target_profile != spec.profile_transitions.get(source_profile)
            or source_preset != "public-web-paper@1"
            or target_preset != spec.preset_transitions.get(source_preset)
            or any(source_lock[field] != target_lock[field] for field in exact_fields)
            or source_lock["deployment"]["name"] != target_lock["deployment"]["name"]
            or source_runtime != target_runtime
            or source_render["adapter"] != "compose"
            or target_render["adapter"] != "compose"
            or source_render["adapter_revision"] != "7"
            or target_render["adapter_revision"] != "8"
            or source_render["services"] != target_render["services"]
            or source_render["volume_roles"] != target_render["volume_roles"]
            or source_render["operator_inputs"] != target_render["operator_inputs"]
            or set(target_render["required_security_controls"])
            != set(source_render["required_security_controls"])
            | {"mcremote-session-only"}
        ):
            _fail(
                "migration_transition_not_reviewed",
                "migration.target_lock",
                "candidate changes state beyond the exact reviewed public b2-to-b3 release transition",
            )
        return

    exact_fields = (
        "environment",
        "world",
        "network",
        "agreements",
        "selection",
        "preset_lifecycle",
        "acknowledgements",
        "operator_inputs",
        "components",
        "artifacts",
        "secret_references",
        "scope",
    )
    source_runtime = {
        key: value for key, value in source_lock["runtime"].items() if key != "volumes"
    }
    target_runtime = {
        key: value for key, value in target_lock["runtime"].items() if key != "volumes"
    }
    source_render = source_lock["render_plan"]
    target_render = target_lock["render_plan"]
    source_controls = set(source_render["required_security_controls"])
    target_controls = set(target_render["required_security_controls"])
    if (
        any(source_lock[field] != target_lock[field] for field in exact_fields)
        or source_lock["input"]["preset"] != target_lock["input"]["preset"]
        or source_lock["deployment"]["name"] != target_lock["deployment"]["name"]
        or source_runtime != target_runtime
        or source_render["adapter"] != target_render["adapter"]
        or source_render["services"] != target_render["services"]
        or source_render["volume_roles"] != target_render["volume_roles"]
        or source_render["operator_inputs"] != target_render["operator_inputs"]
        or target_controls != source_controls | {"mcremote-auth-enforced"}
    ):
        _fail(
            "migration_transition_not_auth_only",
            "migration.target_lock",
            "candidate changes source inputs or runtime state beyond the reviewed auth profile and target volumes",
        )


def _translate_contract(exc: Exception, *, reason: str, path: object) -> AuthMigrationContractError:
    if isinstance(exc, AuthMigrationContractError):
        return exc
    if hasattr(exc, "reason") and hasattr(exc, "path"):
        return AuthMigrationContractError(str(exc.reason), str(exc.path), str(exc))
    return AuthMigrationContractError(reason, path, str(exc))


def _compose_stack(
    output: Path,
    docker_prefix: list[str],
    project_root: Path,
    preserved_compose_files: tuple[Path, ...],
    *,
    project_directory: Path | None = None,
) -> list[str]:
    del project_root
    command = docker_prefix + [
        "compose",
        "--ansi",
        "never",
        "--project-directory",
        str((project_directory or output).resolve()),
        "--file",
        str((output / "compose.yaml").resolve()),
    ]
    for path in preserved_compose_files:
        command.extend(["--file", str(path.resolve())])
    return command


def _validate_preserved_container_record(
    record: dict[str, Any],
    *,
    container_id: str,
    expected_labels: dict[str, str],
    expected_services: set[str],
    expected_files: tuple[Path, ...],
    expected_working_directories: dict[str, set[Path]],
    preserved_file_services: tuple[frozenset[str], ...],
) -> str:
    config = record.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    state = record.get("State")
    if not isinstance(labels, dict) or not isinstance(state, dict):
        _fail(
            "migration_source_provenance_unavailable",
            container_id,
            "preserved runtime labels or state are unavailable",
        )
    if any(labels.get(key) != value for key, value in expected_labels.items()):
        _fail(
            "migration_source_lock_labels_mismatch",
            container_id,
            "runtime deployment, environment, world, or lock labels differ from the source lock",
        )
    service = labels.get("com.docker.compose.service")
    if service not in expected_services:
        _fail(
            "migration_source_service_mismatch",
            container_id,
            "runtime service is not declared by the source lock",
        )
    config_files = labels.get("com.docker.compose.project.config_files")
    actual_files = (
        [item.strip() for item in config_files.split(",")]
        if isinstance(config_files, str)
        else []
    )
    expected_file_strings = [str(path.resolve()) for path in expected_files]
    reviewed_preserved = expected_file_strings[1:]
    if len(preserved_file_services) != len(reviewed_preserved):
        _fail(
            "migration_preserved_composition_invalid",
            container_id,
            "reviewed Compose service scopes do not match the preserved file sequence",
        )
    actual_preserved = actual_files[1:] if actual_files else []
    reviewed_index = -1
    ordered_subset = bool(actual_files) and actual_files[0] == expected_file_strings[0]
    for actual_file in actual_preserved:
        try:
            reviewed_index = reviewed_preserved.index(actual_file, reviewed_index + 1)
        except ValueError:
            ordered_subset = False
            break
    required_files = {
        reviewed_preserved[index]
        for index, services in enumerate(preserved_file_services)
        if service in services
    }
    if not ordered_subset or not required_files.issubset(actual_preserved):
        _fail(
            "migration_source_compose_files_mismatch",
            container_id,
            "runtime Compose provenance recorded "
            f"{len(actual_files)} files but the reviewed stack expected "
            f"{len(expected_file_strings)} in the same order; every file affecting "
            f"service {service} is required",
        )
    allowed_working_directories = {
        str(path.resolve()) for path in expected_working_directories.get(service, set())
    }
    if labels.get("com.docker.compose.project.working_dir") not in (
        allowed_working_directories
    ):
        _fail(
            "migration_source_working_directory_mismatch",
            container_id,
            "runtime Compose working directory differs from the reviewed deployment project",
        )
    if state.get("Running") is not True:
        _fail(
            "migration_source_not_running",
            container_id,
            "source runtime container is not running",
        )
    return service


def _minecraft_copy_image(lock: dict[str, Any]) -> str:
    components = [
        item for item in lock["components"] if item.get("role") == "minecraft-runtime"
    ]
    if len(components) != 1:
        _fail(
            "migration_copy_contract_invalid",
            "components.minecraft-runtime",
            "target lock must contain exactly one Minecraft runtime component",
        )
    artifacts = [
        item
        for item in lock["artifacts"]
        if item.get("id") == components[0].get("artifact")
    ]
    if len(artifacts) != 1 or artifacts[0].get("kind") != "oci":
        _fail(
            "migration_copy_contract_invalid",
            "artifacts.minecraft-runtime",
            "target runtime artifact must be one exact OCI image",
        )
    artifact = artifacts[0]
    return f"{artifact['locator']}:{artifact['version']}@{artifact['digest']}"


class _DockerMigrationHost:
    def __init__(
        self,
        *,
        source_lock: dict[str, Any],
        target_lock: dict[str, Any],
        source_output: Path,
        target_output: Path,
        active_output: Path,
        preserved_compose_files: tuple[Path, ...],
        project_root: Path,
        docker_context: str,
        data_root: Traversable,
        wait_timeout: int,
        runner: CommandRunner,
        hello_probe: Any,
    ) -> None:
        self.source_lock = source_lock
        self.target_lock = target_lock
        self.source_output = source_output
        self.target_output = target_output
        self.active_output = active_output
        self.preserved_compose_files = preserved_compose_files
        self.preserved_compose_service_scopes = tuple(
            _preserved_compose_service_scope(path) for path in preserved_compose_files
        )
        self.project_root = project_root
        self.docker_context = docker_context
        self.data_root = data_root
        self.wait_timeout = wait_timeout
        self.runner = runner
        self.hello_probe = hello_probe
        self.docker_prefix = ["docker", "--context", docker_context]

    def _preflight_docker(self) -> None:
        if not DOCKER_CONTEXT.fullmatch(self.docker_context):
            _fail(
                "docker_context_invalid",
                "migration.docker_context",
                "Docker context must be an explicit name token",
            )
        context = _run(
            self.runner,
            ["docker", "context", "inspect", self.docker_context],
            timeout=30,
            reason="docker_context_unavailable",
            path=self.docker_context,
        )
        record = _single_inspect_record(
            context,
            reason="docker_context_unavailable",
            path=self.docker_context,
        )
        endpoints = record.get("Endpoints")
        docker_endpoint = endpoints.get("docker") if isinstance(endpoints, dict) else None
        host = docker_endpoint.get("Host") if isinstance(docker_endpoint, dict) else None
        if not isinstance(host, str) or not host.startswith("unix://"):
            _fail(
                "docker_context_not_local",
                self.docker_context,
                "migration requires a local unix-socket Docker context on the target host",
            )
        daemon = _run(
            self.runner,
            self.docker_prefix + ["version", "--format", "{{.Server.Version}}"],
            timeout=30,
            reason="docker_unavailable",
            path="docker.daemon",
        )
        compose = _run(
            self.runner,
            self.docker_prefix + ["compose", "version", "--short"],
            timeout=30,
            reason="docker_compose_unavailable",
            path="docker.compose",
        )
        if len(_nonempty_lines(daemon)) != 1 or len(_nonempty_lines(compose)) != 1:
            _fail("docker_unavailable", "docker", "Docker or Compose version is unavailable")

    def _compose_services_for_directory(
        self,
        project_directory: Path,
        expected_services: set[str],
    ) -> dict[str, Any]:
        result = _run(
            self.runner,
            _compose_stack(
                self.source_output,
                self.docker_prefix,
                self.project_root,
                self.preserved_compose_files,
                project_directory=project_directory,
            )
            + ["config", "--format", "json"],
            timeout=60,
            reason="migration_source_compose_invalid",
            path=self.source_output / "compose.yaml",
        )
        try:
            rendered = json.loads(result.stdout)
        except json.JSONDecodeError:
            _fail(
                "migration_source_compose_invalid",
                self.source_output / "compose.yaml",
                "Docker Compose config output is not valid JSON",
            )
        services = rendered.get("services") if isinstance(rendered, dict) else None
        if not isinstance(services, dict) or set(services) != expected_services:
            _fail(
                "migration_source_compose_invalid",
                self.source_output / "compose.yaml",
                "Docker Compose config services do not match the source lock",
            )
        return services

    def _source_working_directories(
        self,
        expected_services: set[str],
    ) -> dict[str, set[Path]]:
        generated_directory = self.source_output.resolve()
        canonical = self._compose_services_for_directory(
            generated_directory,
            expected_services,
        )
        allowed = {
            service: {generated_directory} for service in expected_services
        }
        historical_directory = self.project_root.resolve()
        if historical_directory == generated_directory:
            return allowed
        historical = self._compose_services_for_directory(
            historical_directory,
            expected_services,
        )
        for service in expected_services:
            if canonical[service] == historical[service]:
                allowed[service].add(historical_directory)
        return allowed

    def inspect_source(self, plan: AuthMigrationPlan) -> None:
        try:
            self._preflight_docker()
            services = set(_service_ids(self.source_lock))
            expected_working_directories = self._source_working_directories(services)
            containers = _project_container_ids(
                self.runner,
                self.docker_prefix,
                plan.deployment,
            )
            if len(containers) != len(services):
                _fail(
                    "migration_source_runtime_invalid",
                    plan.deployment,
                    "source runtime must contain exactly the locked service count",
                )
            if self.preserved_compose_files:
                actual = {
                    self._inspect_preserved_container(
                        container,
                        services,
                        expected_working_directories,
                    )
                    for container in containers
                }
            else:
                actual = {
                    _inspect_current_container(
                        self.runner,
                        self.docker_prefix,
                        container,
                        self.source_lock,
                        services,
                        self.source_output,
                    )
                    for container in containers
                }
            if actual != services:
                _fail(
                    "migration_source_runtime_invalid",
                    plan.deployment,
                    "source runtime services do not match the source lock",
                )
            for volume in _volume_identities(self.source_lock):
                _inspect_managed_volume(
                    self.runner,
                    self.docker_prefix,
                    volume,
                    self.source_lock,
                )
        except AuthMigrationContractError:
            raise
        except ApplyContractError as exc:
            translated = _translate_contract(
                exc,
                reason="migration_source_runtime_invalid",
                path=plan.deployment,
            )
            if translated.reason == "bootstrap_runtime_composition_mismatch":
                _fail(
                    "migration_source_runtime_noncanonical",
                    plan.deployment,
                    "source runtime was started from a noncanonical Compose projection",
                )
            _fail(
                "migration_source_runtime_invalid",
                translated.path,
                "source runtime or its managed volumes do not match the source lock",
            )

    def _inspect_preserved_container(
        self,
        container_id: str,
        expected_services: set[str],
        expected_working_directories: dict[str, set[Path]],
    ) -> str:
        record = _single_inspect_record(
            _run(
                self.runner,
                self.docker_prefix + ["inspect", container_id],
                timeout=30,
                reason="migration_source_runtime_invalid",
                path=container_id,
            ),
            reason="migration_source_runtime_invalid",
            path=container_id,
        )
        expected_labels = {
            "com.docker.compose.project": self.source_lock["deployment"]["name"],
            "io.mc-remote.deployment": self.source_lock["deployment"]["name"],
            "io.mc-remote.environment": self.source_lock["environment"]["identity"],
            "io.mc-remote.world": self.source_lock["world"]["identity"],
            "io.mc-remote.lock": self.source_lock["lock_identity"],
        }
        expected_files = [
            self.source_output / "compose.yaml",
            *self.preserved_compose_files,
        ]
        return _validate_preserved_container_record(
            record,
            container_id=container_id,
            expected_labels=expected_labels,
            expected_services=expected_services,
            expected_files=tuple(expected_files),
            expected_working_directories=expected_working_directories,
            preserved_file_services=self.preserved_compose_service_scopes,
        )

    def inspect_targets_absent(self, plan: AuthMigrationPlan) -> None:
        try:
            for _role, _source, target in plan.volume_migrations:
                if _volume_exists(self.runner, self.docker_prefix, target):
                    _fail(
                        "migration_target_volume_exists",
                        target,
                        "plan requires a new target volume identity",
                    )
        except (ApplyContractError, AuthMigrationContractError) as exc:
            raise _translate_contract(
                exc,
                reason="migration_target_volume_inspect_failed",
                path="migration.target_volumes",
            ) from exc

    def pull_target(self, plan: AuthMigrationPlan) -> None:
        try:
            _run(
                self.runner,
                _compose_stack(
                    self.target_output,
                    self.docker_prefix,
                    self.project_root,
                    self.preserved_compose_files,
                )
                + ["pull", "--policy", "always", "--quiet", *plan.services],
                timeout=900,
                reason="migration_target_pull_failed",
                path="artifacts.minecraft-runtime",
            )
        except ApplyContractError as exc:
            raise _translate_contract(exc, reason=exc.reason, path=exc.path) from exc

    def create_target_volumes(self, plan: AuthMigrationPlan) -> None:
        try:
            labels = _expected_volume_labels(self.target_lock)
            for _role, _source, target in plan.volume_migrations:
                if _volume_exists(self.runner, self.docker_prefix, target):
                    _inspect_managed_volume(
                        self.runner,
                        self.docker_prefix,
                        target,
                        self.target_lock,
                    )
                    continue
                command = self.docker_prefix + ["volume", "create", "--driver", "local"]
                for key, value in labels.items():
                    command.extend(["--label", f"{key}={value}"])
                command.append(target)
                created = _run(
                    self.runner,
                    command,
                    timeout=60,
                    reason="migration_target_volume_create_failed",
                    path=target,
                )
                if _nonempty_lines(created) != [target]:
                    _fail(
                        "migration_target_volume_create_failed",
                        target,
                        "Docker did not confirm the exact target volume identity",
                    )
                _inspect_managed_volume(
                    self.runner,
                    self.docker_prefix,
                    target,
                    self.target_lock,
                )
        except (ApplyContractError, AuthMigrationContractError) as exc:
            raise _translate_contract(
                exc,
                reason="migration_target_volume_create_failed",
                path="migration.target_volumes",
            ) from exc

    def stop_source(self, plan: AuthMigrationPlan, source_output: Path) -> None:
        try:
            _run(
                self.runner,
                _compose_stack(
                    source_output,
                    self.docker_prefix,
                    self.project_root,
                    self.preserved_compose_files,
                )
                + ["down", "--timeout", "120"],
                timeout=180,
                reason="migration_source_stop_failed",
                path="docker.compose",
            )
        except ApplyContractError as exc:
            raise _translate_contract(exc, reason=exc.reason, path=exc.path) from exc

    def _copy_image(self) -> str:
        return _minecraft_copy_image(self.target_lock)

    def copy_volumes(self, plan: AuthMigrationPlan) -> None:
        image = self._copy_image()
        try:
            for role, source, target in plan.volume_migrations:
                _run(
                    self.runner,
                    self.docker_prefix
                    + [
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--entrypoint",
                        "/bin/sh",
                        "--mount",
                        f"type=volume,src={source},dst=/source,readonly",
                        "--mount",
                        f"type=volume,src={target},dst=/target",
                        image,
                        "-eu",
                        "-c",
                        "cp -a /source/. /target/",
                    ],
                    timeout=1800,
                    reason="migration_volume_copy_failed",
                    path=role,
                )
        except ApplyContractError as exc:
            raise _translate_contract(exc, reason=exc.reason, path=exc.path) from exc

    def install_target_auth_config(self, plan: AuthMigrationPlan) -> None:
        source = self.active_output / "minecraft" / "plugins" / "McRemote" / "config.yml"
        if source.is_symlink() or not source.is_file():
            _fail(
                "migration_auth_config_invalid",
                source,
                "target render does not contain the generated McRemote config",
            )
        targets = [
            target
            for role, _source, target in plan.volume_migrations
            if role == "minecraft-data"
        ]
        if len(targets) != 1:
            _fail(
                "migration_auth_config_invalid",
                "migration.target_volumes",
                "target transaction requires exactly one minecraft-data volume",
            )
        image = self._copy_image()
        try:
            _run(
                self.runner,
                _compose_stack(
                    self.active_output,
                    self.docker_prefix,
                    self.project_root,
                    self.preserved_compose_files,
                )
                + ["stop", "--timeout", "120", "minecraft"],
                timeout=180,
                reason="migration_target_auth_config_stop_failed",
                path="docker.compose.minecraft",
            )
            _run(
                self.runner,
                self.docker_prefix
                + [
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--user",
                    "0:0",
                    "--entrypoint",
                    "/bin/sh",
                    "--mount",
                    f"type=volume,src={targets[0]},dst=/target",
                    "--mount",
                    f"type=bind,src={source.resolve()},dst=/source/config.yml,readonly",
                    image,
                    "-eu",
                    "-c",
                    "mkdir -p /target/plugins/McRemote && "
                    "cp /source/config.yml /target/plugins/McRemote/.config.yml.mcrctl && "
                    "chown 1000:1000 /target/plugins/McRemote/.config.yml.mcrctl && "
                    "chmod 0644 /target/plugins/McRemote/.config.yml.mcrctl && "
                    "mv -f /target/plugins/McRemote/.config.yml.mcrctl "
                    "/target/plugins/McRemote/config.yml && sync",
                ],
                timeout=180,
                reason="migration_target_auth_config_install_failed",
                path="minecraft-data/plugins/McRemote/config.yml",
            )
        except ApplyContractError as exc:
            raise _translate_contract(exc, reason=exc.reason, path=exc.path) from exc

    def start_target(self, plan: AuthMigrationPlan) -> None:
        try:
            _run(
                self.runner,
                _compose_stack(
                    self.active_output,
                    self.docker_prefix,
                    self.project_root,
                    self.preserved_compose_files,
                )
                + [
                    "up",
                    "--detach",
                    "--wait",
                    "--wait-timeout",
                    str(self.wait_timeout),
                    "--no-build",
                    "--pull",
                    "never",
                    *plan.services,
                ],
                timeout=self.wait_timeout + 60,
                reason="migration_target_start_failed",
                path="docker.compose",
            )
        except ApplyContractError as exc:
            raise _translate_contract(exc, reason=exc.reason, path=exc.path) from exc

    def verify_target(self, plan: AuthMigrationPlan) -> None:
        try:
            doctor_toml_project(
                self.project_root,
                self.target_output,
                docker_context=self.docker_context,
                data_root=self.data_root,
                runner=self.runner,
                hello_probe=self.hello_probe,
            )
        except DoctorContractError as exc:
            raise AuthMigrationContractError(exc.reason, exc.path, str(exc)) from exc


def plan_auth_enforcement_migration(
    project_root: Path,
    output: Path,
    *,
    docker_context: str,
    target_volumes: dict[str, str],
    preserved_compose_files: tuple[Path, ...] = (),
    auth_config_root: Path | None = None,
    data_root: Traversable,
    allow_unverified: bool = False,
    allow_eol: bool = False,
    host: AuthMigrationHost | None = None,
    runner: CommandRunner = _default_runner,
    hello_probe: Any = probe_protocol_hello,
) -> AuthMigrationPlan:
    """Build and inspect an exact auth-enforced candidate without changing the project."""

    project_root = project_root.resolve()
    output = output.absolute()
    if _state_path(project_root).exists():
        state = load_auth_migration_state(project_root)
        _fail(
            "migration_transaction_exists",
            _state_path(project_root),
            f"existing transaction is at phase {state['phase']}; run apply with its exact identities",
        )
    try:
        source_verification = verify_toml_render_output(
            project_root,
            output,
            data_root=data_root,
            allow_historical_lock=True,
        )
        source_lock = source_verification.lock
        target_profile, migrations = _validate_transition(source_lock, target_volumes)
        source_preset = source_lock["input"]["preset"]["ref"]
        target_preset = _ACTIVE_MIGRATION.get().preset_transitions.get(source_preset)
        (
            preserved_files,
            preserved_sha256,
            preserved_identity,
            resolved_auth_root,
        ) = _validate_preserved_composition(
            project_root,
            preserved_compose_files,
            auth_config_root,
        )
        with tempfile.TemporaryDirectory(prefix="mcrctl-auth-migration-plan.") as temporary:
            candidate_root = Path(temporary) / "candidate"
            target_lock, candidate_output = _build_candidate(
                project_root,
                output,
                candidate_root,
                target_profile=target_profile,
                target_preset=target_preset,
                target_volumes=target_volumes,
                data_root=data_root,
                allow_unverified=allow_unverified,
                allow_eol=allow_eol,
                resolved_at=source_lock["resolved_at"],
            )
            _validate_migration_candidate(source_lock, target_lock)
            plan = _make_plan(
                project_root,
                output,
                docker_context,
                source_lock,
                target_lock,
                target_profile,
                migrations,
                preserved_files,
                preserved_sha256,
                preserved_identity,
                resolved_auth_root,
            )
            actual_host = host or _DockerMigrationHost(
                source_lock=source_lock,
                target_lock=target_lock,
                source_output=output,
                target_output=candidate_output,
                active_output=output,
                preserved_compose_files=preserved_files,
                project_root=project_root,
                docker_context=docker_context,
                data_root=data_root,
                wait_timeout=300,
                runner=runner,
                hello_probe=hello_probe,
            )
            actual_host.inspect_source(plan)
            actual_host.inspect_targets_absent(plan)
            return plan
    except (
        AuthMigrationContractError,
        ApplyContractError,
        DoctorContractError,
        ProjectOrderError,
        RenderContractError,
        ResolutionError,
    ) as exc:
        raise _translate_contract(
            exc,
            reason="migration_plan_failed",
            path=project_root,
        ) from exc


def _state_from_plan(plan: AuthMigrationPlan) -> dict[str, Any]:
    return {
        "schema": _ACTIVE_MIGRATION.get().schema,
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "migration": _ACTIVE_MIGRATION.get().name,
        "phase": "prepared",
        "project_root": str(plan.project_root),
        "output": str(plan.output),
        "docker_context": plan.docker_context,
        "source_lock_identity": plan.source_lock_identity,
        "target_lock_identity": plan.target_lock_identity,
        "source_profile": plan.source_profile,
        "target_profile": plan.target_profile,
        "deployment": plan.deployment,
        "environment": plan.environment,
        "services": list(plan.services),
        "volume_migrations": [
            {"role": role, "source": source, "target": target}
            for role, source, target in plan.volume_migrations
        ],
        "preserved_compose_files": [
            {
                "path": str(path),
                "sha256": sha256,
                "snapshot": f"{index:02d}-{path.name}",
            }
            for index, (path, sha256) in enumerate(
                zip(
                    plan.preserved_compose_files,
                    plan.preserved_compose_sha256,
                    strict=True,
                )
            )
        ],
        "preserved_composition_identity": plan.preserved_composition_identity,
        "auth_config_root": (
            str(plan.auth_config_root) if plan.auth_config_root is not None else None
        ),
        "last_error": None,
    }


def _plan_from_state(state: dict[str, Any]) -> AuthMigrationPlan:
    return AuthMigrationPlan(
        project_root=Path(state["project_root"]),
        output=Path(state["output"]),
        docker_context=state["docker_context"],
        source_lock_identity=state["source_lock_identity"],
        target_lock_identity=state["target_lock_identity"],
        source_profile=state["source_profile"],
        target_profile=state["target_profile"],
        deployment=state["deployment"],
        environment=state["environment"],
        services=tuple(state["services"]),
        volume_migrations=tuple(
            (item["role"], item["source"], item["target"])
            for item in state["volume_migrations"]
        ),
        preserved_compose_files=tuple(
            Path(item["path"]) for item in state["preserved_compose_files"]
        ),
        preserved_compose_sha256=tuple(
            item["sha256"] for item in state["preserved_compose_files"]
        ),
        preserved_composition_identity=state["preserved_composition_identity"],
        auth_config_root=(
            Path(state["auth_config_root"])
            if state["auth_config_root"] is not None
            else None
        ),
    )


def _prepare_transaction(
    plan: AuthMigrationPlan,
    *,
    source_output: Path,
    target_volumes: dict[str, str],
    data_root: Traversable,
    allow_unverified: bool,
    allow_eol: bool,
) -> None:
    migration_root = _migration_root(plan.project_root)
    migration_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{_ACTIVE_MIGRATION.get().name}.",
            suffix=".prepare",
            dir=migration_root.parent,
        )
    )
    try:
        candidate_root = temporary / "candidate"
        source_render = temporary / "source-render"
        source_lock = load_lock(plan.project_root, data_root=data_root)
        source_preset = source_lock["input"]["preset"]["ref"]
        target_lock, _candidate_output = _build_candidate(
            plan.project_root,
            source_output,
            candidate_root,
            target_profile=plan.target_profile,
            target_preset=_ACTIVE_MIGRATION.get().preset_transitions.get(source_preset),
            target_volumes=target_volumes,
            data_root=data_root,
            allow_unverified=allow_unverified,
            allow_eol=allow_eol,
            resolved_at=source_lock["resolved_at"],
        )
        if target_lock["lock_identity"] != plan.target_lock_identity:
            _fail(
                "migration_target_lock_changed",
                candidate_root / LOCK_NAME,
                "candidate identity changed after the reviewed plan",
            )
        _copy_project_source(
            plan.project_root,
            temporary / "source-project",
            source_output,
        )
        shutil.copytree(source_output, source_render)
        if (
            _ACTIVE_MIGRATION.get().candidate_policy == "public-b3"
            and plan.auth_config_root is not None
        ):
            source_auth_config = (
                plan.auth_config_root / "plugins" / "McRemote" / "config.yml"
            )
            if source_auth_config.is_symlink() or not source_auth_config.is_file():
                _fail(
                    "migration_auth_config_invalid",
                    source_auth_config,
                    "public b3 migration requires the exact existing external McRemote config",
                )
            shutil.copyfile(source_auth_config, temporary / "source-auth-config.yml")
        state = _state_from_plan(plan)
        preserved_root = temporary / "preserved-compose"
        if state["preserved_compose_files"]:
            preserved_root.mkdir()
        for item, source in zip(
            state["preserved_compose_files"],
            plan.preserved_compose_files,
            strict=True,
        ):
            if _sha256_file(source) != item["sha256"]:
                _fail(
                    "migration_preserved_composition_changed",
                    source,
                    "preserved Compose file changed after the reviewed plan",
                )
            destination = preserved_root / item["snapshot"]
            shutil.copyfile(source, destination)
            if _sha256_file(destination) != item["sha256"]:
                _fail(
                    "migration_preserved_composition_changed",
                    destination,
                    "preserved Compose snapshot digest is invalid",
                )
        _write_state_to_path(temporary / STATE_NAME, state)
        _fsync_tree(temporary)
        os.replace(temporary, migration_root)
        _fsync_directory(migration_root.parent)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _write_state_to_path(path: Path, state: dict[str, Any]) -> None:
    source = (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    _atomic_write(path, source)


def _validate_resume_arguments(
    state: dict[str, Any],
    *,
    project_root: Path,
    output: Path,
    docker_context: str,
    target_volumes: dict[str, str],
    preserved_compose_files: tuple[Path, ...],
    auth_config_root: Path | None,
    expected_source: str,
    expected_target: str,
    expected_preserved_composition: str | None,
) -> None:
    expected_volume_entries = {
        item["role"]: item["target"] for item in state["volume_migrations"]
    }
    mismatches = []
    checks = (
        (state["project_root"], str(project_root.resolve()), "project_root"),
        (state["output"], str(output.absolute()), "output"),
        (state["docker_context"], docker_context, "docker_context"),
        (state["source_lock_identity"], expected_source, "source_lock_identity"),
        (state["target_lock_identity"], expected_target, "target_lock_identity"),
        (
            state["preserved_composition_identity"],
            expected_preserved_composition,
            "preserved_composition_identity",
        ),
        (expected_volume_entries, target_volumes, "target_volumes"),
        (
            tuple(item["path"] for item in state["preserved_compose_files"]),
            tuple(str(path.resolve()) for path in preserved_compose_files),
            "preserved_compose_files",
        ),
        (
            state["auth_config_root"],
            str(auth_config_root.resolve()) if auth_config_root is not None else None,
            "auth_config_root",
        ),
    )
    for recorded, provided, name in checks:
        if recorded != provided:
            mismatches.append(name)
    if mismatches:
        _fail(
            "migration_transaction_mismatch",
            _state_path(project_root),
            f"arguments differ from durable transaction: {', '.join(mismatches)}",
        )


def _validate_expected_composition(
    plan: AuthMigrationPlan,
    expected_identity: str | None,
) -> None:
    if plan.preserved_composition_identity is None:
        if expected_identity is not None:
            _fail(
                "migration_expected_composition_mismatch",
                "migration.expected_preserved_composition_identity",
                "no preserved composition exists for this migration",
            )
        return
    if (
        expected_identity is None
        or not SHA256_IDENTITY.fullmatch(expected_identity)
        or expected_identity != plan.preserved_composition_identity
    ):
        _fail(
            "migration_expected_composition_mismatch",
            "migration.expected_preserved_composition_identity",
            "reviewed preserved composition does not match the current plan",
        )


def _publish_desired(plan: AuthMigrationPlan, *, data_root: Traversable) -> None:
    candidate_root = _migration_root(plan.project_root) / "candidate"
    target_lock = load_lock(candidate_root, data_root=data_root)
    if target_lock["lock_identity"] != plan.target_lock_identity:
        _fail(
            "migration_candidate_invalid",
            candidate_root / LOCK_NAME,
            "durable candidate lock does not match the transaction",
        )
    _atomic_write(plan.project_root / ORDER_NAME, (candidate_root / ORDER_NAME).read_bytes())
    _atomic_write(plan.project_root / LOCK_NAME, (candidate_root / LOCK_NAME).read_bytes())
    result = render_toml_project(plan.project_root, plan.output, data_root=data_root)
    if result.lock_identity != plan.target_lock_identity:
        _fail(
            "migration_candidate_invalid",
            plan.output,
            "published render does not match the target transaction lock",
        )


def _install_auth_config(plan: AuthMigrationPlan) -> None:
    if plan.auth_config_root is None:
        return
    source = plan.output / "minecraft" / "plugins" / "McRemote" / "config.yml"
    if source.is_symlink() or not source.is_file():
        _fail(
            "migration_auth_config_invalid",
            source,
            "target render does not contain the generated McRemote config",
        )
    content = source.read_bytes()
    destination_directory = plan.auth_config_root / "plugins" / "McRemote"
    destination = destination_directory / "config.yml"
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file():
            _fail(
                "migration_auth_config_conflict",
                destination,
                "existing private McRemote config differs from the target generated config",
            )
        existing = destination.read_bytes()
        if existing == content:
            return
        if _ACTIVE_MIGRATION.get().candidate_policy == "public-b3":
            source_snapshot = _migration_root(plan.project_root) / "source-auth-config.yml"
            if (
                source_snapshot.is_symlink()
                or not source_snapshot.is_file()
                or source_snapshot.read_bytes() != existing
            ):
                _fail(
                    "migration_auth_config_conflict",
                    destination,
                    "external McRemote config differs from both reviewed source and target",
                )
            _atomic_write(destination, content, mode=0o640)
            return
        _fail(
            "migration_auth_config_conflict",
            destination,
            "existing private McRemote config differs from the target generated config",
        )
    destination_directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    _atomic_write(destination, content, mode=0o640)
    _fsync_directory(destination_directory.parent)


def _load_transaction_locks(
    plan: AuthMigrationPlan,
    *,
    data_root: Traversable,
) -> tuple[dict[str, Any], dict[str, Any], Path, tuple[Path, ...]]:
    root = _migration_root(plan.project_root)
    source_project = root / "source-project"
    source_output = root / "source-render"
    loaded_source = verify_toml_render_output(
        source_project,
        source_output,
        data_root=data_root,
        allow_historical_lock=True,
    ).lock
    candidate_root = root / "candidate"
    loaded_target = verify_toml_render_output(
        candidate_root,
        candidate_root / "generated",
        data_root=data_root,
    ).lock
    _validate_migration_candidate(loaded_source, loaded_target)
    if (
        loaded_source["lock_identity"] != plan.source_lock_identity
        or loaded_target["lock_identity"] != plan.target_lock_identity
    ):
        _fail(
            "migration_transaction_invalid",
            root,
            "durable source or target lock identity changed",
        )
    source_profile = loaded_source["input"]["profile"]["ref"]
    target_profile = loaded_target["input"]["profile"]["ref"]
    source_volumes = {
        item["role"]: item["identity"]
        for item in loaded_source["runtime"]["volumes"]
    }
    target_volumes = {
        item["role"]: item["identity"]
        for item in loaded_target["runtime"]["volumes"]
    }
    expected_migrations = tuple(
        (role, source_volumes[role], target_volumes[role])
        for role in sorted(source_volumes)
        if role in target_volumes
    )
    if (
        plan.source_profile != source_profile
        or plan.target_profile != target_profile
        or _ACTIVE_MIGRATION.get().profile_transitions.get(source_profile)
        != target_profile
        or _ACTIVE_MIGRATION.get().preset_transitions.get(
            loaded_source["input"]["preset"]["ref"],
            loaded_source["input"]["preset"]["ref"],
        )
        != loaded_target["input"]["preset"]["ref"]
        or loaded_source["deployment"]["name"] != plan.deployment
        or loaded_target["deployment"]["name"] != plan.deployment
        or loaded_source["environment"]["identity"] != plan.environment
        or loaded_target["environment"]["identity"] != plan.environment
        or loaded_source["world"] != loaded_target["world"]
        or set(source_volumes) != set(target_volumes)
        or expected_migrations != plan.volume_migrations
        or tuple(_service_ids(loaded_target)) != plan.services
        or "mcremote-auth-enforced"
        not in loaded_target["render_plan"]["required_security_controls"]
    ):
        _fail(
            "migration_transaction_invalid",
            root,
            "durable transaction metadata does not match its exact source and target locks",
        )
    state = load_auth_migration_state(plan.project_root)
    preserved_snapshots = tuple(
        root / "preserved-compose" / item["snapshot"]
        for item in state["preserved_compose_files"]
    )
    for snapshot, expected in zip(
        preserved_snapshots,
        plan.preserved_compose_sha256,
        strict=True,
    ):
        if snapshot.is_symlink() or not snapshot.is_file() or _sha256_file(snapshot) != expected:
            _fail(
                "migration_transaction_invalid",
                snapshot,
                "preserved Compose snapshot does not match the transaction digest",
            )
    return loaded_source, loaded_target, candidate_root / "generated", preserved_snapshots


def _apply_auth_enforcement_migration_locked(
    project_root: Path,
    output: Path,
    *,
    docker_context: str,
    target_volumes: dict[str, str],
    preserved_compose_files: tuple[Path, ...] = (),
    auth_config_root: Path | None = None,
    expected_source_lock_identity: str,
    expected_target_lock_identity: str,
    expected_preserved_composition_identity: str | None = None,
    data_root: Traversable,
    confirmed: bool,
    allow_unverified: bool = False,
    allow_eol: bool = False,
    wait_timeout: int = 300,
    host: AuthMigrationHost | None = None,
    runner: CommandRunner = _default_runner,
    hello_probe: Any = probe_protocol_hello,
    progress=lambda _step: None,
) -> AuthMigrationResult:
    """Apply or resume one durable migration; failures never restart the source runtime."""

    project_root = project_root.resolve()
    output = output.absolute()
    if not confirmed:
        _fail(
            "migration_confirmation_required",
            "migration.confirmed",
            "live migration requires explicit --yes",
        )
    if not SHA256_IDENTITY.fullmatch(expected_source_lock_identity):
        _fail(
            "migration_expected_lock_invalid",
            "migration.expected_source_lock_identity",
            "expected source lock must be one sha256 identity",
        )
    if not SHA256_IDENTITY.fullmatch(expected_target_lock_identity):
        _fail(
            "migration_expected_lock_invalid",
            "migration.expected_target_lock_identity",
            "expected target lock must be one sha256 identity",
        )
    if wait_timeout < 30 or wait_timeout > 1800:
        _fail(
            "migration_wait_timeout_invalid",
            "migration.wait_timeout",
            "wait timeout must be between 30 and 1800 seconds",
        )

    state_exists = _state_path(project_root).exists()
    if not state_exists:
        plan = plan_auth_enforcement_migration(
            project_root,
            output,
            docker_context=docker_context,
            target_volumes=target_volumes,
            preserved_compose_files=preserved_compose_files,
            auth_config_root=auth_config_root,
            data_root=data_root,
            allow_unverified=allow_unverified,
            allow_eol=allow_eol,
            host=host,
            runner=runner,
            hello_probe=hello_probe,
        )
        if (
            plan.source_lock_identity != expected_source_lock_identity
            or plan.target_lock_identity != expected_target_lock_identity
        ):
            _fail(
                "migration_expected_lock_mismatch",
                "migration.expected_lock_identity",
                "reviewed source or target lock does not match the current plan",
            )
        _validate_expected_composition(
            plan,
            expected_preserved_composition_identity,
        )
        _prepare_transaction(
            plan,
            source_output=output,
            target_volumes=target_volumes,
            data_root=data_root,
            allow_unverified=allow_unverified,
            allow_eol=allow_eol,
        )

    state = load_auth_migration_state(project_root)
    _validate_resume_arguments(
        state,
        project_root=project_root,
        output=output,
        docker_context=docker_context,
        target_volumes=target_volumes,
        preserved_compose_files=preserved_compose_files,
        auth_config_root=auth_config_root,
        expected_source=expected_source_lock_identity,
        expected_target=expected_target_lock_identity,
        expected_preserved_composition=expected_preserved_composition_identity,
    )
    plan = _plan_from_state(state)
    source_lock, target_lock, candidate_output, preserved_snapshots = _load_transaction_locks(
        plan,
        data_root=data_root,
    )
    actual_host = host or _DockerMigrationHost(
        source_lock=source_lock,
        target_lock=target_lock,
        source_output=_migration_root(project_root) / "source-render",
        target_output=candidate_output,
        active_output=output,
        preserved_compose_files=preserved_snapshots,
        project_root=project_root,
        docker_context=docker_context,
        data_root=data_root,
        wait_timeout=wait_timeout,
        runner=runner,
        hello_probe=hello_probe,
    )
    source_output = _migration_root(project_root) / "source-render"
    initial_phase = state["phase"]

    def advance(phase: str) -> None:
        nonlocal state
        state = {**state, "phase": phase, "last_error": None}
        _write_state(project_root, state)

    try:
        if state["phase"] == "prepared":
            progress("pull-target-images")
            actual_host.pull_target(plan)
            progress("create-target-volumes")
            actual_host.create_target_volumes(plan)
            advance("target-volumes-created")
        if state["phase"] == "target-volumes-created":
            progress("stop-source-runtime")
            actual_host.stop_source(plan, source_output)
            advance("source-runtime-stopped")
        if state["phase"] == "source-runtime-stopped":
            progress("publish-desired-state")
            _publish_desired(plan, data_root=data_root)
            advance("desired-published")
        if state["phase"] == "desired-published":
            progress("install-auth-config")
            _install_auth_config(plan)
            advance("auth-config-installed")
        if state["phase"] == "auth-config-installed":
            progress("copy-volumes")
            actual_host.copy_volumes(plan)
            advance("volumes-copied")
        if state["phase"] == "volumes-copied":
            progress("install-target-auth-config")
            actual_host.install_target_auth_config(plan)
            advance("target-auth-config-installed")
        if state["phase"] == "target-auth-config-installed":
            progress(f"start-target-and-wait timeout={wait_timeout}")
            actual_host.start_target(plan)
            progress("verify-target-auth-enforced")
            actual_host.verify_target(plan)
            advance("complete")
    except AuthMigrationContractError as exc:
        state = {
            **state,
            "last_error": {"reason": exc.reason, "path": exc.path},
        }
        _write_state(project_root, state)
        raise
    except (
        ApplyContractError,
        DoctorContractError,
        ProjectOrderError,
        RenderContractError,
        ResolutionError,
    ) as exc:
        wrapped = _translate_contract(
            exc,
            reason="migration_apply_failed",
            path=_migration_root(project_root),
        )
        state = {
            **state,
            "last_error": {"reason": wrapped.reason, "path": wrapped.path},
        }
        _write_state(project_root, state)
        raise wrapped from exc
    except OSError as exc:
        wrapped = AuthMigrationContractError(
            "migration_io_failed",
            _migration_root(project_root),
            str(exc),
        )
        state = {
            **state,
            "last_error": {"reason": wrapped.reason, "path": wrapped.path},
        }
        _write_state(project_root, state)
        raise wrapped from exc

    status = "resumed-complete" if state_exists or initial_phase != "prepared" else "complete"
    return AuthMigrationResult(
        status=status,
        source_lock_identity=plan.source_lock_identity,
        target_lock_identity=plan.target_lock_identity,
        phase=state["phase"],
    )


def apply_auth_enforcement_migration(
    project_root: Path,
    output: Path,
    *,
    docker_context: str,
    target_volumes: dict[str, str],
    preserved_compose_files: tuple[Path, ...] = (),
    auth_config_root: Path | None = None,
    expected_source_lock_identity: str,
    expected_target_lock_identity: str,
    expected_preserved_composition_identity: str | None = None,
    data_root: Traversable,
    confirmed: bool,
    allow_unverified: bool = False,
    allow_eol: bool = False,
    wait_timeout: int = 300,
    host: AuthMigrationHost | None = None,
    runner: CommandRunner = _default_runner,
    hello_probe: Any = probe_protocol_hello,
    progress=lambda _step: None,
) -> AuthMigrationResult:
    """Apply or resume under an exclusive project-local migration lock."""

    project_root = project_root.resolve()
    if not confirmed:
        _fail(
            "migration_confirmation_required",
            "migration.confirmed",
            "live migration requires explicit --yes",
        )
    if not (project_root / ORDER_NAME).is_file():
        _fail(
            "order_missing",
            project_root / ORDER_NAME,
            "migration requires an existing TOML deployment project",
        )
    migration_name = _ACTIVE_MIGRATION.get().name
    lock_path = project_root / ".mcrctl" / "migrations" / f"{migration_name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o640)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail(
                "migration_concurrent_apply",
                lock_path,
                f"another {migration_name} apply is already running",
            )
        return _apply_auth_enforcement_migration_locked(
            project_root,
            output,
            docker_context=docker_context,
            target_volumes=target_volumes,
            preserved_compose_files=preserved_compose_files,
            auth_config_root=auth_config_root,
            expected_source_lock_identity=expected_source_lock_identity,
            expected_target_lock_identity=expected_target_lock_identity,
            expected_preserved_composition_identity=(
                expected_preserved_composition_identity
            ),
            data_root=data_root,
            confirmed=confirmed,
            allow_unverified=allow_unverified,
            allow_eol=allow_eol,
            wait_timeout=wait_timeout,
            host=host,
            runner=runner,
            hello_probe=hello_probe,
            progress=progress,
        )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def plan_public_b3_upgrade(*args: Any, **kwargs: Any) -> AuthMigrationPlan:
    """Plan the exact public b2-to-b3 migration in its own durable namespace."""

    with _activate_migration(PUBLIC_B3_MIGRATION):
        return plan_auth_enforcement_migration(*args, **kwargs)


def apply_public_b3_upgrade(*args: Any, **kwargs: Any) -> AuthMigrationResult:
    """Apply or resume the exact public b2-to-b3 migration."""

    with _activate_migration(PUBLIC_B3_MIGRATION):
        return apply_auth_enforcement_migration(*args, **kwargs)


def load_public_b3_upgrade_state(project_root: Path) -> dict[str, Any]:
    """Load one durable public b3 migration state."""

    with _activate_migration(PUBLIC_B3_MIGRATION):
        return load_auth_migration_state(project_root)
