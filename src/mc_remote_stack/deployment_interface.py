"""The compact Scratch--Stack deployment interface from DEC 2026-08-31-01."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import socket
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
from jsonschema.validators import validator_for

from .artifacts import default_artifact_store
from .preset_registry import (
    PresetDataError,
    evaluate_lifecycle,
    load_catalog_policy,
    semantic_sha256,
)

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
    runtime_status: str
    image_status: str
    network_status: str
    bridge_upstream_status: str
    auth_status: str


CommandRunner = Callable[[list[str], int], subprocess.CompletedProcess[str]]
PortProbe = Callable[[str, int], bool]
BridgeUpstreamProbe = Callable[[str, str, int, CommandRunner, str, int], None]
HelloProbe = Callable[[str, int, str, str, int], object]


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
        if "," in sandbox or any(character.isspace() for character in sandbox):
            _fail(
                "order_schema_invalid",
                f"{path}.sandbox",
                "must be one Bridge sandbox value without commas or whitespace",
            )
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
    policy_resource = data_root.joinpath("preset_catalog_policy.toml")
    if policy_resource.is_file():
        try:
            lifecycle = evaluate_lifecycle(
                load_catalog_policy(data_root=data_root), preset_ref
            )
        except PresetDataError as exc:
            _fail(exc.reason, exc.path, str(exc))
        if not lifecycle.new_resolve_allowed:
            _fail(
                "preset_not_offered",
                preset_ref,
                "preset lifecycle does not permit a new deployment resolution",
            )
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


def _git_blob_identity(source: bytes) -> bytes:
    header = b"blob " + str(len(source)).encode("ascii") + b"\0"
    return hashlib.sha1(header + source).digest()


def _git_tree_identity(root: Traversable) -> str:
    if not root.is_dir():
        _fail("scratch_contract_missing", root, "the locked contract directory is missing")
    try:
        children = list(root.iterdir())
    except OSError as exc:
        _fail("scratch_contract_read_failed", root, str(exc))
    entries: list[tuple[str, bool, bytes]] = []
    for child in children:
        if child.is_dir():
            identity = bytes.fromhex(_git_tree_identity(child))
            entries.append((child.name, True, identity))
        elif child.is_file():
            try:
                identity = _git_blob_identity(child.read_bytes())
            except OSError as exc:
                _fail("scratch_contract_read_failed", child, str(exc))
            entries.append((child.name, False, identity))
        else:
            _fail("scratch_contract_entry_invalid", child, "only regular files and directories are allowed")
    entries.sort(key=lambda entry: (entry[0] + ("/" if entry[1] else "")).encode("utf-8"))
    body = b"".join(
        (b"40000" if is_directory else b"100644")
        + b" "
        + name.encode("utf-8")
        + b"\0"
        + identity
        for name, is_directory, identity in entries
    )
    header = b"tree " + str(len(body)).encode("ascii") + b"\0"
    return hashlib.sha1(header + body).hexdigest()


def _load_verified_contract(
    handoff: dict[str, Any], data_root: Traversable
) -> dict[str, Any]:
    contract_root = data_root.joinpath("scratch-contracts", handoff["commit"])
    if _git_tree_identity(contract_root) != handoff["directory_tree_sha"]:
        _fail(
            "scratch_contract_tree_mismatch",
            contract_root,
            "packaged directory differs from the locked Scratch Git tree",
        )

    schema_resource = contract_root.joinpath("schema.json")
    try:
        schema_source = schema_resource.read_bytes()
    except OSError as exc:
        _fail("scratch_contract_read_failed", schema_resource, str(exc))
    if hashlib.sha256(schema_source).hexdigest() != handoff["schema_sha256"]:
        _fail("scratch_contract_digest_mismatch", schema_resource, "schema bytes are not the locked handoff")
    try:
        schema = json.loads(schema_source)
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
    except (UnicodeDecodeError, json.JSONDecodeError, SchemaError) as exc:
        _fail("scratch_contract_schema_invalid", schema_resource, str(exc))
    if not isinstance(schema, dict):
        _fail("scratch_contract_schema_invalid", schema_resource, "schema root must be an object")

    accepted = set(handoff["accepted_fixtures"])
    rejected = set(handoff["rejected_fixtures"])
    fixture_sha256 = handoff["fixture_sha256"]
    if accepted & rejected or accepted | rejected != set(fixture_sha256):
        _fail(
            "scratch_contract_fixture_set_mismatch",
            contract_root.joinpath("fixtures"),
            "accepted/rejected fixture sets must exactly equal the locked digest set",
        )
    validator = validator_class(schema)
    for relative_path, expected_sha256 in sorted(fixture_sha256.items()):
        fixture_resource = contract_root.joinpath(*PurePosixPath(relative_path).parts)
        try:
            fixture_source = fixture_resource.read_bytes()
        except OSError as exc:
            _fail("scratch_contract_read_failed", fixture_resource, str(exc))
        if hashlib.sha256(fixture_source).hexdigest() != expected_sha256:
            _fail(
                "scratch_contract_fixture_digest_mismatch",
                fixture_resource,
                "fixture bytes differ from the locked handoff",
            )
        try:
            fixture = json.loads(fixture_source)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _fail("scratch_contract_fixture_invalid", fixture_resource, str(exc))
        errors = list(validator.iter_errors(fixture))
        if relative_path in accepted and errors:
            _fail(
                "scratch_contract_fixture_result_mismatch",
                fixture_resource,
                "fixture locked as accept is rejected by the locked schema",
            )
        if relative_path in rejected and not errors:
            _fail(
                "scratch_contract_fixture_result_mismatch",
                fixture_resource,
                "fixture locked as reject is accepted by the locked schema",
            )
    return schema


def _runtime_contract(
    preset: dict[str, Any], data_root: Traversable
) -> tuple[dict[str, Any], dict[str, Any]]:
    handoff = preset["deployment_interface"]["scratch_contract"]
    schema = _load_verified_contract(handoff, data_root)

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


def _mcremote_runtime_config(minecraft_version: str) -> str:
    return f'''# Generated by mcrctl deployment-interface renderer. Seed once, then operator-editable.
api_port: 25575
luckperm_permissions:
  online: "mcr.online"
  offline: "mcr.offline"
  build.range: "mcr.build.range"
default_build_range: 1000
supported_mc_versions:
  - "{minecraft_version}"
auth:
  enforcement: true
  pair_code_ttl_seconds: 120
  session_token_ttl_seconds: 7200
  max_sessions_per_uuid: 16
  credential_store_path: "/data/plugins/McRemote/session-only/store/snapshot.json"
  revocation_authority_path: "/data/plugins/McRemote/session-only/authority"
  max_long_lived_credentials_per_uuid: 16
'''


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
    schema = _load_verified_contract(contract, root)
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
                    "COPY_CONFIG_DEST": "/data",
                    "SYNC_SKIP_NEWER_IN_DESTINATION": "true",
                    "REPLACE_ENV_DURING_SYNC": "false",
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
                        "source": "./runtime/minecraft",
                        "target": "/config",
                        "read_only": True,
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
                "networks": {
                    "app": {
                        "aliases": [target["sandbox"] for target in order["targets"]]
                    }
                },
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
        "runtime/minecraft/plugins/McRemote/config.yml": _mcremote_runtime_config(
            paper_component["minecraft_version"]
        ),
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
        "network": {
            "bind_address": preset["deployment_interface"]["bind_address"],
            "scratch_port": preset["deployment_interface"]["scratch_port"],
            "bridge_port": preset["deployment_interface"]["bridge_port"],
            "java_port": preset["deployment_interface"]["java_port"],
            "mcremote_port": preset["deployment_interface"]["mcremote_port"],
        },
        "renderer": {
            "name": "deployment-interface",
            "revision": preset["deployment_interface"]["renderer_revision"],
        },
    }
    lock["lock_identity"] = f"sha256:{semantic_sha256(lock)}"
    return PreparedDeployment(order_path.resolve(), lock, compose, rendered, schema)


def detect_apply_mode(
    containers: list[dict[str, Any]],
    *,
    expected_services: set[str],
    state_exists: bool,
    volume_exists: bool,
) -> Literal["create", "update"]:
    """Classify create/update from durable state, volume, and managed runtime."""

    if containers:
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
    if not state_exists:
        if not containers and not volume_exists:
            return "create"
        _fail(
            "deployment_state_incomplete",
            "deployment.state",
            "runtime or persistent volume exists without the current exact lock",
        )
    if not volume_exists:
        _fail(
            "deployment_state_incomplete",
            "deployment.volume",
            "current exact lock exists but its persistent world volume is missing",
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
                "record": record,
            }
        )
    return result


def _volume_exists(runner: CommandRunner, context: str, volume: str) -> bool:
    command = ["docker", "--context", context, "volume", "inspect", volume]
    try:
        result = runner(command, 30)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        _fail("deployment_volume_inspect_failed", volume, str(exc))
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    _fail(
        "deployment_volume_inspect_failed",
        volume,
        f"docker volume inspect exited with status {result.returncode}",
    )


def _load_current_lock(state_root: Path, deployment: str) -> dict[str, Any] | None:
    current_path = state_root / deployment / "current.json"
    if not current_path.exists():
        return None
    try:
        current = json.loads(current_path.read_text(encoding="utf-8"))
        render_root = Path(current["render_root"])
        lock = json.loads((render_root / "mc-remote.lock.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        _fail("deployment_state_invalid", current_path, str(exc))
    payload = {key: value for key, value in lock.items() if key != "lock_identity"}
    expected_identity = f"sha256:{semantic_sha256(payload)}"
    if (
        current.get("deployment") != deployment
        or lock.get("deployment") != deployment
        or current.get("lock_identity") != expected_identity
        or lock.get("lock_identity") != expected_identity
    ):
        _fail("deployment_state_invalid", current_path, "current state and exact lock differ")
    return lock


def _validate_stateful_transition(previous: dict[str, Any], requested: dict[str, Any]) -> None:
    previous_ref = previous.get("preset", {}).get("ref")
    requested_ref = requested.get("preset", {}).get("ref")
    previous_match = EXACT_REF.fullmatch(previous_ref) if isinstance(previous_ref, str) else None
    requested_match = EXACT_REF.fullmatch(requested_ref) if isinstance(requested_ref, str) else None
    if previous_match is None or requested_match is None:
        _fail("deployment_state_invalid", "preset.ref", "existing or requested preset identity is invalid")
    if previous_match.group("name") != requested_match.group("name"):
        _fail(
            "stateful_preset_family_change",
            "preset",
            "an existing world volume requires an explicit migration to change preset family",
        )
    if int(requested_match.group("revision")) < int(previous_match.group("revision")):
        _fail(
            "stateful_preset_downgrade",
            "preset",
            "an existing world volume requires an explicit migration to use an older preset revision",
        )


def _default_port_probe(address: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((address, port))
    except OSError:
        return False
    return True


def _required_host_ports(compose: dict[str, Any]) -> set[tuple[str, int]]:
    required: set[tuple[str, int]] = set()
    for service in compose["services"].values():
        for projection in service.get("ports", []):
            try:
                address, host_port, _container_port = projection.rsplit(":", 2)
                required.add((address, int(host_port)))
            except (AttributeError, ValueError) as exc:
                _fail("preset_network_invalid", "compose.ports", str(exc))
    return required


def _owned_host_ports(containers: list[dict[str, Any]]) -> set[tuple[str, int]]:
    owned: set[tuple[str, int]] = set()
    for container in containers:
        record = container.get("record", {})
        network = record.get("NetworkSettings") if isinstance(record, dict) else None
        ports = network.get("Ports") if isinstance(network, dict) else None
        if not isinstance(ports, dict):
            continue
        for projections in ports.values():
            if not isinstance(projections, list):
                continue
            for projection in projections:
                try:
                    owned.add((projection["HostIp"], int(projection["HostPort"])))
                except (KeyError, TypeError, ValueError):
                    continue
    return owned


def _preflight_host_ports(
    compose: dict[str, Any], containers: list[dict[str, Any]], probe: PortProbe
) -> None:
    owned = _owned_host_ports(containers)
    for address, port in sorted(_required_host_ports(compose)):
        if (address, port) in owned:
            continue
        if not probe(address, port):
            _fail(
                "deployment_port_in_use",
                f"{address}:{port}",
                "required host port is owned outside this deployment",
            )


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
    port_probe: PortProbe = _default_port_probe,
) -> InterfaceApplyResult:
    """Resolve, lock, render, and create/update one deployment from one order."""

    prepared = prepare_interface_deployment(
        order_path,
        data_root=data_root,
        artifact_store=artifact_store,
    )
    resolved_state_root = (state_root or default_interface_state_root()).resolve()
    _docker_context(runner, docker_context)

    expected_services = set(prepared.compose["services"])
    containers = _container_records(
        runner,
        docker_context,
        prepared.lock["deployment"],
    )
    volume = prepared.compose["volumes"]["minecraft-data"]["name"]
    volume_exists = _volume_exists(runner, docker_context, volume)
    previous_lock = _load_current_lock(resolved_state_root, prepared.lock["deployment"])
    mode = detect_apply_mode(
        containers,
        expected_services=expected_services,
        state_exists=previous_lock is not None,
        volume_exists=volume_exists,
    )
    if previous_lock is not None:
        _validate_stateful_transition(previous_lock, prepared.lock)
    _preflight_host_ports(prepared.compose, containers, port_probe)

    render_root = _publish_render(prepared, resolved_state_root)
    artifact_fetcher(prepared)
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


def _expected_interface_images(lock: dict[str, Any]) -> dict[str, str]:
    return {
        "scratch": _oci_image(lock, "scratch-runtime"),
        "bridge": _oci_image(lock, "websocket-bridge"),
        "minecraft": _oci_image(lock, "minecraft-runtime"),
    }


def _expected_interface_ports(lock: dict[str, Any]) -> dict[str, dict[str, list[dict[str, str]]]]:
    network = lock.get("network")
    if not isinstance(network, dict):
        _fail("deployment_lock_invalid", "lock.network", "network projection is missing")
    address = network["bind_address"]
    return {
        "scratch": {
            "8080/tcp": [{"HostIp": address, "HostPort": str(network["scratch_port"])}]
        },
        "bridge": {
            "8080/tcp": [{"HostIp": address, "HostPort": str(network["bridge_port"])}]
        },
        "minecraft": {
            "25565/tcp": [{"HostIp": address, "HostPort": str(network["java_port"])}],
            "25575/tcp": [
                {"HostIp": address, "HostPort": str(network["mcremote_port"])}
            ],
        },
    }


def _validate_interface_containers(
    containers: list[dict[str, Any]], lock: dict[str, Any]
) -> None:
    images = _expected_interface_images(lock)
    ports = _expected_interface_ports(lock)
    for container in containers:
        service = container["service"]
        record = container.get("record")
        if not isinstance(record, dict):
            _fail("deployment_runtime_inspect_failed", service, "Docker inspect record is missing")
        config = record.get("Config")
        if not isinstance(config, dict) or config.get("Image") != images[service]:
            _fail(
                "deployment_image_mismatch",
                service,
                "live container does not use the exact image reference from the lock",
            )
        state = record.get("State")
        if not isinstance(state, dict) or state.get("Running") is not True:
            _fail("deployment_runtime_not_running", service, "container is not running")
        if service == "minecraft":
            health = state.get("Health")
            if not isinstance(health, dict) or health.get("Status") != "healthy":
                _fail("deployment_runtime_unhealthy", service, "Minecraft container is not healthy")
        network = record.get("NetworkSettings")
        live_ports = network.get("Ports") if isinstance(network, dict) else None
        if not isinstance(live_ports, dict):
            _fail("deployment_network_mismatch", service, "published port data is missing")
        published = {key: value for key, value in live_ports.items() if value}
        if published != ports[service]:
            _fail(
                "deployment_network_mismatch",
                service,
                "live published ports do not match the exact lock",
            )


def _default_bridge_upstream_probe(
    container_id: str,
    sandbox: str,
    port: int,
    runner: CommandRunner,
    docker_context: str,
    timeout: int,
) -> None:
    script = (
        "const net=require('net');"
        "const s=net.createConnection({host:process.argv[1],port:Number(process.argv[2])});"
        "s.setTimeout(Number(process.argv[3])*1000);"
        "s.on('connect',()=>{s.end();process.exit(0)});"
        "s.on('timeout',()=>{s.destroy();process.exit(2)});"
        "s.on('error',()=>process.exit(3));"
    )
    _run(
        runner,
        [
            "docker",
            "--context",
            docker_context,
            "exec",
            container_id,
            "node",
            "-e",
            script,
            sandbox,
            str(port),
            str(timeout),
        ],
        timeout + 5,
        "bridge_upstream_unreachable",
    )


def _default_hello_probe(
    address: str, port: int, protocol: str, minecraft_version: str, timeout: int
) -> object:
    from .doctor import DoctorContractError, probe_protocol_hello  # noqa: PLC0415

    try:
        return probe_protocol_hello(
            address,
            port,
            protocol,
            minecraft_version,
            "__auth_probe__",
            timeout,
        )
    except DoctorContractError as exc:
        _fail(exc.reason, exc.path, str(exc))


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
    bridge_upstream_probe: BridgeUpstreamProbe = _default_bridge_upstream_probe,
    hello_probe: HelloProbe = _default_hello_probe,
) -> InterfaceDoctorResult:
    """Check the compact deployment's exact runtime, routing, and auth contract."""

    if not IDENTITY.fullmatch(deployment):
        _fail("deployment_invalid", deployment, "must be a lowercase deployment identity")
    if timeout < 1 or timeout > 30:
        _fail("doctor_timeout_invalid", "doctor.timeout", "must be between 1 and 30 seconds")
    root = data_root or files("mc_remote_stack").joinpath("data")
    resolved_state_root = (state_root or default_interface_state_root()).resolve()
    current_path = resolved_state_root / deployment / "current.json"
    lock = _load_current_lock(resolved_state_root, deployment)
    if lock is None:
        _fail("deployment_state_invalid", current_path, "current exact lock is missing")
    expected_identity = lock["lock_identity"]

    _docker_context(runner, docker_context)
    containers = _container_records(runner, docker_context, deployment)
    volume = f"{deployment}-minecraft-data"
    detect_apply_mode(
        containers,
        expected_services={"scratch", "bridge", "minecraft"},
        state_exists=True,
        volume_exists=_volume_exists(runner, docker_context, volume),
    )
    if not containers:
        _fail(
            "deployment_runtime_not_running",
            deployment,
            "the exact deployment has no running container projection",
        )
    _validate_interface_containers(containers, lock)
    bridge = next(container for container in containers if container["service"] == "bridge")
    environment = (
        bridge_environment_probe(bridge["id"], runner)
        if bridge_environment_probe is not None
        else _bridge_environment(bridge["id"], runner, docker_context)
    )
    allowlist = environment.get("BRIDGE_SANDBOX_ALLOWLIST", "")
    default_sandbox = environment.get("BRIDGE_DEFAULT_SANDBOX", "")
    bridge_port_text = environment.get("BRIDGE_SANDBOX_PORT", "")
    if default_sandbox != lock["runtime_config"].get("default_sandbox"):
        _fail(
            "bridge_default_target_mismatch",
            "bridge.BRIDGE_DEFAULT_SANDBOX",
            "Bridge default target differs from the exact Scratch runtime",
        )
    try:
        bridge_port = int(bridge_port_text)
    except ValueError:
        _fail(
            "bridge_upstream_invalid",
            "bridge.BRIDGE_SANDBOX_PORT",
            "Bridge upstream port must be an integer",
        )
    if bridge_port != 25575:
        _fail(
            "bridge_upstream_invalid",
            "bridge.BRIDGE_SANDBOX_PORT",
            "Bridge must use the McRemote container port",
        )
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
    bridge_upstream_probe(
        bridge["id"],
        default_sandbox,
        bridge_port,
        runner,
        docker_context,
        timeout,
    )

    plugin = _one_role(lock, "mcremote-plugin")
    paper = _one_role(lock, "paper-server")
    network = lock["network"]
    probe_address = network["bind_address"]
    if probe_address == "0.0.0.0":
        probe_address = "127.0.0.1"
    elif probe_address == "::":
        probe_address = "::1"
    hello = hello_probe(
        probe_address,
        network["mcremote_port"],
        plugin["protocol"],
        paper["minecraft_version"],
        timeout,
    )
    if getattr(hello, "status", None) != "auth-required":
        _fail(
            "doctor_auth_not_enforced",
            "protocol.hello",
            "token-free hello must be rejected with auth_required",
        )
    return InterfaceDoctorResult(
        deployment=deployment,
        lock_identity=expected_identity,
        scratch_runtime_status="current",
        bridge_allowlist_status="current",
        runtime_status="healthy",
        image_status="current",
        network_status="current",
        bridge_upstream_status="reachable",
        auth_status="enforced",
    )
