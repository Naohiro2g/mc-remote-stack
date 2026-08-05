import fcntl
import os
from importlib.resources import files
from pathlib import Path

import pytest

from mc_remote_stack.auth_migration import (
    AuthMigrationContractError,
    AuthMigrationResult,
    apply_auth_enforcement_migration,
    load_auth_migration_state,
    plan_auth_enforcement_migration,
)
from mc_remote_stack.cli import main
from mc_remote_stack.render import render_toml_project
from mc_remote_stack.resolver import load_lock

from .test_toml_render import _render_fixture


class RecordingHost:
    def __init__(self, *, fail_start_once: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_start_once = fail_start_once

    def inspect_source(self, plan) -> None:
        self.calls.append("inspect-source")

    def inspect_targets_absent(self, plan) -> None:
        self.calls.append("inspect-targets-absent")

    def pull_target(self, plan) -> None:
        self.calls.append("pull-target")

    def create_target_volumes(self, plan) -> None:
        self.calls.append("create-target-volumes")

    def stop_source(self, plan, source_output: Path) -> None:
        assert source_output.name == "source-render"
        self.calls.append("stop-source")

    def copy_volumes(self, plan) -> None:
        self.calls.append("copy-volumes")

    def start_target(self, plan) -> None:
        self.calls.append("start-target")
        if self.fail_start_once:
            self.fail_start_once = False
            raise AuthMigrationContractError(
                "migration_target_start_failed",
                "docker.compose",
                "fixture",
            )

    def verify_target(self, plan) -> None:
        self.calls.append("verify-target")


def _prepared_migration_project(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    project, data_root, _ = _render_fixture(
        tmp_path,
        deployment_name="home-alpha",
        identity="home-alpha",
        channel="alpha",
        preset_revision="2",
        profile_revision="2",
    )
    target_profile = data_root / "profiles" / "home-server" / "4" / "profile.toml"
    target_profile.parent.mkdir(parents=True)
    target_profile.write_text(
        files("mc_remote_stack")
        .joinpath("data", "profiles", "home-server", "4", "profile.toml")
        .read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    output = project / "generated"
    render_toml_project(project, output, data_root=data_root)
    return project, data_root, output, load_lock(project, data_root=data_root)


def test_plan_builds_auth_enforced_candidate_without_mutating_source(
    tmp_path: Path,
) -> None:
    project, data_root, output, source_lock = _prepared_migration_project(tmp_path)
    before = {
        path.relative_to(project): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }
    host = RecordingHost()

    plan = plan_auth_enforcement_migration(
        project,
        output,
        docker_context="default",
        target_volumes={
            "minecraft-data": "home-alpha-auth-minecraft-data",
        },
        data_root=data_root,
        allow_unverified=True,
        host=host,
    )

    after = {
        path.relative_to(project): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert plan.source_lock_identity == source_lock["lock_identity"]
    assert plan.source_profile == "home-server@2"
    assert plan.target_profile == "home-server@4"
    assert plan.volume_migrations == (
        (
            "minecraft-data",
            "home-alpha-minecraft-data",
            "home-alpha-auth-minecraft-data",
        ),
    )
    assert plan.target_lock_identity != plan.source_lock_identity
    assert host.calls == ["inspect-source", "inspect-targets-absent"]


def test_plan_records_reviewed_compose_files_without_reading_private_config(
    tmp_path: Path,
) -> None:
    project, data_root, output, _source_lock = _prepared_migration_project(tmp_path)
    recovery = project / "recovery"
    recovery.mkdir()
    preserved = recovery / "compose.plugins.yaml"
    preserved.write_text("services:\n  minecraft: {}\n", encoding="utf-8")
    private_config = tmp_path / "private-config"
    private_config.mkdir()
    (private_config / "secret.yml").write_text("do-not-read\n", encoding="utf-8")

    plan = plan_auth_enforcement_migration(
        project,
        output,
        docker_context="default",
        target_volumes={"minecraft-data": "home-alpha-auth-minecraft-data"},
        preserved_compose_files=(preserved,),
        auth_config_root=private_config,
        data_root=data_root,
        allow_unverified=True,
        host=RecordingHost(),
    )

    assert plan.preserved_compose_files == (preserved.resolve(),)
    assert plan.preserved_composition_identity.startswith("sha256:")
    assert plan.auth_config_root == private_config.resolve()
    assert (private_config / "secret.yml").read_text(encoding="utf-8") == "do-not-read\n"


def test_plan_rejects_symlinked_preserved_compose_file(tmp_path: Path) -> None:
    project, data_root, output, _source_lock = _prepared_migration_project(tmp_path)
    recovery = project / "recovery"
    recovery.mkdir()
    actual = recovery / "actual.yaml"
    actual.write_text("services: {}\n", encoding="utf-8")
    preserved = recovery / "compose.plugins.yaml"
    preserved.symlink_to(actual)
    private_config = tmp_path / "private-config"
    private_config.mkdir()

    with pytest.raises(AuthMigrationContractError) as exc_info:
        plan_auth_enforcement_migration(
            project,
            output,
            docker_context="default",
            target_volumes={"minecraft-data": "home-alpha-auth-minecraft-data"},
            preserved_compose_files=(preserved,),
            auth_config_root=private_config,
            data_root=data_root,
            allow_unverified=True,
            host=RecordingHost(),
        )

    assert exc_info.value.reason == "migration_preserved_composition_invalid"


def test_apply_installs_only_generated_auth_config_for_preserved_composition(
    tmp_path: Path,
) -> None:
    project, data_root, output, source_lock = _prepared_migration_project(tmp_path)
    recovery = project / "recovery"
    recovery.mkdir()
    preserved = recovery / "compose.plugins.yaml"
    preserved.write_text("services:\n  minecraft: {}\n", encoding="utf-8")
    private_config = tmp_path / "private-config"
    private_config.mkdir()
    secret = private_config / "secret.yml"
    secret.write_text("do-not-read\n", encoding="utf-8")
    volumes = {"minecraft-data": "home-alpha-auth-minecraft-data"}
    plan = plan_auth_enforcement_migration(
        project,
        output,
        docker_context="default",
        target_volumes=volumes,
        preserved_compose_files=(preserved,),
        auth_config_root=private_config,
        data_root=data_root,
        allow_unverified=True,
        host=RecordingHost(),
    )

    result = apply_auth_enforcement_migration(
        project,
        output,
        docker_context="default",
        target_volumes=volumes,
        preserved_compose_files=(preserved,),
        auth_config_root=private_config,
        expected_source_lock_identity=source_lock["lock_identity"],
        expected_target_lock_identity=plan.target_lock_identity,
        expected_preserved_composition_identity=plan.preserved_composition_identity,
        data_root=data_root,
        allow_unverified=True,
        confirmed=True,
        host=RecordingHost(),
    )

    installed = private_config / "plugins" / "McRemote" / "config.yml"
    assert result.phase == "complete"
    assert "auth:\n  enforcement: true\n" in installed.read_text(encoding="utf-8")
    assert secret.read_text(encoding="utf-8") == "do-not-read\n"


def test_apply_rejects_preserved_compose_changed_after_review(tmp_path: Path) -> None:
    project, data_root, output, source_lock = _prepared_migration_project(tmp_path)
    recovery = project / "recovery"
    recovery.mkdir()
    preserved = recovery / "compose.plugins.yaml"
    preserved.write_text("services:\n  minecraft: {}\n", encoding="utf-8")
    private_config = tmp_path / "private-config"
    private_config.mkdir()
    volumes = {"minecraft-data": "home-alpha-auth-minecraft-data"}
    plan = plan_auth_enforcement_migration(
        project,
        output,
        docker_context="default",
        target_volumes=volumes,
        preserved_compose_files=(preserved,),
        auth_config_root=private_config,
        data_root=data_root,
        allow_unverified=True,
        host=RecordingHost(),
    )
    preserved.write_text("services:\n  minecraft:\n    restart: never\n", encoding="utf-8")

    with pytest.raises(AuthMigrationContractError) as exc_info:
        apply_auth_enforcement_migration(
            project,
            output,
            docker_context="default",
            target_volumes=volumes,
            preserved_compose_files=(preserved,),
            auth_config_root=private_config,
            expected_source_lock_identity=source_lock["lock_identity"],
            expected_target_lock_identity=plan.target_lock_identity,
            expected_preserved_composition_identity=plan.preserved_composition_identity,
            data_root=data_root,
            allow_unverified=True,
            confirmed=True,
            host=RecordingHost(),
        )

    assert exc_info.value.reason == "migration_expected_composition_mismatch"
    assert not (
        project / ".mcrctl" / "migrations" / "auth-enforcement" / "state.json"
    ).exists()


def test_apply_failure_stays_stopped_and_same_transaction_resumes(
    tmp_path: Path,
) -> None:
    project, data_root, output, source_lock = _prepared_migration_project(tmp_path)
    host = RecordingHost(fail_start_once=True)
    progress: list[str] = []
    kwargs = {
        "project_root": project,
        "output": output,
        "docker_context": "default",
        "target_volumes": {
            "minecraft-data": "home-alpha-auth-minecraft-data",
        },
        "expected_source_lock_identity": source_lock["lock_identity"],
        "data_root": data_root,
        "allow_unverified": True,
        "confirmed": True,
        "host": host,
        "progress": progress.append,
    }
    planned = plan_auth_enforcement_migration(
        project,
        output,
        docker_context="default",
        target_volumes=kwargs["target_volumes"],
        data_root=data_root,
        allow_unverified=True,
        host=RecordingHost(),
    )
    kwargs["expected_target_lock_identity"] = planned.target_lock_identity

    with pytest.raises(AuthMigrationContractError) as exc_info:
        apply_auth_enforcement_migration(**kwargs)

    assert exc_info.value.reason == "migration_target_start_failed"
    state = load_auth_migration_state(project)
    assert state["phase"] == "volumes-copied"
    assert state["last_error"] == {
        "reason": "migration_target_start_failed",
        "path": "docker.compose",
    }
    assert host.calls == [
        "inspect-source",
        "inspect-targets-absent",
        "pull-target",
        "create-target-volumes",
        "stop-source",
        "copy-volumes",
        "start-target",
    ]
    assert host.calls.count("stop-source") == 1
    assert "start-source" not in host.calls
    assert progress[:7] == [
        "pull-target-images",
        "create-target-volumes",
        "stop-source-runtime",
        "publish-desired-state",
        "install-auth-config",
        "copy-volumes",
        "start-target-and-wait timeout=300",
    ]

    result = apply_auth_enforcement_migration(**kwargs)

    assert result == AuthMigrationResult(
        status="resumed-complete",
        source_lock_identity=source_lock["lock_identity"],
        target_lock_identity=planned.target_lock_identity,
        phase="complete",
    )
    assert host.calls.count("stop-source") == 1
    assert host.calls.count("copy-volumes") == 1
    assert host.calls.count("start-target") == 2
    assert host.calls[-1] == "verify-target"
    assert load_auth_migration_state(project)["phase"] == "complete"


def test_existing_transaction_rejects_changed_expected_identity(tmp_path: Path) -> None:
    project, data_root, output, source_lock = _prepared_migration_project(tmp_path)
    host = RecordingHost(fail_start_once=True)
    plan = plan_auth_enforcement_migration(
        project,
        output,
        docker_context="default",
        target_volumes={"minecraft-data": "home-alpha-auth-minecraft-data"},
        data_root=data_root,
        allow_unverified=True,
        host=RecordingHost(),
    )
    with pytest.raises(AuthMigrationContractError):
        apply_auth_enforcement_migration(
            project,
            output,
            docker_context="default",
            target_volumes={"minecraft-data": "home-alpha-auth-minecraft-data"},
            expected_source_lock_identity=source_lock["lock_identity"],
            expected_target_lock_identity=plan.target_lock_identity,
            data_root=data_root,
            allow_unverified=True,
            confirmed=True,
            host=host,
        )

    with pytest.raises(AuthMigrationContractError) as exc_info:
        apply_auth_enforcement_migration(
            project,
            output,
            docker_context="default",
            target_volumes={"minecraft-data": "home-alpha-auth-minecraft-data"},
            expected_source_lock_identity=source_lock["lock_identity"],
            expected_target_lock_identity="sha256:" + "0" * 64,
            data_root=data_root,
            allow_unverified=True,
            confirmed=True,
            host=host,
        )

    assert exc_info.value.reason == "migration_transaction_mismatch"


def test_apply_without_confirmation_does_not_create_transaction_state(
    tmp_path: Path,
) -> None:
    project, data_root, output, source_lock = _prepared_migration_project(tmp_path)
    plan = plan_auth_enforcement_migration(
        project,
        output,
        docker_context="default",
        target_volumes={"minecraft-data": "home-alpha-auth-minecraft-data"},
        data_root=data_root,
        allow_unverified=True,
        host=RecordingHost(),
    )

    with pytest.raises(AuthMigrationContractError) as exc_info:
        apply_auth_enforcement_migration(
            project,
            output,
            docker_context="default",
            target_volumes={"minecraft-data": "home-alpha-auth-minecraft-data"},
            expected_source_lock_identity=source_lock["lock_identity"],
            expected_target_lock_identity=plan.target_lock_identity,
            data_root=data_root,
            allow_unverified=True,
            confirmed=False,
            host=RecordingHost(),
        )

    assert exc_info.value.reason == "migration_confirmation_required"
    assert not (project / ".mcrctl").exists()


def test_apply_rejects_concurrent_transaction_process(tmp_path: Path) -> None:
    project, data_root, output, source_lock = _prepared_migration_project(tmp_path)
    plan = plan_auth_enforcement_migration(
        project,
        output,
        docker_context="default",
        target_volumes={"minecraft-data": "home-alpha-auth-minecraft-data"},
        data_root=data_root,
        allow_unverified=True,
        host=RecordingHost(),
    )
    lock_path = project / ".mcrctl" / "migrations" / "auth-enforcement.lock"
    lock_path.parent.mkdir(parents=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o640)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(AuthMigrationContractError) as exc_info:
            apply_auth_enforcement_migration(
                project,
                output,
                docker_context="default",
                target_volumes={"minecraft-data": "home-alpha-auth-minecraft-data"},
                expected_source_lock_identity=source_lock["lock_identity"],
                expected_target_lock_identity=plan.target_lock_identity,
                data_root=data_root,
                allow_unverified=True,
                confirmed=True,
                host=RecordingHost(),
            )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    assert exc_info.value.reason == "migration_concurrent_apply"


def test_cli_auth_migration_plan_and_apply_forward_exact_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "deployment"
    output = project / "generated"
    source = "sha256:" + "1" * 64
    target = "sha256:" + "2" * 64
    plan = pytest.importorskip("mc_remote_stack.auth_migration").AuthMigrationPlan(
        project_root=project.resolve(),
        output=output.absolute(),
        docker_context="default",
        source_lock_identity=source,
        target_lock_identity=target,
        source_profile="home-server@2",
        target_profile="home-server@4",
        deployment="home-alpha",
        environment="home-alpha",
        services=("minecraft",),
        volume_migrations=(("minecraft-data", "old-volume", "new-volume"),),
        preserved_compose_files=(project / "recovery" / "compose.plugins.yaml",),
        preserved_compose_sha256=("3" * 64,),
        preserved_composition_identity="sha256:" + "4" * 64,
        auth_config_root=project / "private-config",
    )
    received: dict[str, object] = {}

    monkeypatch.setattr(
        "mc_remote_stack.cli.plan_auth_enforcement_migration",
        lambda *_args, **_kwargs: plan,
    )

    def fake_apply(*_args, **kwargs):
        received.update(kwargs)
        return AuthMigrationResult("complete", source, target, "complete")

    monkeypatch.setattr("mc_remote_stack.cli.apply_auth_enforcement_migration", fake_apply)

    common = [
        "--project",
        str(project),
        "--output",
        str(output),
        "--docker-context",
        "default",
        "--target-volume",
        "minecraft-data=new-volume",
        "--allow-unverified",
        "--preserve-compose-file",
        str(project / "recovery" / "compose.plugins.yaml"),
        "--auth-config-root",
        str(project / "private-config"),
    ]
    assert main(["migration", "auth-enforcement", "plan", *common]) == 0
    assert main(
        [
            "migration",
            "auth-enforcement",
            "apply",
            *common,
            "--expected-source-lock-identity",
            source,
            "--expected-target-lock-identity",
            target,
            "--expected-preserved-composition-identity",
            plan.preserved_composition_identity,
            "--yes",
        ]
    ) == 0

    assert received["expected_source_lock_identity"] == source
    assert received["expected_target_lock_identity"] == target
    assert received["expected_preserved_composition_identity"] == plan.preserved_composition_identity
    assert received["target_volumes"] == {"minecraft-data": "new-volume"}
    assert received["preserved_compose_files"] == (
        project / "recovery" / "compose.plugins.yaml",
    )
    assert received["auth_config_root"] == project / "private-config"
    text = capsys.readouterr().out
    assert "PLAN migration=auth-enforcement" in text
    assert "PLAN volume=minecraft-data:old-volume->new-volume" in text
    assert "OK migration auth-enforcement status=complete" in text
