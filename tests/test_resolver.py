import os
import shutil
from importlib.resources import files
from pathlib import Path

import pytest

from mc_remote_stack.preset_registry import (
    build_preset_catalog,
    component_set_sha256,
    load_preset,
    load_profile,
)
from mc_remote_stack.resolver import (
    ResolutionError,
    inspect_lock,
    load_lock,
    resolve_project,
)
from mc_remote_stack.toml_project import init_toml_project, update_order_scalar

from .test_preset_registry import (
    _data_root,
    _write_compatibility_record,
    _write_policy,
    _write_preset,
    _write_profile,
)

FIRST_RESOLVED_AT = "2026-07-24T00:00:00Z"
SECOND_RESOLVED_AT = "2026-07-24T01:00:00Z"


def _instance_kwargs(identity: str = "home-beta") -> dict[str, object]:
    return {
        "artifact_store": "/var/lib/mc-remote/artifacts",
        "runtime_volumes": {"minecraft-data": f"{identity}-minecraft-data"},
        "world_identity": f"{identity}-world",
        "bind_address": "127.0.0.1",
        "java_port": 25565,
        "mcremote_port": 25575,
        "minecraft_eula": True,
    }


def _fixture(
    tmp_path: Path,
    *,
    lifecycle: str = "active",
    verified: bool = False,
) -> tuple[Path, Path]:
    data_root = _data_root(tmp_path)
    _write_profile(data_root)
    _write_preset(data_root)

    policy = {
        "ref": "classroom-paper@3",
        "status": lifecycle,
        "available_since": "2026-07-23",
    }
    if lifecycle == "deprecated":
        policy.update(
            {
                "deprecated_since": "2026-07-24",
                "reason": "use a later tested revision",
            }
        )
    if lifecycle == "eol":
        policy.update(
            {
                "deprecated_since": "2026-07-23",
                "eol_since": "2026-07-24",
                "reason": "no longer supported for new resolution",
            }
        )
    _write_policy(data_root, [policy])
    if verified:
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

    project = init_toml_project(
        tmp_path / "home-beta",
        deployment_name="home",
        profile="home-server@1",
        environment_identity="home-beta",
        channel="beta",
        exposure="isolated",
        purpose="integration",
        preset="classroom-paper@3",
        **_instance_kwargs(),
    )
    return project.root, data_root


def _acknowledge(project: Path, kind: str) -> None:
    update_order_scalar(
        project,
        ("acknowledgements", f"{kind}_reason"),
        f"explicit {kind} test acknowledgement",
    )
    update_order_scalar(project, ("acknowledgements", f"allow_{kind}"), True)


@pytest.mark.parametrize(
    ("order_ack", "cli_ack"),
    [
        (False, False),
        (True, False),
        (False, True),
    ],
)
def test_unverified_resolution_requires_order_and_cli_acknowledgement(
    tmp_path: Path,
    order_ack: bool,
    cli_ack: bool,
) -> None:
    project, data_root = _fixture(tmp_path)
    if order_ack:
        _acknowledge(project, "unverified")

    with pytest.raises(ResolutionError) as exc_info:
        resolve_project(
            project,
            data_root=data_root,
            allow_unverified=cli_ack,
            resolved_at=FIRST_RESOLVED_AT,
        )

    assert exc_info.value.reason == "unverified_not_acknowledged"
    assert not (project / "mc-remote.lock.toml").exists()


def test_successful_resolution_writes_one_exact_environment_lock(tmp_path: Path) -> None:
    project, data_root = _fixture(tmp_path)
    _acknowledge(project, "unverified")

    result = resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=FIRST_RESOLVED_AT,
    )
    lock = load_lock(project, data_root=data_root)

    assert result.status == "created"
    assert result.lock_identity == lock["lock_identity"]
    assert lock["resolved_at"] == FIRST_RESOLVED_AT
    assert lock["input"]["profile"]["ref"] == "home-server@1"
    assert lock["input"]["preset"]["ref"] == "classroom-paper@3"
    assert lock["environment"] == {
        "identity": "home-beta",
        "channel": "beta",
        "exposure": "isolated",
        "purpose": "integration",
    }
    assert lock["runtime"] == {
        "artifact_store": "/var/lib/mc-remote/artifacts",
        "volumes": [{"role": "minecraft-data", "identity": "home-beta-minecraft-data"}],
    }
    assert lock["world"] == {"identity": "home-beta-world"}
    assert lock["network"] == {
        "bind_address": "127.0.0.1",
        "java_port": 25565,
        "mcremote_port": 25575,
    }
    assert lock["agreements"] == {"minecraft_eula": True}
    assert lock["selection"]["kind"] == "preset"
    assert lock["compatibility"]["status"] == "unverified"
    assert lock["artifacts"][0]["digest"].startswith("sha256:")
    assert lock["scope"] == {
        "secret_values": "excluded",
        "secret_injected_bytes": "excluded",
        "runtime_owned_state": "excluded",
    }


@pytest.mark.parametrize(
    ("caller_umask", "expected_mode"),
    [
        (0o000, 0o640),
        (0o077, 0o600),
    ],
)
def test_new_lock_clamps_permissions_without_relaxing_caller_umask(
    tmp_path: Path,
    caller_umask: int,
    expected_mode: int,
) -> None:
    project, data_root = _fixture(tmp_path)
    _acknowledge(project, "unverified")

    previous_umask = os.umask(caller_umask)
    try:
        resolve_project(
            project,
            data_root=data_root,
            allow_unverified=True,
            resolved_at=FIRST_RESOLVED_AT,
        )
    finally:
        os.umask(previous_umask)

    assert (project / "mc-remote.lock.toml").stat().st_mode & 0o777 == expected_mode


def test_resolution_requires_explicit_minecraft_eula_acceptance(tmp_path: Path) -> None:
    project, data_root = _fixture(tmp_path)
    update_order_scalar(project, ("agreements", "minecraft_eula"), False)
    _acknowledge(project, "unverified")

    with pytest.raises(ResolutionError) as exc_info:
        resolve_project(
            project,
            data_root=data_root,
            allow_unverified=True,
            resolved_at=FIRST_RESOLVED_AT,
        )

    assert exc_info.value.reason == "minecraft_eula_not_accepted"
    assert not (project / "mc-remote.lock.toml").exists()


def test_profile_volume_roles_must_match_order_assignments_exactly(tmp_path: Path) -> None:
    project, data_root = _fixture(tmp_path)
    order_path = project / "mc-remote.toml"
    order_path.write_text(
        order_path.read_text(encoding="utf-8").replace(
            'role = "minecraft-data"',
            'role = "other-data"',
        ),
        encoding="utf-8",
    )
    _acknowledge(project, "unverified")

    with pytest.raises(ResolutionError) as exc_info:
        resolve_project(
            project,
            data_root=data_root,
            allow_unverified=True,
            resolved_at=FIRST_RESOLVED_AT,
        )

    assert exc_info.value.reason == "profile_incompatible"
    assert "missing: minecraft-data" in str(exc_info.value)
    assert "extra: other-data" in str(exc_info.value)


def test_world_identity_change_replaces_lock_and_render_identity(tmp_path: Path) -> None:
    project, data_root = _fixture(tmp_path)
    _acknowledge(project, "unverified")
    first = resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=FIRST_RESOLVED_AT,
    )
    first_render = load_lock(project, data_root=data_root)["render_plan"]["semantic_sha256"]

    update_order_scalar(project, ("world", "identity"), "home-beta-world-replacement")
    replacement = resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=SECOND_RESOLVED_AT,
    )
    lock = load_lock(project, data_root=data_root)

    assert replacement.status == "replaced"
    assert replacement.lock_identity != first.lock_identity
    assert lock["world"]["identity"] == "home-beta-world-replacement"
    assert lock["render_plan"]["semantic_sha256"] != first_render


def test_noop_resolve_preserves_lock_bytes_mtime_and_first_resolved_at(tmp_path: Path) -> None:
    project, data_root = _fixture(tmp_path)
    _acknowledge(project, "unverified")
    first = resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=FIRST_RESOLVED_AT,
    )
    lock_path = project / "mc-remote.lock.toml"
    before = lock_path.read_bytes()
    before_mtime = lock_path.stat().st_mtime_ns

    second = resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=SECOND_RESOLVED_AT,
    )

    assert first.lock_identity == second.lock_identity
    assert second.status == "unchanged"
    assert lock_path.read_bytes() == before
    assert lock_path.stat().st_mtime_ns == before_mtime
    assert load_lock(project, data_root=data_root)["resolved_at"] == FIRST_RESOLVED_AT


def test_lexical_only_order_change_is_a_noop_resolve(tmp_path: Path) -> None:
    project, data_root = _fixture(tmp_path)
    _acknowledge(project, "unverified")
    first = resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=FIRST_RESOLVED_AT,
    )
    lock_path = project / "mc-remote.lock.toml"
    before = lock_path.read_bytes()
    order_path = project / "mc-remote.toml"
    order_path.write_text(
        order_path.read_text(encoding="utf-8").replace(
            'name = "home"',
            "name = 'home' # lexical-only change",
        ),
        encoding="utf-8",
    )

    result = resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=SECOND_RESOLVED_AT,
    )

    assert result.status == "unchanged"
    assert result.lock_identity == first.lock_identity
    assert lock_path.read_bytes() == before


def test_semantic_order_change_is_stale_then_explicitly_replaced(tmp_path: Path) -> None:
    project, data_root = _fixture(tmp_path)
    _acknowledge(project, "unverified")
    first = resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=FIRST_RESOLVED_AT,
    )
    update_order_scalar(project, ("deployment", "name"), "renamed-home")

    inspection = inspect_lock(project, data_root=data_root)
    replacement = resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=SECOND_RESOLVED_AT,
    )

    assert inspection.status == "stale"
    assert replacement.status == "replaced"
    assert replacement.lock_identity != first.lock_identity
    assert load_lock(project, data_root=data_root)["deployment"]["name"] == "renamed-home"


def test_failed_resolution_does_not_modify_existing_lock(tmp_path: Path) -> None:
    project, data_root = _fixture(tmp_path)
    _acknowledge(project, "unverified")
    resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=FIRST_RESOLVED_AT,
    )
    lock_path = project / "mc-remote.lock.toml"
    before = lock_path.read_bytes()
    update_order_scalar(project, ("environment", "channel"), "stable")

    with pytest.raises(ResolutionError) as exc_info:
        resolve_project(
            project,
            data_root=data_root,
            allow_unverified=True,
            resolved_at=SECOND_RESOLVED_AT,
        )

    assert exc_info.value.reason == "unsupported_environment_combination"
    assert lock_path.read_bytes() == before


def test_atomic_lock_replace_failure_preserves_original_and_cleans_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, data_root = _fixture(tmp_path)
    _acknowledge(project, "unverified")
    resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=FIRST_RESOLVED_AT,
    )
    lock_path = project / "mc-remote.lock.toml"
    before = lock_path.read_bytes()
    update_order_scalar(project, ("deployment", "name"), "renamed-home")

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError(f"simulated replace failure: {source} -> {destination}")

    monkeypatch.setattr("mc_remote_stack.resolver.os.replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        resolve_project(
            project,
            data_root=data_root,
            allow_unverified=True,
            resolved_at=SECOND_RESOLVED_AT,
        )

    assert lock_path.read_bytes() == before
    assert not list(project.glob(".mc-remote.lock.toml.*.tmp"))


def test_lock_body_tamper_is_not_treated_as_stale(tmp_path: Path) -> None:
    project, data_root = _fixture(tmp_path)
    _acknowledge(project, "unverified")
    resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=FIRST_RESOLVED_AT,
    )
    lock_path = project / "mc-remote.lock.toml"
    lock_path.write_text(
        lock_path.read_text(encoding="utf-8").replace(
            'name = "home"',
            'name = "tampered-home"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ResolutionError) as exc_info:
        load_lock(project, data_root=data_root)

    assert exc_info.value.reason == "lock_identity_mismatch"


def test_missing_and_copied_lock_states_are_distinct(tmp_path: Path) -> None:
    project, data_root = _fixture(tmp_path)
    _acknowledge(project, "unverified")
    assert inspect_lock(project, data_root=data_root).status == "missing"
    resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=FIRST_RESOLVED_AT,
    )

    sibling = init_toml_project(
        tmp_path / "home-alpha",
        deployment_name="other-home",
        profile="home-server@1",
        environment_identity="home-alpha",
        channel="beta",
        exposure="isolated",
        purpose="integration",
        preset="classroom-paper@3",
        **_instance_kwargs("home-alpha"),
    )
    _acknowledge(sibling.root, "unverified")
    shutil.copyfile(project / "mc-remote.lock.toml", sibling.lock)

    assert inspect_lock(sibling.root, data_root=data_root).status == "stale"


def test_exact_compatibility_coverage_produces_verified_lock_without_unverified_ack(tmp_path: Path) -> None:
    project, data_root = _fixture(tmp_path, verified=True)

    result = resolve_project(
        project,
        data_root=data_root,
        resolved_at=FIRST_RESOLVED_AT,
    )
    lock = load_lock(project, data_root=data_root)

    assert result.status == "created"
    assert lock["compatibility"]["status"] == "verified"
    assert lock["compatibility"]["records"][0]["id"] == "home-server-classroom-paper-3"
    assert lock["acknowledgements"]["allow_unverified"] is False


def test_eol_resolution_requires_order_and_cli_acknowledgement(tmp_path: Path) -> None:
    project, data_root = _fixture(tmp_path, lifecycle="eol", verified=True)

    with pytest.raises(ResolutionError) as missing_order:
        resolve_project(
            project,
            data_root=data_root,
            allow_eol=True,
            resolved_at=FIRST_RESOLVED_AT,
        )
    assert missing_order.value.reason == "preset_eol"

    _acknowledge(project, "eol")
    with pytest.raises(ResolutionError) as missing_cli:
        resolve_project(
            project,
            data_root=data_root,
            resolved_at=FIRST_RESOLVED_AT,
        )
    assert missing_cli.value.reason == "preset_eol"

    result = resolve_project(
        project,
        data_root=data_root,
        allow_eol=True,
        resolved_at=FIRST_RESOLVED_AT,
    )

    assert result.status == "created"
    assert load_lock(project, data_root=data_root)["preset_lifecycle"]["status"] == "eol"


def test_bundled_verified_home_preset_resolves_without_unverified_ack(tmp_path: Path) -> None:
    project = init_toml_project(
        tmp_path / "home-beta",
        deployment_name="home",
        profile="home-server@2",
        environment_identity="home-beta",
        channel="beta",
        exposure="isolated",
        purpose="integration",
        preset="mcremote-paper@1",
        **_instance_kwargs(),
    )
    data_root = files("mc_remote_stack").joinpath("data")

    result = resolve_project(
        project.root,
        data_root=data_root,
        resolved_at=FIRST_RESOLVED_AT,
    )
    lock = load_lock(project.root, data_root=data_root)

    assert result.status == "created"
    assert lock["compatibility"]["status"] == "verified"
    assert [record["id"] for record in lock["compatibility"]["records"]] == [
        "home-server-2-mcremote-paper-1-live-auto"
    ]
    assert lock["acknowledgements"]["allow_unverified"] is False
    assert [artifact["id"] for artifact in lock["artifacts"]] == [
        "minecraft-image",
        "paper-jar",
        "mcremote-jar",
    ]
    assert lock["artifacts"][1]["sha256"] == "5ffef465eeeb5f2a3c23a24419d97c51afd7dbb4923ff42df9a3f58bba1ccfba"


def test_bundled_alpha_preset_resolves_only_through_unverified_gate(
    tmp_path: Path,
) -> None:
    project = init_toml_project(
        tmp_path / "home-alpha",
        deployment_name="home-alpha",
        profile="home-server@2",
        environment_identity="home-alpha",
        channel="alpha",
        exposure="isolated",
        purpose="integration",
        preset="mcremote-paper@2",
        **_instance_kwargs("home-alpha"),
    )
    _acknowledge(project.root, "unverified")
    data_root = files("mc_remote_stack").joinpath("data")

    result = resolve_project(
        project.root,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=FIRST_RESOLVED_AT,
    )
    lock = load_lock(project.root, data_root=data_root)

    assert result.status == "created"
    assert lock["environment"] == {
        "identity": "home-alpha",
        "channel": "alpha",
        "exposure": "isolated",
        "purpose": "integration",
    }
    assert lock["input"]["profile"]["ref"] == "home-server@2"
    assert lock["input"]["preset"]["ref"] == "mcremote-paper@2"
    assert lock["compatibility"]["status"] == "unverified"
    assert lock["runtime"]["volumes"] == [
        {"role": "minecraft-data", "identity": "home-alpha-minecraft-data"}
    ]
    assert lock["world"]["identity"] == "home-alpha-world"


def test_public_web_profile_resolves_exact_multiservice_lock(tmp_path: Path) -> None:
    data_root = files("mc_remote_stack").joinpath("data")
    project = init_toml_project(
        tmp_path / "official-public-beta",
        deployment_name="official-public-beta",
        profile="vps-server@3",
        environment_identity="official-public-beta",
        channel="beta",
        exposure="public",
        purpose="integration",
        preset="public-web-paper@1",
        artifact_store=str(tmp_path / "artifacts"),
        runtime_volumes={
            "minecraft-data": "official-public-beta-minecraft-data",
            "caddy-data": "official-public-beta-caddy-data",
            "caddy-config": "official-public-beta-caddy-config",
        },
        world_identity="official-public-beta-world",
        bind_address="0.0.0.0",
        java_port=25565,
        mcremote_port=25575,
        minecraft_eula=True,
    )
    project.order.write_text(
        project.order.read_text(encoding="utf-8")
        + """
[[operator_inputs]]
role = "public-routes"
adapter = "public-routes@1"
path = "operator/public-routes/routes.toml"
""",
        encoding="utf-8",
    )
    routes = project.root / "operator" / "public-routes" / "routes.toml"
    routes.parent.mkdir(parents=True)
    routes.write_text(
        """
homepage = "mc-remote.example"
homepage_aliases = ["www.mc-remote.example"]
scratch = "scratch.mc-remote.example"
bridge = "bridge.mc-remote.example"
minecraft = "sb.mc-remote.example"
""".lstrip(),
        encoding="utf-8",
    )
    _acknowledge(project.root, "unverified")

    result = resolve_project(
        project.root,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=FIRST_RESOLVED_AT,
    )
    lock = load_lock(project.root, data_root=data_root)

    assert result.status == "created"
    assert lock["render_plan"]["adapter_revision"] == "3"
    assert [service["id"] for service in lock["render_plan"]["services"]] == [
        "caddy",
        "scratch",
        "bridge",
        "minecraft",
    ]
    assert [artifact["id"] for artifact in lock["artifacts"]] == [
        "caddy-image",
        "scratch-image",
        "bridge-image",
        "minecraft-image",
        "paper-jar",
        "mcremote-jar",
    ]
    assert lock["compatibility"]["status"] == "unverified"
