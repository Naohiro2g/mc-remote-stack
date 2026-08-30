"""Lock-bound, bootstrap-only live apply for current Compose projects."""

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
from .runtime_contract import MINECRAFT_RUNTIME_GID, MINECRAFT_RUNTIME_UID

BOOTSTRAP_CONTRACTS = frozenset(
    {
        (
            "home-server@4",
            "mcremote-paper@1",
            "beta",
            "isolated",
            "integration",
        ),
        (
            "home-server@4",
            "mcremote-paper@2",
            "alpha",
            "isolated",
            "integration",
        ),
        (
            "home-server@3",
            "mcremote-paper@3",
            "alpha",
            "isolated",
            "integration",
        ),
        (
            "home-server@3",
            "mcremote-paper@6",
            "alpha",
            "isolated",
            "integration",
        ),
        (
            "home-server@5",
            "mcremote-paper@7",
            "dev",
            "lan-only",
            "integration",
        ),
        (
            "home-server@6",
            "home-alpha-full@1",
            "alpha",
            "isolated",
            "integration",
        ),
        (
            "vps-server@7",
            "public-web-paper@2",
            "beta",
            "public",
            "integration",
        ),
        (
            "vps-server@8",
            "public-web-paper@3",
            "beta",
            "public",
            "integration",
        ),
        (
            "vps-server@9",
            "public-web-paper@4",
            "beta",
            "public",
            "integration",
        ),
        (
            "vps-server@10",
            "public-web-paper@4",
            "beta",
            "public",
            "integration",
        ),
        (
            "vps-server@11",
            "public-web-paper@4",
            "beta",
            "public",
            "integration",
        ),
        (
            "vps-server@12",
            "public-web-paper@5",
            "beta",
            "public",
            "integration",
        ),
    }
)
DOCKER_CONTEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
URL_USERINFO = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^@\s/]+@"
)
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)([\"']?(?:password|passwd|token|secret|credential|authorization)"
    r"[\"']?\s*[=:]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|.*)"
)


class CommandRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]: ...


PortProbe = Callable[[str, int], None]
ProgressReporter = Callable[[str], None]


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


def _no_progress(_step: str) -> None:
    pass


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
        message = (
            f"Docker command failed with exit status {result.returncode}"
        )
        detail = _safe_command_failure_detail(result)
        if detail is not None:
            message = f"{message}; detail={detail}"
        _fail(reason, path, message)
    return result


def _safe_command_failure_detail(
    result: subprocess.CompletedProcess[str],
) -> str | None:
    detail = None
    for stream in (result.stderr, result.stdout):
        lines = [
            line.strip()
            for line in stream.splitlines()
            if line.strip()
        ]
        if lines:
            detail = lines[-1]
            break
    if detail is None:
        return None
    detail = ANSI_ESCAPE.sub("", detail)
    detail = "".join(
        character if character.isprintable() else " "
        for character in detail
    )
    detail = URL_USERINFO.sub(r"\1<redacted>@", detail)
    detail = SENSITIVE_ASSIGNMENT.sub(r"\1<redacted>", detail)
    detail = " ".join(detail.split())
    if not detail:
        return None
    if len(detail) > 400:
        detail = f"{detail[:399]}…"
    return detail


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


def expected_compose_provenance(output: Path) -> dict[str, str]:
    resolved = output.resolve()
    return {
        "com.docker.compose.project.config_files": str(
            resolved / "compose.yaml"
        ),
        "com.docker.compose.project.working_dir": str(resolved),
    }


def compose_render_status(
    labels: dict[str, Any],
    output: Path,
) -> str:
    expected = expected_compose_provenance(output)
    config_files = labels.get(
        "com.docker.compose.project.config_files"
    )
    working_dir = labels.get(
        "com.docker.compose.project.working_dir"
    )
    if (
        config_files
        == expected["com.docker.compose.project.config_files"]
        and working_dir
        == expected["com.docker.compose.project.working_dir"]
    ):
        return "current"
    if isinstance(config_files, str) and (
        expected["com.docker.compose.project.config_files"]
        in {
            item.strip()
            for item in config_files.split(",")
            if item.strip()
        }
        and working_dir
        == expected["com.docker.compose.project.working_dir"]
    ):
        return "additional-compose-files"
    return "noncanonical"


def _service_ids(lock: dict[str, Any]) -> list[str]:
    services = [service["id"] for service in lock["render_plan"]["services"]]
    if not services or len(services) != len(set(services)):
        _fail(
            "bootstrap_contract_unsupported",
            "render_plan.services",
            "bootstrap apply requires a non-empty unique service projection",
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
            "bootstrap_contract_unsupported",
            "runtime.volumes",
            "bootstrap apply requires exactly one assignment for every declared volume role",
        )
    return [assignments[role] for role in roles]


def _minecraft_runtime_image(lock: dict[str, Any]) -> str:
    components = [
        component
        for component in lock["components"]
        if component["role"] == "minecraft-runtime"
    ]
    if len(components) != 1:
        _fail(
            "bootstrap_contract_unsupported",
            "components.minecraft-runtime",
            "bootstrap requires exactly one minecraft-runtime component",
        )
    artifact_id = components[0]["artifact"]
    artifacts = [
        artifact for artifact in lock["artifacts"] if artifact["id"] == artifact_id
    ]
    if len(artifacts) != 1 or artifacts[0]["kind"] != "oci":
        _fail(
            "bootstrap_contract_unsupported",
            f"artifacts.{artifact_id}",
            "bootstrap requires one exact OCI minecraft-runtime artifact",
        )
    artifact = artifacts[0]
    return f"{artifact['locator']}:{artifact['version']}@{artifact['digest']}"


def _initialize_created_credential_volumes(
    runner: CommandRunner,
    docker_prefix: list[str],
    *,
    image: str,
    volume_assignments: dict[str, str],
    created_volumes: set[str],
) -> None:
    mounts: list[tuple[str, str]] = []
    for role in ("credential-store", "credential-revocations"):
        identity = volume_assignments.get(role)
        if identity in created_volumes:
            mounts.append((identity, f"/{role}"))
    if not mounts:
        return

    command = docker_prefix + [
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--user",
        "0:0",
    ]
    for identity, target in mounts:
        command.extend(
            [
                "--mount",
                f"type=volume,source={identity},target={target},volume-nocopy",
            ]
        )
    command.extend(
        [
            "--entrypoint",
            "chown",
            image,
            f"{MINECRAFT_RUNTIME_UID}:{MINECRAFT_RUNTIME_GID}",
            *(target for _identity, target in mounts),
        ]
    )
    _run(
        runner,
        command,
        timeout=120,
        reason="bootstrap_volume_initialize_failed",
        path="runtime.volumes.credential",
    )


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
    labels = record.get("Labels")
    expected = _expected_volume_labels(lock)
    stable_labels = {
        key: value
        for key, value in expected.items()
        if key != "io.mc-remote.created-by-lock"
    }
    if (
        record.get("Name") != volume
        or record.get("Driver") != "local"
        or not isinstance(labels, dict)
        or set(labels) != set(expected)
        or any(labels.get(key) != value for key, value in stable_labels.items())
        or not isinstance(labels.get("io.mc-remote.created-by-lock"), str)
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            labels["io.mc-remote.created-by-lock"],
        )
        is None
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
    expected_services: set[str],
    output: Path,
) -> str:
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
    service = labels.get("com.docker.compose.service") if isinstance(labels, dict) else None
    expected = {
        "com.docker.compose.project": lock["deployment"]["name"],
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
    if compose_render_status(labels, output) != "current":
        _fail(
            "bootstrap_runtime_composition_mismatch",
            container_id,
            "running Compose files or working directory do not match "
            "the current canonical render",
        )
    if service not in expected_services:
        _fail(
            "bootstrap_runtime_unmanaged",
            container_id,
            "existing Compose service is not declared by the current lock",
        )
    if not isinstance(state, dict) or state.get("Running") is not True:
        _fail(
            "bootstrap_runtime_not_running",
            container_id,
            "the current locked container exists but is not running",
        )
    return service


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
    ports = [lock["network"]["java_port"], lock["network"]["mcremote_port"]]
    if lock["render_plan"]["adapter_revision"] in {
        "2",
        "3",
        "4",
        "7",
        "8",
        "9",
        "10",
        "11",
        "12",
    }:
        ports = [80, 443, *ports]
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
            "initial live apply supports only explicitly listed bootstrap contracts",
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
    progress: ProgressReporter,
) -> None:
    progress("rollback-containers")
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
    progress: ProgressReporter = _no_progress,
) -> TomlApplyResult:
    """Apply one exact initial Compose projection without supporting upgrades."""

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

    progress("verify-render")
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
    progress("validate-lock")
    _validate_bootstrap_contract(
        lock,
        allow_unverified=allow_unverified,
        allow_eol=allow_eol,
    )

    compose_project = lock["deployment"]["name"]
    services = _service_ids(lock)
    volumes = _volume_identities(lock)
    volume_assignments = {
        assignment["role"]: assignment["identity"]
        for assignment in lock["runtime"]["volumes"]
    }
    progress("docker-preflight")
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

    progress("runtime-preflight")
    containers = _project_container_ids(runner, docker_prefix, compose_project)
    if len(containers) > len(services):
        _fail(
            "bootstrap_runtime_unmanaged",
            compose_project,
            "bootstrap apply found more containers than the current lock declares",
        )
    volume_states = {
        volume: _volume_exists(runner, docker_prefix, volume) for volume in volumes
    }
    for volume, exists in volume_states.items():
        if exists:
            _inspect_managed_volume(runner, docker_prefix, volume, lock)
    if containers:
        if not all(volume_states.values()) or len(containers) != len(services):
            _fail(
                "bootstrap_runtime_unmanaged",
                compose_project,
                "current project is incomplete relative to its locked services or volumes",
            )
        actual_services = {
            _inspect_current_container(
                runner,
                docker_prefix,
                container_id,
                lock,
                set(services),
                output,
            )
            for container_id in containers
        }
        if actual_services != set(services):
            _fail(
                "bootstrap_runtime_unmanaged",
                compose_project,
                "current project does not contain every service declared by the lock",
            )
        progress("complete")
        return TomlApplyResult(
            status="unchanged",
            lock_identity=lock["lock_identity"],
            compose_project=compose_project,
            service=",".join(services),
            volume=",".join(volumes),
        )

    progress("check-ports")
    _check_ports(runner, docker_prefix, lock, port_probe)
    progress("pull-images")
    _run(
        runner,
        compose_base + ["pull", "--policy", "always", "--quiet", *services],
        timeout=900,
        reason="compose_pull_failed",
        path="artifacts.minecraft-runtime",
    )
    status = "resumed"
    progress("prepare-volumes")
    created_volumes: set[str] = set()
    for volume, exists in volume_states.items():
        if exists:
            continue
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
        created_volumes.add(volume)
        status = "created"

    created_credential_volumes = created_volumes.intersection(
        {
            identity
            for role, identity in volume_assignments.items()
            if role in {"credential-store", "credential-revocations"}
        }
    )
    if created_credential_volumes:
        progress("initialize-credential-volumes")
        _initialize_created_credential_volumes(
            runner,
            docker_prefix,
            image=_minecraft_runtime_image(lock),
            volume_assignments=volume_assignments,
            created_volumes=created_credential_volumes,
        )

    try:
        progress(f"start-services-and-wait timeout={wait_timeout}")
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
                *services,
            ],
            timeout=wait_timeout + 60,
            reason="compose_up_failed",
            path="docker.compose",
        )
        progress("post-check")
        current = _project_container_ids(runner, docker_prefix, compose_project)
        if len(current) != len(services):
            _fail(
                "apply_postcheck_failed",
                compose_project,
                "Compose apply did not produce exactly the locked service count",
            )
        actual_services = {
            _inspect_current_container(
                runner,
                docker_prefix,
                container_id,
                lock,
                set(services),
                output,
            )
            for container_id in current
        }
        if actual_services != set(services):
            _fail(
                "apply_postcheck_failed",
                compose_project,
                "Compose apply did not produce every locked service",
            )
    except ApplyContractError as exc:
        _rollback_containers(runner, compose_base, exc, progress)

    progress("complete")
    return TomlApplyResult(
        status=status,
        lock_identity=lock["lock_identity"],
        compose_project=compose_project,
        service=",".join(services),
        volume=",".join(volumes),
    )
