import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from mc_remote_stack.archive import inspect_world_archive
from mc_remote_stack.cli import main
from mc_remote_stack.restore import (
    WorldRestoreError,
    WorldRestorePlan,
    WorldRestoreResult,
    apply_world_restore,
    plan_world_restore,
)

from .test_toml_apply import _prepared_credential_project, _prepared_project


def _world_archive(path: Path, *, unsafe: bool = False) -> str:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("world/level.dat", b"overworld")
        archive.writestr("world/region/r.0.0.mca", b"region")
        archive.writestr("world_nether/level.dat", b"nether")
        archive.writestr("world_the_end/level.dat", b"end")
        archive.writestr(
            "plugins/McRemote/credential-store.fixture",
            b"token-hash\nrevoke-tombstone\n",
        )
        if unsafe:
            archive.writestr("../escape", b"unsafe")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_world_archive_rejects_unsafe_entries_and_selects_only_world_roots(
    tmp_path: Path,
) -> None:
    safe = tmp_path / "safe.zip"
    sha256 = _world_archive(safe)

    result = inspect_world_archive(
        safe,
        source_world="world",
        expected_sha256=sha256,
    )

    assert result["world_roots"] == ["world", "world_nether", "world_the_end"]
    assert result["world_entry_count"] == 4
    assert "plugins" not in json.dumps(result)

    unsafe = tmp_path / "unsafe.zip"
    unsafe_sha256 = _world_archive(unsafe, unsafe=True)
    with pytest.raises(ValueError, match="unsafe ZIP entry"):
        inspect_world_archive(
            unsafe,
            source_world="world",
            expected_sha256=unsafe_sha256,
        )


def test_world_restore_plan_is_bound_to_lock_archive_and_world_mapping(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    archive = tmp_path / "backup.zip"
    archive_sha256 = _world_archive(archive)

    result = plan_world_restore(
        project,
        output,
        archive,
        source_world="world",
        expected_archive_sha256=archive_sha256,
        expected_lock_identity=lock["lock_identity"],
        data_root=data_root,
    )

    assert result.status == "planned"
    assert result.volume == "home-beta-minecraft-data"
    assert result.world_mapping == (
        ("world", "home-beta-world"),
        ("world_nether", "home-beta-world_nether"),
        ("world_the_end", "home-beta-world_the_end"),
    )
    assert result.rollback_name.startswith(".mcrctl-world-restore-rollback-")


def test_credential_profile_restore_targets_only_world_volume(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_credential_project(tmp_path)
    archive = tmp_path / "backup.zip"
    archive_sha256 = _world_archive(archive)

    result = plan_world_restore(
        project,
        output,
        archive,
        source_world="world",
        expected_archive_sha256=archive_sha256,
        expected_lock_identity=lock["lock_identity"],
        data_root=data_root,
    )

    assert result.volume == "home-alpha-minecraft-data"
    assert result.volume != "home-alpha-credential-store"
    assert result.volume != "home-alpha-credential-revocations"
    assert "plugins" not in json.dumps(result.world_mapping)


class RestoreDocker:
    def __init__(
        self,
        lock: dict,
        volume: str,
        *,
        output: Path,
        fail_first_start: bool = False,
        extra_compose_file: bool = False,
    ) -> None:
        self.lock = lock
        self.volume = volume
        self.output = output.resolve()
        self.fail_first_start = fail_first_start
        self.extra_compose_file = extra_compose_file
        self.start_calls = 0
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        command: list[str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        call = tuple(command)
        self.calls.append(call)
        if call[:4] == ("docker", "context", "inspect", "default"):
            stdout = json.dumps(
                [{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}]
            )
        elif "volume" in call and "inspect" in call:
            stdout = json.dumps(
                [
                    {
                        "Name": self.volume,
                        "Driver": "local",
                        "Labels": {
                            "io.mc-remote.owner": "mcrctl",
                            "io.mc-remote.deployment": "home",
                            "io.mc-remote.environment": "home-beta",
                            "io.mc-remote.world": "home-beta-world",
                            "io.mc-remote.created-by-lock": self.lock[
                                "lock_identity"
                            ],
                        },
                    }
                ]
            )
        elif "compose" in call and "ps" in call:
            stdout = "minecraft-container\n"
        elif call[-2:] == ("inspect", "minecraft-container"):
            stdout = json.dumps(
                [
                    {
                        "Config": {
                            "Labels": {
                                "com.docker.compose.project": "home",
                                "com.docker.compose.service": "minecraft",
                                "com.docker.compose.project.config_files": (
                                    f"{self.output / 'compose.yaml'},"
                                    f"{self.output / 'override.yaml'}"
                                    if self.extra_compose_file
                                    else str(self.output / "compose.yaml")
                                ),
                                "com.docker.compose.project.working_dir": str(
                                    self.output
                                ),
                                "io.mc-remote.deployment": "home",
                                "io.mc-remote.environment": "home-beta",
                                "io.mc-remote.world": "home-beta-world",
                                "io.mc-remote.lock": self.lock["lock_identity"],
                            }
                        },
                        "State": {"Running": True},
                    }
                ]
            )
        elif "compose" in call and "up" in call:
            self.start_calls += 1
            if self.fail_first_start and self.start_calls == 1:
                return subprocess.CompletedProcess(command, 1, "", "failed")
            stdout = ""
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout, "")


def test_world_restore_apply_stops_cutover_starts_and_doctors(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    archive = tmp_path / "backup.zip"
    archive_sha256 = _world_archive(archive)
    runner = RestoreDocker(
        lock,
        "home-beta-minecraft-data",
        output=output,
    )
    doctor_calls = 0

    def doctor(*args, **kwargs) -> None:
        nonlocal doctor_calls
        doctor_calls += 1

    result = apply_world_restore(
        project,
        output,
        archive,
        source_world="world",
        expected_archive_sha256=archive_sha256,
        expected_lock_identity=lock["lock_identity"],
        docker_context="default",
        data_root=data_root,
        confirmed=True,
        runner=runner,
        doctor=doctor,
    )

    assert result.status == "restored-healthy"
    assert doctor_calls == 1
    commands = [" ".join(call) for call in runner.calls]
    assert any("prepare" in command for command in commands)
    assert any(" stop --timeout 120 minecraft" in command for command in commands)
    assert any("cutover" in command for command in commands)
    assert any(" up --detach --wait" in command for command in commands)

    prepare_call = next(call for call in runner.calls if "prepare" in call)
    prepare_phase = prepare_call.index("prepare")
    assert prepare_call[prepare_phase + 2 :] == (
        "world",
        "world_nether",
        "world_the_end",
    )
    assert not any(
        argument.startswith("plugins")
        for argument in prepare_call[prepare_phase + 2 :]
    )


def test_world_restore_apply_requires_explicit_confirmation(tmp_path: Path) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    archive = tmp_path / "backup.zip"
    archive_sha256 = _world_archive(archive)

    with pytest.raises(WorldRestoreError) as exc_info:
        apply_world_restore(
            project,
            output,
            archive,
            source_world="world",
            expected_archive_sha256=archive_sha256,
            expected_lock_identity=lock["lock_identity"],
            docker_context="default",
            data_root=data_root,
            confirmed=False,
        )

    assert exc_info.value.reason == "restore_confirmation_required"


def test_world_restore_start_failure_rolls_back_and_restarts_prior_world(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    archive = tmp_path / "backup.zip"
    archive_sha256 = _world_archive(archive)
    runner = RestoreDocker(
        lock,
        "home-beta-minecraft-data",
        output=output,
        fail_first_start=True,
    )

    with pytest.raises(WorldRestoreError) as exc_info:
        apply_world_restore(
            project,
            output,
            archive,
            source_world="world",
            expected_archive_sha256=archive_sha256,
            expected_lock_identity=lock["lock_identity"],
            docker_context="default",
            data_root=data_root,
            confirmed=True,
            runner=runner,
            doctor=lambda *args, **kwargs: None,
        )

    assert exc_info.value.reason == "restore_failed_rolled_back"
    assert runner.start_calls == 2
    assert any("rollback" in " ".join(call) for call in runner.calls)


def test_world_restore_rejects_runtime_started_with_extra_compose_file(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    archive = tmp_path / "backup.zip"
    archive_sha256 = _world_archive(archive)
    runner = RestoreDocker(
        lock,
        "home-beta-minecraft-data",
        output=output,
        extra_compose_file=True,
    )

    with pytest.raises(WorldRestoreError) as exc_info:
        apply_world_restore(
            project,
            output,
            archive,
            source_world="world",
            expected_archive_sha256=archive_sha256,
            expected_lock_identity=lock["lock_identity"],
            docker_context="default",
            data_root=data_root,
            confirmed=True,
            runner=runner,
            doctor=lambda *args, **kwargs: None,
        )

    assert exc_info.value.reason == "restore_runtime_unmanaged"
    assert not any("prepare" in " ".join(call) for call in runner.calls)


def test_world_restore_cli_keeps_plan_and_apply_explicit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project = tmp_path / "deployment"
    output = project / "generated"
    archive = tmp_path / "backup.zip"
    lock_identity = f"sha256:{'1' * 64}"
    archive_sha256 = "2" * 64
    plan = WorldRestorePlan(
        status="planned",
        lock_identity=lock_identity,
        archive_path=archive,
        archive_sha256=archive_sha256,
        volume="beta-data",
        image="example.invalid/minecraft:tag@sha256:" + "3" * 64,
        world_mapping=(("world", "beta-world"),),
        world_entry_count=10,
        world_uncompressed_size_bytes=1000,
        transaction_id=archive_sha256[:16],
        rollback_name=".mcrctl-world-restore-rollback-" + archive_sha256[:16],
    )

    monkeypatch.setattr(
        "mc_remote_stack.cli.plan_world_restore",
        lambda *args, **kwargs: plan,
    )

    def fake_apply(*args, progress, **kwargs):
        progress("prepare-world-staging bytes=1000 entries=10")
        progress("complete")
        return WorldRestoreResult(
            status="restored-healthy",
            lock_identity=lock_identity,
            archive_sha256=archive_sha256,
            volume="beta-data",
            world_mapping=(("world", "beta-world"),),
            rollback_name=plan.rollback_name,
        )

    monkeypatch.setattr("mc_remote_stack.cli.apply_world_restore", fake_apply)
    common = [
        str(archive),
        "--project",
        str(project),
        "--output",
        str(output),
        "--source-world",
        "world",
        "--expected-archive-sha256",
        archive_sha256,
        "--expected-lock-identity",
        lock_identity,
    ]

    assert main(["world", "restore", "plan", *common]) == 0
    assert main(["world", "restore", "apply", *common, "--yes"]) == 0

    output_text = capsys.readouterr().out
    assert "PLAN world-restore" in output_text
    assert "world=world->beta-world" in output_text
    assert "STEP world restore prepare-world-staging" in output_text
    assert "OK world restore status=restored-healthy" in output_text
    assert f"rollback={plan.rollback_name}" in output_text
