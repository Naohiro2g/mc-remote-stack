"""Render validated deployment state into deterministic runtime files."""

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .preset_registry import semantic_sha256
from .resolver import inspect_lock, load_lock
from .runtime_contract import MINECRAFT_RUNTIME_GID, MINECRAFT_RUNTIME_UID
from .toml_project import load_order
from .validation import Issue, LoadedProject, validate_project
from .yamlio import dump_mapping

COMPOSE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
OCI_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


class RenderError(ValueError):
    """Raised when runtime configuration cannot be rendered safely."""


class RenderContractError(RenderError):
    """Stable, fail-closed diagnostic for the TOML render boundary."""

    def __init__(self, reason: str, path: object, message: str) -> None:
        self.reason = reason
        self.path = str(path)
        super().__init__(f"{reason}: {path}: {message}")


@dataclass(frozen=True)
class TomlRenderResult:
    status: str
    adapter: str
    adapter_revision: str
    lock_identity: str
    output: Path
    paths: tuple[Path, ...]


@dataclass(frozen=True)
class TomlRenderVerification:
    lock: dict[str, Any]
    output: Path
    manifest: dict[str, Any]


def _render_fail(reason: str, path: object, message: str) -> None:
    raise RenderContractError(reason, path, message)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _component_for_role(lock: dict[str, Any], role: str) -> dict[str, Any]:
    matches = [component for component in lock["components"] if component["role"] == role]
    if len(matches) != 1:
        _render_fail(
            "render_plan_invalid",
            f"components.{role}",
            f"compose@1 requires exactly one {role} component; found {len(matches)}",
        )
    return matches[0]


def _artifact_for_component(lock: dict[str, Any], component: dict[str, Any]) -> dict[str, Any]:
    matches = [artifact for artifact in lock["artifacts"] if artifact["id"] == component["artifact"]]
    if len(matches) != 1:
        _render_fail(
            "render_plan_invalid",
            f"artifacts.{component['artifact']}",
            f"component artifact must resolve exactly once; found {len(matches)}",
        )
    return matches[0]


def _file_artifact_identity(artifact: dict[str, Any]) -> tuple[str, str]:
    if artifact["kind"] == "https-file":
        return artifact["filename"], artifact["sha256"]
    if artifact["kind"] in {"git-build", "recovery-archive-member"}:
        return artifact["output_filename"], artifact["output_sha256"]
    _render_fail(
        "unsupported_artifact_kind",
        f"artifacts.{artifact['id']}",
        f"compose@1 cannot mount {artifact['kind']} as a file",
    )


def _verify_artifact_file(artifact_store: Path, artifact: dict[str, Any]) -> tuple[str, str, Path]:
    filename, expected_sha256 = _file_artifact_identity(artifact)
    path = artifact_store / "sha256" / expected_sha256
    if not path.is_file():
        _render_fail(
            "artifact_missing",
            path,
            f"locked artifact {artifact['id']} is absent from the content-addressed store",
        )
    actual_sha256 = _sha256_file(path)
    if actual_sha256 != expected_sha256:
        _render_fail(
            "artifact_tampered",
            path,
            f"locked artifact {artifact['id']} expected {expected_sha256}, got {actual_sha256}",
        )
    return filename, expected_sha256, path


def _locked_minecraft_motd(
    lock: dict[str, Any],
    *,
    allowed_roles: frozenset[str] = frozenset({"minecraft-motd"}),
) -> str | None:
    operator_inputs = lock["operator_inputs"]
    if operator_inputs != lock["render_plan"]["operator_inputs"]:
        _render_fail(
            "render_plan_invalid",
            "render_plan.operator_inputs",
            "render plan operator inputs must exactly match the lock projection",
        )
    matches = [item for item in operator_inputs if item["role"] == "minecraft-motd"]
    if len(matches) > 1 or any(item["role"] not in allowed_roles for item in operator_inputs):
        _render_fail(
            "render_plan_invalid",
            "operator_inputs",
            "compose@1 supports only one optional minecraft-motd operator input",
        )
    if not matches:
        return None
    operator_input = matches[0]
    if (
        operator_input["adapter"] != "minecraft-motd@1"
        or operator_input["path"] != "operator/minecraft-motd/server.properties"
        or operator_input["semantic_sha256"] != semantic_sha256(operator_input["semantic"])
    ):
        _render_fail(
            "render_plan_invalid",
            "operator_inputs.minecraft-motd",
            "locked minecraft-motd adapter identity or semantic digest is invalid",
        )
    motd = operator_input["semantic"]["motd"]
    if (
        not isinstance(motd, str)
        or not motd
        or "\\" in motd
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in motd)
    ):
        _render_fail(
            "render_plan_invalid",
            "operator_inputs.minecraft-motd.semantic.motd",
            "locked MOTD is outside the safe properties subset",
        )
    return motd


def _locked_public_routes(lock: dict[str, Any]) -> dict[str, Any]:
    operator_inputs = lock["operator_inputs"]
    if operator_inputs != lock["render_plan"]["operator_inputs"]:
        _render_fail(
            "render_plan_invalid",
            "render_plan.operator_inputs",
            "render plan operator inputs must exactly match the lock projection",
        )
    matches = [item for item in operator_inputs if item["role"] == "public-routes"]
    if len(matches) != 1 or any(
        item["role"]
        not in {"public-routes", "minecraft-motd", "minecraft-server", "connection-targets"}
        for item in operator_inputs
    ):
        _render_fail(
            "render_plan_invalid",
            "operator_inputs",
            "compose@2 requires exactly one public-routes input and permits one minecraft-motd input",
        )
    operator_input = matches[0]
    semantic = operator_input["semantic"]
    if (
        operator_input["adapter"] not in {"public-routes@1", "public-routes@2"}
        or operator_input["path"] != "operator/public-routes/routes.toml"
        or operator_input["semantic_sha256"] != semantic_sha256(semantic)
    ):
        _render_fail(
            "render_plan_invalid",
            "operator_inputs.public-routes",
            "locked public-routes adapter identity or semantic digest is invalid",
        )
    expected_keys = {"homepage", "homepage_aliases", "scratch", "bridge", "minecraft"}
    if operator_input["adapter"] == "public-routes@2":
        expected_keys.add("wirescope")
    if set(semantic) != expected_keys:
        _render_fail(
            "render_plan_invalid",
            "operator_inputs.public-routes.semantic",
            "locked public routes do not contain the exact required keys",
        )
    return semantic


def _locked_connection_targets(lock: dict[str, Any]) -> list[dict[str, str]] | None:
    operator_inputs = lock["operator_inputs"]
    if operator_inputs != lock["render_plan"]["operator_inputs"]:
        _render_fail(
            "render_plan_invalid",
            "render_plan.operator_inputs",
            "render plan operator inputs must exactly match the lock projection",
        )
    matches = [item for item in operator_inputs if item["role"] == "connection-targets"]
    if len(matches) > 1:
        _render_fail(
            "render_plan_invalid",
            "operator_inputs.connection-targets",
            "compose@2 supports only one optional connection-targets operator input",
        )
    if not matches:
        return None
    operator_input = matches[0]
    semantic = operator_input["semantic"]
    if (
        operator_input["adapter"] != "connection-targets@1"
        or operator_input["path"] != "operator/connection-targets/targets.toml"
        or operator_input["semantic_sha256"] != semantic_sha256(semantic)
    ):
        _render_fail(
            "render_plan_invalid",
            "operator_inputs.connection-targets",
            "locked connection-targets adapter identity or semantic digest is invalid",
        )
    return semantic["targets"]


def _locked_minecraft_server(lock: dict[str, Any]) -> dict[str, Any]:
    matches = [
        item for item in lock["operator_inputs"] if item["role"] == "minecraft-server"
    ]
    if len(matches) != 1:
        _render_fail(
            "render_plan_invalid",
            "operator_inputs.minecraft-server",
            "compose@4 requires exactly one minecraft-server input",
        )
    operator_input = matches[0]
    semantic = operator_input["semantic"]
    if (
        operator_input["adapter"] != "minecraft-server@1"
        or operator_input["path"] != "operator/minecraft-server/server.toml"
        or operator_input["semantic_sha256"] != semantic_sha256(semantic)
    ):
        _render_fail(
            "render_plan_invalid",
            "operator_inputs.minecraft-server",
            "locked minecraft-server adapter identity or semantic digest is invalid",
        )
    return semantic


def _oci_image(lock: dict[str, Any], role: str, *, adapter: str) -> tuple[dict[str, Any], str]:
    component = _component_for_role(lock, role)
    artifact = _artifact_for_component(lock, component)
    if artifact["kind"] != "oci":
        _render_fail(
            "unsupported_artifact_kind",
            f"artifacts.{artifact['id']}",
            f"{adapter} requires an OCI {role} artifact",
        )
    if not OCI_TAG.fullmatch(artifact["version"]):
        _render_fail(
            "render_plan_invalid",
            f"artifacts.{artifact['id']}.version",
            f"{adapter} requires an explicit OCI tag-compatible version",
        )
    return artifact, f"{artifact['locator']}:{artifact['version']}@{artifact['digest']}"


def _compose_v1(
    lock: dict[str, Any],
    *,
    credential_storage: bool = False,
) -> tuple[dict[str, Any], str]:
    adapter = "compose@5" if credential_storage else "compose@1"
    runtime_component = _component_for_role(lock, "minecraft-runtime")
    paper_component = _component_for_role(lock, "paper-server")
    plugin_component = _component_for_role(lock, "mcremote-plugin")
    runtime_artifact = _artifact_for_component(lock, runtime_component)
    paper_artifact = _artifact_for_component(lock, paper_component)
    plugin_artifact = _artifact_for_component(lock, plugin_component)

    if runtime_artifact["kind"] != "oci":
        _render_fail(
            "unsupported_artifact_kind",
            f"artifacts.{runtime_artifact['id']}",
            f"{adapter} requires an OCI minecraft-runtime artifact",
        )
    if not OCI_TAG.fullmatch(runtime_artifact["version"]):
        _render_fail(
            "render_plan_invalid",
            f"artifacts.{runtime_artifact['id']}.version",
            f"{adapter} requires an explicit OCI tag-compatible version",
        )
    minecraft_version = paper_component.get("minecraft_version")
    if not isinstance(minecraft_version, str) or not minecraft_version:
        _render_fail(
            "render_plan_invalid",
            "components.paper-server.minecraft_version",
            f"{adapter} requires an explicit Minecraft target version",
        )

    artifact_store = Path(lock["runtime"]["artifact_store"])
    paper_filename, paper_sha256, paper_path = _verify_artifact_file(artifact_store, paper_artifact)
    plugin_filename, plugin_sha256, plugin_path = _verify_artifact_file(artifact_store, plugin_artifact)

    deployment_name = lock["deployment"]["name"]
    if not COMPOSE_NAME.fullmatch(deployment_name):
        _render_fail(
            "render_plan_invalid",
            "deployment.name",
            f"{adapter} deployment name must be a Compose-compatible token",
        )
    services = lock["render_plan"]["services"]
    if services != [{"id": "minecraft", "role": "minecraft"}]:
        _render_fail(
            "render_plan_invalid",
            "render_plan.services",
            f"{adapter} requires exactly the minecraft service declared by the selected profile",
        )
    service_id = services[0]["id"]
    volume_assignments = {assignment["role"]: assignment["identity"] for assignment in lock["runtime"]["volumes"]}
    volume_roles = lock["render_plan"]["volume_roles"]
    expected_volume_roles = [{"id": "minecraft-data", "kind": "world"}]
    if credential_storage:
        expected_volume_roles.extend(
            [
                {"id": "credential-store", "kind": "runtime-data"},
                {"id": "credential-revocations", "kind": "security-state"},
            ]
        )
    if volume_roles != expected_volume_roles:
        _render_fail(
            "render_plan_invalid",
            "render_plan.volume_roles",
            f"{adapter} requires its exact declared volume roles",
        )
    expected_volume_ids = {role["id"] for role in expected_volume_roles}
    if set(volume_assignments) != expected_volume_ids:
        _render_fail(
            "render_plan_invalid",
            "runtime.volumes",
            f"{adapter} requires exactly one assignment for every declared volume role",
        )
    world_identity = lock["world"]["identity"]
    network = lock["network"]
    lock_identity = lock["lock_identity"]
    motd = _locked_minecraft_motd(lock)

    image = f"{runtime_artifact['locator']}:{runtime_artifact['version']}@{runtime_artifact['digest']}"
    minecraft_volumes = [
        {
            "type": "volume",
            "source": "minecraft-data",
            "target": "/data",
        }
    ]
    if credential_storage:
        minecraft_volumes.extend(
            [
                {
                    "type": "volume",
                    "source": "credential-store",
                    "target": "/mcremote/credential-store",
                },
                {
                    "type": "volume",
                    "source": "credential-revocations",
                    "target": "/mcremote/credential-revocations",
                },
            ]
        )
    minecraft_volumes.extend(
        [
            {
                "type": "bind",
                "source": f"./{service_id}",
                "target": "/config",
                "read_only": True,
            },
            {
                "type": "bind",
                "source": str(paper_path),
                "target": f"/artifacts/{paper_filename}",
                "read_only": True,
            },
            {
                "type": "bind",
                "source": str(plugin_path),
                "target": f"/plugins/{plugin_filename}",
                "read_only": True,
            },
        ]
    )

    environment = {
        "EULA": "TRUE",
        "TYPE": "PAPER",
        "VERSION": minecraft_version,
        "PAPER_CUSTOM_JAR": f"/artifacts/{paper_filename}",
        "ONLINE_MODE": "true",
        "ENABLE_RCON": "false",
        "CREATE_CONSOLE_IN_PIPE": "true",
        "REMOVE_OLD_MODS": "true",
        "REMOVE_OLD_MODS_DEPTH": "1",
        "SKIP_DOWNLOAD_DEFAULTS": "true",
        "COPY_CONFIG_DEST": "/data",
        "SYNC_SKIP_NEWER_IN_DESTINATION": "false",
        "REPLACE_ENV_DURING_SYNC": "false",
        "LEVEL": world_identity,
    }
    if credential_storage:
        environment.update(
            {
                "UID": str(MINECRAFT_RUNTIME_UID),
                "GID": str(MINECRAFT_RUNTIME_GID),
            }
        )

    compose = {
        "name": deployment_name,
        "services": {
            service_id: {
                "image": image,
                "restart": "unless-stopped",
                "environment": environment,
                "ports": [
                    f"{network['bind_address']}:{network['java_port']}:25565/tcp",
                    f"{network['bind_address']}:{network['mcremote_port']}:25575/tcp",
                ],
                "volumes": minecraft_volumes,
                "labels": {
                    "io.mc-remote.deployment": deployment_name,
                    "io.mc-remote.environment": lock["environment"]["identity"],
                    "io.mc-remote.world": world_identity,
                    "io.mc-remote.lock": lock_identity,
                    "io.mc-remote.paper-sha256": paper_sha256,
                    "io.mc-remote.plugin-sha256": plugin_sha256,
                },
            }
        },
        "volumes": {
            role: {"name": identity, "external": True}
            for role, identity in volume_assignments.items()
        },
    }
    property_values = [
        ("enable-rcon", "false"),
        ("enforce-secure-profile", "true"),
        ("level-name", world_identity),
    ]
    if motd is not None:
        property_values.append(("motd", motd))
    property_values.extend(
        [
            ("online-mode", "true"),
            ("server-port", "25565"),
        ]
    )
    properties = f"# Generated by mcrctl {adapter}. Do not edit.\n" + "".join(
        f"{key}={value}\n" for key, value in property_values
    )
    return compose, properties


def _compose_v5(lock: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    compose, properties = _compose_v1(lock, credential_storage=True)
    minecraft_version = _component_for_role(lock, "paper-server")["minecraft_version"]
    credential_config = f'''# Generated by mcrctl compose@5. Do not edit.
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
  credential_store_path: "/mcremote/credential-store/snapshot.json"
  revocation_authority_path: "/mcremote/credential-revocations"
  max_long_lived_credentials_per_uuid: 16
'''
    return compose, {
        "minecraft/server.properties": properties,
        "minecraft/plugins/McRemote/config.yml": credential_config,
    }


def _mcremote_b2_config(*, adapter: str, minecraft_version: str) -> str:
    return f'''# Generated by mcrctl {adapter}. Do not edit.
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
  player_token_ttl_seconds: 0
  max_sessions_per_uuid: 16
'''


def _mcremote_b3_session_only_config(*, adapter: str, minecraft_version: str) -> str:
    return f'''# Generated by mcrctl {adapter}. Do not edit.
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
  credential_store_path: "/config/mcremote-session-only/store/snapshot.json"
  revocation_authority_path: "/config/mcremote-session-only/authority"
  max_long_lived_credentials_per_uuid: 16
'''


def _compose_v6(lock: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    compose, properties = _compose_v1(lock)
    minecraft_version = _component_for_role(lock, "paper-server")["minecraft_version"]
    return compose, {
        "minecraft/server.properties": properties.replace("compose@1", "compose@6"),
        "minecraft/plugins/McRemote/config.yml": _mcremote_b2_config(
            adapter="compose@6",
            minecraft_version=minecraft_version,
        ),
    }


def _compose_v2(lock: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    expected_services = [
        {"id": "caddy", "role": "caddy-edge"},
        {"id": "scratch", "role": "scratch-runtime"},
        {"id": "bridge", "role": "websocket-bridge"},
        {"id": "minecraft", "role": "minecraft"},
    ]
    if lock["render_plan"]["services"] != expected_services:
        _render_fail(
            "render_plan_invalid",
            "render_plan.services",
            "compose@2 requires the canonical caddy, scratch, bridge, and minecraft services",
        )
    expected_volume_roles = [
        {"id": "minecraft-data", "kind": "world"},
        {"id": "caddy-data", "kind": "runtime-data"},
        {"id": "caddy-config", "kind": "runtime-data"},
    ]
    if lock["render_plan"]["volume_roles"] != expected_volume_roles:
        _render_fail(
            "render_plan_invalid",
            "render_plan.volume_roles",
            "compose@2 requires the canonical Minecraft and Caddy volume roles",
        )
    volume_assignments = {
        assignment["role"]: assignment["identity"] for assignment in lock["runtime"]["volumes"]
    }
    if set(volume_assignments) != {"minecraft-data", "caddy-data", "caddy-config"}:
        _render_fail(
            "render_plan_invalid",
            "runtime.volumes",
            "compose@2 requires exactly one assignment for every declared volume role",
        )

    deployment_name = lock["deployment"]["name"]
    if not COMPOSE_NAME.fullmatch(deployment_name):
        _render_fail(
            "render_plan_invalid",
            "deployment.name",
            "compose@2 deployment name must be a Compose-compatible token",
        )
    if lock["network"]["bind_address"] != "0.0.0.0":
        _render_fail(
            "render_plan_invalid",
            "network.bind_address",
            "compose@2 public publication requires the explicit IPv4 wildcard address",
        )

    caddy_artifact, caddy_image = _oci_image(lock, "caddy-edge", adapter="compose@2")
    scratch_artifact, scratch_image = _oci_image(lock, "scratch-runtime", adapter="compose@2")
    _, bridge_image = _oci_image(lock, "websocket-bridge", adapter="compose@2")
    _, minecraft_image = _oci_image(lock, "minecraft-runtime", adapter="compose@2")
    paper_component = _component_for_role(lock, "paper-server")
    plugin_component = _component_for_role(lock, "mcremote-plugin")
    paper_artifact = _artifact_for_component(lock, paper_component)
    plugin_artifact = _artifact_for_component(lock, plugin_component)
    artifact_store = Path(lock["runtime"]["artifact_store"])
    paper_filename, paper_sha256, paper_path = _verify_artifact_file(
        artifact_store, paper_artifact
    )
    plugin_filename, plugin_sha256, plugin_path = _verify_artifact_file(
        artifact_store, plugin_artifact
    )
    minecraft_version = paper_component.get("minecraft_version")
    if not isinstance(minecraft_version, str) or not minecraft_version:
        _render_fail(
            "render_plan_invalid",
            "components.paper-server.minecraft_version",
            "compose@2 requires an explicit Minecraft target version",
        )

    routes = _locked_public_routes(lock)
    connection_targets = _locked_connection_targets(lock)
    motd = _locked_minecraft_motd(
        lock,
        allowed_roles=frozenset(
            {"public-routes", "minecraft-motd", "minecraft-server", "connection-targets"}
        ),
    )
    world_identity = lock["world"]["identity"]
    common_labels = {
        "io.mc-remote.deployment": deployment_name,
        "io.mc-remote.environment": lock["environment"]["identity"],
        "io.mc-remote.world": world_identity,
        "io.mc-remote.lock": lock["lock_identity"],
    }
    network = lock["network"]
    homepage_domains = ", ".join([routes["homepage"], *routes["homepage_aliases"]])
    caddyfile = f"""# Generated by mcrctl compose@2. Do not edit.
{homepage_domains} {{
    encode zstd gzip
    respond "McRemote public edge is healthy; homepage content is not installed." 200
}}

{routes["scratch"]} {{
    reverse_proxy scratch:8080
}}

{routes["bridge"]} {{
    reverse_proxy bridge:8080
}}
"""
    runtime_config: dict[str, Any] = {
        "bridge_url": f"wss://{routes['bridge']}",
        "default_sandbox": routes["minecraft"],
    }
    if connection_targets is not None:
        runtime_config["connection_targets"] = [
            {"id": target["id"], "label": target["label"], "sandbox": target["sandbox"]}
            for target in connection_targets
        ]
    runtime_config["connection_enabled"] = True
    runtime_config["release_identity"] = scratch_artifact["version"]

    sandbox_allowlist = {routes["minecraft"]}
    if connection_targets is not None:
        sandbox_allowlist.update(target["sandbox"] for target in connection_targets)
    property_values = [
        ("enable-rcon", "false"),
        ("enforce-secure-profile", "true"),
        ("level-name", world_identity),
    ]
    if motd is not None:
        property_values.append(("motd", motd))
    property_values.extend([("online-mode", "true"), ("server-port", "25565")])
    properties = "# Generated by mcrctl compose@2. Do not edit.\n" + "".join(
        f"{key}={value}\n" for key, value in property_values
    )

    compose = {
        "name": deployment_name,
        "services": {
            "caddy": {
                "image": caddy_image,
                "restart": "unless-stopped",
                "cap_drop": ["ALL"],
                "cap_add": ["NET_BIND_SERVICE"],
                "ports": [
                    "0.0.0.0:80:80/tcp",
                    "0.0.0.0:443:443/tcp",
                ],
                "volumes": [
                    {"type": "bind", "source": "./Caddyfile", "target": "/etc/caddy/Caddyfile", "read_only": True},
                    {"type": "volume", "source": "caddy-data", "target": "/data"},
                    {"type": "volume", "source": "caddy-config", "target": "/config"},
                ],
                "networks": ["edge", "app"],
                "labels": common_labels,
            },
            "scratch": {
                "image": scratch_image,
                "restart": "unless-stopped",
                "volumes": [
                    {
                        "type": "bind",
                        "source": "./runtime/scratch.json",
                        "target": "/usr/share/nginx/html/mc-remote-runtime-config.json",
                        "read_only": True,
                    }
                ],
                "networks": ["app"],
                "labels": common_labels,
            },
            "bridge": {
                "image": bridge_image,
                "restart": "unless-stopped",
                "environment": {
                    "BRIDGE_WS_HOST": "0.0.0.0",
                    "BRIDGE_WS_PORT": "8080",
                    "BRIDGE_ORIGIN_ALLOWLIST": f"https://{routes['scratch']}",
                    "BRIDGE_SANDBOX_ALLOWLIST": ",".join(sorted(sandbox_allowlist)),
                    "BRIDGE_DEFAULT_SANDBOX": routes["minecraft"],
                    "BRIDGE_SANDBOX_PORT": "25575",
                },
                "networks": ["app"],
                "labels": common_labels,
            },
            "minecraft": {
                "image": minecraft_image,
                "restart": "unless-stopped",
                "environment": {
                    "EULA": "TRUE",
                    "TYPE": "PAPER",
                    "VERSION": minecraft_version,
                    "PAPER_CUSTOM_JAR": f"/artifacts/{paper_filename}",
                    "ONLINE_MODE": "true",
                    "ENABLE_RCON": "false",
                    "CREATE_CONSOLE_IN_PIPE": "true",
                    "REMOVE_OLD_MODS": "true",
                    "REMOVE_OLD_MODS_DEPTH": "1",
                    "SKIP_DOWNLOAD_DEFAULTS": "true",
                    "COPY_CONFIG_DEST": "/data",
                    "SYNC_SKIP_NEWER_IN_DESTINATION": "false",
                    "REPLACE_ENV_DURING_SYNC": "false",
                    "LEVEL": world_identity,
                },
                "ports": [
                    f"0.0.0.0:{network['java_port']}:25565/tcp",
                    f"0.0.0.0:{network['java_port']}:19132/udp",
                    f"0.0.0.0:{network['mcremote_port']}:25575/tcp",
                ],
                "volumes": [
                    {"type": "volume", "source": "minecraft-data", "target": "/data"},
                    {"type": "bind", "source": "./minecraft", "target": "/config", "read_only": True},
                    {
                        "type": "bind",
                        "source": str(paper_path),
                        "target": f"/artifacts/{paper_filename}",
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": str(plugin_path),
                        "target": f"/plugins/{plugin_filename}",
                        "read_only": True,
                    },
                ],
                "networks": {"app": {"aliases": [routes["minecraft"]]}},
                "labels": {
                    **common_labels,
                    "io.mc-remote.paper-sha256": paper_sha256,
                    "io.mc-remote.plugin-sha256": plugin_sha256,
                },
            },
        },
        "networks": {
            "edge": {"internal": False, "enable_ipv6": False},
            "app": {"internal": True, "enable_ipv6": False},
        },
        "volumes": {
            role: {"name": identity, "external": True}
            for role, identity in volume_assignments.items()
        },
    }
    files_to_render = {
        "Caddyfile": caddyfile,
        "runtime/scratch.json": json.dumps(runtime_config, ensure_ascii=False, indent=2)
        + "\n",
        "minecraft/server.properties": properties,
    }
    return compose, files_to_render


def _compose_v3(lock: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    compose, rendered_files = _compose_v2(lock)
    compose["services"]["minecraft"]["networks"] = {
        "app": {
            "aliases": compose["services"]["minecraft"]["networks"]["app"][
                "aliases"
            ]
        },
        "egress": {"gw_priority": 1},
    }
    compose["networks"]["egress"] = {
        "internal": False,
        "enable_ipv6": False,
    }
    rendered_files = {
        relative: content.replace("compose@2", "compose@3")
        for relative, content in rendered_files.items()
    }
    return compose, rendered_files


def _compose_v4(lock: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    compose, rendered_files = _compose_v3(lock)
    server = _locked_minecraft_server(lock)
    properties = [
        ("allow-flight", str(server["allow_flight"]).lower()),
        ("difficulty", server["difficulty"]),
        ("enable-query", str(server["enable_query"]).lower()),
        ("enable-rcon", "false"),
        ("enable-status", str(server["enable_status"]).lower()),
        ("enforce-secure-profile", "true"),
        ("force-gamemode", str(server["force_gamemode"]).lower()),
        ("gamemode", server["gamemode"]),
        ("hardcore", str(server["hardcore"]).lower()),
        ("level-name", lock["world"]["identity"]),
        ("log-ips", str(server["log_ips"]).lower()),
        (
            "management-server-enabled",
            str(server["management_server_enabled"]).lower(),
        ),
        ("max-players", str(server["max_players"])),
        ("max-tick-time", str(server["max_tick_time"])),
        ("max-world-size", str(server["max_world_size"])),
        ("motd", server["motd"]),
        (
            "network-compression-threshold",
            str(server["network_compression_threshold"]),
        ),
        ("online-mode", "true"),
        ("server-port", "25565"),
        ("simulation-distance", str(server["simulation_distance"])),
        ("spawn-protection", str(server["spawn_protection"])),
        ("view-distance", str(server["view_distance"])),
        ("white-list", str(server["white_list"]).lower()),
    ]
    rendered_files["minecraft/server.properties"] = (
        "# Generated by mcrctl compose@4. Do not edit.\n"
        + "".join(f"{key}={value}\n" for key, value in properties)
    )
    rendered_files = {
        relative: content.replace("compose@3", "compose@4")
        for relative, content in rendered_files.items()
    }
    return compose, rendered_files


def _compose_v7(lock: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    compose, rendered_files = _compose_v4(lock)
    minecraft_version = _component_for_role(lock, "paper-server")["minecraft_version"]
    rendered_files = {
        relative: content.replace("compose@4", "compose@7")
        for relative, content in rendered_files.items()
    }
    rendered_files["minecraft/plugins/McRemote/config.yml"] = _mcremote_b2_config(
        adapter="compose@7",
        minecraft_version=minecraft_version,
    )
    return compose, rendered_files


def _compose_v8(lock: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    compose, rendered_files = _compose_v4(lock)
    minecraft_version = _component_for_role(lock, "paper-server")["minecraft_version"]
    rendered_files = {
        relative: content.replace("compose@4", "compose@8")
        for relative, content in rendered_files.items()
    }
    rendered_files["minecraft/plugins/McRemote/config.yml"] = (
        _mcremote_b3_session_only_config(
            adapter="compose@8",
            minecraft_version=minecraft_version,
        )
    )
    return compose, rendered_files


def _compose_v9(lock: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    compose, rendered_files = _compose_v8(lock)
    runtime_path = "runtime/scratch.json"
    try:
        runtime_config = json.loads(rendered_files[runtime_path])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        _render_fail(
            "scratch_runtime_config_invalid",
            runtime_path,
            f"compose@9 requires one valid Scratch runtime JSON document: {exc}",
        )
    if not isinstance(runtime_config, dict):
        _render_fail(
            "scratch_runtime_config_invalid",
            runtime_path,
            "Scratch runtime config must be a JSON object",
        )
    targets = runtime_config.get("connection_targets")
    if not isinstance(targets, list) or not targets:
        _render_fail(
            "scratch_runtime_config_invalid",
            f"{runtime_path}.connection_targets",
            "compose@9 requires a non-empty connection_targets array",
        )
    sandboxes = {
        target.get("sandbox")
        for target in targets
        if isinstance(target, dict) and isinstance(target.get("sandbox"), str)
    }
    if runtime_config.get("default_sandbox") not in sandboxes:
        _render_fail(
            "scratch_runtime_config_invalid",
            f"{runtime_path}.default_sandbox",
            "default_sandbox must be listed in connection_targets",
        )
    runtime_config["notices"] = []
    rendered_files[runtime_path] = (
        json.dumps(runtime_config, ensure_ascii=False, indent=2) + "\n"
    )
    rendered_files = {
        relative: content.replace("compose@8", "compose@9")
        for relative, content in rendered_files.items()
    }
    return compose, rendered_files


def _mcremote_b4_session_only_config(*, minecraft_version: str) -> str:
    return f'''# Generated by mcrctl compose@10. Do not edit.
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


def _compose_v10(lock: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    compose, rendered_files = _compose_v9(lock)
    minecraft_version = _component_for_role(lock, "paper-server")[
        "minecraft_version"
    ]
    rendered_files = {
        relative: content.replace("compose@9", "compose@10")
        for relative, content in rendered_files.items()
    }
    rendered_files["minecraft/plugins/McRemote/config.yml"] = (
        _mcremote_b4_session_only_config(minecraft_version=minecraft_version)
    )
    return compose, rendered_files


MAX_WIRESCOPE_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_WIRESCOPE_ASSETS = 512


def _safe_wirescope_asset_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        _render_fail("wirescope_artifact_invalid", name, "WireScope asset path is unsafe")
    return path


def _verified_wirescope_assets(
    lock: dict[str, Any],
) -> tuple[list[tuple[str, bytes]], bytes]:
    archive_component = _component_for_role(lock, "wirescope-app")
    manifest_component = _component_for_role(lock, "wirescope-manifest")
    archive_artifact = _artifact_for_component(lock, archive_component)
    manifest_artifact = _artifact_for_component(lock, manifest_component)
    artifact_store = Path(lock["runtime"]["artifact_store"])
    archive_filename, archive_sha256, archive_path = _verify_artifact_file(
        artifact_store, archive_artifact
    )
    manifest_filename, _manifest_sha256, manifest_path = _verify_artifact_file(
        artifact_store, manifest_artifact
    )
    if archive_filename != "wirescope-app.zip" or manifest_filename != (
        "wirescope-app.manifest.json"
    ):
        _render_fail(
            "wirescope_artifact_invalid",
            "artifacts.wirescope",
            "compose@11 requires the canonical WireScope archive and manifest filenames",
        )
    manifest_source = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_source)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _render_fail(
            "wirescope_manifest_invalid",
            manifest_path,
            f"detached manifest is not valid UTF-8 JSON: {exc}",
        )
    archive_record = manifest.get("archive") if isinstance(manifest, dict) else None
    protocols = manifest.get("protocols") if isinstance(manifest, dict) else None
    asset_records = manifest.get("assets") if isinstance(manifest, dict) else None
    if (
        manifest.get("manifest_schema") != "mcremote.wirescope.app-manifest"
        or manifest.get("manifest_version") != 1
        or not isinstance(archive_record, dict)
        or archive_record.get("file") != archive_filename
        or archive_record.get("format") != "zip"
        or archive_record.get("format_version") != 1
        or archive_record.get("sha256") != archive_sha256
        or not isinstance(protocols, dict)
        or protocols.get("observer_schema")
        != {"name": "mcremote.observer", "version": 1}
        or protocols.get("observer_session") != 1
        or protocols.get("scratch_handoff") != 1
        or protocols.get("station_attach") != 1
        or not isinstance(asset_records, list)
        or not asset_records
        or len(asset_records) > MAX_WIRESCOPE_ASSETS
    ):
        _render_fail(
            "wirescope_manifest_invalid",
            manifest_path,
            "detached manifest does not identify the locked b4 schema/session/handoff contract",
        )
    expected_assets: dict[str, tuple[int, str]] = {}
    for index, record in enumerate(asset_records):
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "bytes", "sha256"}
            or not isinstance(record.get("path"), str)
            or not isinstance(record.get("bytes"), int)
            or isinstance(record.get("bytes"), bool)
            or record["bytes"] < 0
            or not isinstance(record.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None
        ):
            _render_fail(
                "wirescope_manifest_invalid",
                f"assets[{index}]",
                "asset inventory entry is invalid",
            )
        asset_name = _safe_wirescope_asset_path(record["path"]).as_posix()
        if asset_name in expected_assets:
            _render_fail(
                "wirescope_manifest_invalid",
                f"assets[{index}]",
                "asset inventory contains a duplicate path",
            )
        expected_assets[asset_name] = (record["bytes"], record["sha256"])
    if "index.html" not in expected_assets:
        _render_fail(
            "wirescope_manifest_invalid",
            "assets",
            "WireScope artifact must contain index.html",
        )

    extracted: list[tuple[str, bytes]] = []
    total_bytes = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if archive.testzip() is not None:
                _render_fail(
                    "wirescope_artifact_invalid",
                    archive_path,
                    "WireScope archive failed CRC verification",
                )
            observed_names: set[str] = set()
            for info in infos:
                name = _safe_wirescope_asset_path(info.filename).as_posix()
                unix_mode = info.external_attr >> 16
                if info.is_dir() or (unix_mode and stat.S_ISLNK(unix_mode)):
                    _render_fail(
                        "wirescope_artifact_invalid",
                        name,
                        "directories and symbolic links are forbidden in the WireScope archive",
                    )
                if name in observed_names:
                    _render_fail(
                        "wirescope_artifact_invalid",
                        name,
                        "WireScope archive contains a duplicate path",
                    )
                observed_names.add(name)
                total_bytes += info.file_size
                if total_bytes > MAX_WIRESCOPE_ARCHIVE_BYTES:
                    _render_fail(
                        "wirescope_artifact_invalid",
                        archive_path,
                        "WireScope archive exceeds the extracted-size limit",
                    )
                content = archive.read(info)
                expected = expected_assets.get(name)
                if expected != (len(content), hashlib.sha256(content).hexdigest()):
                    _render_fail(
                        "wirescope_artifact_invalid",
                        name,
                        "WireScope asset does not match the detached manifest",
                    )
                extracted.append((name, content))
    except zipfile.BadZipFile as exc:
        _render_fail(
            "wirescope_artifact_invalid",
            archive_path,
            f"WireScope archive is not a valid ZIP: {exc}",
        )
    if {name for name, _content in extracted} != set(expected_assets):
        _render_fail(
            "wirescope_artifact_invalid",
            archive_path,
            "WireScope archive inventory does not exactly match the detached manifest",
        )
    return sorted(extracted), manifest_source


def _compose_v11(lock: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    compose, rendered_files = _compose_v10(lock)
    routes = _locked_public_routes(lock)
    if "wirescope" not in routes:
        _render_fail(
            "render_plan_invalid",
            "operator_inputs.public-routes.semantic.wirescope",
            "compose@11 requires the public WireScope hostname",
        )
    _verified_wirescope_assets(lock)
    runtime_path = "runtime/scratch.json"
    runtime = json.loads(rendered_files[runtime_path])
    runtime["wirescope_url"] = f"https://{routes['wirescope']}/"
    rendered_files[runtime_path] = json.dumps(runtime, ensure_ascii=False, indent=2) + "\n"

    scratch_block = (
        f"{routes['scratch']} {{\n"
        "    reverse_proxy scratch:8080\n"
        "}"
    )
    scratch_with_referrer = (
        f"{routes['scratch']} {{\n"
        '    header Referrer-Policy "strict-origin-when-cross-origin"\n'
        "    reverse_proxy scratch:8080\n"
        "}"
    )
    caddyfile = rendered_files["Caddyfile"]
    if scratch_block not in caddyfile:
        _render_fail(
            "render_plan_invalid",
            "Caddyfile.scratch",
            "compose@11 could not locate the canonical Scratch route",
        )
    caddyfile = caddyfile.replace(scratch_block, scratch_with_referrer, 1)
    caddyfile += f'''\n{routes["wirescope"]} {{
    root * /srv/wirescope
    header {{
        Cross-Origin-Opener-Policy "unsafe-none"
        Referrer-Policy "no-referrer"
        X-Content-Type-Options "nosniff"
        Cache-Control "no-store"
        Content-Security-Policy "default-src 'none'; script-src 'self'; style-src 'self'; \
connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    }}
    file_server
}}
'''
    rendered_files["Caddyfile"] = caddyfile.replace("compose@10", "compose@11")
    rendered_files = {
        relative: content.replace("compose@10", "compose@11")
        for relative, content in rendered_files.items()
    }
    compose["services"]["caddy"]["volumes"].append(
        {
            "type": "bind",
            "source": "./wirescope",
            "target": "/srv/wirescope",
            "read_only": True,
        }
    )
    return compose, rendered_files


def _write_synced(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_compose_v1(lock: dict[str, Any], staging: Path) -> tuple[str, ...]:
    compose, properties = _compose_v1(lock)
    compose_source = yaml.safe_dump(compose, sort_keys=False, allow_unicode=True).encode("utf-8")
    _write_synced(staging / "compose.yaml", compose_source)
    _write_synced(staging / "minecraft" / "server.properties", properties.encode("utf-8"))

    rendered_paths = ("compose.yaml", "minecraft/server.properties")
    manifest = {
        "schema_version": 1,
        "adapter": "compose",
        "adapter_revision": "1",
        "lock_identity": lock["lock_identity"],
        "render_plan_sha256": lock["render_plan"]["semantic_sha256"],
        "files": [
            {
                "path": relative,
                "sha256": _sha256_file(staging / PurePosixPath(relative)),
            }
            for relative in rendered_paths
        ],
    }
    manifest_source = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write_synced(staging / "render-manifest.json", manifest_source)
    _fsync_directory(staging / "minecraft")
    _fsync_directory(staging)
    return rendered_paths


def _stage_compose_v2(lock: dict[str, Any], staging: Path) -> tuple[str, ...]:
    compose, rendered_files = _compose_v2(lock)
    _write_synced(
        staging / "compose.yaml",
        yaml.safe_dump(compose, sort_keys=False, allow_unicode=True).encode("utf-8"),
    )
    for relative, content in rendered_files.items():
        _write_synced(staging / PurePosixPath(relative), content.encode("utf-8"))
    rendered_paths = ("compose.yaml", *rendered_files)
    manifest = {
        "schema_version": 1,
        "adapter": "compose",
        "adapter_revision": "2",
        "lock_identity": lock["lock_identity"],
        "render_plan_sha256": lock["render_plan"]["semantic_sha256"],
        "files": [
            {
                "path": relative,
                "sha256": _sha256_file(staging / PurePosixPath(relative)),
            }
            for relative in rendered_paths
        ],
    }
    _write_synced(
        staging / "render-manifest.json",
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    for directory in (staging / "runtime", staging / "minecraft", staging):
        _fsync_directory(directory)
    return rendered_paths


def _stage_compose_v3(lock: dict[str, Any], staging: Path) -> tuple[str, ...]:
    compose, rendered_files = _compose_v3(lock)
    _write_synced(
        staging / "compose.yaml",
        yaml.safe_dump(compose, sort_keys=False, allow_unicode=True).encode("utf-8"),
    )
    for relative, content in rendered_files.items():
        _write_synced(staging / PurePosixPath(relative), content.encode("utf-8"))
    rendered_paths = ("compose.yaml", *rendered_files)
    manifest = {
        "schema_version": 1,
        "adapter": "compose",
        "adapter_revision": "3",
        "lock_identity": lock["lock_identity"],
        "render_plan_sha256": lock["render_plan"]["semantic_sha256"],
        "files": [
            {
                "path": relative,
                "sha256": _sha256_file(staging / PurePosixPath(relative)),
            }
            for relative in rendered_paths
        ],
    }
    _write_synced(
        staging / "render-manifest.json",
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    for directory in (staging / "runtime", staging / "minecraft", staging):
        _fsync_directory(directory)
    return rendered_paths


def _stage_compose_v4(lock: dict[str, Any], staging: Path) -> tuple[str, ...]:
    compose, rendered_files = _compose_v4(lock)
    _write_synced(
        staging / "compose.yaml",
        yaml.safe_dump(compose, sort_keys=False, allow_unicode=True).encode("utf-8"),
    )
    for relative, content in rendered_files.items():
        _write_synced(staging / PurePosixPath(relative), content.encode("utf-8"))
    rendered_paths = ("compose.yaml", *rendered_files)
    manifest = {
        "schema_version": 1,
        "adapter": "compose",
        "adapter_revision": "4",
        "lock_identity": lock["lock_identity"],
        "render_plan_sha256": lock["render_plan"]["semantic_sha256"],
        "files": [
            {
                "path": relative,
                "sha256": _sha256_file(staging / PurePosixPath(relative)),
            }
            for relative in rendered_paths
        ],
    }
    _write_synced(
        staging / "render-manifest.json",
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    for directory in (staging / "runtime", staging / "minecraft", staging):
        _fsync_directory(directory)
    return rendered_paths


def _stage_compose_v5(lock: dict[str, Any], staging: Path) -> tuple[str, ...]:
    compose, rendered_files = _compose_v5(lock)
    _write_synced(
        staging / "compose.yaml",
        yaml.safe_dump(compose, sort_keys=False, allow_unicode=True).encode("utf-8"),
    )
    for relative, content in rendered_files.items():
        _write_synced(staging / PurePosixPath(relative), content.encode("utf-8"))
    rendered_paths = ("compose.yaml", *rendered_files)
    manifest = {
        "schema_version": 1,
        "adapter": "compose",
        "adapter_revision": "5",
        "lock_identity": lock["lock_identity"],
        "render_plan_sha256": lock["render_plan"]["semantic_sha256"],
        "files": [
            {
                "path": relative,
                "sha256": _sha256_file(staging / PurePosixPath(relative)),
            }
            for relative in rendered_paths
        ],
    }
    _write_synced(
        staging / "render-manifest.json",
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    for directory in (
        staging / "minecraft" / "plugins" / "McRemote",
        staging / "minecraft" / "plugins",
        staging / "minecraft",
        staging,
    ):
        _fsync_directory(directory)
    return rendered_paths


def _stage_auth_enforced_compose(
    lock: dict[str, Any],
    staging: Path,
    *,
    revision: str,
    renderer: Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, str]]],
) -> tuple[str, ...]:
    compose, rendered_files = renderer(lock)
    _write_synced(
        staging / "compose.yaml",
        yaml.safe_dump(compose, sort_keys=False, allow_unicode=True).encode("utf-8"),
    )
    for relative, content in rendered_files.items():
        _write_synced(staging / PurePosixPath(relative), content.encode("utf-8"))
    rendered_paths = ("compose.yaml", *rendered_files)
    manifest = {
        "schema_version": 1,
        "adapter": "compose",
        "adapter_revision": revision,
        "lock_identity": lock["lock_identity"],
        "render_plan_sha256": lock["render_plan"]["semantic_sha256"],
        "files": [
            {
                "path": relative,
                "sha256": _sha256_file(staging / PurePosixPath(relative)),
            }
            for relative in rendered_paths
        ],
    }
    _write_synced(
        staging / "render-manifest.json",
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    directories = {
        staging,
        staging / "minecraft",
        staging / "minecraft" / "plugins",
        staging / "minecraft" / "plugins" / "McRemote",
    }
    if "runtime/scratch.json" in rendered_files:
        directories.add(staging / "runtime")
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _fsync_directory(directory)
    return rendered_paths


def _stage_compose_v6(lock: dict[str, Any], staging: Path) -> tuple[str, ...]:
    return _stage_auth_enforced_compose(
        lock,
        staging,
        revision="6",
        renderer=_compose_v6,
    )


def _stage_compose_v7(lock: dict[str, Any], staging: Path) -> tuple[str, ...]:
    return _stage_auth_enforced_compose(
        lock,
        staging,
        revision="7",
        renderer=_compose_v7,
    )


def _stage_compose_v8(lock: dict[str, Any], staging: Path) -> tuple[str, ...]:
    return _stage_auth_enforced_compose(
        lock,
        staging,
        revision="8",
        renderer=_compose_v8,
    )


def _stage_compose_v9(lock: dict[str, Any], staging: Path) -> tuple[str, ...]:
    return _stage_auth_enforced_compose(
        lock,
        staging,
        revision="9",
        renderer=_compose_v9,
    )


def _stage_compose_v10(lock: dict[str, Any], staging: Path) -> tuple[str, ...]:
    return _stage_auth_enforced_compose(
        lock,
        staging,
        revision="10",
        renderer=_compose_v10,
    )


def _stage_compose_v11(lock: dict[str, Any], staging: Path) -> tuple[str, ...]:
    compose, rendered_files = _compose_v11(lock)
    assets, manifest_source = _verified_wirescope_assets(lock)
    _write_synced(
        staging / "compose.yaml",
        yaml.safe_dump(compose, sort_keys=False, allow_unicode=True).encode("utf-8"),
    )
    for relative, content in rendered_files.items():
        _write_synced(staging / PurePosixPath(relative), content.encode("utf-8"))
    wirescope_paths: list[str] = []
    for relative, content in assets:
        output_path = f"wirescope/{relative}"
        _write_synced(staging / PurePosixPath(output_path), content)
        wirescope_paths.append(output_path)
    detached_path = "wirescope/wirescope-app.manifest.json"
    _write_synced(staging / detached_path, manifest_source)
    wirescope_paths.append(detached_path)
    rendered_paths = ("compose.yaml", *rendered_files, *wirescope_paths)
    manifest = {
        "schema_version": 1,
        "adapter": "compose",
        "adapter_revision": "11",
        "lock_identity": lock["lock_identity"],
        "render_plan_sha256": lock["render_plan"]["semantic_sha256"],
        "files": [
            {
                "path": relative,
                "sha256": _sha256_file(staging / PurePosixPath(relative)),
            }
            for relative in rendered_paths
        ],
    }
    _write_synced(
        staging / "render-manifest.json",
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    directories = {staging}
    directories.update(path.parent for path in staging.rglob("*") if path.is_file())
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _fsync_directory(directory)
    return rendered_paths


def _stage_current(lock: dict[str, Any], staging: Path) -> tuple[str, ...]:
    revision = lock["render_plan"]["adapter_revision"]
    if revision == "1":
        return _stage_compose_v1(lock, staging)
    if revision == "2":
        return _stage_compose_v2(lock, staging)
    if revision == "3":
        return _stage_compose_v3(lock, staging)
    if revision == "4":
        return _stage_compose_v4(lock, staging)
    if revision == "5":
        return _stage_compose_v5(lock, staging)
    if revision == "6":
        return _stage_compose_v6(lock, staging)
    if revision == "7":
        return _stage_compose_v7(lock, staging)
    if revision == "8":
        return _stage_compose_v8(lock, staging)
    if revision == "9":
        return _stage_compose_v9(lock, staging)
    if revision == "10":
        return _stage_compose_v10(lock, staging)
    if revision == "11":
        return _stage_compose_v11(lock, staging)
    _render_fail("unsupported_renderer", "render_plan", f"unsupported renderer: compose@{revision}")


def _output_files(root: Path) -> set[str]:
    paths: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            _render_fail("render_output_unmanaged", path, "symlinks are forbidden in managed render output")
        if path.is_file():
            paths.add(path.relative_to(root).as_posix())
    return paths


def _load_managed_manifest(output: Path) -> dict[str, Any] | None:
    if not output.exists():
        return None
    if output.is_symlink() or not output.is_dir():
        _render_fail("render_output_unmanaged", output, "render output must be a real directory")
    actual_files = _output_files(output)
    if not actual_files:
        return None
    manifest_path = output / "render-manifest.json"
    if "render-manifest.json" not in actual_files:
        _render_fail(
            "render_output_unmanaged",
            output,
            "non-empty render output lacks render-manifest.json",
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _render_fail("render_output_tampered", manifest_path, str(exc))
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {
            "schema_version",
            "adapter",
            "adapter_revision",
            "lock_identity",
            "render_plan_sha256",
            "files",
        }
        or manifest.get("schema_version") != 1
        or manifest.get("adapter") != "compose"
        or manifest.get("adapter_revision") not in {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11"}
        or not isinstance(manifest.get("files"), list)
    ):
        _render_fail("render_output_tampered", manifest_path, "managed render manifest shape is invalid")

    expected_files = {"render-manifest.json"}
    for index, entry in enumerate(manifest["files"]):
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            _render_fail(
                "render_output_tampered",
                manifest_path,
                f"files[{index}] must contain only path and sha256",
            )
        relative = entry["path"]
        expected_sha256 = entry["sha256"]
        if not isinstance(relative, str) or not isinstance(expected_sha256, str):
            _render_fail("render_output_tampered", manifest_path, f"files[{index}] types are invalid")
        relative_path = PurePosixPath(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts or relative in expected_files:
            _render_fail("render_output_tampered", manifest_path, f"files[{index}] path is invalid")
        path = output / relative_path
        if not path.is_file() or _sha256_file(path) != expected_sha256:
            _render_fail("render_output_tampered", path, "managed file does not match its manifest digest")
        expected_files.add(relative)
    if actual_files != expected_files:
        _render_fail(
            "render_output_unmanaged",
            output,
            "render output contains files not owned by its manifest",
        )
    return manifest


def _trees_equal(left: Path, right: Path) -> bool:
    left_files = _output_files(left)
    right_files = _output_files(right)
    return left_files == right_files and all(
        (left / PurePosixPath(relative)).read_bytes()
        == (right / PurePosixPath(relative)).read_bytes()
        for relative in left_files
    )


def _validate_output_boundary(project_root: Path, output: Path, artifact_store: Path) -> None:
    if output == project_root or project_root.is_relative_to(output):
        _render_fail(
            "render_output_unsafe",
            output,
            "render output must not be the project root or one of its ancestors",
        )
    if output == artifact_store or output.is_relative_to(artifact_store) or artifact_store.is_relative_to(output):
        _render_fail(
            "render_output_unsafe",
            output,
            "render output and artifact store must not overlap",
        )


def _publish_staging(staging: Path, output: Path, *, managed: bool) -> str:
    if managed and _trees_equal(staging, output):
        return "unchanged"
    output.parent.mkdir(parents=True, exist_ok=True)
    if not managed:
        os.replace(staging, output)
        _fsync_directory(output.parent)
        return "created"

    backup_root = Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}.",
            suffix=".backup",
            dir=output.parent,
        )
    )
    backup = backup_root / "previous"
    os.replace(output, backup)
    try:
        os.replace(staging, output)
    except OSError as publish_error:
        try:
            os.replace(backup, output)
        except OSError as rollback_error:
            _render_fail(
                "render_rollback_failed",
                output,
                f"publish failed ({publish_error}); rollback also failed ({rollback_error})",
            )
        shutil.rmtree(backup_root)
        raise
    _fsync_directory(output.parent)
    shutil.rmtree(backup_root)
    return "replaced"


def _load_current_toml_render_lock(
    project_root: Path,
    *,
    data_root: Traversable,
    allow_historical_lock: bool = False,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    loaded_order = load_order(project_root)
    if allow_historical_lock:
        lock = load_lock(project_root, data_root=data_root)
        if lock["input"]["order"]["semantic_sha256"] != semantic_sha256(
            loaded_order.order
        ):
            _render_fail(
                "stale_lock",
                loaded_order.paths.lock,
                "historical migration source order changed after its lock was resolved",
            )
    else:
        inspection = inspect_lock(project_root, data_root=data_root)
        if inspection.status == "missing":
            _render_fail(
                "lock_missing",
                loaded_order.paths.lock,
                "resolve the project before render",
            )
        if inspection.status == "stale":
            _render_fail(
                "stale_lock",
                loaded_order.paths.lock,
                "order or exact bundled input changed; run mcrctl resolve explicitly",
            )
        lock = load_lock(project_root, data_root=data_root)
    adapter = lock["render_plan"]["adapter"]
    adapter_revision = lock["render_plan"]["adapter_revision"]
    if adapter != "compose" or adapter_revision not in {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11"}:
        _render_fail(
            "unsupported_renderer",
            "render_plan",
            f"unsupported renderer: {adapter}@{adapter_revision}",
        )
    return lock


def verify_toml_render_output(
    project_root: Path,
    output: Path,
    *,
    data_root: Traversable,
    allow_historical_lock: bool = False,
) -> TomlRenderVerification:
    """Verify exact managed output, optionally from an unchanged historical order."""

    project_root = project_root.resolve()
    lock = _load_current_toml_render_lock(
        project_root,
        data_root=data_root,
        allow_historical_lock=allow_historical_lock,
    )
    output = output.absolute()
    if output.is_symlink():
        _render_fail("render_output_unmanaged", output, "render output must not be a symlink")
    artifact_store = Path(lock["runtime"]["artifact_store"]).resolve()
    _validate_output_boundary(project_root, output.resolve(strict=False), artifact_store)
    manifest = _load_managed_manifest(output)
    if manifest is None:
        _render_fail(
            "render_output_missing",
            output,
            "apply requires an existing managed render; run mcrctl render explicitly",
        )
    if (
        manifest["adapter"] != lock["render_plan"]["adapter"]
        or manifest["adapter_revision"] != lock["render_plan"]["adapter_revision"]
        or
        manifest["lock_identity"] != lock["lock_identity"]
        or manifest["render_plan_sha256"] != lock["render_plan"]["semantic_sha256"]
    ):
        _render_fail(
            "render_output_not_current",
            output / "render-manifest.json",
            "managed render does not identify the current lock and render plan",
        )

    with tempfile.TemporaryDirectory(prefix="mc-remote-render-verify.") as temporary:
        expected = Path(temporary)
        _stage_current(lock, expected)
        if not _trees_equal(expected, output):
            _render_fail(
                "render_output_not_current",
                output,
                "managed files are self-consistent but not the canonical current render",
            )
    return TomlRenderVerification(lock=lock, output=output, manifest=manifest)


def render_toml_project(
    project_root: Path,
    output: Path,
    *,
    data_root: Traversable,
) -> TomlRenderResult:
    """Render a current TOML lock through Compose without touching a live runtime."""

    project_root = project_root.resolve()
    lock = _load_current_toml_render_lock(project_root, data_root=data_root)
    adapter = lock["render_plan"]["adapter"]
    adapter_revision = lock["render_plan"]["adapter_revision"]
    output = output.absolute()
    if output.is_symlink():
        _render_fail("render_output_unmanaged", output, "render output must not be a symlink")
    artifact_store = Path(lock["runtime"]["artifact_store"]).resolve()
    _validate_output_boundary(project_root, output.resolve(strict=False), artifact_store)
    managed = _load_managed_manifest(output) is not None
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}.",
            suffix=".render",
            dir=output.parent,
        )
    )
    try:
        rendered_paths = _stage_current(lock, staging)
        status = _publish_staging(staging, output, managed=managed)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return TomlRenderResult(
        status=status,
        adapter=adapter,
        adapter_revision=adapter_revision,
        lock_identity=lock["lock_identity"],
        output=output,
        paths=tuple(output / PurePosixPath(relative) for relative in rendered_paths)
        + (output / "render-manifest.json",),
    )


def _require_valid(project: LoadedProject) -> None:
    failures = [issue for issue in validate_project(project) if issue.severity == "FAIL"]
    if failures:
        details = "; ".join(f"{issue.path}: {issue.message}" for issue in failures)
        raise RenderError(f"deployment project is not renderable: {details}")


def _runtime_config(
    bridge_domain: str,
    minecraft_domain: str,
    release_identity: str,
    *,
    connection_enabled: bool = True,
) -> dict[str, Any]:
    return {
        "bridge_url": f"wss://{bridge_domain}",
        "default_sandbox": minecraft_domain,
        "connection_enabled": connection_enabled,
        "release_identity": release_identity,
    }


def _minecraft_properties(config: dict[str, Any]) -> str:
    gameplay = config["gameplay"]
    world = config["world"]
    performance = config["performance"]
    motd = "\\n".join(config["identity"]["motd"])
    properties = {
        "allow-flight": "false",
        "difficulty": "hard",
        "enable-query": "false",
        "enable-rcon": "false",
        "enable-status": "true",
        "enforce-secure-profile": "true",
        "force-gamemode": str(gameplay["force_gamemode"]).lower(),
        "gamemode": gameplay["gamemode"],
        "hardcore": str(gameplay["hardcore"]).lower(),
        "log-ips": "true",
        "management-server-enabled": "false",
        "max-players": "18",
        "max-tick-time": str(performance["max_tick_time"]),
        "max-world-size": str(world["radius_blocks"]),
        "motd": motd,
        "network-compression-threshold": str(performance["network_compression_threshold"]),
        "online-mode": "true",
        "server-port": "25565",
        "simulation-distance": "6",
        "spawn-protection": str(world["spawn_protection_radius"]),
        "view-distance": "10",
        "white-list": "false",
    }
    return "# Generated by mcrctl. Do not edit.\n" + "".join(f"{key}={value}\n" for key, value in properties.items())


def _bridge_service(image: str, scratch_domain: str, sandbox_domain: str) -> dict[str, Any]:
    return {
        "image": image,
        "restart": "unless-stopped",
        "environment": {
            "BRIDGE_WS_HOST": "0.0.0.0",
            "BRIDGE_WS_PORT": "8080",
            "BRIDGE_ORIGIN_ALLOWLIST": f"https://{scratch_domain}",
            "BRIDGE_SANDBOX_ALLOWLIST": sandbox_domain,
            "BRIDGE_DEFAULT_SANDBOX": sandbox_domain,
            "BRIDGE_SANDBOX_PORT": "25575",
        },
        "networks": ["app"],
    }


def _minecraft_service(
    *,
    channel: str,
    image: str,
    minecraft: dict[str, Any],
    locked_minecraft: dict[str, Any],
    data_path: str,
    backup_path: str,
    artifact_volumes: list[str],
    sandbox_domain: str,
    timezone: str,
) -> dict[str, Any]:
    paper = locked_minecraft["paper"]
    return {
        "image": image,
        "profiles": [channel],
        "restart": "unless-stopped",
        "stop_grace_period": f"{minecraft['stop_grace_seconds']}s",
        "environment": {
            "TZ": timezone,
            "EULA": "TRUE",
            "TYPE": "PAPER",
            "VERSION": locked_minecraft["version"],
            "PAPER_CUSTOM_JAR": f"/artifacts/{paper['filename']}",
            "UID": str(minecraft["uid"]),
            "GID": str(minecraft["gid"]),
            "MEMORY": minecraft["memory"],
            "ENABLE_RCON": "false",
            "CREATE_CONSOLE_IN_PIPE": "true",
            "STOP_DURATION": str(minecraft["stop_grace_seconds"] - 10),
            "REMOVE_OLD_MODS": "true",
            "REMOVE_OLD_MODS_DEPTH": "1",
            "SKIP_DOWNLOAD_DEFAULTS": "true",
            "COPY_CONFIG_DEST": "/data",
            "SYNC_SKIP_NEWER_IN_DESTINATION": "false",
            "REPLACE_ENV_DURING_SYNC": "false",
        },
        "ports": [
            f"{minecraft['java_port']}:25565/tcp",
            f"{minecraft['bedrock_port']}:19132/udp",
            f"{minecraft['mcremote_port']}:25575/tcp",
        ],
        "volumes": [
            f"{data_path}:/data",
            f"{backup_path}:/backup",
            f"./minecraft-{channel}:/config:ro",
            *artifact_volumes,
        ],
        "networks": {"app": {"aliases": [sandbox_domain]}},
    }


def _artifact_volumes(
    artifact_root: str,
    config_plugins: dict[str, Any],
    locked: dict[str, Any],
) -> list[str]:
    paper = locked["minecraft"]["paper"]
    volumes = [f"{artifact_root}/sha256/{paper['sha256']}:/artifacts/{paper['filename']}:ro"]
    for plugin_name in config_plugins["enabled"]:
        artifact = locked["plugins"][plugin_name]
        volumes.append(f"{artifact_root}/sha256/{artifact['sha256']}:/plugins/{artifact['filename']}:ro")
    return volumes


def _compose(project: LoadedProject) -> dict[str, Any]:
    config = project.config
    lock = project.lock
    domains = config["domains"]
    host_paths = config["host"]["paths"]
    images = lock["images"]
    beta = config["beta"]
    homepage = lock["homepage"]
    artifact_root = host_paths["artifacts"].rstrip("/")

    services: dict[str, Any] = {
        "caddy": {
            "image": images["caddy"],
            "restart": "unless-stopped",
            "cap_drop": ["ALL"],
            "cap_add": ["NET_BIND_SERVICE"],
            "ports": ["80:80/tcp", "443:443/tcp"],
            "volumes": [
                "./Caddyfile:/etc/caddy/Caddyfile:ro",
                f"{host_paths['caddy']}/data:/data",
                f"{host_paths['caddy']}/config:/config",
                f"{host_paths['homepage'].rstrip('/')}/sha256/{homepage['sha256']}:/srv/homepage:ro",
            ],
            "networks": ["edge", "app"],
        },
        "scratch-stable": {
            "image": images["scratch_stable"],
            "restart": "unless-stopped",
            "volumes": ["./runtime/stable.json:/usr/share/nginx/html/mc-remote-runtime-config.json:ro"],
            "networks": ["app"],
        },
        "scratch-beta": {
            "image": images["scratch_beta"],
            "restart": "unless-stopped",
            "volumes": ["./runtime/beta.json:/usr/share/nginx/html/mc-remote-runtime-config.json:ro"],
            "networks": ["app"],
        },
        "bridge-stable": _bridge_service(images["bridge"], domains["scratch"], domains["minecraft"]),
        "bridge-beta": _bridge_service(images["bridge"], domains["scratch_beta"], beta["domain"]),
        "minecraft-stable": _minecraft_service(
            channel="stable",
            image=images["minecraft"],
            minecraft=config["minecraft"],
            locked_minecraft=lock["minecraft"],
            data_path=host_paths["minecraft"],
            backup_path=host_paths["backup"],
            artifact_volumes=_artifact_volumes(artifact_root, config["plugins"], lock),
            sandbox_domain=domains["minecraft"],
            timezone=config["deployment"]["timezone"],
        ),
    }

    if beta.get("enabled") is True:
        services["minecraft-beta"] = _minecraft_service(
            channel="beta",
            image=lock["beta"]["image"],
            minecraft=beta["minecraft"],
            locked_minecraft=lock["beta"]["minecraft"],
            data_path=beta["paths"]["minecraft"],
            backup_path=beta["paths"]["backup"],
            artifact_volumes=_artifact_volumes(artifact_root, beta["plugins"], lock["beta"]),
            sandbox_domain=beta["domain"],
            timezone=config["deployment"]["timezone"],
        )

    return {
        "name": "mc-remote",
        "services": services,
        "networks": {"edge": {}, "app": {"internal": False}},
    }


def render_project(project: LoadedProject, output: Path) -> list[Path]:
    _require_valid(project)
    output = output.resolve()
    (output / "runtime").mkdir(parents=True, exist_ok=True)
    (output / "bridge").mkdir(parents=True, exist_ok=True)
    (output / "minecraft-stable" / "plugins" / "ServerBackup").mkdir(parents=True, exist_ok=True)

    config = project.config
    lock = project.lock
    domains = config["domains"]
    stable_runtime = _runtime_config(domains["bridge"], domains["minecraft"], lock["images"]["scratch_stable"])
    beta = config["beta"]
    beta_enabled = beta.get("enabled") is True
    beta_runtime = _runtime_config(
        domains["bridge_beta"],
        beta["domain"],
        lock["images"]["scratch_beta"],
        connection_enabled=beta_enabled,
    )
    if beta_enabled:
        (output / "minecraft-beta" / "plugins" / "ServerBackup").mkdir(parents=True, exist_ok=True)
        (output / "operations").mkdir(parents=True, exist_ok=True)

    compose_path = output / "compose.yaml"
    dump_mapping(compose_path, _compose(project))

    caddyfile = output / "Caddyfile"
    homepage_domains = ", ".join([domains["homepage"], *domains["homepage_aliases"]])
    caddyfile.write_text(
        f"""{homepage_domains} {{
    root * /srv/homepage
    encode zstd gzip
    file_server
}}

{domains["scratch"]} {{
    reverse_proxy scratch-stable:8080
}}

{domains["scratch_beta"]} {{
    reverse_proxy scratch-beta:8080
}}

{domains["bridge"]} {{
    reverse_proxy bridge-stable:8080
}}

{domains["bridge_beta"]} {{
    reverse_proxy bridge-beta:8080
}}
""",
        encoding="utf-8",
    )

    stable_path = output / "runtime" / "stable.json"
    beta_path = output / "runtime" / "beta.json"
    stable_path.write_text(json.dumps(stable_runtime, indent=2) + "\n", encoding="utf-8")
    beta_path.write_text(json.dumps(beta_runtime, indent=2) + "\n", encoding="utf-8")

    routes_path = output / "bridge" / "routes.yml"
    routes = {domains["minecraft"]: {"host": "minecraft-stable", "port": 25575}}
    if beta_enabled:
        routes[beta["domain"]] = {"host": "minecraft-beta", "port": 25575}
    dump_mapping(routes_path, {"routes": routes})

    backup = config["backup"]
    server_backup_path = output / "minecraft-stable" / "plugins" / "ServerBackup" / "config.yml"
    dump_mapping(
        server_backup_path,
        {
            "AutomaticBackups": True,
            "BackupTimer": {
                "Days": [
                    "MONDAY",
                    "TUESDAY",
                    "WEDNESDAY",
                    "THURSDAY",
                    "FRIDAY",
                    "SATURDAY",
                    "SUNDAY",
                ],
                "Times": [value.replace(":", "-") for value in backup["times"]],
            },
            "BackupWorlds": [backup["source"]],
            "DeleteOldBackups": 0,
            "BackupLimiter": 0,
            "KeepUniqueBackups": False,
            "UpdateAvailableMessage": True,
            "AutomaticUpdates": False,
            "BackupDestination": backup["output"],
            "Ftp": {"UploadBackup": False, "DeleteLocalBackup": False},
        },
    )

    server_properties_path = output / "minecraft-stable" / "server.properties"
    server_properties_path.write_text(_minecraft_properties(config), encoding="utf-8")

    bukkit_path = output / "minecraft-stable" / "bukkit.yml"
    dump_mapping(bukkit_path, {"settings": {"connection-throttle": 4000}})

    spigot_path = output / "minecraft-stable" / "spigot.yml"
    dump_mapping(spigot_path, {"settings": {"restart-on-crash": False, "restart-script": ""}})

    beta_paths: list[Path] = []
    if beta_enabled:
        beta_backup = beta["backup"]
        beta_server_backup_path = output / "minecraft-beta" / "plugins" / "ServerBackup" / "config.yml"
        dump_mapping(
            beta_server_backup_path,
            {
                "AutomaticBackups": True,
                "BackupTimer": {
                    "Days": [
                        "MONDAY",
                        "TUESDAY",
                        "WEDNESDAY",
                        "THURSDAY",
                        "FRIDAY",
                        "SATURDAY",
                        "SUNDAY",
                    ],
                    "Times": [value.replace(":", "-") for value in beta_backup["times"]],
                },
                "BackupWorlds": [beta_backup["source"]],
                "DeleteOldBackups": 0,
                "BackupLimiter": 0,
                "KeepUniqueBackups": False,
                "UpdateAvailableMessage": True,
                "AutomaticUpdates": False,
                "BackupDestination": beta_backup["output"],
                "Ftp": {"UploadBackup": False, "DeleteLocalBackup": False},
            },
        )
        beta_properties_path = output / "minecraft-beta" / "server.properties"
        beta_properties_path.write_text(_minecraft_properties(config), encoding="utf-8")
        beta_bukkit_path = output / "minecraft-beta" / "bukkit.yml"
        dump_mapping(beta_bukkit_path, {"settings": {"connection-throttle": 4000}})
        beta_spigot_path = output / "minecraft-beta" / "spigot.yml"
        dump_mapping(beta_spigot_path, {"settings": {"restart-on-crash": False, "restart-script": ""}})
        operation_paths = []
        assets = files("mc_remote_stack").joinpath("assets")
        for filename in ("use-beta.sh", "use-stable.sh"):
            operation_path = output / "operations" / filename
            operation_path.write_text(assets.joinpath(filename).read_text(encoding="utf-8"), encoding="utf-8")
            operation_path.chmod(0o755)
            operation_paths.append(operation_path)
        beta_paths = [
            beta_server_backup_path,
            beta_properties_path,
            beta_bukkit_path,
            beta_spigot_path,
            *operation_paths,
        ]

    return [
        compose_path,
        caddyfile,
        stable_path,
        beta_path,
        routes_path,
        server_backup_path,
        server_properties_path,
        bukkit_path,
        spigot_path,
        *beta_paths,
    ]


def render_issues(project: LoadedProject) -> list[Issue]:
    """Expose render blockers using the common issue contract."""
    return validate_project(project)
