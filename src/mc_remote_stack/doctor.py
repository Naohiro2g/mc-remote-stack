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

from .apply import DOCKER_CONTEXT
from .render import RenderContractError, verify_toml_render_output

SERVICE = "minecraft"
MAX_HELLO_BYTES = 64 * 1024


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
    network_scope: str
    bind_address: str
    java_port: int
    mcremote_port: int
    protocol_status: str
    protocol: str | None
    minecraft_version: str | None
    compatibility_status: str


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


def _volume_identity(lock: dict[str, Any]) -> str:
    matches = [
        assignment["identity"]
        for assignment in lock["runtime"]["volumes"]
        if assignment["role"] == "minecraft-data"
    ]
    if len(matches) != 1:
        _fail(
            "doctor_contract_unsupported",
            "runtime.volumes",
            "doctor requires exactly one minecraft-data volume",
        )
    return matches[0]


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


def _validate_container(record: dict[str, Any], lock: dict[str, Any]) -> None:
    config = record.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    expected_labels = {
        "com.docker.compose.project": lock["deployment"]["name"],
        "com.docker.compose.service": SERVICE,
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

    state = record.get("State")
    if not isinstance(state, dict) or state.get("Running") is not True:
        _fail(
            "doctor_runtime_not_running",
            lock["deployment"]["name"],
            "current locked container is not running",
        )
    health = state.get("Health")
    if not isinstance(health, dict) or health.get("Status") != "healthy":
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
    published = {key: value for key, value in ports.items() if value}
    if published != expected_ports:
        _fail(
            "doctor_network_mismatch",
            lock["deployment"]["name"],
            "live published ports do not match the current lock",
        )


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
) -> TomlDoctorResult:
    """Check one current compose@1 runtime without mutating host state."""

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
    volume = _volume_identity(lock)

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
    if len(containers) != 1:
        reason = "doctor_runtime_missing" if not containers else "doctor_runtime_unmanaged"
        _fail(
            reason,
            lock["deployment"]["name"],
            "doctor requires exactly one current Compose project container",
        )
    container = _single_inspect_record(
        _run(
            runner,
            docker_prefix + ["inspect", containers[0]],
            timeout=30,
            reason="doctor_runtime_inspect_failed",
            path=lock["deployment"]["name"],
        ),
        reason="doctor_runtime_inspect_failed",
        path=lock["deployment"]["name"],
    )
    _validate_container(container, lock)

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

    network = lock["network"]
    hello = hello_probe(
        network["bind_address"],
        network["mcremote_port"],
        protocol,
        minecraft_version,
        lock["world"]["identity"],
        timeout,
    )
    return TomlDoctorResult(
        deployment=lock["deployment"]["name"],
        environment=lock["environment"]["identity"],
        lock_identity=lock["lock_identity"],
        docker_context=docker_context,
        runtime_status="healthy",
        network_scope=(
            "loopback"
            if ip_address(network["bind_address"]).is_loopback
            else "non-loopback"
        ),
        bind_address=network["bind_address"],
        java_port=network["java_port"],
        mcremote_port=network["mcremote_port"],
        protocol_status=hello.status,
        protocol=hello.protocol,
        minecraft_version=hello.minecraft_version,
        compatibility_status=lock["compatibility"]["status"],
    )
