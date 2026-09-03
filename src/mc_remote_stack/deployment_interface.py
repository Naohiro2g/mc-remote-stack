"""The compact Scratch--Stack deployment interface from DEC 2026-08-31-01."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .artifacts import default_artifact_store
from .preset_registry import semantic_sha256

EXACT_REF = re.compile(r"^(?P<name>[a-z0-9][a-z0-9-]{0,62})@(?P<revision>[1-9][0-9]*)$")
IDENTITY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class DeploymentInterfaceError(ValueError):
    """Stable, fail-closed error for the compact deployment interface."""

    def __init__(self, reason: str, path: object, message: str) -> None:
        self.reason = reason
        self.path = str(path)
        super().__init__(f"{reason}: {path}: {message}")


@dataclass(frozen=True)
class PreparedDeployment:
    order_path: Path
    lock: dict[str, Any]
    compose: dict[str, Any]
    files: dict[str, str]
    runtime_schema: dict[str, Any]


@dataclass(frozen=True)
class InterfaceApplyResult:
    deployment: str
    mode: Literal["create", "update"]
    lock_identity: str
    render_root: Path
    runtime_config: str


@dataclass(frozen=True)
class InterfaceDoctorResult:
    deployment: str
    lock_identity: str
    scratch_runtime_status: str
    bridge_allowlist_status: str


CommandRunner = Callable[[list[str], int], subprocess.CompletedProcess[str]]


def _fail(reason: str, path: object, message: str) -> None:
    raise DeploymentInterfaceError(reason, path, message)


def _default_runner(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)


def default_interface_state_root() -> Path:
    configured = os.environ.get("MC_REMOTE_STATE_HOME")
    if configured:
        return Path(configured).expanduser().resolve() / "deployments"
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return (state_home / "mc-remote" / "deployments").resolve()


def _run(runner: CommandRunner, command: list[str], timeout: int, reason: str) -> str:
    try:
        result = runner(command, timeout)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        _fail(reason, command[0], str(exc))
    if result.returncode != 0:
        _fail(reason, " ".join(command[:4]), f"exit status {result.returncode}")
    return result.stdout


def _reject_unknown(value: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _fail("unknown_order_key", path, f"unknown keys: {', '.join(unknown)}")


def _nonempty(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("order_schema_invalid", path, "must be a non-empty string")
    return value


def _root_url(value: object, path: str, scheme: str) -> str:
    text = _nonempty(value, path)
    parsed = urlsplit(text)
    if (
        parsed.scheme != scheme
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        _fail("order_schema_invalid", path, f"must be a credential-free {scheme} root URL")
    return text if text.endswith("/") else text + "/"


def _https_url(value: object, path: str) -> str:
    text = _nonempty(value, path)
    parsed = urlsplit(text)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        _fail("order_schema_invalid", path, "must be a credential-free HTTPS URL")
    return text


def load_interface_order(order_path: Path) -> dict[str, Any]:
    """Load the one operator-owned TOML file; no includes or adjacent inputs exist."""

    order_path = order_path.resolve()
    if not order_path.is_file() or order_path.name != "mc-remote.toml":
        _fail("order_missing", order_path, "apply requires an explicit mc-remote.toml file")
    try:
        source = order_path.read_bytes()
        if source.startswith(b"\xef\xbb\xbf"):
            _fail("order_encoding_invalid", order_path, "UTF-8 BOM is forbidden")
        parsed = tomllib.loads(source.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        _fail("order_parse_failed", order_path, str(exc))
    if not isinstance(parsed, dict):
        _fail("order_schema_invalid", order_path, "top level must be a table")
    _reject_unknown(
        parsed,
        {"schema_version", "deployment", "preset", "surfaces", "targets", "notices"},
        "root",
    )
    if parsed.get("schema_version") != 1 or isinstance(parsed.get("schema_version"), bool):
        _fail("order_schema_invalid", "schema_version", "must be integer 1")
    deployment = _nonempty(parsed.get("deployment"), "deployment")
    if not IDENTITY.fullmatch(deployment):
        _fail("order_schema_invalid", "deployment", "must be a lowercase deployment identity")
    preset_ref = _nonempty(parsed.get("preset"), "preset")
    if EXACT_REF.fullmatch(preset_ref) is None:
        _fail("mutable_selector", "preset", "must use exact name@positive-revision form")

    surfaces = parsed.get("surfaces")
    if not isinstance(surfaces, dict):
        _fail("order_schema_invalid", "surfaces", "must be a table")
    _reject_unknown(surfaces, {"scratch_url", "bridge_url", "wirescope_url"}, "surfaces")
    surfaces["scratch_url"] = _root_url(surfaces.get("scratch_url"), "surfaces.scratch_url", "https")
    surfaces["bridge_url"] = _root_url(surfaces.get("bridge_url"), "surfaces.bridge_url", "wss")
    if "wirescope_url" in surfaces:
        surfaces["wirescope_url"] = _root_url(
            surfaces["wirescope_url"], "surfaces.wirescope_url", "https"
        )

    targets = parsed.get("targets")
    if not isinstance(targets, list) or not targets:
        _fail("order_schema_invalid", "targets", "must contain at least one target")
    ids: set[str] = set()
    sandboxes: set[str] = set()
    default_count = 0
    for index, target in enumerate(targets):
        path = f"targets[{index}]"
        if not isinstance(target, dict):
            _fail("order_schema_invalid", path, "must be a table")
        _reject_unknown(target, {"id", "label", "sandbox", "default"}, path)
        target_id = _nonempty(target.get("id"), f"{path}.id")
        if not IDENTITY.fullmatch(target_id):
            _fail("order_schema_invalid", f"{path}.id", "must be a lowercase identity")
        _nonempty(target.get("label"), f"{path}.label")
        sandbox = _nonempty(target.get("sandbox"), f"{path}.sandbox")
        if target_id in ids:
            _fail("target_id_duplicate", f"{path}.id", target_id)
        if sandbox in sandboxes:
            _fail("target_sandbox_duplicate", f"{path}.sandbox", sandbox)
        ids.add(target_id)
        sandboxes.add(sandbox)
        if not isinstance(target.get("default"), bool):
            _fail("order_schema_invalid", f"{path}.default", "must be boolean")
        default_count += int(target["default"])
    if default_count != 1:
        _fail("default_target_count_invalid", "targets", "exactly one target must set default=true")

    notices = parsed.get("notices", [])
    if not isinstance(notices, list) or len(notices) > 1:
        _fail("order_schema_invalid", "notices", "the initial slice permits zero or one notice")
    for index, notice in enumerate(notices):
        path = f"notices[{index}]"
        if not isinstance(notice, dict):
            _fail("order_schema_invalid", path, "must be a table")
        _reject_unknown(notice, {"heading", "body", "link"}, path)
        _nonempty(notice.get("heading"), f"{path}.heading")
        _nonempty(notice.get("body"), f"{path}.body")
        if "link" in notice:
            link = notice["link"]
            if not isinstance(link, dict):
                _fail("order_schema_invalid", f"{path}.link", "must be a table")
            _reject_unknown(link, {"href", "label"}, f"{path}.link")
            link["href"] = _https_url(link.get("href"), f"{path}.link.href")
            _nonempty(link.get("label"), f"{path}.link.label")
    return parsed


def _read_json(resource: Traversable, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(reason, resource, str(exc))
    if not isinstance(value, dict):
        _fail(reason, resource, "root must be an object")
    return value


def _load_preset(preset_ref: str, data_root: Traversable) -> tuple[dict[str, Any], str]:
    match = EXACT_REF.fullmatch(preset_ref)
    if match is None:  # already checked by the order loader
        _fail("mutable_selector", "preset", preset_ref)
    resource = data_root.joinpath(
        "preset_registry", match.group("name"), match.group("revision"), "preset.toml"
    )
    if not resource.is_file():
        _fail("preset_missing", preset_ref, "immutable preset does not exist")
    try:
        source = resource.read_bytes()
        preset = tomllib.loads(source.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        _fail("preset_invalid", resource, str(exc))
    schema = _read_json(
        files("mc_remote_stack").joinpath(
            "data", "schemas", "deployment-interface-preset.schema.json"
        ),
        "preset_schema_invalid",
    )
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(preset), key=lambda error: error.json_path)
    if errors:
        _fail("preset_invalid", resource, f"{errors[0].json_path}: {errors[0].message}")
    identity = preset["preset"]
    if identity["name"] != match.group("name") or identity["revision"] != match.group("revision"):
        _fail("preset_identity_mismatch", resource, "path and declared preset identity differ")
    return preset, hashlib.sha256(source).hexdigest()


def _runtime_contract(
    preset: dict[str, Any], data_root: Traversable
) -> tuple[dict[str, Any], dict[str, Any]]:
    handoff = preset["deployment_interface"]["scratch_contract"]
    commit = handoff["commit"]
    schema_resource = data_root.joinpath("scratch-contracts", commit, "schema.json")
    if not schema_resource.is_file():
        _fail(
            "scratch_contract_missing",
            schema_resource,
            "the returned Scratch contract directory has not been packaged",
        )
    try:
        schema_source = schema_resource.read_bytes()
    except OSError as exc:
        _fail("scratch_contract_read_failed", schema_resource, str(exc))
    if hashlib.sha256(schema_source).hexdigest() != handoff["schema_sha256"]:
        _fail("scratch_contract_digest_mismatch", schema_resource, "schema bytes are not the locked handoff")
    try:
        schema = json.loads(schema_source)
        Draft202012Validator.check_schema(schema)
    except (UnicodeDecodeError, json.JSONDecodeError, SchemaError) as exc:
        _fail("scratch_contract_schema_invalid", schema_resource, str(exc))
    if not isinstance(schema, dict):
        _fail("scratch_contract_schema_invalid", schema_resource, "schema root must be an object")

    scratch_component = _one_role(preset, "scratch-runtime")
    scratch_artifact = _artifact(preset, scratch_component["artifact"])
    if scratch_artifact["kind"] != "oci":
        _fail("scratch_image_invalid", "components.scratch-runtime", "must use one exact OCI image")
    if scratch_artifact["digest"] != handoff["image_digest"]:
        _fail(
            "scratch_image_digest_mismatch",
            "deployment_interface.scratch_contract.image_digest",
            "handoff image digest differs from the exact Scratch artifact",
        )
    return copy.deepcopy(handoff), schema


def _one_role(preset: dict[str, Any], role: str) -> dict[str, Any]:
    matches = [component for component in preset["components"] if component["role"] == role]
    if len(matches) != 1:
        _fail("preset_component_invalid", f"components.{role}", "requires exactly one component")
    return matches[0]


def _artifact(preset: dict[str, Any], artifact_id: str) -> dict[str, Any]:
    matches = [artifact for artifact in preset["artifacts"] if artifact["id"] == artifact_id]
    if len(matches) != 1:
        _fail("preset_artifact_invalid", f"artifacts.{artifact_id}", "requires exactly one artifact")
    return matches[0]


def _oci_image(preset: dict[str, Any], role: str) -> str:
    artifact = _artifact(preset, _one_role(preset, role)["artifact"])
    if artifact["kind"] != "oci":
        _fail("preset_artifact_invalid", f"components.{role}", "must reference an OCI artifact")
    return f"{artifact['locator']}@{artifact['digest']}"


def _file_artifact(preset: dict[str, Any], role: str) -> dict[str, Any]:
    artifact = _artifact(preset, _one_role(preset, role)["artifact"])
    if artifact["kind"] != "https-file":
        _fail("preset_artifact_invalid", f"components.{role}", "must reference an HTTPS file artifact")
    return artifact


def _validate_runtime(
    observed: object,
    *,
    expected: dict[str, Any],
    schema: dict[str, Any],
    bridge_allowlist: list[str],
) -> None:
    """Validate schema and the cross-component target invariant used by doctor."""

    errors = sorted(Draft202012Validator(schema).iter_errors(observed), key=lambda error: error.json_path)
    if errors:
        _fail("scratch_runtime_schema_invalid", errors[0].json_path, errors[0].message)
    if observed != expected:
        _fail("scratch_runtime_mismatch", "scratch.runtime", "live runtime differs from the exact render")
    assert isinstance(observed, dict)  # established by the schema above
    targets = observed.get("connection_targets")
    if not isinstance(targets, list):
        _fail("scratch_runtime_schema_invalid", "$.connection_targets", "must be an array")
    sandboxes = [target.get("sandbox") for target in targets if isinstance(target, dict)]
    if len(sandboxes) != len(targets) or sorted(sandboxes) != sorted(bridge_allowlist):
        _fail(
            "bridge_allowlist_mismatch",
            "bridge.sandbox_allowlist",
            "Bridge allowlist must exactly equal the Scratch target set",
        )
    if observed.get("default_sandbox") not in sandboxes:
        _fail("default_target_invalid", "scratch.runtime.default_sandbox", "must select one target")


def validate_interface_runtime(
    observed: object,
    *,
    lock: dict[str, Any],
    bridge_allowlist: str,
    data_root: Traversable | None = None,
) -> None:
    """Validate a live runtime document using only the locked Scratch handoff."""

    root = data_root or files("mc_remote_stack").joinpath("data")
    contract = lock.get("scratch_contract")
    if not isinstance(contract, dict):
        _fail("scratch_contract_missing", "lock.scratch_contract", "lock has no Scratch handoff")
    commit = contract.get("commit")
    if not isinstance(commit, str):
        _fail("scratch_contract_missing", "lock.scratch_contract.commit", "commit is missing")
    schema_resource = root.joinpath("scratch-contracts", commit, "schema.json")
    try:
        source = schema_resource.read_bytes()
    except OSError as exc:
        _fail("scratch_contract_read_failed", schema_resource, str(exc))
    if hashlib.sha256(source).hexdigest() != contract.get("schema_sha256"):
        _fail("scratch_contract_digest_mismatch", schema_resource, "schema differs from the lock")
    try:
        schema = json.loads(source)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("scratch_contract_schema_invalid", schema_resource, str(exc))
    expected = lock.get("runtime_config")
    if not isinstance(expected, dict):
        _fail("scratch_runtime_lock_invalid", "lock.runtime_config", "expected runtime is missing")
    allowlist = bridge_allowlist.split(",") if bridge_allowlist else []
    _validate_runtime(
        observed,
        expected=expected,
        schema=schema,
        bridge_allowlist=allowlist,
    )


def _render(
    order: dict[str, Any],
    preset: dict[str, Any],
    contract: dict[str, Any],
    schema: dict[str, Any],
    artifact_store: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    config = preset["deployment_interface"]
    targets = [
        {"id": target["id"], "label": target["label"], "sandbox": target["sandbox"]}
        for target in order["targets"]
    ]
    default = next(target for target in order["targets"] if target["default"])
    runtime: dict[str, Any] = {
        "schema_version": 1,
        "connection_enabled": True,
        "bridge_url": order["surfaces"]["bridge_url"],
        "default_sandbox": default["sandbox"],
        "connection_targets": targets,
    }
    if "wirescope_url" in order["surfaces"]:
        runtime["wirescope_url"] = order["surfaces"]["wirescope_url"]
    if order.get("notices"):
        runtime["notices"] = copy.deepcopy(order["notices"])
    _validate_runtime(
        runtime,
        expected=runtime,
        schema=schema,
        bridge_allowlist=[target["sandbox"] for target in order["targets"]],
    )

    paper = _file_artifact(preset, "paper-server")
    plugin = _file_artifact(preset, "mcremote-plugin")
    paper_component = _one_role(preset, "paper-server")
    digest_store = artifact_store / "sha256"
    labels = {
        "io.mc-remote.deployment": order["deployment"],
        "io.mc-remote.interface": "2026-08-31-01",
    }
    sandbox_allowlist = ",".join(target["sandbox"] for target in order["targets"])
    origin = urlsplit(order["surfaces"]["scratch_url"])
    scratch_origin = f"{origin.scheme}://{origin.netloc}"
    compose = {
        "name": order["deployment"],
        "services": {
            "scratch": {
                "image": _oci_image(preset, "scratch-runtime"),
                "restart": "unless-stopped",
                "ports": [f"{config['bind_address']}:{config['scratch_port']}:8080/tcp"],
                "volumes": [
                    {
                        "type": "bind",
                        "source": "./runtime/scratch.json",
                        "target": contract["container_mount_path"],
                        "read_only": True,
                    }
                ],
                "networks": ["app"],
                "labels": labels,
            },
            "bridge": {
                "image": _oci_image(preset, "websocket-bridge"),
                "restart": "unless-stopped",
                "ports": [f"{config['bind_address']}:{config['bridge_port']}:8080/tcp"],
                "environment": {
                    "BRIDGE_WS_HOST": "0.0.0.0",
                    "BRIDGE_WS_PORT": "8080",
                    "BRIDGE_ORIGIN_ALLOWLIST": scratch_origin,
                    "BRIDGE_SANDBOX_ALLOWLIST": sandbox_allowlist,
                    "BRIDGE_DEFAULT_SANDBOX": default["sandbox"],
                    "BRIDGE_SANDBOX_PORT": "25575",
                },
                "networks": ["app"],
                "labels": labels,
            },
            "minecraft": {
                "image": _oci_image(preset, "minecraft-runtime"),
                "restart": "unless-stopped",
                "environment": {
                    "EULA": "TRUE",
                    "TYPE": "PAPER",
                    "VERSION": paper_component["minecraft_version"],
                    "PAPER_CUSTOM_JAR": f"/artifacts/{paper['filename']}",
                    "ONLINE_MODE": "true",
                    "ENABLE_RCON": "false",
                    "CREATE_CONSOLE_IN_PIPE": "true",
                    "SKIP_DOWNLOAD_DEFAULTS": "true",
                },
                "ports": [
                    f"{config['bind_address']}:{config['java_port']}:25565/tcp",
                    f"{config['bind_address']}:{config['mcremote_port']}:25575/tcp",
                ],
                "volumes": [
                    {
                        "type": "volume",
                        "source": "minecraft-data",
                        "target": "/data",
                    },
                    {
                        "type": "bind",
                        "source": str(digest_store / paper["sha256"]),
                        "target": f"/artifacts/{paper['filename']}",
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": str(digest_store / plugin["sha256"]),
                        "target": f"/plugins/{plugin['filename']}",
                        "read_only": True,
                    },
                ],
                "networks": ["app"],
                "labels": labels,
            },
        },
        "networks": {"app": {"internal": False, "enable_ipv6": False}},
        "volumes": {
            "minecraft-data": {"name": f"{order['deployment']}-minecraft-data"}
        },
    }
    rendered = {
        "runtime/scratch.json": json.dumps(runtime, ensure_ascii=False, indent=2) + "\n",
        "compose.yaml": yaml.safe_dump(compose, sort_keys=False),
    }
    return compose, rendered


def prepare_interface_deployment(
    order_path: Path,
    *,
    data_root: Traversable | None = None,
    artifact_store: Path | None = None,
) -> PreparedDeployment:
    """Resolve and render the compact order without mutating a deployment."""

    root = data_root or files("mc_remote_stack").joinpath("data")
    order = load_interface_order(order_path)
    preset, preset_sha256 = _load_preset(order["preset"], root)
    contract, schema = _runtime_contract(preset, root)
    resolved_store = (artifact_store or default_artifact_store().parent).resolve()
    compose, rendered = _render(order, preset, contract, schema, resolved_store)
    lock: dict[str, Any] = {
        "schema_version": 1,
        "contract": "2026-08-31-01",
        "deployment": order["deployment"],
        "order_sha256": semantic_sha256(order),
        "preset": {"ref": order["preset"], "content_sha256": preset_sha256},
        "scratch_contract": contract,
        "artifact_store": str(resolved_store),
        "components": copy.deepcopy(preset["components"]),
        "artifacts": copy.deepcopy(preset["artifacts"]),
        "targets": copy.deepcopy(order["targets"]),
        "surfaces": copy.deepcopy(order["surfaces"]),
        "runtime_config": json.loads(rendered["runtime/scratch.json"]),
        "bridge_allowlist": [target["sandbox"] for target in order["targets"]],
        "renderer": {
            "name": "deployment-interface",
            "revision": preset["deployment_interface"]["renderer_revision"],
        },
    }
    lock["lock_identity"] = f"sha256:{semantic_sha256(lock)}"
    return PreparedDeployment(order_path.resolve(), lock, compose, rendered, schema)


def detect_apply_mode(
    containers: list[dict[str, str]], *, expected_services: set[str]
) -> Literal["create", "update"]:
    """Classify create/update from the exact managed Compose projection."""

    if not containers:
        return "create"
    services = {container.get("service", "") for container in containers}
    if (
        len(containers) != len(expected_services)
        or services != expected_services
        or any(container.get("managed") is not True for container in containers)
    ):
        _fail(
            "deployment_runtime_unmanaged",
            "docker.containers",
            "existing Compose projection does not exactly match the preset services",
        )
    return "update"


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_render(prepared: PreparedDeployment, state_root: Path) -> Path:
    lock_suffix = prepared.lock["lock_identity"].removeprefix("sha256:")
    render_root = state_root / prepared.lock["deployment"] / "renders" / lock_suffix
    for relative, content in prepared.files.items():
        _atomic_write(render_root / PurePosixPath(relative), content.encode("utf-8"))
    _atomic_write(
        render_root / "mc-remote.lock.json",
        (json.dumps(prepared.lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
    )
    return render_root


def _docker_context(runner: CommandRunner, context: str) -> None:
    source = _run(
        runner,
        ["docker", "context", "inspect", context],
        30,
        "docker_context_unavailable",
    )
    try:
        records = json.loads(source)
        host = records[0]["Endpoints"]["docker"]["Host"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        _fail("docker_context_unavailable", context, str(exc))
    if not isinstance(host, str) or not host.startswith("unix://"):
        _fail("docker_context_not_local", context, "apply requires a local Unix Docker context")


def _container_records(
    runner: CommandRunner, context: str, deployment: str
) -> list[dict[str, Any]]:
    prefix = ["docker", "--context", context]
    source = _run(
        runner,
        prefix
        + [
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={deployment}",
        ],
        30,
        "deployment_runtime_inspect_failed",
    )
    result: list[dict[str, Any]] = []
    for container_id in [line.strip() for line in source.splitlines() if line.strip()]:
        inspected = _run(
            runner,
            prefix + ["inspect", container_id],
            30,
            "deployment_runtime_inspect_failed",
        )
        try:
            record = json.loads(inspected)[0]
            labels = record["Config"]["Labels"]
            service = labels["com.docker.compose.service"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            _fail("deployment_runtime_unmanaged", container_id, str(exc))
        result.append(
            {
                "id": container_id,
                "service": service,
                "managed": labels.get("io.mc-remote.interface") == "2026-08-31-01",
            }
        )
    return result


def _fetch_interface_artifacts(prepared: PreparedDeployment) -> None:
    # Reuse the bounded HTTPS/digest implementation. Interface locks contain the
    # same immutable artifact records, but do not expose a separate operator step.
    from .artifacts import _default_open_url, _download_locked_artifact

    digest_store = Path(prepared.lock["artifact_store"]) / "sha256"
    digest_store.mkdir(parents=True, exist_ok=True)
    for artifact in prepared.lock["artifacts"]:
        if artifact["kind"] == "https-file":
            _download_locked_artifact(
                artifact,
                digest_store=digest_store,
                open_url=_default_open_url,
            )


def apply_interface_order(
    order_path: Path,
    *,
    data_root: Traversable | None = None,
    state_root: Path | None = None,
    artifact_store: Path | None = None,
    docker_context: str = "default",
    runner: CommandRunner = _default_runner,
    artifact_fetcher: Callable[[PreparedDeployment], None] = _fetch_interface_artifacts,
) -> InterfaceApplyResult:
    """Resolve, lock, render, and create/update one deployment from one order."""

    prepared = prepare_interface_deployment(
        order_path,
        data_root=data_root,
        artifact_store=artifact_store,
    )
    resolved_state_root = (state_root or default_interface_state_root()).resolve()
    render_root = _publish_render(prepared, resolved_state_root)
    artifact_fetcher(prepared)
    _docker_context(runner, docker_context)

    expected_services = set(prepared.compose["services"])
    containers = _container_records(
        runner,
        docker_context,
        prepared.lock["deployment"],
    )
    mode = detect_apply_mode(containers, expected_services=expected_services)
    compose = [
        "docker",
        "--context",
        docker_context,
        "compose",
        "--ansi",
        "never",
        "--project-directory",
        str(render_root),
        "--file",
        str(render_root / "compose.yaml"),
    ]
    _run(runner, compose + ["config", "--quiet"], 60, "compose_config_invalid")
    _run(runner, compose + ["pull", "--quiet"], 600, "artifact_pull_failed")
    _run(
        runner,
        compose + ["up", "--detach", "--remove-orphans", "--wait"],
        600,
        "deployment_apply_failed",
    )
    current = {
        "schema_version": 1,
        "deployment": prepared.lock["deployment"],
        "lock_identity": prepared.lock["lock_identity"],
        "render_root": str(render_root),
    }
    _atomic_write(
        resolved_state_root / prepared.lock["deployment"] / "current.json",
        (json.dumps(current, indent=2, sort_keys=True) + "\n").encode(),
    )
    return InterfaceApplyResult(
        deployment=prepared.lock["deployment"],
        mode=mode,
        lock_identity=prepared.lock["lock_identity"],
        render_root=render_root,
        runtime_config=prepared.files["runtime/scratch.json"],
    )


def _bridge_environment(
    container_id: str, runner: CommandRunner, docker_context: str
) -> dict[str, str]:
    source = _run(
        runner,
        ["docker", "--context", docker_context, "inspect", container_id],
        30,
        "deployment_runtime_inspect_failed",
    )
    try:
        environment = json.loads(source)[0]["Config"]["Env"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        _fail("bridge_environment_invalid", container_id, str(exc))
    result: dict[str, str] = {}
    for item in environment:
        key, separator, value = item.partition("=")
        if separator:
            result[key] = value
    return result


def doctor_interface_deployment(
    deployment: str,
    *,
    data_root: Traversable | None = None,
    state_root: Path | None = None,
    docker_context: str = "default",
    timeout: int = 5,
    runner: CommandRunner = _default_runner,
    runtime_probe: Callable[[str, int], object] | None = None,
    bridge_environment_probe: Callable[[str, CommandRunner], dict[str, str]] | None = None,
) -> InterfaceDoctorResult:
    """Check live Scratch config against its locked handoff and Bridge allowlist."""

    if not IDENTITY.fullmatch(deployment):
        _fail("deployment_invalid", deployment, "must be a lowercase deployment identity")
    if timeout < 1 or timeout > 30:
        _fail("doctor_timeout_invalid", "doctor.timeout", "must be between 1 and 30 seconds")
    root = data_root or files("mc_remote_stack").joinpath("data")
    resolved_state_root = (state_root or default_interface_state_root()).resolve()
    current_path = resolved_state_root / deployment / "current.json"
    try:
        current = json.loads(current_path.read_text(encoding="utf-8"))
        render_root = Path(current["render_root"])
        lock = json.loads((render_root / "mc-remote.lock.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        _fail("deployment_state_invalid", current_path, str(exc))
    identity_payload = {key: value for key, value in lock.items() if key != "lock_identity"}
    expected_identity = f"sha256:{semantic_sha256(identity_payload)}"
    if current.get("lock_identity") != expected_identity or lock.get("lock_identity") != expected_identity:
        _fail("deployment_lock_invalid", current_path, "current state and exact lock differ")

    _docker_context(runner, docker_context)
    containers = _container_records(runner, docker_context, deployment)
    detect_apply_mode(containers, expected_services={"scratch", "bridge", "minecraft"})
    bridge = next(container for container in containers if container["service"] == "bridge")
    environment = (
        bridge_environment_probe(bridge["id"], runner)
        if bridge_environment_probe is not None
        else _bridge_environment(bridge["id"], runner, docker_context)
    )
    allowlist = environment.get("BRIDGE_SANDBOX_ALLOWLIST", "")
    if runtime_probe is None:
        from .doctor import probe_scratch_runtime_config  # noqa: PLC0415

        runtime_probe = probe_scratch_runtime_config
    observed = runtime_probe(lock["surfaces"]["scratch_url"] + "mc-remote-runtime-config.json", timeout)
    validate_interface_runtime(
        observed,
        lock=lock,
        bridge_allowlist=allowlist,
        data_root=root,
    )
    return InterfaceDoctorResult(
        deployment=deployment,
        lock_identity=expected_identity,
        scratch_runtime_status="current",
        bridge_allowlist_status="current",
    )
