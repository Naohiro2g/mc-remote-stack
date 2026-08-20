"""Preflight for the trusted human operator environment."""

from __future__ import annotations

import getpass
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MINIMUM_PYTHON = (3, 11, 0)
MINIMUM_COMPOSE = (2, 33, 1)
DOCKER_CONTEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
VERSION = re.compile(r"(?:v)?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)")


class OperatorEnvironmentError(RuntimeError):
    def __init__(self, reason: str, path: object, message: str) -> None:
        super().__init__(f"{reason}: {path}: {message}")
        self.reason = reason
        self.path = path


class CommandRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class OperatorEnvironmentResult:
    status: str
    operator: str
    uid: int
    project_root: Path
    python_version: str
    git_version: str
    uv_version: str
    docker_context: str
    docker_version: str
    compose_version: str


def _default_runner(
    command: list[str],
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _fail(reason: str, path: object, message: str) -> None:
    raise OperatorEnvironmentError(reason, path, message)


def _run_tool(
    runner: CommandRunner,
    command: list[str],
    *,
    path: str,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(command, timeout=timeout)
    except FileNotFoundError:
        _fail(
            "operator_tool_missing",
            path,
            f"required command {command[0]!r} is not installed; run the operator bootstrap",
        )
    except subprocess.TimeoutExpired:
        _fail("operator_tool_timeout", path, f"{' '.join(command)} timed out")
    if result.returncode == 0:
        return result
    detail = (result.stderr or result.stdout).strip().splitlines()
    last = detail[-1] if detail else "command failed without diagnostic output"
    if command[0] == "docker" and (
        "permission denied" in last.lower() or "docker daemon socket" in last.lower()
    ):
        _fail(
            "operator_docker_access_missing",
            "operator.docker",
            "the trusted operator must be a member of the docker group and must re-login; "
            "do not run mcrctl with sudo",
        )
    _fail("operator_tool_unavailable", path, last)


def _one_line(result: subprocess.CompletedProcess[str], *, path: str) -> str:
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        _fail("operator_tool_output_invalid", path, "expected exactly one non-empty line")
    return lines[0]


def _parse_version(value: str, *, path: str) -> tuple[int, int, int]:
    match = VERSION.search(value)
    if match is None:
        _fail("operator_tool_version_invalid", path, f"cannot parse version from {value!r}")
    return tuple(int(match.group(name)) for name in ("major", "minor", "patch"))


def _validate_project_tree(project: Path, uid: int) -> None:
    for entry in (project, *sorted(project.rglob("*"))):
        if entry.is_symlink():
            _fail(
                "operator_project_symlink_forbidden",
                entry,
                "deployment project entries must not be symbolic links",
            )
        try:
            owner = entry.stat().st_uid
        except OSError as exc:
            _fail("operator_project_unreadable", entry, str(exc))
        if owner != uid:
            _fail(
                "operator_project_owner_mismatch",
                entry,
                f"project entry is owned by uid {owner}, but mcrctl is running as uid {uid}; "
                "run the operator bootstrap with --install --repair-project",
            )
        required = os.R_OK | os.W_OK
        if entry.is_dir():
            required |= os.X_OK
        if not os.access(entry, required):
            _fail(
                "operator_project_entry_not_writable",
                entry,
                "the project-owning operator requires read/write access; run the operator "
                "bootstrap with --install --repair-project",
            )


def _declared_artifact_store(project: Path) -> Path | None:
    order_path = project / "mc-remote.toml"
    if not order_path.exists():
        return None
    try:
        order = tomllib.loads(order_path.read_text(encoding="utf-8"))
        artifact_store = order["runtime"]["artifact_store"]
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        _fail(
            "operator_project_order_invalid",
            order_path,
            f"cannot identify the deployment artifact store: {exc}",
        )
    if not isinstance(artifact_store, str) or not Path(artifact_store).is_absolute():
        _fail(
            "operator_project_order_invalid",
            order_path,
            "runtime.artifact_store must be one absolute path",
        )
    return Path(artifact_store)


def _validate_artifact_store(path: Path, uid: int) -> None:
    if not path.exists():
        parent = path.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if parent.is_symlink() or parent.stat().st_uid != uid or not os.access(
            parent, os.W_OK | os.X_OK
        ):
            _fail(
                "operator_artifact_store_not_writable",
                path,
                "the nearest existing artifact-store parent is not owned and writable by the operator",
            )
        return
    for entry in (path, *sorted(path.rglob("*"))):
        if entry.is_symlink():
            _fail(
                "operator_artifact_store_symlink_forbidden",
                entry,
                "artifact-store entries must not be symbolic links",
            )
        try:
            owner = entry.stat().st_uid
        except OSError as exc:
            _fail("operator_artifact_store_unreadable", entry, str(exc))
        if owner != uid:
            _fail(
                "operator_artifact_store_owner_mismatch",
                entry,
                f"artifact-store entry is owned by uid {owner}; run the operator bootstrap "
                "with --install --repair-artifact-store",
            )
        required = os.R_OK
        if entry.is_dir():
            required |= os.W_OK | os.X_OK
        if not os.access(entry, required):
            _fail(
                "operator_artifact_store_not_writable",
                entry,
                "artifact-store directories must be writable by the deployment operator",
            )


def check_operator_environment(
    project_root: Path,
    *,
    docker_context: str = "default",
    effective_uid: int | None = None,
    effective_user: str | None = None,
    runner: CommandRunner = _default_runner,
    python_version: tuple[int, int, int] | None = None,
) -> OperatorEnvironmentResult:
    """Verify one durable operator identity before deployment work starts."""

    uid = os.geteuid() if effective_uid is None else effective_uid
    operator = getpass.getuser() if effective_user is None else effective_user
    if uid == 0:
        _fail(
            "operator_root_forbidden",
            "operator.uid",
            "run mcrctl as the project-owning operator; Docker access is prepared separately",
        )
    if not DOCKER_CONTEXT.fullmatch(docker_context):
        _fail(
            "operator_docker_context_invalid",
            "operator.docker_context",
            "Docker context must be an explicit name token",
        )

    project = project_root.resolve()
    if not project.is_dir():
        _fail("operator_project_missing", project, "deployment project directory does not exist")
    _validate_project_tree(project, uid)
    artifact_store = _declared_artifact_store(project)
    if artifact_store is not None:
        _validate_artifact_store(artifact_store, uid)

    current_python = python_version or (
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    )
    if current_python < MINIMUM_PYTHON:
        _fail(
            "operator_python_too_old",
            "operator.python",
            "Python 3.11 or newer is required; run the operator bootstrap",
        )

    git = _one_line(
        _run_tool(runner, ["git", "--version"], path="operator.git"),
        path="operator.git",
    )
    uv = _one_line(
        _run_tool(runner, ["uv", "--version"], path="operator.uv"),
        path="operator.uv",
    )
    context = _run_tool(
        runner,
        ["docker", "context", "inspect", docker_context],
        path="operator.docker_context",
    )
    try:
        records = json.loads(context.stdout)
        docker_host = records[0]["Endpoints"]["docker"]["Host"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        _fail(
            "operator_docker_context_invalid",
            docker_context,
            "Docker context inspect did not return one usable endpoint",
        )
    if not isinstance(docker_host, str) or not docker_host.startswith("unix://"):
        _fail(
            "operator_docker_context_not_local",
            docker_context,
            "deployment operations require the target host's local Unix-socket Docker context",
        )

    docker_prefix = ["docker", "--context", docker_context]
    docker = _one_line(
        _run_tool(
            runner,
            docker_prefix + ["version", "--format", "{{.Server.Version}}"],
            path="operator.docker",
        ),
        path="operator.docker",
    )
    compose = _one_line(
        _run_tool(
            runner,
            docker_prefix + ["compose", "version", "--short"],
            path="operator.compose",
        ),
        path="operator.compose",
    )
    if _parse_version(compose, path="operator.compose") < MINIMUM_COMPOSE:
        _fail(
            "operator_compose_too_old",
            "operator.compose",
            "Docker Compose 2.33.1 or newer is required for the locked network gateway contract",
        )

    return OperatorEnvironmentResult(
        status="ready",
        operator=operator,
        uid=uid,
        project_root=project,
        python_version=".".join(str(part) for part in current_python),
        git_version=git,
        uv_version=uv,
        docker_context=docker_context,
        docker_version=docker,
        compose_version=compose,
    )
