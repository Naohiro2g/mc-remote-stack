from pathlib import Path

from mc_remote_stack.cli import main
from mc_remote_stack.preset_registry import (
    build_preset_catalog,
    component_set_sha256,
    load_preset,
    load_profile,
)

from .test_preset_registry import (
    _data_root,
    _write_compatibility_record,
    _write_policy,
    _write_preset,
    _write_profile,
)
from .test_resolver import _acknowledge, _fixture


def _catalog_fixture(tmp_path: Path, *, status: str = "active") -> Path:
    data_root = _data_root(tmp_path)
    _write_profile(data_root)
    _write_preset(data_root)
    policy = {
        "ref": "classroom-paper@3",
        "status": status,
        "available_since": "2026-07-23",
    }
    if status == "eol":
        policy.update(
            {
                "deprecated_since": "2026-07-23",
                "eol_since": "2026-07-24",
                "reason": "no longer offered by default",
            }
        )
    _write_policy(data_root, [policy])
    profile = load_profile("home-server@1", data_root=data_root)
    preset = load_preset("classroom-paper@3", data_root=data_root)
    _write_compatibility_record(
        data_root,
        record_id="home-server-classroom-paper-3",
        preset_sha256=preset.content_sha256,
        profile_sha256=profile.content_sha256,
        component_set_digest=component_set_sha256(preset.data),
    )
    (data_root / "preset_catalog.toml").write_bytes(build_preset_catalog(data_root=data_root))
    return data_root


def test_cli_preset_list_and_show_use_qualified_catalog_and_exact_registry(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    data_root = _catalog_fixture(tmp_path)
    monkeypatch.setattr("mc_remote_stack.cli._preset_data_root", lambda: data_root)

    assert main(["preset", "list"]) == 0
    assert main(["preset", "show", "classroom-paper@3"]) == 0

    output = capsys.readouterr().out
    assert "PRESET ref=classroom-paper@3 status=active compatibility=verified" in output
    assert "content-sha256=" in output
    assert "COMPONENT id=minecraft-server role=minecraft artifact=minecraft-image" in output
    assert "ARTIFACT id=minecraft-image kind=oci" in output
    assert "compatibility-records=home-server-classroom-paper-3" in output


def test_cli_preset_list_hides_eol_unless_all_is_explicit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    data_root = _catalog_fixture(tmp_path, status="eol")
    monkeypatch.setattr("mc_remote_stack.cli._preset_data_root", lambda: data_root)

    assert main(["preset", "list"]) == 0
    default_output = capsys.readouterr().out
    assert "PRESET none" in default_output
    assert "classroom-paper@3" not in default_output

    assert main(["preset", "list", "--all"]) == 0
    all_output = capsys.readouterr().out
    assert "PRESET ref=classroom-paper@3 status=eol compatibility=verified" in all_output


def test_cli_resolve_requires_two_stage_ack_and_reports_noop(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    project, data_root = _fixture(tmp_path)
    _acknowledge(project, "unverified")
    monkeypatch.setattr("mc_remote_stack.cli._preset_data_root", lambda: data_root)

    assert main(["resolve", "--project", str(project)]) == 2
    failure = capsys.readouterr().out
    assert "reason=unverified_not_acknowledged" in failure

    assert main(["resolve", "--project", str(project), "--allow-unverified"]) == 0
    created = capsys.readouterr().out
    assert "OK resolve status=created lock=sha256:" in created
    assert "WARN compatibility evidence does not cover all required claims" in created

    assert main(["resolve", "--project", str(project), "--allow-unverified"]) == 0
    unchanged = capsys.readouterr().out
    assert "OK resolve status=unchanged lock=sha256:" in unchanged


def test_cli_resolve_fails_closed_on_mixed_yaml_and_toml(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    project, data_root = _fixture(tmp_path)
    (project / "mc-remote.yml").write_text("deployment: {}\n", encoding="utf-8")
    monkeypatch.setattr("mc_remote_stack.cli._preset_data_root", lambda: data_root)

    assert main(["resolve", "--project", str(project), "--allow-unverified"]) == 2

    assert "reason=mixed_order_formats" in capsys.readouterr().out


def _bundled_home_project(tmp_path: Path) -> Path:
    from mc_remote_stack.toml_project import init_toml_project

    return init_toml_project(
        tmp_path / "home-beta",
        deployment_name="home",
        profile="home-server@1",
        environment_identity="home-beta",
        channel="beta",
        exposure="isolated",
        purpose="integration",
        preset="mcremote-paper@1",
        artifact_store="/var/lib/mc-remote/artifacts",
        runtime_volumes={"minecraft-data": "home-beta-minecraft-data"},
        world_identity="home-beta-world",
        bind_address="127.0.0.1",
        java_port=25565,
        mcremote_port=25575,
    ).root


def test_cli_toml_accept_eula_requires_yes_and_losslessly_records_once(
    tmp_path: Path,
    capsys,
) -> None:
    project = _bundled_home_project(tmp_path)
    order_path = project / "mc-remote.toml"
    order_path.write_text(
        order_path.read_text(encoding="utf-8").replace(
            "minecraft_eula = false",
            "minecraft_eula = false # operator reviewed the linked terms",
        ),
        encoding="utf-8",
    )
    before = order_path.read_bytes()

    assert main(["accept-eula", "--project", str(project)]) == 2
    assert "requires --yes" in capsys.readouterr().out
    assert order_path.read_bytes() == before

    assert main(["accept-eula", "--project", str(project), "--yes"]) == 0
    assert "OK recorded explicit EULA acceptance" in capsys.readouterr().out
    accepted = order_path.read_bytes()
    assert b"minecraft_eula = true # operator reviewed the linked terms" in accepted

    assert main(["accept-eula", "--project", str(project), "--yes"]) == 0
    assert "OK already-recorded explicit EULA acceptance" in capsys.readouterr().out
    assert order_path.read_bytes() == accepted


def test_cli_toml_validate_accepts_unresolved_order_but_plan_requires_lock(
    tmp_path: Path,
    capsys,
) -> None:
    project = _bundled_home_project(tmp_path)

    assert main(["validate", "--project", str(project)]) == 0
    validate_output = capsys.readouterr().out
    assert "OK validate format=toml order=valid lock=missing" in validate_output

    assert main(["plan", "--project", str(project)]) == 2
    plan_output = capsys.readouterr().out
    assert "reason=lock_missing" in plan_output
    assert not (project / "mc-remote.lock.toml").exists()


def test_cli_toml_plan_reports_resolved_home_intent_and_unverified_warning(
    tmp_path: Path,
    capsys,
) -> None:
    project = _bundled_home_project(tmp_path)
    assert main(["accept-eula", "--project", str(project), "--yes"]) == 0
    capsys.readouterr()
    _acknowledge(project, "unverified")
    assert main(["resolve", "--project", str(project), "--allow-unverified"]) == 0
    capsys.readouterr()

    assert main(["plan", "--project", str(project)]) == 1

    output = capsys.readouterr().out
    assert "PLAN deployment=home environment=home-beta" in output
    assert "PLAN channel=beta exposure=isolated purpose=integration" in output
    assert "PLAN profile=home-server@1 content-sha256=" in output
    assert "PLAN preset=mcremote-paper@1 content-sha256=" in output
    assert "PLAN selection=preset compatibility=unverified lifecycle=active" in output
    assert "PLAN artifact-store=/var/lib/mc-remote/artifacts" in output
    assert "PLAN runtime-volume=minecraft-data:home-beta-minecraft-data" in output
    assert "PLAN world=home-beta-world" in output
    assert "PLAN network-bind=127.0.0.1 java-port=25565 mcremote-port=25575" in output
    assert "PLAN minecraft-eula=accepted" in output
    assert "PLAN volume-roles=minecraft-data:world" in output
    assert "PLAN security-controls=online-mode,rcon-disabled" in output
    assert "PLAN lock=unchanged identity=sha256:" in output
    assert "WARN compatibility evidence does not cover all required claims" in output


def test_cli_toml_validate_and_plan_reject_stale_lock(
    tmp_path: Path,
    capsys,
) -> None:
    from mc_remote_stack.toml_project import update_order_scalar

    project = _bundled_home_project(tmp_path)
    assert main(["accept-eula", "--project", str(project), "--yes"]) == 0
    capsys.readouterr()
    _acknowledge(project, "unverified")
    assert main(["resolve", "--project", str(project), "--allow-unverified"]) == 0
    capsys.readouterr()
    update_order_scalar(project, ("deployment", "name"), "renamed-home")

    assert main(["validate", "--project", str(project)]) == 2
    assert "reason=stale_lock" in capsys.readouterr().out
    assert main(["plan", "--project", str(project)]) == 2
    assert "reason=stale_lock" in capsys.readouterr().out


def test_cli_toml_validate_and_plan_reject_tampered_lock(
    tmp_path: Path,
    capsys,
) -> None:
    project = _bundled_home_project(tmp_path)
    assert main(["accept-eula", "--project", str(project), "--yes"]) == 0
    capsys.readouterr()
    _acknowledge(project, "unverified")
    assert main(["resolve", "--project", str(project), "--allow-unverified"]) == 0
    capsys.readouterr()
    lock_path = project / "mc-remote.lock.toml"
    lock_path.write_text(
        lock_path.read_text(encoding="utf-8").replace(
            'name = "home"',
            'name = "tampered-home"',
            1,
        ),
        encoding="utf-8",
    )

    assert main(["validate", "--project", str(project)]) == 2
    assert "reason=lock_identity_mismatch" in capsys.readouterr().out
    assert main(["plan", "--project", str(project)]) == 2
    assert "reason=lock_identity_mismatch" in capsys.readouterr().out
