import copy
import shutil
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from mc_remote_stack import auth_migration
from mc_remote_stack.auth_migration import (
    AuthMigrationContractError,
    AuthMigrationPlan,
    AuthMigrationResult,
    MigrationSpec,
    _activate_migration,
    _build_candidate,
    _DockerMigrationHost,
    _install_auth_config,
    _migration_root,
    _validate_migration_candidate,
)
from mc_remote_stack.cli import main
from mc_remote_stack.toml_project import init_toml_project, load_order


def _release_lock(*, target: bool = False) -> dict:
    profile = "vps-server@6" if target else "vps-server@5"
    preset = "public-web-paper@2" if target else "public-web-paper@1"
    controls = ["online-mode", "mcremote-auth-enforced"]
    if target:
        controls.append("mcremote-session-only")
    return {
        "input": {
            "profile": {"ref": profile},
            "preset": {"ref": preset},
        },
        "deployment": {"name": "official-public-beta"},
        "environment": {"identity": "official-public-beta"},
        "world": {"identity": "official-public-beta-world"},
        "network": {"bind_address": "0.0.0.0"},
        "agreements": {"minecraft_eula": True},
        "acknowledgements": {"allow_unverified": True},
        "operator_inputs": [{"role": "public-routes", "sha256": "fixture"}],
        "secret_references": [],
        "scope": {"channel": "beta"},
        "runtime": {
            "artifact_store": "/artifacts",
            "volumes": [
                {
                    "role": "minecraft-data",
                    "identity": "target-world" if target else "source-world",
                }
            ],
        },
        "components": [{"id": "mcremote", "version": "b3" if target else "b2"}],
        "artifacts": [{"id": "mcremote", "sha256": "b3" if target else "b2"}],
        "selection": {"preset": preset},
        "preset_lifecycle": {"status": "active"},
        "render_plan": {
            "adapter": "compose",
            "adapter_revision": "8" if target else "7",
            "services": [{"id": "minecraft", "role": "minecraft"}],
            "volume_roles": [{"id": "minecraft-data", "kind": "world"}],
            "operator_inputs": [{"role": "public-routes"}],
            "required_security_controls": controls,
        },
    }


def test_public_b3_migration_has_independent_state_root() -> None:
    project = Path("/deployment")

    with _activate_migration(auth_migration.PUBLIC_B3_MIGRATION):
        assert _migration_root(project) == (
            project / ".mcrctl" / "migrations" / "public-b3"
        )

    assert _migration_root(project) == (
        project / ".mcrctl" / "migrations" / "auth-enforcement"
    )


def test_public_b3_candidate_allows_only_reviewed_release_transition() -> None:
    source = _release_lock()
    target = _release_lock(target=True)

    with _activate_migration(auth_migration.PUBLIC_B3_MIGRATION):
        _validate_migration_candidate(source, target)

        target["world"] = {"identity": "wrong-world"}
        with pytest.raises(AuthMigrationContractError) as exc_info:
            _validate_migration_candidate(source, target)

    assert exc_info.value.reason == "migration_transition_not_reviewed"


def test_public_b4_migration_has_exact_vps_transition() -> None:
    spec = auth_migration.PUBLIC_B4_MIGRATION

    assert spec.name == "public-b4"
    assert spec.profile_transitions == {"vps-server@7": "vps-server@8"}
    assert spec.preset_transitions == {"public-web-paper@2": "public-web-paper@3"}
    with _activate_migration(spec):
        assert _migration_root(Path("/deployment")) == (
            Path("/deployment/.mcrctl/migrations/public-b4")
        )


def test_public_b4_candidate_allows_only_reviewed_release_transition() -> None:
    source = _release_lock(target=True)
    source["input"]["profile"]["ref"] = "vps-server@7"
    source["render_plan"]["adapter_revision"] = "9"
    source["operator_inputs"] = [
        {"role": "connection-targets", "sha256": "fixture"}
    ]
    source["render_plan"]["operator_inputs"] = [
        {"role": "connection-targets"}
    ]
    target = copy.deepcopy(source)
    target["input"]["profile"]["ref"] = "vps-server@8"
    target["input"]["preset"]["ref"] = "public-web-paper@3"
    target["selection"]["preset"] = "public-web-paper@3"
    target["render_plan"]["adapter_revision"] = "10"
    target["runtime"]["volumes"][0]["identity"] = "target-world"
    target["components"] = [{"id": "mcremote", "version": "b4"}]
    target["artifacts"] = [{"id": "mcremote", "sha256": "b4"}]

    with _activate_migration(auth_migration.PUBLIC_B4_MIGRATION):
        _validate_migration_candidate(source, target)


def test_public_b4_target_requires_one_shot_credential_health_acknowledgement() -> None:
    host = object.__new__(_DockerMigrationHost)
    host.credential_health_acknowledged = False

    with _activate_migration(auth_migration.PUBLIC_B4_MIGRATION):
        with pytest.raises(AuthMigrationContractError) as exc_info:
            host.verify_target(object())

    assert exc_info.value.reason == (
        "migration_credential_health_acknowledgement_required"
    )


def test_public_migration_rejects_compose_that_masks_exact_plugin_artifact() -> None:
    lock = {
        "runtime": {"artifact_store": "/artifacts"},
        "components": [
            {
                "role": "mcremote-plugin",
                "artifact": "mcremote-jar",
            }
        ],
        "artifacts": [
            {
                "id": "mcremote-jar",
                "filename": "mc-remote-b4.jar",
                "sha256": "1" * 64,
            }
        ],
    }
    service = {
        "volumes": [
            {
                "type": "bind",
                "source": "/recovery/plugins",
                "target": "/plugins",
                "read_only": True,
            }
        ]
    }

    with pytest.raises(AuthMigrationContractError) as exc_info:
        auth_migration._validate_effective_mcremote_mount(
            service,
            lock,
            path="migration.source.minecraft",
        )

    assert exc_info.value.reason == "migration_artifact_mount_mismatch"


def test_public_b3_candidate_updates_profile_preset_and_new_volumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[tuple[tuple[str, ...], str]] = []
    volume_updates: list[tuple[str, str]] = []
    monkeypatch.setattr(auth_migration, "_copy_project_source", lambda *_args: None)
    monkeypatch.setattr(
        auth_migration,
        "update_order_scalar",
        lambda _root, key, value: updates.append((key, value)),
    )
    monkeypatch.setattr(
        auth_migration,
        "update_order_volume_identity",
        lambda _root, role, value: volume_updates.append((role, value)),
    )
    monkeypatch.setattr(auth_migration, "resolve_project", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_migration, "render_toml_project", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        auth_migration,
        "verify_toml_render_output",
        lambda *_args, **_kwargs: type("Verification", (), {"lock": {}})(),
    )

    _build_candidate(
        tmp_path / "project",
        tmp_path / "generated",
        tmp_path / "candidate",
        target_profile="vps-server@6",
        target_preset="public-web-paper@2",
        target_volumes={"minecraft-data": "official-public-beta-b3-minecraft-data"},
        data_root=tmp_path,
        allow_unverified=True,
        allow_eol=False,
        resolved_at="2026-08-16T00:00:00Z",
    )

    assert updates == [
        (("deployment", "profile"), "vps-server@6"),
        (("environment", "preset"), "public-web-paper@2"),
    ]
    assert volume_updates == [
        ("minecraft-data", "official-public-beta-b3-minecraft-data")
    ]


def test_public_b3_candidate_updates_preset_in_real_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = init_toml_project(
        tmp_path / "source",
        deployment_name="official-public-beta",
        profile="vps-server@5",
        environment_identity="official-public-beta",
        channel="beta",
        exposure="public",
        purpose="integration",
        preset="public-web-paper@1",
        artifact_store=str(tmp_path / "artifacts"),
        runtime_volumes={
            "minecraft-data": "source-minecraft-data",
            "caddy-data": "source-caddy-data",
            "caddy-config": "source-caddy-config",
        },
        world_identity="official-public-beta-world",
        bind_address="0.0.0.0",
        java_port=25565,
        mcremote_port=25575,
        minecraft_eula=True,
    )
    candidate = tmp_path / "candidate"
    monkeypatch.setattr(
        auth_migration,
        "_copy_project_source",
        lambda source_root, destination, _output: shutil.copytree(
            source_root, destination
        ),
    )
    monkeypatch.setattr(auth_migration, "resolve_project", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_migration, "render_toml_project", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        auth_migration,
        "verify_toml_render_output",
        lambda *_args, **_kwargs: type("Verification", (), {"lock": {}})(),
    )

    _build_candidate(
        source.root,
        source.root / "generated",
        candidate,
        target_profile="vps-server@6",
        target_preset="public-web-paper@2",
        target_volumes={
            "minecraft-data": "target-minecraft-data",
            "caddy-data": "target-caddy-data",
            "caddy-config": "target-caddy-config",
        },
        data_root=tmp_path,
        allow_unverified=True,
        allow_eol=False,
        resolved_at="2026-08-16T00:00:00Z",
    )

    order = load_order(candidate).order
    assert order["deployment"]["profile"] == "vps-server@6"
    assert order["environment"]["preset"] == "public-web-paper@2"


def test_migration_spec_is_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        auth_migration.PUBLIC_B3_MIGRATION.name = "changed"
    assert isinstance(auth_migration.PUBLIC_B3_MIGRATION, MigrationSpec)


def test_public_b3_cli_forwards_reviewed_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "deployment"
    output = project / "generated"
    source = "sha256:" + "1" * 64
    target = "sha256:" + "2" * 64
    plan = AuthMigrationPlan(
        project_root=project.resolve(),
        output=output.absolute(),
        docker_context="default",
        source_lock_identity=source,
        target_lock_identity=target,
        source_profile="vps-server@5",
        target_profile="vps-server@6",
        deployment="official-public-beta",
        environment="official-public-beta",
        services=("caddy", "scratch", "bridge", "minecraft"),
        volume_migrations=(("minecraft-data", "old-world", "new-world"),),
        preserved_compose_files=(project / "recovery" / "compose.plugins.yaml",),
        preserved_compose_sha256=("3" * 64,),
        preserved_composition_identity="sha256:" + "4" * 64,
        auth_config_root=project / "private-config",
    )
    received: dict[str, object] = {}
    monkeypatch.setattr(
        "mc_remote_stack.cli.plan_public_b3_upgrade",
        lambda *_args, **_kwargs: plan,
    )

    def fake_apply(*_args, **kwargs):
        received.update(kwargs)
        return AuthMigrationResult("complete", source, target, "complete")

    monkeypatch.setattr("mc_remote_stack.cli.apply_public_b3_upgrade", fake_apply)
    common = [
        "--project",
        str(project),
        "--output",
        str(output),
        "--docker-context",
        "default",
        "--target-volume",
        "minecraft-data=new-world",
        "--preserve-compose-file",
        str(project / "recovery" / "compose.plugins.yaml"),
        "--auth-config-root",
        str(project / "private-config"),
        "--allow-unverified",
    ]

    assert main(["migration", "public-b3", "plan", *common]) == 0
    assert main(
        [
            "migration",
            "public-b3",
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

    assert received["target_volumes"] == {"minecraft-data": "new-world"}
    assert received["expected_source_lock_identity"] == source
    assert received["expected_target_lock_identity"] == target
    text = capsys.readouterr().out
    assert "PLAN migration=public-b3" in text
    assert "PLAN release=public-web-paper@1->public-web-paper@2" in text
    assert "OK migration public-b3 status=complete" in text


def test_public_b4_cli_forwards_reviewed_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "deployment"
    output = project / "generated"
    source = "sha256:" + "1" * 64
    target = "sha256:" + "2" * 64
    plan = AuthMigrationPlan(
        project_root=project.resolve(),
        output=output.absolute(),
        docker_context="default",
        source_lock_identity=source,
        target_lock_identity=target,
        source_profile="vps-server@7",
        target_profile="vps-server@8",
        deployment="official-public-beta",
        environment="official-public-beta",
        services=("caddy", "scratch", "bridge", "minecraft"),
        volume_migrations=(("minecraft-data", "old-world", "new-world"),),
        preserved_compose_files=(project / "recovery" / "compose.plugins.yaml",),
        preserved_compose_sha256=("3" * 64,),
        preserved_composition_identity="sha256:" + "4" * 64,
        auth_config_root=project / "private-config",
    )
    monkeypatch.setattr(
        "mc_remote_stack.cli.plan_public_b4_upgrade",
        lambda *_args, **_kwargs: plan,
    )
    monkeypatch.setattr(
        "mc_remote_stack.cli.apply_public_b4_upgrade",
        lambda *_args, **_kwargs: AuthMigrationResult(
            "complete", source, target, "complete"
        ),
    )
    common = [
        "--project",
        str(project),
        "--output",
        str(output),
        "--docker-context",
        "default",
        "--target-volume",
        "minecraft-data=new-world",
        "--preserve-compose-file",
        str(project / "recovery" / "compose.plugins.yaml"),
        "--auth-config-root",
        str(project / "private-config"),
        "--allow-unverified",
    ]

    assert main(["migration", "public-b4", "plan", *common]) == 0
    assert main(
        [
            "migration",
            "public-b4",
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

    text = capsys.readouterr().out
    assert "PLAN migration=public-b4" in text
    assert "PLAN release=public-web-paper@2->public-web-paper@3" in text
    assert "OK migration public-b4 status=complete" in text


def test_public_b3_config_replacement_requires_preserved_source(
    tmp_path: Path,
) -> None:
    project = tmp_path / "deployment"
    output = project / "generated"
    auth_root = tmp_path / "private-config"
    destination = auth_root / "plugins" / "McRemote" / "config.yml"
    destination.parent.mkdir(parents=True)
    destination.write_text("source-b2\n", encoding="utf-8")
    rendered = output / "minecraft" / "plugins" / "McRemote" / "config.yml"
    rendered.parent.mkdir(parents=True)
    rendered.write_text("target-b3\n", encoding="utf-8")
    plan = AuthMigrationPlan(
        project_root=project,
        output=output,
        docker_context="default",
        source_lock_identity="sha256:" + "1" * 64,
        target_lock_identity="sha256:" + "2" * 64,
        source_profile="vps-server@5",
        target_profile="vps-server@6",
        deployment="official-public-beta",
        environment="official-public-beta",
        services=("minecraft",),
        volume_migrations=(("minecraft-data", "old", "new"),),
        preserved_compose_files=(),
        preserved_compose_sha256=(),
        preserved_composition_identity=None,
        auth_config_root=auth_root,
    )
    snapshot = project / ".mcrctl" / "migrations" / "public-b3" / "source-auth-config.yml"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("source-b2\n", encoding="utf-8")

    with _activate_migration(auth_migration.PUBLIC_B3_MIGRATION):
        _install_auth_config(plan)
    assert destination.read_text(encoding="utf-8") == "target-b3\n"

    destination.write_text("unexpected\n", encoding="utf-8")
    with _activate_migration(auth_migration.PUBLIC_B3_MIGRATION):
        with pytest.raises(AuthMigrationContractError) as exc_info:
            _install_auth_config(plan)
    assert exc_info.value.reason == "migration_auth_config_conflict"
