"""Read-only desired-state, Docker runtime, and protocol checks."""

from __future__ import annotations

import json
import socket
import subprocess
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .apply import DOCKER_CONTEXT, compose_render_status
from .render import (
    RenderContractError,
    _locked_public_routes,
    verify_toml_render_output,
)

MAX_HELLO_BYTES = 64 * 1024
MAX_SCRATCH_RUNTIME_BYTES = 64 * 1024
MCREMOTE_B2_SHA256 = "ad2674fa93645cc3c4c0d2b6aa5b37f11a8f9519162f61ac00b8be7122b023c7"


class CommandRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]: ...


class SocketLike(Protocol):
    def __enter__(self) -> SocketLike: ...

    def __exit__(self, *_args: object) -> object: ...

    def sendall(self, value: bytes) -> None: ...

    def recv(self, size: int) -> bytes: ...


class Connector(Protocol):
    def __call__(
        self,
        address: tuple[str, int],
        timeout: int,
    ) -> SocketLike: ...


@dataclass(frozen=True)
class ProtocolHelloResult:
    status: str
    protocol: str | None
    minecraft_version: str | None


@dataclass(frozen=True)
class TomlDoctorResult:
    deployment: str
    environment: str
    lock_identity: str
    docker_context: str
    runtime_status: str
    render_status: str
    network_scope: str
    bind_address: str
    java_port: int
    mcremote_port: int
    protocol_status: str
    protocol: str | None
    minecraft_version: str | None
    compatibility_status: str
    scratch_runtime_status: str = "not-applicable"


class DoctorContractError(ValueError):
    """Stable, fail-closed diagnostic for read-only live checks."""

    def __init__(self, reason: str, path: object, message: str) -> None:
        self.reason = reason
        self.path = str(path)
        super().__init__(f"{reason}: {path}: {message}")


def _fail(reason: str, path: object, message: str) -> None:
    raise DoctorContractError(reason, path, message)


def _default_runner(
    command: list[str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run(
    runner: CommandRunner,
    command: list[str],
    *,
    timeout: int,
    reason: str,
    path: object,
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(command, timeout)
    except FileNotFoundError:
        _fail("docker_unavailable", command[0], "Docker CLI is required for doctor")
    except subprocess.TimeoutExpired:
        _fail(reason, path, f"Docker command timed out after {timeout} seconds")
    except OSError as exc:
        _fail(reason, path, f"cannot execute Docker command: {exc}")
    if result.returncode != 0:
        _fail(reason, path, f"Docker command failed with exit status {result.returncode}")
    return result


def probe_scratch_runtime_config(url: str, timeout: int) -> object:
    """Fetch one public Scratch runtime document without browser credentials."""

    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "mcrctl-doctor/1",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            source = response.read(MAX_SCRATCH_RUNTIME_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        _fail(
            "doctor_scratch_runtime_unavailable",
            url,
            f"cannot fetch the public Scratch runtime config: {exc}",
        )
    if len(source) > MAX_SCRATCH_RUNTIME_BYTES:
        _fail(
            "doctor_scratch_runtime_invalid",
            url,
            "Scratch runtime config exceeded the 64 KiB limit",
        )
    try:
        return json.loads(source)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(
            "doctor_scratch_runtime_invalid",
            url,
            f"Scratch runtime config is not valid UTF-8 JSON: {exc}",
        )


def validate_scratch_runtime_config(
    observed: object,
    *,
    expected: dict[str, Any],
) -> None:
    """Validate the live Scratch route contract before exact desired-state comparison."""

    if not isinstance(observed, dict):
        _fail(
            "doctor_scratch_runtime_invalid",
            "scratch.runtime",
            "runtime config must be a JSON object",
        )
    targets = observed.get("connection_targets")
    if not isinstance(targets, list) or not targets:
        _fail(
            "doctor_scratch_runtime_invalid",
            "scratch.runtime.connection_targets",
            "connection_targets must be a non-empty array",
        )
    sandboxes: set[str] = set()
    ids: set[str] = set()
    for index, target in enumerate(targets):
        if not isinstance(target, dict) or set(target) != {"id", "label", "sandbox"}:
            _fail(
                "doctor_scratch_runtime_invalid",
                f"scratch.runtime.connection_targets[{index}]",
                "target must contain exactly id, label, and sandbox",
            )
        if any(not isinstance(target[key], str) or not target[key].strip() for key in target):
            _fail(
                "doctor_scratch_runtime_invalid",
                f"scratch.runtime.connection_targets[{index}]",
                "target fields must be non-empty strings",
            )
        if target["id"] in ids or target["sandbox"] in sandboxes:
            _fail(
                "doctor_scratch_runtime_invalid",
                f"scratch.runtime.connection_targets[{index}]",
                "target ids and sandbox values must be unique",
            )
        ids.add(target["id"])
        sandboxes.add(target["sandbox"])
    if observed.get("default_sandbox") not in sandboxes:
        _fail(
            "doctor_scratch_runtime_invalid",
            "scratch.runtime.default_sandbox",
            "default_sandbox must be listed in connection_targets",
        )
    notices = observed.get("notices")
    if not isinstance(notices, list):
        _fail(
            "doctor_scratch_runtime_invalid",
            "scratch.runtime.notices",
            "runtime config requires a notices array",
        )
    for index, notice in enumerate(notices):
        if not isinstance(notice, dict) or not {"heading", "body"} <= set(notice):
            _fail(
                "doctor_scratch_runtime_invalid",
                f"scratch.runtime.notices[{index}]",
                "notice requires heading and body",
            )
        if any(
            not isinstance(notice[key], str) or not notice[key].strip()
            for key in ("heading", "body")
        ):
            _fail(
                "doctor_scratch_runtime_invalid",
                f"scratch.runtime.notices[{index}]",
                "notice heading and body must be non-empty strings",
            )
    if observed != expected:
        _fail(
            "doctor_scratch_runtime_mismatch",
            "scratch.runtime",
            "public Scratch runtime config does not match the current canonical render",
        )


def _nonempty_lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _single_inspect_record(
    result: subprocess.CompletedProcess[str],
    *,
    reason: str,
    path: object,
) -> dict[str, Any]:
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        _fail(reason, path, "Docker inspect output is not valid JSON")
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        _fail(reason, path, "Docker inspect must return exactly one object")
    return value[0]


def _component_value(lock: dict[str, Any], role: str, key: str) -> str:
    matches = [
        component.get(key)
        for component in lock["components"]
        if component.get("role") == role
    ]
    if len(matches) != 1 or not isinstance(matches[0], str) or not matches[0]:
        _fail(
            "doctor_contract_unsupported",
            f"components.{role}",
            f"doctor requires exactly one {role} component with {key}",
        )
    return matches[0]


def _auth_enforcement_required(lock: dict[str, Any]) -> bool:
    controls = lock["render_plan"].get("required_security_controls", [])
    if "mcremote-auth-enforced" in controls:
        return True
    components = [
        component
        for component in lock["components"]
        if component.get("role") == "mcremote-plugin"
    ]
    if len(components) != 1:
        return False
    artifact_id = components[0].get("artifact")
    artifacts = [
        artifact
        for artifact in lock["artifacts"]
        if artifact.get("id") == artifact_id
    ]
    return len(artifacts) == 1 and (
        artifacts[0].get("version") == "2100.0.0b2"
        and artifacts[0].get("sha256") == MCREMOTE_B2_SHA256
    )


def _service_ids(lock: dict[str, Any]) -> list[str]:
    services = [service["id"] for service in lock["render_plan"]["services"]]
    if not services or len(services) != len(set(services)):
        _fail(
            "doctor_contract_unsupported",
            "render_plan.services",
            "doctor requires a non-empty unique service projection",
        )
    return services


def _volume_identities(lock: dict[str, Any]) -> list[str]:
    assignments = {
        assignment["role"]: assignment["identity"]
        for assignment in lock["runtime"]["volumes"]
    }
    roles = [role["id"] for role in lock["render_plan"]["volume_roles"]]
    if not roles or set(assignments) != set(roles) or len(roles) != len(set(roles)):
        _fail(
            "doctor_contract_unsupported",
            "runtime.volumes",
            "doctor requires exactly one assignment for every declared volume role",
        )
    return [assignments[role] for role in roles]


def _expected_volume_labels(lock: dict[str, Any]) -> dict[str, str]:
    return {
        "io.mc-remote.owner": "mcrctl",
        "io.mc-remote.deployment": lock["deployment"]["name"],
        "io.mc-remote.environment": lock["environment"]["identity"],
        "io.mc-remote.world": lock["world"]["identity"],
        "io.mc-remote.created-by-lock": lock["lock_identity"],
    }


def _validate_volume(record: dict[str, Any], volume: str, lock: dict[str, Any]) -> None:
    if (
        record.get("Name") != volume
        or record.get("Driver") != "local"
        or record.get("Labels") != _expected_volume_labels(lock)
    ):
        _fail(
            "doctor_volume_unmanaged",
            volume,
            "runtime volume does not match the current lock and mcrctl ownership labels",
        )


def _validate_credential_mounts(record: dict[str, Any], lock: dict[str, Any]) -> None:
    if lock["render_plan"]["adapter_revision"] != "5":
        return
    assignments = {
        assignment["role"]: assignment["identity"]
        for assignment in lock["runtime"]["volumes"]
    }
    expected = {
        "/data": assignments.get("minecraft-data"),
        "/mcremote/credential-store": assignments.get("credential-store"),
        "/mcremote/credential-revocations": assignments.get(
            "credential-revocations"
        ),
    }
    mounts = record.get("Mounts")
    if not isinstance(mounts, list) or any(value is None for value in expected.values()):
        _fail(
            "doctor_credential_mount_mismatch",
            "runtime.mounts",
            "credential profile requires exact world, snapshot, and authority volume mounts",
        )
    actual: dict[str, str] = {}
    for mount in mounts:
        if not isinstance(mount, dict) or mount.get("Type") != "volume":
            continue
        destination = mount.get("Destination")
        name = mount.get("Name")
        if (
            not isinstance(destination, str)
            or not isinstance(name, str)
            or mount.get("RW") is not True
            or destination in actual
        ):
            _fail(
                "doctor_credential_mount_mismatch",
                "runtime.mounts",
                "managed volume mounts must be unique writable paths",
            )
        actual[destination] = name
    if actual != expected:
        _fail(
            "doctor_credential_mount_mismatch",
            "runtime.mounts",
            "live world, credential snapshot, and revocation authority mounts do not match the lock",
        )
    for destination in (
        "/mcremote/credential-store",
        "/mcremote/credential-revocations",
    ):
        if destination == "/data" or destination.startswith("/data/"):
            _fail(
                "doctor_credential_mount_mismatch",
                destination,
                "credential state must remain outside the Minecraft data write set",
            )


def _validate_container(
    record: dict[str, Any],
    lock: dict[str, Any],
    expected_services: set[str],
    output: Path,
) -> tuple[str, str]:
    config = record.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    expected_labels = {
        "com.docker.compose.project": lock["deployment"]["name"],
        "io.mc-remote.deployment": lock["deployment"]["name"],
        "io.mc-remote.environment": lock["environment"]["identity"],
        "io.mc-remote.world": lock["world"]["identity"],
        "io.mc-remote.lock": lock["lock_identity"],
    }
    if not isinstance(labels, dict) or any(
        labels.get(key) != value for key, value in expected_labels.items()
    ):
        _fail(
            "doctor_runtime_unmanaged",
            lock["deployment"]["name"],
            "runtime container does not match the current lock",
        )
    service = labels.get("com.docker.compose.service")
    if service not in expected_services:
        _fail(
            "doctor_runtime_unmanaged",
            lock["deployment"]["name"],
            "runtime container service is not declared by the current lock",
        )
    if service == "minecraft":
        _validate_credential_mounts(record, lock)

    state = record.get("State")
    if not isinstance(state, dict) or state.get("Running") is not True:
        _fail(
            "doctor_runtime_not_running",
            lock["deployment"]["name"],
            "current locked container is not running",
        )
    health = state.get("Health")
    if service == "minecraft" and (
        not isinstance(health, dict) or health.get("Status") != "healthy"
    ):
        _fail(
            "doctor_runtime_unhealthy",
            lock["deployment"]["name"],
            "current locked container is not healthy",
        )

    network = record.get("NetworkSettings")
    ports = network.get("Ports") if isinstance(network, dict) else None
    if not isinstance(ports, dict):
        _fail(
            "doctor_network_mismatch",
            lock["deployment"]["name"],
            "container port mappings are unavailable",
        )
    address = lock["network"]["bind_address"]
    expected_ports: dict[str, list[dict[str, str]]] = {}
    if service == "minecraft":
        expected_ports = {
            "25565/tcp": [
                {
                    "HostIp": address,
                    "HostPort": str(lock["network"]["java_port"]),
                }
            ],
            "25575/tcp": [
                {
                    "HostIp": address,
                    "HostPort": str(lock["network"]["mcremote_port"]),
                }
            ],
        }
        if lock["render_plan"]["adapter_revision"] in {"2", "3", "4", "7", "8", "9"}:
            expected_ports["19132/udp"] = [
                {
                    "HostIp": address,
                    "HostPort": str(lock["network"]["java_port"]),
                }
            ]
    elif service == "caddy":
        expected_ports = {
            "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "80"}],
            "443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "443"}],
        }
    published = {key: value for key, value in ports.items() if value}
    if published != expected_ports:
        _fail(
            "doctor_network_mismatch",
            lock["deployment"]["name"],
            "live published ports do not match the current lock",
        )
    return service, compose_render_status(labels, output)


def _validate_hello_result(
    response: object,
    *,
    protocol: str,
    minecraft_version: str,
    world: str,
) -> ProtocolHelloResult:
    if not isinstance(response, dict) or response.get("jsonrpc") != "2.0" or response.get("id") != 1:
        _fail(
            "protocol_hello_invalid",
            "protocol.hello",
            "response does not contain the expected JSON-RPC envelope",
        )

    result = response.get("result")
    if isinstance(result, dict):
        supported = result.get("supported_mc_versions")
        constants = result.get("world_constants")
        origin = result.get("origin")
        if result.get("protocol") != protocol:
            _fail(
                "protocol_hello_mismatch",
                "protocol.hello.protocol",
                "server protocol does not match the current lock",
            )
        if result.get("mc_version") != minecraft_version:
            _fail(
                "protocol_hello_mismatch",
                "protocol.hello.mc_version",
                "server Minecraft version does not match the current lock",
            )
        if (
            not isinstance(supported, list)
            or minecraft_version not in supported
            or "catalogHash" not in result
            or not isinstance(constants, dict)
            or "y_sea" not in constants
            or result.get("world") != world
            or not isinstance(origin, list)
            or len(origin) != 3
            or any(isinstance(value, bool) or not isinstance(value, int | float) for value in origin)
        ):
            _fail(
                "protocol_hello_invalid",
                "protocol.hello.result",
                "response is missing required public protocol fields",
            )
        return ProtocolHelloResult(
            status="ok",
            protocol=protocol,
            minecraft_version=minecraft_version,
        )

    error = response.get("error")
    data = error.get("data") if isinstance(error, dict) else None
    if isinstance(data, dict) and data.get("reason") == "auth_required":
        return ProtocolHelloResult(
            status="auth-required",
            protocol=None,
            minecraft_version=None,
        )
    _fail(
        "protocol_hello_failed",
        "protocol.hello",
        "server returned a non-success response other than auth_required",
    )


def probe_protocol_hello(
    address: str,
    port: int,
    protocol: str,
    minecraft_version: str,
    world: str,
    timeout: int,
    *,
    connector: Connector = socket.create_connection,
) -> ProtocolHelloResult:
    """Send one token-free JSON-RPC hello without exposing response credentials."""

    if timeout < 1 or timeout > 30:
        _fail(
            "doctor_timeout_invalid",
            "doctor.timeout",
            "protocol timeout must be between 1 and 30 seconds",
        )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "hello",
        "params": {"protocol": protocol},
    }
    payload = json.dumps(request, separators=(",", ":")).encode() + b"\n"
    received = bytearray()
    try:
        with connector((address, port), timeout) as connection:
            connection.sendall(payload)
            while b"\n" not in received:
                chunk = connection.recv(min(4096, MAX_HELLO_BYTES + 1 - len(received)))
                if not chunk:
                    break
                received.extend(chunk)
                if len(received) > MAX_HELLO_BYTES:
                    _fail(
                        "protocol_hello_invalid",
                        "protocol.hello",
                        "response exceeded the 64 KiB limit",
                    )
    except DoctorContractError:
        raise
    except TimeoutError:
        _fail(
            "protocol_hello_timeout",
            f"{address}:{port}",
            f"server did not answer hello within {timeout} seconds",
        )
    except OSError:
        _fail(
            "protocol_hello_unavailable",
            f"{address}:{port}",
            "cannot connect to the locked McRemote endpoint",
        )

    line, separator, _remainder = bytes(received).partition(b"\n")
    if not separator or not line or len(line) > MAX_HELLO_BYTES:
        _fail(
            "protocol_hello_invalid",
            "protocol.hello",
            "server did not return one bounded LF-terminated response",
        )
    try:
        response = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(
            "protocol_hello_invalid",
            "protocol.hello",
            "server response is not valid UTF-8 JSON",
        )
    return _validate_hello_result(
        response,
        protocol=protocol,
        minecraft_version=minecraft_version,
        world=world,
    )


def doctor_toml_project(
    project_root: Path,
    output: Path,
    *,
    docker_context: str,
    data_root: Traversable,
    timeout: int = 5,
    runner: CommandRunner = _default_runner,
    hello_probe=probe_protocol_hello,
    scratch_runtime_probe=probe_scratch_runtime_config,
) -> TomlDoctorResult:
    """Check one current Compose runtime without mutating host state."""

    if not DOCKER_CONTEXT.fullmatch(docker_context):
        _fail(
            "docker_context_invalid",
            "doctor.docker_context",
            "Docker context must be an explicit name token",
        )
    if timeout < 1 or timeout > 30:
        _fail(
            "doctor_timeout_invalid",
            "doctor.timeout",
            "protocol timeout must be between 1 and 30 seconds",
        )
    try:
        verification = verify_toml_render_output(
            project_root,
            output,
            data_root=data_root,
        )
    except RenderContractError as exc:
        raise DoctorContractError(exc.reason, exc.path, str(exc)) from exc
    lock = verification.lock
    output = verification.output
    protocol = _component_value(lock, "mcremote-plugin", "protocol")
    minecraft_version = _component_value(lock, "paper-server", "minecraft_version")
    services = _service_ids(lock)
    volumes = _volume_identities(lock)

    context = _run(
        runner,
        ["docker", "context", "inspect", docker_context],
        timeout=30,
        reason="docker_context_unavailable",
        path=docker_context,
    )
    context_record = _single_inspect_record(
        context,
        reason="docker_context_unavailable",
        path=docker_context,
    )
    endpoints = context_record.get("Endpoints")
    docker_endpoint = endpoints.get("docker") if isinstance(endpoints, dict) else None
    docker_host = docker_endpoint.get("Host") if isinstance(docker_endpoint, dict) else None
    if not isinstance(docker_host, str) or not docker_host.startswith("unix://"):
        _fail(
            "docker_context_not_local",
            docker_context,
            "doctor checks only a local unix-socket Docker context on the target host",
        )

    docker_prefix = ["docker", "--context", docker_context]
    daemon = _run(
        runner,
        docker_prefix + ["version", "--format", "{{.Server.Version}}"],
        timeout=30,
        reason="docker_unavailable",
        path="docker.daemon",
    )
    if len(_nonempty_lines(daemon)) != 1:
        _fail("docker_unavailable", "docker.daemon", "Docker daemon version is unavailable")
    compose = _run(
        runner,
        docker_prefix + ["compose", "version", "--short"],
        timeout=30,
        reason="docker_compose_unavailable",
        path="docker.compose",
    )
    if len(_nonempty_lines(compose)) != 1:
        _fail("docker_compose_unavailable", "docker.compose", "Docker Compose v2 is unavailable")

    compose_base = docker_prefix + [
        "compose",
        "--ansi",
        "never",
        "--project-directory",
        str(output),
        "--file",
        str(output / "compose.yaml"),
    ]
    _run(
        runner,
        compose_base + ["config", "--quiet"],
        timeout=60,
        reason="compose_config_invalid",
        path=output / "compose.yaml",
    )

    container_result = _run(
        runner,
        docker_prefix
        + [
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={lock['deployment']['name']}",
        ],
        timeout=30,
        reason="doctor_runtime_inspect_failed",
        path=lock["deployment"]["name"],
    )
    containers = _nonempty_lines(container_result)
    if len(containers) != len(services):
        reason = "doctor_runtime_missing" if not containers else "doctor_runtime_unmanaged"
        _fail(
            reason,
            lock["deployment"]["name"],
            "doctor requires exactly the current lock's Compose service count",
        )
    actual_services = set()
    render_statuses = set()
    for container_id in containers:
        container = _single_inspect_record(
            _run(
                runner,
                docker_prefix + ["inspect", container_id],
                timeout=30,
                reason="doctor_runtime_inspect_failed",
                path=lock["deployment"]["name"],
            ),
            reason="doctor_runtime_inspect_failed",
            path=lock["deployment"]["name"],
        )
        service, render_status = _validate_container(
            container,
            lock,
            set(services),
            output,
        )
        actual_services.add(service)
        render_statuses.add(render_status)
    if actual_services != set(services):
        _fail(
            "doctor_runtime_unmanaged",
            lock["deployment"]["name"],
            "runtime does not contain every service declared by the current lock",
        )
    if render_statuses == {"current"}:
        runtime_render_status = "current"
    elif render_statuses <= {"current", "additional-compose-files"}:
        runtime_render_status = "additional-compose-files"
    else:
        runtime_render_status = "noncanonical"

    for volume in volumes:
        volume_record = _single_inspect_record(
            _run(
                runner,
                docker_prefix + ["volume", "inspect", volume],
                timeout=30,
                reason="doctor_volume_missing",
                path=volume,
            ),
            reason="doctor_volume_unmanaged",
            path=volume,
        )
        _validate_volume(volume_record, volume, lock)

    scratch_runtime_status = "not-applicable"
    if lock["render_plan"]["adapter_revision"] == "9":
        runtime_path = output / "runtime" / "scratch.json"
        try:
            expected_runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            _fail(
                "doctor_scratch_runtime_invalid",
                runtime_path,
                f"cannot read canonical Scratch runtime config: {exc}",
            )
        routes = _locked_public_routes(lock)
        scratch_runtime_url = (
            f"https://{routes['scratch']}/mc-remote-runtime-config.json"
        )
        observed_runtime = scratch_runtime_probe(scratch_runtime_url, timeout)
        validate_scratch_runtime_config(
            observed_runtime,
            expected=expected_runtime,
        )
        scratch_runtime_status = "current"

    if lock["render_plan"]["adapter_revision"] == "5":
        _fail(
            "doctor_credential_health_unsupported",
            "credential.health",
            "credential profile mount topology is valid, but the plugin does not "
            "yet expose the required machine-readable domain health projection",
        )

    network = lock["network"]
    hello = hello_probe(
        network["bind_address"],
        network["mcremote_port"],
        protocol,
        minecraft_version,
        lock["world"]["identity"],
        timeout,
    )
    if _auth_enforcement_required(lock) and hello.status != "auth-required":
        _fail(
            "doctor_auth_not_enforced",
            "protocol.hello",
            "the locked McRemote release requires authentication, but token-free hello succeeded",
        )
    return TomlDoctorResult(
        deployment=lock["deployment"]["name"],
        environment=lock["environment"]["identity"],
        lock_identity=lock["lock_identity"],
        docker_context=docker_context,
        runtime_status="healthy",
        render_status=runtime_render_status,
        network_scope=(
            "loopback"
            if ip_address(network["bind_address"]).is_loopback
            else (
                "public"
                if lock["environment"]["exposure"] == "public"
                else "non-loopback"
            )
        ),
        bind_address=network["bind_address"],
        java_port=network["java_port"],
        mcremote_port=network["mcremote_port"],
        protocol_status=hello.status,
        protocol=hello.protocol,
        minecraft_version=hello.minecraft_version,
        compatibility_status=lock["compatibility"]["status"],
        scratch_runtime_status=scratch_runtime_status,
    )
