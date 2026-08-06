"""Lock- and archive-bound Minecraft world restore transaction."""

from __future__ import annotations

import json
import re
import subprocess
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Protocol

from .archive import inspect_world_archive
from .doctor import doctor_toml_project
from .render import RenderContractError, verify_toml_render_output

DOCKER_CONTEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")

PREPARE_SCRIPT = r"""
phase="$1"
transaction="$2"
shift 2
test "$phase" = prepare
staging="/data/.mcrctl-world-restore-staging-$transaction"
rollback="/data/.mcrctl-world-restore-rollback-$transaction"
test ! -e "$staging"
test ! -e "$rollback"
mkdir -m 0700 "$staging"
for root in "$@"; do
  unzip -q /recovery/archive.zip "$root/*" -d "$staging"
  test -f "$staging/$root/level.dat"
done
"""

CUTOVER_SCRIPT = r"""
phase="$1"
transaction="$2"
shift 2
test "$phase" = cutover
staging="/data/.mcrctl-world-restore-staging-$transaction"
rollback="/data/.mcrctl-world-restore-rollback-$transaction"
test -d "$staging"
test ! -e "$rollback"
mkdir -m 0700 "$rollback"
pairs="$*"
while [ "$#" -gt 0 ]; do
  source_root="$1"
  destination_root="$2"
  test -f "$staging/$source_root/level.dat"
  test ! -e "$rollback/$destination_root"
  shift 2
done
set -- $pairs
while [ "$#" -gt 0 ]; do
  source_root="$1"
  destination_root="$2"
  if [ -e "/data/$destination_root" ]; then
    mv "/data/$destination_root" "$rollback/$destination_root"
  fi
  shift 2
done
set -- $pairs
while [ "$#" -gt 0 ]; do
  source_root="$1"
  destination_root="$2"
  mv "$staging/$source_root" "/data/$destination_root"
  if [ -e "$rollback/$destination_root" ]; then
    owner="$(stat -c '%u:%g' "$rollback/$destination_root")"
  else
    owner="$(stat -c '%u:%g' /data)"
  fi
  chown -R "$owner" "/data/$destination_root"
  shift 2
done
rmdir "$staging"
"""

ROLLBACK_SCRIPT = r"""
phase="$1"
transaction="$2"
shift 2
test "$phase" = rollback
rollback="/data/.mcrctl-world-restore-rollback-$transaction"
failed="/data/.mcrctl-world-restore-failed-$transaction"
test -d "$rollback"
test ! -e "$failed"
mkdir -m 0700 "$failed"
while [ "$#" -gt 0 ]; do
  source_root="$1"
  destination_root="$2"
  if [ -e "/data/$destination_root" ]; then
    mv "/data/$destination_root" "$failed/$destination_root"
  fi
  if [ -e "$rollback/$destination_root" ]; then
    mv "$rollback/$destination_root" "/data/$destination_root"
  fi
  shift 2
done
rmdir "$rollback"
"""

CLEANUP_SCRIPT = r"""
phase="$1"
transaction="$2"
test "$phase" = cleanup
staging="/data/.mcrctl-world-restore-staging-$transaction"
if [ -d "$staging" ]; then
  rm -rf -- "$staging"
fi
"""


class CommandRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]: ...


Doctor = Callable[..., Any]
ProgressReporter = Callable[[str], None]


class WorldRestoreError(ValueError):
    """Stable fail-closed diagnostic for world restore."""

    def __init__(self, reason: str, path: object, message: str) -> None:
        self.reason = reason
        self.path = str(path)
        super().__init__(f"{reason}: {path}: {message}")


@dataclass(frozen=True)
class WorldRestorePlan:
    status: str
    lock_identity: str
    archive_path: Path
    archive_sha256: str
    volume: str
    image: str
    world_mapping: tuple[tuple[str, str], ...]
    world_entry_count: int
    world_uncompressed_size_bytes: int
    transaction_id: str
    rollback_name: str


@dataclass(frozen=True)
class WorldRestoreResult:
    status: str
    lock_identity: str
    archive_sha256: str
    volume: str
    world_mapping: tuple[tuple[str, str], ...]
    rollback_name: str


def _fail(reason: str, path: object, message: str) -> None:
    raise WorldRestoreError(reason, path, message)


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
        _fail("docker_unavailable", command[0], "Docker CLI is required")
    except subprocess.TimeoutExpired:
        _fail(reason, path, f"Docker command timed out after {timeout} seconds")
    except OSError as exc:
        _fail(reason, path, f"cannot execute Docker command: {exc}")
    if result.returncode != 0:
        _fail(
            reason,
            path,
            f"Docker command failed with exit status {result.returncode}",
        )
    return result


def _single_record(
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


def _minecraft_volume(lock: dict[str, Any]) -> str:
    matches = [
        assignment["identity"]
        for assignment in lock["runtime"]["volumes"]
        if assignment["role"] == "minecraft-data"
    ]
    if len(matches) != 1:
        _fail(
            "restore_contract_unsupported",
            "runtime.volumes",
            "restore requires exactly one minecraft-data volume",
        )
    return matches[0]


def _minecraft_image(lock: dict[str, Any]) -> str:
    component = next(
        (
            item
            for item in lock["components"]
            if item["role"] == "minecraft-runtime"
        ),
        None,
    )
    if component is None:
        _fail(
            "restore_contract_unsupported",
            "components",
            "restore requires a minecraft-runtime component",
        )
    artifact = next(
        (
            item
            for item in lock["artifacts"]
            if item["id"] == component["artifact"]
        ),
        None,
    )
    if (
        artifact is None
        or artifact.get("kind") != "oci"
        or not all(artifact.get(key) for key in ("locator", "version", "digest"))
    ):
        _fail(
            "restore_contract_unsupported",
            "artifacts",
            "restore requires an exact OCI minecraft-runtime artifact",
        )
    return f"{artifact['locator']}:{artifact['version']}@{artifact['digest']}"


def plan_world_restore(
    project_root: Path,
    output: Path,
    archive: Path,
    *,
    source_world: str,
    expected_archive_sha256: str,
    expected_lock_identity: str,
    data_root: Traversable,
) -> WorldRestorePlan:
    """Build a read-only restore plan bound to the current render and archive."""
    try:
        verification = verify_toml_render_output(
            project_root,
            output,
            data_root=data_root,
        )
    except RenderContractError as exc:
        raise WorldRestoreError(exc.reason, exc.path, str(exc)) from exc
    lock = verification.lock
    if expected_lock_identity != lock["lock_identity"]:
        _fail(
            "restore_lock_identity_mismatch",
            "restore.expected_lock_identity",
            f"expected {expected_lock_identity} does not match current "
            f"{lock['lock_identity']}",
        )
    try:
        inventory = inspect_world_archive(
            archive,
            source_world=source_world,
            expected_sha256=expected_archive_sha256,
        )
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        _fail("restore_archive_invalid", archive, str(exc))

    target_world = lock["world"]["identity"]
    suffixes = {
        source_world: "",
        f"{source_world}_nether": "_nether",
        f"{source_world}_the_end": "_the_end",
    }
    mapping = tuple(
        (root, f"{target_world}{suffixes[root]}")
        for root in inventory["world_roots"]
    )
    transaction_id = expected_archive_sha256[:16]
    return WorldRestorePlan(
        status="planned",
        lock_identity=lock["lock_identity"],
        archive_path=archive.expanduser().resolve(),
        archive_sha256=inventory["archive_sha256"],
        volume=_minecraft_volume(lock),
        image=_minecraft_image(lock),
        world_mapping=mapping,
        world_entry_count=inventory["world_entry_count"],
        world_uncompressed_size_bytes=inventory[
            "world_uncompressed_size_bytes"
        ],
        transaction_id=transaction_id,
        rollback_name=f".mcrctl-world-restore-rollback-{transaction_id}",
    )


def _compose_base(
    output: Path,
    docker_prefix: list[str],
) -> list[str]:
    resolved = output.resolve()
    return docker_prefix + [
        "compose",
        "--ansi",
        "never",
        "--project-directory",
        str(resolved),
        "--file",
        str(resolved / "compose.yaml"),
    ]


def _helper_command(
    plan: WorldRestorePlan,
    docker_prefix: list[str],
    *,
    phase: str,
    script: str,
) -> list[str]:
    command = docker_prefix + [
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "/bin/sh",
        "--mount",
        f"type=volume,src={plan.volume},dst=/data",
    ]
    if phase == "prepare":
        command.extend(
            [
                "--mount",
                f"type=bind,src={plan.archive_path},dst=/recovery/archive.zip,readonly",
            ]
        )
    command.extend(
        [
            plan.image,
            "-eu",
            "-c",
            script,
            "mcrctl-world-restore",
            phase,
            plan.transaction_id,
        ]
    )
    if phase == "prepare":
        command.extend(source for source, _destination in plan.world_mapping)
    elif phase in {"cutover", "rollback"}:
        for source, destination in plan.world_mapping:
            command.extend([source, destination])
    return command


def _expected_volume_labels(lock: dict[str, Any]) -> dict[str, str]:
    return {
        "io.mc-remote.owner": "mcrctl",
        "io.mc-remote.deployment": lock["deployment"]["name"],
        "io.mc-remote.environment": lock["environment"]["identity"],
        "io.mc-remote.world": lock["world"]["identity"],
        "io.mc-remote.created-by-lock": lock["lock_identity"],
    }


def _preflight_runtime(
    plan: WorldRestorePlan,
    lock: dict[str, Any],
    output: Path,
    docker_context: str,
    runner: CommandRunner,
) -> tuple[list[str], list[str]]:
    if not DOCKER_CONTEXT.fullmatch(docker_context):
        _fail(
            "docker_context_invalid",
            "restore.docker_context",
            "Docker context must be an explicit name token",
        )
    context = _single_record(
        _run(
            runner,
            ["docker", "context", "inspect", docker_context],
            timeout=30,
            reason="docker_context_unavailable",
            path=docker_context,
        ),
        reason="docker_context_unavailable",
        path=docker_context,
    )
    endpoints = context.get("Endpoints")
    docker_endpoint = endpoints.get("docker") if isinstance(endpoints, dict) else None
    docker_host = (
        docker_endpoint.get("Host") if isinstance(docker_endpoint, dict) else None
    )
    if not isinstance(docker_host, str) or not docker_host.startswith("unix://"):
        _fail(
            "docker_context_not_local",
            docker_context,
            "world restore requires a local unix-socket Docker context",
        )

    docker_prefix = ["docker", "--context", docker_context]
    volume = _single_record(
        _run(
            runner,
            docker_prefix + ["volume", "inspect", plan.volume],
            timeout=30,
            reason="restore_volume_missing",
            path=plan.volume,
        ),
        reason="restore_volume_unmanaged",
        path=plan.volume,
    )
    if (
        volume.get("Name") != plan.volume
        or volume.get("Driver") != "local"
        or volume.get("Labels") != _expected_volume_labels(lock)
    ):
        _fail(
            "restore_volume_unmanaged",
            plan.volume,
            "target is not the exact current mcrctl-managed volume",
        )

    compose_base = _compose_base(output, docker_prefix)
    container_result = _run(
        runner,
        compose_base + ["ps", "--all", "--quiet", "minecraft"],
        timeout=30,
        reason="restore_runtime_missing",
        path="minecraft",
    )
    containers = [
        line.strip()
        for line in container_result.stdout.splitlines()
        if line.strip()
    ]
    if len(containers) != 1:
        _fail(
            "restore_runtime_missing",
            "minecraft",
            "restore requires exactly one current Minecraft container",
        )
    container = _single_record(
        _run(
            runner,
            docker_prefix + ["inspect", containers[0]],
            timeout=30,
            reason="restore_runtime_inspect_failed",
            path=containers[0],
        ),
        reason="restore_runtime_unmanaged",
        path=containers[0],
    )
    config = container.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    state = container.get("State")
    expected_labels = {
        "com.docker.compose.project": lock["deployment"]["name"],
        "com.docker.compose.service": "minecraft",
        "com.docker.compose.project.config_files": str(
            output.resolve() / "compose.yaml"
        ),
        "com.docker.compose.project.working_dir": str(output.resolve()),
        "io.mc-remote.deployment": lock["deployment"]["name"],
        "io.mc-remote.environment": lock["environment"]["identity"],
        "io.mc-remote.world": lock["world"]["identity"],
        "io.mc-remote.lock": lock["lock_identity"],
    }
    if (
        not isinstance(labels, dict)
        or any(labels.get(key) != value for key, value in expected_labels.items())
        or not isinstance(state, dict)
        or state.get("Running") is not True
    ):
        _fail(
            "restore_runtime_unmanaged",
            containers[0],
            "Minecraft container is not the running current-lock service",
        )
    return docker_prefix, compose_base


def apply_world_restore(
    project_root: Path,
    output: Path,
    archive: Path,
    *,
    source_world: str,
    expected_archive_sha256: str,
    expected_lock_identity: str,
    docker_context: str,
    data_root: Traversable,
    confirmed: bool,
    wait_timeout: int = 300,
    runner: CommandRunner = _default_runner,
    doctor: Doctor = doctor_toml_project,
    progress: ProgressReporter = _no_progress,
) -> WorldRestoreResult:
    """Restore selected world roots, retaining the prior roots for rollback."""
    if not confirmed:
        _fail(
            "restore_confirmation_required",
            "restore.confirmed",
            "live world replacement requires explicit --yes",
        )
    if wait_timeout < 30 or wait_timeout > 1800:
        _fail(
            "restore_wait_timeout_invalid",
            "restore.wait_timeout",
            "wait timeout must be between 30 and 1800 seconds",
        )
    progress("validate-plan")
    plan = plan_world_restore(
        project_root,
        output,
        archive,
        source_world=source_world,
        expected_archive_sha256=expected_archive_sha256,
        expected_lock_identity=expected_lock_identity,
        data_root=data_root,
    )
    verification = verify_toml_render_output(
        project_root,
        output,
        data_root=data_root,
    )
    lock = verification.lock
    progress("runtime-preflight")
    docker_prefix, compose_base = _preflight_runtime(
        plan,
        lock,
        output,
        docker_context,
        runner,
    )
    try:
        progress(
            "prepare-world-staging "
            f"bytes={plan.world_uncompressed_size_bytes} "
            f"entries={plan.world_entry_count}"
        )
        _run(
            runner,
            _helper_command(
                plan,
                docker_prefix,
                phase="prepare",
                script=PREPARE_SCRIPT,
            ),
            timeout=1800,
            reason="restore_prepare_failed",
            path=plan.archive_path,
        )
        progress("verify-archive-after-staging")
        inspect_world_archive(
            plan.archive_path,
            source_world=source_world,
            expected_sha256=expected_archive_sha256,
        )
    except WorldRestoreError:
        try:
            _run(
                runner,
                _helper_command(
                    plan,
                    docker_prefix,
                    phase="cleanup",
                    script=CLEANUP_SCRIPT,
                ),
                timeout=300,
                reason="restore_cleanup_failed",
                path=plan.volume,
            )
        except WorldRestoreError:
            pass
        raise
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        try:
            _run(
                runner,
                _helper_command(
                    plan,
                    docker_prefix,
                    phase="cleanup",
                    script=CLEANUP_SCRIPT,
                ),
                timeout=300,
                reason="restore_cleanup_failed",
                path=plan.volume,
            )
        except WorldRestoreError:
            pass
        _fail(
            "restore_archive_changed_during_prepare",
            plan.archive_path,
            str(exc),
        )

    start_command = compose_base + [
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        str(wait_timeout),
        "--no-build",
        "--pull",
        "never",
        "minecraft",
    ]
    try:
        progress("stop-minecraft")
        _run(
            runner,
            compose_base + ["stop", "--timeout", "120", "minecraft"],
            timeout=180,
            reason="restore_stop_failed",
            path="minecraft",
        )
    except WorldRestoreError:
        try:
            _run(
                runner,
                _helper_command(
                    plan,
                    docker_prefix,
                    phase="cleanup",
                    script=CLEANUP_SCRIPT,
                ),
                timeout=300,
                reason="restore_cleanup_failed",
                path=plan.volume,
            )
            _run(
                runner,
                start_command,
                timeout=wait_timeout + 60,
                reason="restore_restart_after_stop_failure_failed",
                path="minecraft",
            )
        except WorldRestoreError as recovery_exc:
            _fail(
                "restore_stop_failed_state_unknown",
                plan.volume,
                f"stop failed and runtime recovery also failed: {recovery_exc}",
            )
        raise

    try:
        progress("cutover-world-roots")
        _run(
            runner,
            _helper_command(
                plan,
                docker_prefix,
                phase="cutover",
                script=CUTOVER_SCRIPT,
            ),
            timeout=300,
            reason="restore_cutover_failed",
            path=plan.volume,
        )
    except WorldRestoreError as cutover_exc:
        progress("rollback-after-cutover-failure")
        try:
            _run(
                runner,
                _helper_command(
                    plan,
                    docker_prefix,
                    phase="rollback",
                    script=ROLLBACK_SCRIPT,
                ),
                timeout=300,
                reason="restore_rollback_failed",
                path=plan.volume,
            )
            _run(
                runner,
                start_command,
                timeout=wait_timeout + 60,
                reason="restore_rollback_start_failed",
                path="minecraft",
            )
        except WorldRestoreError as rollback_exc:
            _fail(
                "restore_failed_rollback_failed",
                plan.volume,
                f"cutover failed ({cutover_exc}); rollback also failed "
                f"({rollback_exc})",
            )
        _fail(
            "restore_failed_rolled_back",
            plan.volume,
            f"cutover failed and prior world was restarted: {cutover_exc}",
        )

    try:
        progress(f"start-minecraft-and-wait timeout={wait_timeout}")
        _run(
            runner,
            start_command,
            timeout=wait_timeout + 60,
            reason="restore_start_failed",
            path="minecraft",
        )
        progress("doctor")
        doctor(
            project_root,
            output,
            docker_context=docker_context,
            data_root=data_root,
            runner=runner,
        )
    except Exception as exc:
        progress("rollback-after-verification-failure")
        try:
            _run(
                runner,
                compose_base + ["stop", "--timeout", "120", "minecraft"],
                timeout=180,
                reason="restore_rollback_stop_failed",
                path="minecraft",
            )
            _run(
                runner,
                _helper_command(
                    plan,
                    docker_prefix,
                    phase="rollback",
                    script=ROLLBACK_SCRIPT,
                ),
                timeout=300,
                reason="restore_rollback_failed",
                path=plan.volume,
            )
            _run(
                runner,
                start_command,
                timeout=wait_timeout + 60,
                reason="restore_rollback_start_failed",
                path="minecraft",
            )
        except WorldRestoreError as rollback_exc:
            _fail(
                "restore_failed_rollback_failed",
                plan.volume,
                f"restore failed ({exc}); rollback also failed ({rollback_exc})",
            )
        _fail(
            "restore_failed_rolled_back",
            plan.volume,
            f"restored runtime failed verification and prior world was restarted: {exc}",
        )

    progress("complete")
    return WorldRestoreResult(
        status="restored-healthy",
        lock_identity=plan.lock_identity,
        archive_sha256=plan.archive_sha256,
        volume=plan.volume,
        world_mapping=plan.world_mapping,
        rollback_name=plan.rollback_name,
    )
