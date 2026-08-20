"""Release-independent, in-place deployment update transaction boundaries."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import tomllib
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Protocol

import tomlkit

from .apply import (
    DOCKER_CONTEXT,
    ApplyContractError,
    CommandRunner,
    _default_runner,
    _project_container_ids,
    _run,
    _service_ids,
    _single_inspect_record,
)
from .artifacts import fetch_locked_artifacts
from .auth_migration import _compose_stack, _validate_effective_mcremote_mount
from .doctor import doctor_toml_project, probe_protocol_hello
from .preset_registry import load_profile
from .render import render_toml_project, verify_toml_render_output
from .resolver import load_lock, resolve_project
from .toml_project import load_order, update_order_scalar

MAX_PRESERVED_COMPOSE_FILES = 4
MAX_PRESERVED_COMPOSE_BYTES = 64 * 1024
INPUT_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
SHA256_IDENTITY = re.compile(r"^sha256:[0-9a-f]{64}$")
UPDATE_SCHEMA = "mcrctl.deployment-update"
UPDATE_SCHEMA_VERSION = 1
UPDATE_RELATIVE = Path(".mcrctl") / "updates"
UPDATE_PHASES = (
    "prepared",
    "source-stopped",
    "target-published",
    "rolled-back",
    "rollback-failed",
    "complete",
)


class DeploymentUpdateContractError(ValueError):
    """Stable fail-closed diagnostic for a generic deployment update."""

    def __init__(self, reason: str, path: object, message: str) -> None:
        self.reason = reason
        self.path = str(path)
        super().__init__(f"{reason}: {path}: {message}")


def _fail(reason: str, path: object, message: str) -> None:
    raise DeploymentUpdateContractError(reason, path, message)


@dataclass(frozen=True)
class DeploymentUpdatePlan:
    plan_id: str
    project_root: Path
    output: Path
    docker_context: str
    source_lock_identity: str
    target_lock_identity: str
    source_profile: str
    target_profile: str
    source_preset: str
    target_preset: str
    deployment: str
    environment: str
    services: tuple[str, ...]
    volumes: tuple[tuple[str, str], ...]
    preserved_compose_files: tuple[Path, ...]
    preserved_compose_sha256: tuple[str, ...]
    kind: str = "release-update"


@dataclass(frozen=True)
class DeploymentUpdateResult:
    status: str
    plan_id: str
    source_lock_identity: str
    target_lock_identity: str
    phase: str


class DeploymentUpdateHost(Protocol):
    def discover_source_composition(
        self,
        output: Path,
        *,
        deployment: str,
        lock_identity: str,
    ) -> tuple[Path, ...]: ...

    def validate_plan(
        self,
        plan: DeploymentUpdatePlan,
        source_lock: dict[str, Any],
        target_lock: dict[str, Any],
        target_output: Path,
    ) -> None: ...

    def pull_target(self, plan: DeploymentUpdatePlan, target_output: Path) -> None: ...

    def stop_source(self, plan: DeploymentUpdatePlan, source_output: Path) -> None: ...

    def start_target(self, plan: DeploymentUpdatePlan, target_output: Path) -> None: ...

    def verify_target(self, plan: DeploymentUpdatePlan, target_output: Path) -> None: ...

    def start_source(self, plan: DeploymentUpdatePlan, source_output: Path) -> None: ...

    def verify_source(self, plan: DeploymentUpdatePlan, source_output: Path) -> None: ...


def _atomic_replace(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, path.stat().st_mode & 0o7777)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_project_source(source: Path, destination: Path, output: Path) -> None:
    ignored_names = {".mcrctl"}
    if output.parent.resolve() == source.resolve():
        ignored_names.add(output.name)

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return set(names) & ignored_names

    shutil.copytree(source, destination, symlinks=False, ignore=ignore)


def _update_operator_adapter(project_root: Path, role: str, adapter: str) -> None:
    loaded = load_order(project_root)
    matching = [
        index
        for index, item in enumerate(loaded.order.get("operator_inputs", []))
        if item["role"] == role
    ]
    if len(matching) != 1:
        _fail(
            "update_operator_input_missing",
            f"operator_inputs.{role}",
            "target profile requires exactly one existing operator input role",
        )
    index = matching[0]
    if loaded.order["operator_inputs"][index]["adapter"] == adapter:
        return
    document = tomlkit.parse(loaded.source_bytes.decode("utf-8"))
    document["operator_inputs"][index]["adapter"] = adapter
    _atomic_replace(
        project_root / "mc-remote.toml",
        tomlkit.dumps(document).encode("utf-8"),
    )
    load_order(project_root)


def _update_operator_input_scalar(
    project_root: Path,
    role: str,
    key: str,
    value: str,
) -> None:
    if not INPUT_KEY.fullmatch(key):
        _fail(
            "update_input_override_invalid",
            f"{role}.{key}",
            "operator input keys must be lowercase TOML tokens",
        )
    loaded = load_order(project_root)
    matching = [
        item
        for item in loaded.order.get("operator_inputs", [])
        if item["role"] == role
    ]
    if len(matching) != 1:
        _fail(
            "update_operator_input_missing",
            f"operator_inputs.{role}",
            "input override requires exactly one existing operator input role",
        )
    path = project_root / matching[0]["path"]
    if path.is_symlink() or not path.is_file():
        _fail(
            "update_operator_input_invalid",
            path,
            "operator input must be one existing regular project file",
        )
    try:
        source = path.read_text(encoding="utf-8")
        current = tomllib.loads(source)
        document = tomlkit.parse(source)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        _fail("update_operator_input_invalid", path, str(exc))
    existing = current.get(key)
    if existing is not None and not isinstance(existing, str):
        _fail(
            "update_input_override_type_mismatch",
            f"{role}.{key}",
            "string override cannot replace a non-string value",
        )
    document[key] = value
    _atomic_replace(path, tomlkit.dumps(document).encode("utf-8"))


def _replace_operator_input_file(
    project_root: Path,
    role: str,
    source_path: Path,
) -> None:
    loaded = load_order(project_root)
    matching = [
        item
        for item in loaded.order.get("operator_inputs", [])
        if item["role"] == role
    ]
    if len(matching) != 1:
        _fail(
            "update_operator_input_missing",
            f"operator_inputs.{role}",
            "input replacement requires exactly one existing operator input role",
        )
    if source_path.is_symlink() or not source_path.is_file():
        _fail(
            "update_input_file_invalid",
            source_path,
            "replacement input must be one existing regular non-symlink file",
        )
    try:
        content = source_path.read_bytes()
    except OSError as exc:
        _fail("update_input_file_invalid", source_path, str(exc))
    target = project_root / matching[0]["path"]
    _atomic_replace(target, content)


def _adapt_candidate_order(
    destination: Path,
    *,
    target_profile: str,
    target_preset: str,
    input_overrides: dict[tuple[str, str], str],
    input_files: dict[str, Path] | None = None,
    data_root: Traversable,
) -> None:
    """Project release identities and typed-input adapters into one candidate."""

    update_order_scalar(destination, ("deployment", "profile"), target_profile)
    update_order_scalar(destination, ("environment", "preset"), target_preset)
    profile = load_profile(target_profile, data_root=data_root)
    for required in profile.data.get("operator_input_roles", []):
        _update_operator_adapter(destination, required["id"], required["adapter"])
    replacements = {} if input_files is None else input_files
    overlapping = {role for role, _key in input_overrides} & set(replacements)
    if overlapping:
        _fail(
            "update_input_override_invalid",
            ",".join(sorted(overlapping)),
            "one role cannot use scalar overrides and whole-file replacement together",
        )
    for role, source_path in sorted(replacements.items()):
        _replace_operator_input_file(destination, role, source_path)
    for (role, key), value in sorted(input_overrides.items()):
        _update_operator_input_scalar(destination, role, key, value)
    load_order(destination)


def _prepare_candidate_order(
    project_root: Path,
    output: Path,
    destination: Path,
    *,
    target_profile: str,
    target_preset: str,
    input_overrides: dict[tuple[str, str], str],
    data_root: Traversable,
    input_files: dict[str, Path] | None = None,
) -> None:
    """Create a lossless candidate order and adapt its typed-input identities."""

    _copy_project_source(project_root.resolve(), destination, output)
    _adapt_candidate_order(
        destination,
        target_profile=target_profile,
        target_preset=target_preset,
        input_overrides=input_overrides,
        input_files=input_files,
        data_root=data_root,
    )


def _family(ref: str) -> str:
    name, separator, revision = ref.partition("@")
    if not separator or not revision.isdigit():
        _fail("update_reference_invalid", ref, "release reference must be exact name@revision")
    return name


def _revision(ref: str) -> int:
    _family(ref)
    return int(ref.rpartition("@")[2])


def _volume_map(lock: dict[str, Any]) -> dict[str, str]:
    return {
        item["role"]: item["identity"]
        for item in lock["runtime"]["volumes"]
    }


def _validate_in_place_transition(
    source: dict[str, Any],
    target: dict[str, Any],
    *,
    allowed_operator_input_additions: frozenset[str] = frozenset(),
    allow_renderer_adapter_change: bool = False,
) -> None:
    """Allow release projection changes while protecting stateful identities."""

    immutable = (
        (source["deployment"]["name"], target["deployment"]["name"]),
        (source["environment"], target["environment"]),
        (source["world"], target["world"]),
        (source["network"], target["network"]),
        (source["agreements"], target["agreements"]),
        (
            source["runtime"]["artifact_store"],
            target["runtime"]["artifact_store"],
        ),
        (source.get("secret_references", []), target.get("secret_references", [])),
    )
    if any(left != right for left, right in immutable):
        _fail(
            "update_instance_identity_changed",
            "deployment.update",
            "deployment, environment, world, network, agreement, secret, "
            "and artifact-store identity must remain unchanged",
        )
    if _volume_map(source) != _volume_map(target):
        _fail(
            "update_stateful_identity_changed",
            "runtime.volumes",
            "normal release updates keep every existing volume role and identity",
        )
    source_profile = source["input"]["profile"]["ref"]
    target_profile = target["input"]["profile"]["ref"]
    source_preset = source["input"]["preset"]["ref"]
    target_preset = target["input"]["preset"]["ref"]
    if _family(source_profile) != _family(target_profile) or _family(
        source_preset
    ) != _family(target_preset):
        _fail(
            "update_release_family_changed",
            "deployment.update",
            "generic in-place update cannot change profile or preset families",
        )
    profile_revision = _revision(target_profile)
    source_profile_revision = _revision(source_profile)
    preset_revision = _revision(target_preset)
    source_preset_revision = _revision(source_preset)
    if (
        profile_revision < source_profile_revision
        or preset_revision < source_preset_revision
    ):
        _fail(
            "update_not_forward",
            "deployment.update",
            "normal update cannot move either profile or preset revision backward",
        )
    if (
        profile_revision == source_profile_revision
        and preset_revision == source_preset_revision
        and source.get("operator_inputs") == target.get("operator_inputs")
    ):
        _fail(
            "update_no_change",
            "deployment.update",
            "same-release update requires one typed operator-input change",
        )
    source_inputs = {
        item["role"]: item["path"] for item in source.get("operator_inputs", [])
    }
    target_inputs = {
        item["role"]: item["path"] for item in target.get("operator_inputs", [])
    }
    added_inputs = set(target_inputs) - set(source_inputs)
    retained_inputs = {
        role: path for role, path in target_inputs.items() if role in source_inputs
    }
    if (
        retained_inputs != source_inputs
        or added_inputs != set(allowed_operator_input_additions)
    ):
        _fail(
            "update_operator_input_shape_changed",
            "operator_inputs",
            "release projection changed typed-input roles or retained paths outside "
            "the reviewed allowance",
        )
    source_render = source["render_plan"]
    target_render = target["render_plan"]
    if (
        source_render["services"],
        source_render["volume_roles"],
    ) != (
        target_render["services"],
        target_render["volume_roles"],
    ) or (
        not allow_renderer_adapter_change
        and source_render["adapter"] != target_render["adapter"]
    ):
        _fail(
            "update_runtime_shape_changed",
            "render_plan",
            "release projection changed services, volume roles, or the renderer "
            "outside the reviewed allowance",
        )
    source_controls = set(source_render["required_security_controls"])
    target_controls = set(target_render["required_security_controls"])
    if not source_controls.issubset(target_controls):
        _fail(
            "update_security_control_removed",
            "render_plan.required_security_controls",
            "normal release updates cannot remove an existing security control",
        )


def _discover_runtime_compose_files(
    output: Path,
    *,
    deployment: str,
    lock_identity: str,
    docker_context: str,
    runner: CommandRunner = _default_runner,
) -> tuple[Path, ...]:
    """Read additional Compose provenance from the live Minecraft container."""

    if not DOCKER_CONTEXT.fullmatch(docker_context):
        _fail(
            "docker_context_invalid",
            docker_context,
            "Docker context must be an explicit name token",
        )
    docker_prefix = ["docker", "--context", docker_context]
    try:
        containers = _project_container_ids(runner, docker_prefix, deployment)
        minecraft_records: list[dict[str, Any]] = []
        for container in containers:
            record = _single_inspect_record(
                _run(
                    runner,
                    docker_prefix + ["inspect", container],
                    timeout=30,
                    reason="update_source_runtime_invalid",
                    path=container,
                ),
                reason="update_source_runtime_invalid",
                path=container,
            )
            config = record.get("Config")
            labels = config.get("Labels") if isinstance(config, dict) else None
            if isinstance(labels, dict) and labels.get(
                "com.docker.compose.service"
            ) == "minecraft":
                minecraft_records.append(record)
    except ApplyContractError as exc:
        raise DeploymentUpdateContractError(exc.reason, exc.path, str(exc)) from exc
    if len(minecraft_records) != 1:
        _fail(
            "update_source_runtime_invalid",
            deployment,
            "running deployment must have exactly one Minecraft service container",
        )
    record = minecraft_records[0]
    labels = record["Config"]["Labels"]
    state = record.get("State")
    if (
        labels.get("com.docker.compose.project") != deployment
        or labels.get("io.mc-remote.deployment") != deployment
        or labels.get("io.mc-remote.lock") != lock_identity
        or not isinstance(state, dict)
        or state.get("Running") is not True
    ):
        _fail(
            "update_source_runtime_invalid",
            deployment,
            "live Minecraft provenance does not match the current deployment lock",
        )
    config_files = labels.get("com.docker.compose.project.config_files")
    values = (
        [value.strip() for value in config_files.split(",") if value.strip()]
        if isinstance(config_files, str)
        else []
    )
    canonical = (output / "compose.yaml").resolve()
    if not values or Path(values[0]).resolve() != canonical:
        _fail(
            "update_source_composition_invalid",
            deployment,
            "live Compose provenance must start with the current canonical render",
        )
    raw_additional = tuple(Path(value) for value in values[1:])
    if any(not path.is_absolute() or path.is_symlink() for path in raw_additional):
        _fail(
            "update_source_composition_invalid",
            deployment,
            "additional Compose inputs must be absolute non-symlink paths",
        )
    additional = tuple(path.resolve() for path in raw_additional)
    if len(additional) > MAX_PRESERVED_COMPOSE_FILES or len(set(additional)) != len(
        additional
    ):
        _fail(
            "update_source_composition_invalid",
            deployment,
            "live Compose provenance has too many or duplicate additional files",
        )
    total_bytes = 0
    for path in additional:
        if (
            not path.is_file()
            or path.stat().st_size > MAX_PRESERVED_COMPOSE_BYTES
        ):
            _fail(
                "update_source_composition_invalid",
                path,
                "additional Compose input must be a small regular file readable by the operator",
            )
        total_bytes += path.stat().st_size
    if total_bytes > MAX_PRESERVED_COMPOSE_BYTES:
        _fail(
            "update_source_composition_invalid",
            deployment,
            "combined additional Compose inputs exceed the transaction limit",
        )
    return additional


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _plan_payload(
    *,
    project_root: Path,
    output: Path,
    docker_context: str,
    source_lock: dict[str, Any],
    target_lock: dict[str, Any],
    preserved_sha256: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "project_root": str(project_root.resolve()),
        "output": str(output.absolute()),
        "docker_context": docker_context,
        "source_lock_identity": source_lock["lock_identity"],
        "target_lock_identity": target_lock["lock_identity"],
        "source_profile": source_lock["input"]["profile"]["ref"],
        "target_profile": target_lock["input"]["profile"]["ref"],
        "source_preset": source_lock["input"]["preset"]["ref"],
        "target_preset": target_lock["input"]["preset"]["ref"],
        "deployment": source_lock["deployment"]["name"],
        "environment": source_lock["environment"]["identity"],
        "services": _service_ids(target_lock),
        "volumes": sorted(_volume_map(target_lock).items()),
        "preserved_compose_sha256": list(preserved_sha256),
    }


def _make_plan(
    payload: dict[str, Any],
    preserved_compose_files: tuple[Path, ...],
) -> DeploymentUpdatePlan:
    kind = payload.get("kind", "release-update")
    identity_payload = dict(payload)
    if kind != "release-update":
        identity_payload["kind"] = kind
    else:
        identity_payload.pop("kind", None)
    canonical = json.dumps(
        identity_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    plan_id = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    return DeploymentUpdatePlan(
        plan_id=plan_id,
        project_root=Path(payload["project_root"]),
        output=Path(payload["output"]),
        docker_context=payload["docker_context"],
        source_lock_identity=payload["source_lock_identity"],
        target_lock_identity=payload["target_lock_identity"],
        source_profile=payload["source_profile"],
        target_profile=payload["target_profile"],
        source_preset=payload["source_preset"],
        target_preset=payload["target_preset"],
        deployment=payload["deployment"],
        environment=payload["environment"],
        services=tuple(payload["services"]),
        volumes=tuple(tuple(item) for item in payload["volumes"]),
        preserved_compose_files=preserved_compose_files,
        preserved_compose_sha256=tuple(payload["preserved_compose_sha256"]),
        kind=kind,
    )


def _updates_root(project_root: Path) -> Path:
    return project_root.resolve() / UPDATE_RELATIVE


def _plan_root(project_root: Path, plan_id: str) -> Path:
    if not SHA256_IDENTITY.fullmatch(plan_id):
        _fail("update_plan_id_invalid", plan_id, "plan id must be one sha256 identity")
    return _updates_root(project_root) / plan_id.removeprefix("sha256:")


def _active_path(project_root: Path) -> Path:
    return _updates_root(project_root) / "active.json"


def _state_path(project_root: Path, plan_id: str) -> Path:
    return _plan_root(project_root, plan_id) / "state.json"


def _write_json(path: Path, value: dict[str, Any]) -> None:
    content = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    if path.exists():
        _atomic_replace(path, content)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _state_from_plan(plan: DeploymentUpdatePlan) -> dict[str, Any]:
    state = {
        "schema": UPDATE_SCHEMA,
        "schema_version": UPDATE_SCHEMA_VERSION,
        "phase": "prepared",
        "plan_id": plan.plan_id,
        "project_root": str(plan.project_root),
        "output": str(plan.output),
        "docker_context": plan.docker_context,
        "source_lock_identity": plan.source_lock_identity,
        "target_lock_identity": plan.target_lock_identity,
        "source_profile": plan.source_profile,
        "target_profile": plan.target_profile,
        "source_preset": plan.source_preset,
        "target_preset": plan.target_preset,
        "deployment": plan.deployment,
        "environment": plan.environment,
        "services": list(plan.services),
        "volumes": [
            {"role": role, "identity": identity} for role, identity in plan.volumes
        ],
        "preserved_compose_files": [
            {
                "source_path": str(path),
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
        "last_error": None,
    }
    if plan.kind != "release-update":
        state["kind"] = plan.kind
    return state


def _plan_from_state(state: dict[str, Any]) -> DeploymentUpdatePlan:
    return DeploymentUpdatePlan(
        plan_id=state["plan_id"],
        project_root=Path(state["project_root"]),
        output=Path(state["output"]),
        docker_context=state["docker_context"],
        source_lock_identity=state["source_lock_identity"],
        target_lock_identity=state["target_lock_identity"],
        source_profile=state["source_profile"],
        target_profile=state["target_profile"],
        source_preset=state["source_preset"],
        target_preset=state["target_preset"],
        deployment=state["deployment"],
        environment=state["environment"],
        services=tuple(state["services"]),
        volumes=tuple(
            (item["role"], item["identity"]) for item in state["volumes"]
        ),
        preserved_compose_files=tuple(
            Path(item["source_path"]) for item in state["preserved_compose_files"]
        ),
        preserved_compose_sha256=tuple(
            item["sha256"] for item in state["preserved_compose_files"]
        ),
        kind=state.get("kind", "release-update"),
    )


def _load_state(project_root: Path, plan_id: str) -> dict[str, Any]:
    path = _state_path(project_root, plan_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("update_transaction_invalid", path, str(exc))
    required = {
        "schema",
        "schema_version",
        "phase",
        "plan_id",
        "project_root",
        "output",
        "docker_context",
        "source_lock_identity",
        "target_lock_identity",
        "source_profile",
        "target_profile",
        "source_preset",
        "target_preset",
        "deployment",
        "environment",
        "services",
        "volumes",
        "preserved_compose_files",
        "last_error",
    }
    if (
        not isinstance(value, dict)
        or frozenset(value) not in {frozenset(required), frozenset(required | {"kind"})}
        or value.get("schema") != UPDATE_SCHEMA
        or value.get("schema_version") != UPDATE_SCHEMA_VERSION
        or value.get("phase") not in UPDATE_PHASES
        or value.get("plan_id") != plan_id
    ):
        _fail(
            "update_transaction_invalid",
            path,
            "durable update state does not match the supported schema",
        )
    compose_entries = value["preserved_compose_files"]
    volumes = value["volumes"]
    services = value["services"]
    scalar_fields = (
        "plan_id",
        "project_root",
        "output",
        "docker_context",
        "source_lock_identity",
        "target_lock_identity",
        "source_profile",
        "target_profile",
        "source_preset",
        "target_preset",
        "deployment",
        "environment",
    )
    if (
        not all(isinstance(value[field], str) and value[field] for field in scalar_fields)
        or Path(value["project_root"]).resolve() != project_root.resolve()
        or not isinstance(services, list)
        or not services
        or not all(isinstance(item, str) and item for item in services)
        or len(set(services)) != len(services)
        or not isinstance(volumes, list)
        or not isinstance(compose_entries, list)
        or len(compose_entries) > MAX_PRESERVED_COMPOSE_FILES
        or (
            "kind" in value
            and value["kind"] != "composition-canonicalization"
        )
    ):
        _fail(
            "update_transaction_invalid",
            path,
            "durable update identity or collection shape is invalid",
        )
    if any(
        not isinstance(item, dict)
        or set(item) != {"role", "identity"}
        or not all(isinstance(field, str) and field for field in item.values())
        for item in volumes
    ):
        _fail("update_transaction_invalid", path, "volume identity record is invalid")
    if any(
        not isinstance(item, dict)
        or set(item) != {"source_path", "sha256", "snapshot"}
        or not isinstance(item["source_path"], str)
        or not isinstance(item["sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
        or not isinstance(item["snapshot"], str)
        or re.fullmatch(r"[0-9]{2}-[A-Za-z0-9_.-]+\.ya?ml", item["snapshot"])
        is None
        for item in compose_entries
    ):
        _fail(
            "update_transaction_invalid",
            path,
            "preserved Compose record is invalid",
        )
    plan = _plan_from_state(value)
    payload = {
        "project_root": str(plan.project_root),
        "output": str(plan.output),
        "docker_context": plan.docker_context,
        "source_lock_identity": plan.source_lock_identity,
        "target_lock_identity": plan.target_lock_identity,
        "source_profile": plan.source_profile,
        "target_profile": plan.target_profile,
        "source_preset": plan.source_preset,
        "target_preset": plan.target_preset,
        "deployment": plan.deployment,
        "environment": plan.environment,
        "services": list(plan.services),
        "volumes": [list(item) for item in plan.volumes],
        "preserved_compose_sha256": list(plan.preserved_compose_sha256),
    }
    if plan.kind != "release-update":
        payload["kind"] = plan.kind
    if _make_plan(payload, plan.preserved_compose_files).plan_id != plan_id:
        _fail(
            "update_transaction_invalid",
            path,
            "durable update content does not match its plan id",
        )
    return value


def load_deployment_update_plan(
    project_root: Path,
    plan_id: str,
) -> DeploymentUpdatePlan:
    """Load and integrity-check one durable update plan without mutation."""

    return _plan_from_state(_load_state(project_root.resolve(), plan_id))


def _prepare_transaction(
    plan: DeploymentUpdatePlan,
    *,
    temporary_root: Path,
    source_output: Path,
) -> None:
    updates = _updates_root(plan.project_root)
    updates.mkdir(parents=True, exist_ok=True)
    destination = _plan_root(plan.project_root, plan.plan_id)
    if destination.exists():
        _fail(
            "update_transaction_exists",
            destination,
            "exact update plan already exists; apply it by plan id",
        )
    _copy_project_source(
        plan.project_root,
        temporary_root / "source-project",
        source_output,
    )
    shutil.copytree(source_output, temporary_root / "source-render")
    preserved_root = temporary_root / "preserved-compose"
    if plan.preserved_compose_files:
        preserved_root.mkdir()
    state = _state_from_plan(plan)
    for item, source in zip(
        state["preserved_compose_files"],
        plan.preserved_compose_files,
        strict=True,
    ):
        if _sha256_file(source) != item["sha256"]:
            _fail(
                "update_source_composition_changed",
                source,
                "additional Compose input changed while preparing the plan",
            )
        shutil.copyfile(source, preserved_root / item["snapshot"])
    _write_json(temporary_root / "state.json", state)
    os.replace(temporary_root, destination)
    _write_json(_active_path(plan.project_root), {"plan_id": plan.plan_id})


def _validate_required_effective_mounts(
    services: dict[str, Any],
    lock: dict[str, Any],
    target_output: Path,
) -> None:
    controls = set(lock["render_plan"]["required_security_controls"])
    if "wirescope-cross-origin-handoff" not in controls:
        return
    caddy = services.get("caddy")
    mounts = caddy.get("volumes") if isinstance(caddy, dict) else None
    if not isinstance(mounts, list):
        _fail(
            "update_target_control_masked",
            "services.caddy.volumes",
            "WireScope target requires generated Caddy mounts",
        )
    required = {
        "/etc/caddy/Caddyfile": (target_output / "Caddyfile").resolve(),
        "/srv/wirescope": (target_output / "wirescope").resolve(),
    }
    for target, source in required.items():
        matching = [
            item
            for item in mounts
            if isinstance(item, dict) and item.get("target") == target
        ]
        if (
            len(matching) != 1
            or matching[0].get("type") != "bind"
            or matching[0].get("read_only") is not True
            or not isinstance(matching[0].get("source"), str)
            or Path(matching[0]["source"]).resolve() != source
        ):
            _fail(
                "update_target_control_masked",
                f"services.caddy.volumes{target}",
                "additional Compose input masks a generated WireScope or Caddy mount",
            )


class _DockerUpdateHost:
    def __init__(
        self,
        *,
        project_root: Path,
        docker_context: str,
        data_root: Traversable,
        wait_timeout: int,
        runner: CommandRunner,
        hello_probe: Any,
        preserved_compose_files: tuple[Path, ...] = (),
    ) -> None:
        self.project_root = project_root
        self.docker_context = docker_context
        self.data_root = data_root
        self.wait_timeout = wait_timeout
        self.runner = runner
        self.hello_probe = hello_probe
        self.preserved_compose_files = preserved_compose_files
        self.docker_prefix = ["docker", "--context", docker_context]

    def discover_source_composition(
        self,
        output: Path,
        *,
        deployment: str,
        lock_identity: str,
    ) -> tuple[Path, ...]:
        return _discover_runtime_compose_files(
            output,
            deployment=deployment,
            lock_identity=lock_identity,
            docker_context=self.docker_context,
            runner=self.runner,
        )

    def _compose(self, output: Path) -> list[str]:
        return _compose_stack(
            output,
            self.docker_prefix,
            self.project_root,
            self.preserved_compose_files,
        )

    def _validate_target_compose(
        self,
        target_output: Path,
        target_lock: dict[str, Any],
    ) -> None:
        result = _run(
            self.runner,
            self._compose(target_output) + ["config", "--format", "json"],
            timeout=60,
            reason="update_target_compose_invalid",
            path=target_output / "compose.yaml",
        )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            _fail(
                "update_target_compose_invalid",
                target_output / "compose.yaml",
                "Docker Compose config output is not valid JSON",
            )
        services = value.get("services") if isinstance(value, dict) else None
        expected = set(_service_ids(target_lock))
        if not isinstance(services, dict) or set(services) != expected:
            _fail(
                "update_target_compose_invalid",
                target_output / "compose.yaml",
                "effective target services do not match the target lock",
            )
        minecraft = services.get("minecraft")
        if not isinstance(minecraft, dict):
            _fail(
                "update_target_compose_invalid",
                "services.minecraft",
                "effective target has no Minecraft service",
            )
        try:
            _validate_effective_mcremote_mount(
                minecraft,
                target_lock,
                path="deployment.update.target.minecraft",
            )
        except Exception as exc:
            if hasattr(exc, "reason") and hasattr(exc, "path"):
                raise DeploymentUpdateContractError(
                    str(exc.reason), str(exc.path), str(exc)
                ) from exc
            raise
        _validate_required_effective_mounts(services, target_lock, target_output)

    def validate_plan(
        self,
        plan: DeploymentUpdatePlan,
        source_lock: dict[str, Any],
        target_lock: dict[str, Any],
        target_output: Path,
    ) -> None:
        del source_lock
        try:
            doctor_toml_project(
                plan.project_root,
                plan.output,
                docker_context=self.docker_context,
                data_root=self.data_root,
                runner=self.runner,
                hello_probe=self.hello_probe,
            )
            self._validate_target_compose(target_output, target_lock)
        except DeploymentUpdateContractError:
            raise
        except Exception as exc:
            if hasattr(exc, "reason") and hasattr(exc, "path"):
                raise DeploymentUpdateContractError(
                    str(exc.reason), str(exc.path), str(exc)
                ) from exc
            raise

    def pull_target(self, plan: DeploymentUpdatePlan, target_output: Path) -> None:
        _run(
            self.runner,
            self._compose(target_output)
            + ["pull", "--policy", "always", "--quiet", *plan.services],
            timeout=900,
            reason="update_target_pull_failed",
            path="docker.compose",
        )

    def stop_source(self, plan: DeploymentUpdatePlan, source_output: Path) -> None:
        del plan
        _run(
            self.runner,
            self._compose(source_output) + ["down", "--timeout", "120"],
            timeout=180,
            reason="update_source_stop_failed",
            path="docker.compose",
        )

    def _start(self, plan: DeploymentUpdatePlan, output: Path, reason: str) -> None:
        _run(
            self.runner,
            self._compose(output)
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
            reason=reason,
            path="docker.compose",
        )

    def start_target(self, plan: DeploymentUpdatePlan, target_output: Path) -> None:
        self._start(plan, target_output, "update_target_start_failed")

    def verify_target(self, plan: DeploymentUpdatePlan, target_output: Path) -> None:
        del target_output
        doctor_toml_project(
            plan.project_root,
            plan.output,
            docker_context=self.docker_context,
            data_root=self.data_root,
            runner=self.runner,
            hello_probe=self.hello_probe,
        )

    def start_source(self, plan: DeploymentUpdatePlan, source_output: Path) -> None:
        self._start(plan, source_output, "update_source_restart_failed")

    def verify_source(self, plan: DeploymentUpdatePlan, source_output: Path) -> None:
        del source_output
        doctor_toml_project(
            plan.project_root,
            plan.output,
            docker_context=self.docker_context,
            data_root=self.data_root,
            runner=self.runner,
            hello_probe=self.hello_probe,
        )


def _plan_deployment_update_locked(
    project_root: Path,
    output: Path,
    *,
    target_profile: str,
    target_preset: str,
    input_overrides: dict[tuple[str, str], str],
    input_files: dict[str, Path] | None = None,
    docker_context: str,
    data_root: Traversable,
    allow_unverified: bool = False,
    allow_eol: bool = False,
    host: DeploymentUpdateHost | None = None,
    runner: CommandRunner = _default_runner,
    hello_probe: Any = probe_protocol_hello,
) -> DeploymentUpdatePlan:
    """Prepare one exact in-place release plan without changing the live project."""

    project_root = project_root.resolve()
    output = output.absolute()
    source_verification = verify_toml_render_output(
        project_root,
        output,
        data_root=data_root,
        allow_historical_lock=True,
    )
    source_lock = source_verification.lock
    actual_host = host or _DockerUpdateHost(
        project_root=project_root,
        docker_context=docker_context,
        data_root=data_root,
        wait_timeout=300,
        runner=runner,
        hello_probe=hello_probe,
    )
    preserved = actual_host.discover_source_composition(
        output,
        deployment=source_lock["deployment"]["name"],
        lock_identity=source_lock["lock_identity"],
    )
    preserved_sha256 = tuple(_sha256_file(path) for path in preserved)
    updates = _updates_root(project_root)
    updates.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".plan.", suffix=".prepare", dir=updates)
    )
    moved = False
    try:
        candidate = temporary / "candidate"
        _prepare_candidate_order(
            project_root,
            output,
            candidate,
            target_profile=target_profile,
            target_preset=target_preset,
            input_overrides=input_overrides,
            data_root=data_root,
            input_files=input_files,
        )
        resolve_project(
            candidate,
            data_root=data_root,
            allow_unverified=allow_unverified,
            allow_eol=allow_eol,
            resolved_at=source_lock["resolved_at"],
        )
        fetch_locked_artifacts(candidate, data_root=data_root)
        target_output = candidate / "generated"
        render_toml_project(candidate, target_output, data_root=data_root)
        target_lock = load_lock(candidate, data_root=data_root)
        _validate_in_place_transition(source_lock, target_lock)
        payload = _plan_payload(
            project_root=project_root,
            output=output,
            docker_context=docker_context,
            source_lock=source_lock,
            target_lock=target_lock,
            preserved_sha256=preserved_sha256,
        )
        plan = _make_plan(payload, preserved)
        if isinstance(actual_host, _DockerUpdateHost):
            actual_host.preserved_compose_files = preserved
        actual_host.validate_plan(plan, source_lock, target_lock, target_output)
        _prepare_transaction(
            plan,
            temporary_root=temporary,
            source_output=output,
        )
        moved = True
        return plan
    finally:
        if not moved and temporary.exists():
            shutil.rmtree(temporary)


def _acquire_transaction_lock(project_root: Path) -> tuple[int, Path]:
    lock_path = _updates_root(project_root) / "transaction.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o640)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        _fail(
            "update_concurrent_transaction",
            lock_path,
            "another deployment update plan or apply is already running",
        )
    return descriptor, lock_path


def _release_transaction_lock(descriptor: int) -> None:
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)


def _ensure_no_active_transaction(project_root: Path) -> None:
    active = _active_path(project_root)
    if not active.exists():
        return
    try:
        value = json.loads(active.read_text(encoding="utf-8"))
        plan_id = value["plan_id"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        _fail("update_transaction_invalid", active, str(exc))
    state = _load_state(project_root, plan_id)
    if state["phase"] != "complete":
        _fail(
            "update_transaction_active",
            _state_path(project_root, plan_id),
            f"existing update is at phase {state['phase']}; apply its exact plan id",
        )


def plan_deployment_update(
    project_root: Path,
    output: Path,
    *,
    target_profile: str,
    target_preset: str,
    input_overrides: dict[tuple[str, str], str],
    input_files: dict[str, Path] | None = None,
    docker_context: str,
    data_root: Traversable,
    allow_unverified: bool = False,
    allow_eol: bool = False,
    host: DeploymentUpdateHost | None = None,
    runner: CommandRunner = _default_runner,
    hello_probe: Any = probe_protocol_hello,
) -> DeploymentUpdatePlan:
    """Prepare one exact update while excluding concurrent plan/apply mutation."""

    project_root = project_root.resolve()
    descriptor, _lock_path = _acquire_transaction_lock(project_root)
    try:
        _ensure_no_active_transaction(project_root)
        return _plan_deployment_update_locked(
            project_root,
            output,
            target_profile=target_profile,
            target_preset=target_preset,
            input_overrides=input_overrides,
            input_files=input_files,
            docker_context=docker_context,
            data_root=data_root,
            allow_unverified=allow_unverified,
            allow_eol=allow_eol,
            host=host,
            runner=runner,
            hello_probe=hello_probe,
        )
    finally:
        _release_transaction_lock(descriptor)


def _snapshot_paths(project_root: Path, state: dict[str, Any]) -> tuple[Path, ...]:
    root = _plan_root(project_root, state["plan_id"]) / "preserved-compose"
    paths = tuple(
        root / item["snapshot"] for item in state["preserved_compose_files"]
    )
    for path, item in zip(paths, state["preserved_compose_files"], strict=True):
        if path.is_symlink() or not path.is_file() or _sha256_file(path) != item["sha256"]:
            _fail(
                "update_transaction_invalid",
                path,
                "preserved Compose snapshot does not match the plan",
            )
    return paths


def _publish_project(
    source: Path,
    destination: Path,
    output: Path,
    *,
    data_root: Traversable,
) -> None:
    loaded = load_order(source)
    previous = load_order(destination)
    previous_inputs = {
        item["path"] for item in previous.order.get("operator_inputs", [])
    }
    next_inputs = {
        item["path"] for item in loaded.order.get("operator_inputs", [])
    }
    for name in ("mc-remote.toml", "mc-remote.lock.toml"):
        _atomic_replace(destination / name, (source / name).read_bytes())
    for item in loaded.order.get("operator_inputs", []):
        relative = Path(item["path"])
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            _atomic_replace(target, (source / relative).read_bytes())
        else:
            shutil.copyfile(source / relative, target)
    for relative_source in sorted(previous_inputs - next_inputs):
        relative = Path(relative_source)
        target = destination / relative
        if target.is_symlink() or not target.is_file():
            _fail(
                "update_operator_input_cleanup_failed",
                target,
                "obsolete operator input must remain one regular non-symlink file",
            )
        target.unlink()
        parent = target.parent
        operator_root = destination / "operator"
        while parent != operator_root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    render_toml_project(destination, output, data_root=data_root)


def apply_deployment_update(
    project_root: Path,
    *,
    plan_id: str,
    confirmed: bool,
    data_root: Traversable,
    wait_timeout: int = 300,
    host: DeploymentUpdateHost | None = None,
    runner: CommandRunner = _default_runner,
    hello_probe: Any = probe_protocol_hello,
    progress=lambda _step: None,
    expected_kind: str = "release-update",
) -> DeploymentUpdateResult:
    """Apply or retry one durable same-volume release update."""

    if not confirmed:
        _fail(
            "update_confirmation_required",
            "deployment.update.confirmed",
            "live update requires explicit --yes",
        )
    if wait_timeout < 30 or wait_timeout > 1800:
        _fail(
            "update_wait_timeout_invalid",
            wait_timeout,
            "wait timeout must be between 30 and 1800 seconds",
        )
    project_root = project_root.resolve()
    descriptor, _lock_path = _acquire_transaction_lock(project_root)
    try:
        state = _load_state(project_root, plan_id)
        plan = _plan_from_state(state)
        if plan.kind != expected_kind:
            _fail(
                "update_plan_kind_mismatch",
                plan_id,
                f"plan kind {plan.kind} cannot be applied as {expected_kind}",
            )
        root = _plan_root(project_root, plan_id)
        source_project = root / "source-project"
        source_output = root / "source-render"
        candidate = root / "candidate"
        target_output = candidate / "generated"
        source_lock = load_lock(source_project, data_root=data_root)
        target_lock = load_lock(candidate, data_root=data_root)
        if (
            source_lock["lock_identity"] != plan.source_lock_identity
            or target_lock["lock_identity"] != plan.target_lock_identity
        ):
            _fail(
                "update_transaction_invalid",
                root,
                "source or target lock differs from the durable plan",
            )
        snapshots = _snapshot_paths(project_root, state)
        actual_host = host or _DockerUpdateHost(
            project_root=project_root,
            docker_context=plan.docker_context,
            data_root=data_root,
            wait_timeout=wait_timeout,
            runner=runner,
            hello_probe=hello_probe,
            preserved_compose_files=snapshots,
        )
        if isinstance(actual_host, _DockerUpdateHost):
            actual_host.preserved_compose_files = snapshots

        def advance(phase: str, last_error: dict[str, str] | None = None) -> None:
            nonlocal state
            state = {**state, "phase": phase, "last_error": last_error}
            _write_json(_state_path(project_root, plan_id), state)

        if state["phase"] == "complete":
            return DeploymentUpdateResult(
                "already-complete",
                plan_id,
                plan.source_lock_identity,
                plan.target_lock_identity,
                "complete",
            )
        if state["phase"] == "rollback-failed":
            _fail(
                "update_manual_recovery_required",
                root,
                "automatic source projection restart previously failed",
            )
        if state["phase"] == "rolled-back":
            advance("prepared")

        try:
            if state["phase"] == "prepared":
                current = verify_toml_render_output(
                    project_root,
                    plan.output,
                    data_root=data_root,
                    allow_historical_lock=True,
                )
                if current.lock["lock_identity"] != plan.source_lock_identity:
                    _fail(
                        "update_source_changed",
                        project_root,
                        "live desired state changed after the plan was prepared",
                    )
                progress("pull-target-images")
                actual_host.pull_target(plan, target_output)
                progress("stop-source-runtime")
                actual_host.stop_source(plan, source_output)
                advance("source-stopped")
            if state["phase"] == "source-stopped":
                progress("publish-target-desired-state")
                _publish_project(
                    candidate,
                    project_root,
                    plan.output,
                    data_root=data_root,
                )
                advance("target-published")
            if state["phase"] == "target-published":
                progress(f"start-target-and-wait timeout={wait_timeout}")
                actual_host.start_target(plan, plan.output)
                progress("doctor-target")
                actual_host.verify_target(plan, plan.output)
                advance("complete")
        except Exception as original:
            if state["phase"] in {"source-stopped", "target-published"}:
                progress("rollback-source-projection")
                try:
                    _publish_project(
                        source_project,
                        project_root,
                        plan.output,
                        data_root=data_root,
                    )
                    actual_host.start_source(plan, plan.output)
                    actual_host.verify_source(plan, plan.output)
                except Exception as rollback:
                    advance(
                        "rollback-failed",
                        {
                            "reason": getattr(original, "reason", type(original).__name__),
                            "path": str(getattr(original, "path", root)),
                        },
                    )
                    _fail(
                        "update_rollback_failed",
                        root,
                        f"target failed ({original}) and source restart failed ({rollback})",
                    )
                advance(
                    "rolled-back",
                    {
                        "reason": getattr(original, "reason", type(original).__name__),
                        "path": str(getattr(original, "path", root)),
                    },
                )
            if isinstance(original, DeploymentUpdateContractError):
                raise
            if hasattr(original, "reason") and hasattr(original, "path"):
                raise DeploymentUpdateContractError(
                    str(original.reason), str(original.path), str(original)
                ) from original
            raise
        return DeploymentUpdateResult(
            "complete",
            plan_id,
            plan.source_lock_identity,
            plan.target_lock_identity,
            state["phase"],
        )
    finally:
        _release_transaction_lock(descriptor)
