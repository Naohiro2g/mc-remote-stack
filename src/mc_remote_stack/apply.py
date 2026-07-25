"""Lock-bound, bootstrap-only live apply for compose@1 projects."""

from __future__ import annotations

import json
import re
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Protocol

from .render import RenderContractError, verify_toml_render_output

BOOTSTRAP_CONTRACTS = frozenset(
    {
        (
            "home-server@2",
            "mcremote-paper@1",
            "beta",
            "isolated",
            "integration",
        ),
        (
            "home-server@2",
            "mcremote-paper@2",
            "alpha",
            "isolated",
            "integration",
        ),
    }
)
SERVICE = "minecraft"
DOCKER_CONTEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


class CommandRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]: ...


PortProbe = Callable[[str, int], None]


class ApplyContractError(ValueError):
    """Stable, fail-closed diagnostic for a live apply boundary."""

    def __init__(self, reason: str, path: object, message: str) -> None:
        self.reason = reason
        self.path = str(path)
        super().__init__(f"{reason}: {path}: {message}")


@dataclass(frozen=True)
class TomlApplyResult:
    status: str
    lock_identity: str
    compose_project: str
    service: str
    volume: str


def _fail(reason: str, path: object, message: str) -> None:
    raise ApplyContractError(reason, path, message)


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


def _default_port_probe(address: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((address, port))


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
        _fail("docker_unavailable", command[0], "Docker CLI is required for live apply")
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


def _compose_base(output: Path, docker_prefix: list[str]) -> list[str]:
    return docker_prefix + [
        "compose",
        "--ansi",
        "never",
        "--project-directory",
        str(output),
        "--file",
        str(output / "compose.yaml"),
    ]


def _volume_identity(lock: dict[str, Any]) -> str:
    matches = [
        assignment["identity"]
        for assignment in lock["runtime"]["volumes"]
        if assignment["role"] == "minecraft-data"
    ]
    if len(matches) != 1:
        _fail(
            "bootstrap_contract_unsupported",
            "runtime.volumes",
            "bootstrap apply requires exactly one minecraft-data volume",
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


def _inspect_managed_volume(
    runner: CommandRunner,
    docker_prefix: list[str],
    volume: str,
    lock: dict[str, Any],
) -> None:
    result = _run(
        runner,
        docker_prefix + ["volume", "inspect", volume],
        timeout=30,
        reason="bootstrap_volume_inspect_failed",
        path=volume,
    )
    record = _single_inspect_record(
        result,
        reason="bootstrap_volume_unmanaged",
        path=volume,
    )
    if (
        record.get("Name") != volume
        or record.get("Driver") != "local"
        or record.get("Labels") != _expected_volume_labels(lock)
    ):
        _fail(
            "bootstrap_volume_unmanaged",
            volume,
            "existing volume is not the exact mcrctl-managed bootstrap volume",
        )


def _inspect_current_container(
    runner: CommandRunner,
    docker_prefix: list[str],
    container_id: str,
    lock: dict[str, Any],
) -> None:
    result = _run(
        runner,
        docker_prefix + ["inspect", container_id],
        timeout=30,
        reason="bootstrap_runtime_inspect_failed",
        path=container_id,
    )
    record = _single_inspect_record(
        result,
        reason="bootstrap_runtime_unmanaged",
        path=container_id,
    )
    config = record.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    state = record.get("State")
    expected = {
        "com.docker.compose.project": lock["deployment"]["name"],
        "com.docker.compose.service": SERVICE,
        "io.mc-remote.deployment": lock["deployment"]["name"],
        "io.mc-remote.environment": lock["environment"]["identity"],
        "io.mc-remote.world": lock["world"]["identity"],
        "io.mc-remote.lock": lock["lock_identity"],
    }
    if not isinstance(labels, dict) or any(labels.get(key) != value for key, value in expected.items()):
        _fail(
            "bootstrap_runtime_unmanaged",
            container_id,
            "existing Compose project does not match the current lock",
        )
    if not isinstance(state, dict) or state.get("Running") is not True:
        _fail(
            "bootstrap_runtime_not_running",
            container_id,
            "the current locked container exists but is not running",
        )


def _project_container_ids(
    runner: CommandRunner,
    docker_prefix: list[str],
    compose_project: str,
) -> list[str]:
    result = _run(
        runner,
        docker_prefix
        + [
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={compose_project}",
        ],
        timeout=30,
        reason="docker_preflight_failed",
        path="docker.containers",
    )
    return _nonempty_lines(result)


def _volume_exists(
    runner: CommandRunner,
    docker_prefix: list[str],
    volume: str,
) -> bool:
    result = _run(
        runner,
        docker_prefix
        + [
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"name=^{re.escape(volume)}$",
        ],
        timeout=30,
        reason="docker_preflight_failed",
        path=volume,
    )
    names = _nonempty_lines(result)
    if any(name != volume for name in names) or len(names) > 1:
        _fail(
            "docker_preflight_failed",
            volume,
            "exact volume lookup returned an unexpected result",
        )
    return names == [volume]


def _check_ports(
    runner: CommandRunner,
    docker_prefix: list[str],
    lock: dict[str, Any],
    port_probe: PortProbe,
) -> None:
    address = lock["network"]["bind_address"]
    ports = (lock["network"]["java_port"], lock["network"]["mcremote_port"])
    for port in ports:
        published = _run(
            runner,
            docker_prefix
            + [
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"publish={port}",
            ],
            timeout=30,
            reason="docker_preflight_failed",
            path=f"network.{address}:{port}",
        )
        if _nonempty_lines(published):
            _fail(
                "host_port_in_use",
                f"{address}:{port}",
                "another Docker container already publishes the requested port",
            )
        try:
            port_probe(address, port)
        except OSError as exc:
            _fail(
                "host_port_in_use",
                f"{address}:{port}",
                f"host bind preflight failed: {exc}",
            )


def _validate_bootstrap_contract(
    lock: dict[str, Any],
    *,
    allow_unverified: bool,
    allow_eol: bool,
) -> None:
    actual_contract = (
        lock["input"]["profile"]["ref"],
        lock["input"]["preset"]["ref"],
        lock["environment"]["channel"],
        lock["environment"]["exposure"],
        lock["environment"]["purpose"],
    )
    if actual_contract not in BOOTSTRAP_CONTRACTS:
        _fail(
            "bootstrap_contract_unsupported",
            "environment",
            "initial live apply supports only explicitly listed home bootstrap contracts",
        )
    if lock["agreements"]["minecraft_eula"] is not True:
        _fail(
            "minecraft_eula_not_accepted",
            "agreements.minecraft_eula",
            "live apply requires explicit Minecraft EULA acceptance",
        )
    if lock["preset_lifecycle"]["status"] == "eol" and not (
        lock["acknowledgements"]["allow_eol"] and allow_eol
    ):
        _fail(
            "preset_eol",
            "environment.preset",
            "EOL apply requires an order reason and one-shot --allow-eol",
        )
    if lock["compatibility"]["status"] == "unverified" and not (
        lock["acknowledgements"]["allow_unverified"] and allow_unverified
    ):
        _fail(
            "unverified_not_acknowledged",
            "environment.preset",
            "unverified apply requires an order reason and one-shot --allow-unverified",
        )


def _rollback_containers(
    runner: CommandRunner,
    compose_base: list[str],
    original: ApplyContractError,
) -> None:
    try:
        _run(
            runner,
            compose_base + ["down", "--timeout", "120"],
            timeout=180,
            reason="apply_rollback_failed",
            path="docker.compose",
        )
    except ApplyContractError as rollback:
        _fail(
            "apply_rollback_failed",
            "docker.compose",
            f"{original.reason} occurred and container rollback failed: {rollback}",
        )
    raise original


def apply_toml_project(
    project_root: Path,
    output: Path,
    *,
    expected_lock_identity: str,
    docker_context: str,
    data_root: Traversable,
    bootstrap: bool,
    confirmed: bool,
    allow_unverified: bool = False,
    allow_eol: bool = False,
    wait_timeout: int = 300,
    runner: CommandRunner = _default_runner,
    port_probe: PortProbe = _default_port_probe,
) -> TomlApplyResult:
    """Apply one exact initial compose@1 projection without supporting upgrades."""

    if not bootstrap:
        _fail(
            "bootstrap_confirmation_required",
            "apply.bootstrap",
            "the initial live slice requires explicit --bootstrap",
        )
    if not confirmed:
        _fail(
            "apply_confirmation_required",
            "apply.confirmed",
            "live host mutation requires explicit --yes",
        )
    if wait_timeout < 30 or wait_timeout > 1800:
        _fail(
            "apply_wait_timeout_invalid",
            "apply.wait_timeout",
            "wait timeout must be between 30 and 1800 seconds",
        )
    if not DOCKER_CONTEXT.fullmatch(docker_context):
        _fail(
            "docker_context_invalid",
            "apply.docker_context",
            "Docker context must be an explicit name token",
        )

    try:
        verification = verify_toml_render_output(
            project_root,
            output,
            data_root=data_root,
        )
    except RenderContractError as exc:
        raise ApplyContractError(exc.reason, exc.path, str(exc)) from exc
    lock = verification.lock
    output = verification.output
    if expected_lock_identity != lock["lock_identity"]:
        _fail(
            "apply_lock_identity_mismatch",
            "apply.expected_lock_identity",
            f"expected {expected_lock_identity} does not match current {lock['lock_identity']}",
        )
    _validate_bootstrap_contract(
        lock,
        allow_unverified=allow_unverified,
        allow_eol=allow_eol,
    )

    compose_project = lock["deployment"]["name"]
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
            "bootstrap apply requires a local unix-socket Docker context on the target host",
        )
    docker_prefix = ["docker", "--context", docker_context]
    compose_base = _compose_base(output, docker_prefix)
    daemon = _run(
        runner,
        docker_prefix + ["version", "--format", "{{.Server.Version}}"],
        timeout=30,
        reason="docker_unavailable",
        path="docker.daemon",
    )
    if len(_nonempty_lines(daemon)) != 1:
        _fail("docker_unavailable", "docker.daemon", "Docker daemon version is unavailable")
    compose_version = _run(
        runner,
        docker_prefix + ["compose", "version", "--short"],
        timeout=30,
        reason="docker_compose_unavailable",
        path="docker.compose",
    )
    if len(_nonempty_lines(compose_version)) != 1:
        _fail("docker_compose_unavailable", "docker.compose", "Docker Compose v2 is unavailable")
    _run(
        runner,
        compose_base + ["config", "--quiet"],
        timeout=60,
        reason="compose_config_invalid",
        path=output / "compose.yaml",
    )

    containers = _project_container_ids(runner, docker_prefix, compose_project)
    if len(containers) > 1:
        _fail(
            "bootstrap_runtime_unmanaged",
            compose_project,
            "bootstrap apply found more than one container in the Compose project",
        )
    volume_exists = _volume_exists(runner, docker_prefix, volume)
    if volume_exists:
        _inspect_managed_volume(runner, docker_prefix, volume, lock)
    if containers:
        if not volume_exists:
            _fail(
                "bootstrap_runtime_unmanaged",
                compose_project,
                "current project container exists without its managed world volume",
            )
        _inspect_current_container(runner, docker_prefix, containers[0], lock)
        return TomlApplyResult(
            status="unchanged",
            lock_identity=lock["lock_identity"],
            compose_project=compose_project,
            service=SERVICE,
            volume=volume,
        )

    _check_ports(runner, docker_prefix, lock, port_probe)
    _run(
        runner,
        compose_base + ["pull", "--policy", "always", "--quiet", SERVICE],
        timeout=900,
        reason="compose_pull_failed",
        path="artifacts.minecraft-runtime",
    )
    status = "resumed"
    if not volume_exists:
        labels = _expected_volume_labels(lock)
        create_command = docker_prefix + ["volume", "create", "--driver", "local"]
        for key, value in labels.items():
            create_command.extend(["--label", f"{key}={value}"])
        create_command.append(volume)
        created = _run(
            runner,
            create_command,
            timeout=60,
            reason="bootstrap_volume_create_failed",
            path=volume,
        )
        if _nonempty_lines(created) != [volume]:
            _fail(
                "bootstrap_volume_create_failed",
                volume,
                "Docker did not confirm the exact requested volume identity",
            )
        _inspect_managed_volume(runner, docker_prefix, volume, lock)
        status = "created"

    try:
        _run(
            runner,
            compose_base
            + [
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                str(wait_timeout),
                "--no-build",
                "--pull",
                "never",
                SERVICE,
            ],
            timeout=wait_timeout + 60,
            reason="compose_up_failed",
            path="docker.compose",
        )
        current = _project_container_ids(runner, docker_prefix, compose_project)
        if len(current) != 1:
            _fail(
                "apply_postcheck_failed",
                compose_project,
                "Compose apply did not produce exactly one managed container",
            )
        _inspect_current_container(runner, docker_prefix, current[0], lock)
    except ApplyContractError as exc:
        _rollback_containers(runner, compose_base, exc)

    return TomlApplyResult(
        status=status,
        lock_identity=lock["lock_identity"],
        compose_project=compose_project,
        service=SERVICE,
        volume=volume,
    )
