import copy
import hashlib
import json
import os
import zipfile
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

import mc_remote_stack.render as render_module
from mc_remote_stack.cli import main
from mc_remote_stack.preset_registry import build_preset_catalog, semantic_sha256
from mc_remote_stack.render import RenderContractError, render_toml_project
from mc_remote_stack.resolver import ResolutionError, load_lock, resolve_project
from mc_remote_stack.runtime_content import import_homepage_tree
from mc_remote_stack.toml_project import init_toml_project, update_order_scalar

from .test_preset_registry import _data_root, _write_policy
from .test_resolver import FIRST_RESOLVED_AT, SECOND_RESOLVED_AT, _acknowledge

PAPER_BYTES = b"deterministic paper fixture\n"
PLUGIN_BYTES = b"deterministic mcremote fixture\n"
PAPER_SHA256 = hashlib.sha256(PAPER_BYTES).hexdigest()
PLUGIN_SHA256 = hashlib.sha256(PLUGIN_BYTES).hexdigest()
OCI_DIGEST = f"sha256:{11:064x}"


def _preset_source(
    *,
    revision: str = "1",
    allowed_channel: str = "beta",
) -> str:
    return f"""schema_version = 1

[preset]
name = "mcremote-paper"
revision = "{revision}"
description = "Deterministic compose renderer fixture"

[requirements]
profile_capabilities = ["compose", "paper", "persistent-world"]
allowed_channels = ["{allowed_channel}"]
required_claims = ["profile-render"]

[[components]]
id = "minecraft-runtime"
role = "minecraft-runtime"
artifact = "minecraft-image"

[[components]]
id = "paper-server"
role = "paper-server"
artifact = "paper-jar"
minecraft_version = "1.21.11"

[[components]]
id = "mcremote-paper"
role = "mcremote-plugin"
artifact = "mcremote-jar"
protocol = "21.0.0"

[[artifacts]]
id = "minecraft-image"
kind = "oci"
version = "fixture-java21"
locator = "registry.example/minecraft"
digest = "{OCI_DIGEST}"

[[artifacts]]
id = "paper-jar"
kind = "https-file"
version = "1.21.11-132"
filename = "paper-fixture.jar"
sha256 = "{PAPER_SHA256}"
origin = "https://example.invalid/paper-fixture.jar"

[[artifacts]]
id = "mcremote-jar"
kind = "https-file"
version = "2100.0.0b2"
filename = "mcremote-fixture.jar"
sha256 = "{PLUGIN_SHA256}"
origin = "https://example.invalid/mcremote-fixture.jar"
"""


def _render_fixture(
    tmp_path: Path,
    *,
    deployment_name: str = "home",
    identity: str = "home-beta",
    channel: str = "beta",
    preset_revision: str = "1",
    profile_name: str = "home-server",
    profile_revision: str = "4",
    exposure: str = "isolated",
    bind_address: str = "127.0.0.1",
) -> tuple[Path, Path, Path]:
    data_root = _data_root(tmp_path, "render-data")
    profile_path = (
        data_root / "profiles" / profile_name / profile_revision / "profile.toml"
    )
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        files("mc_remote_stack")
        .joinpath(
            "data",
            "profiles",
            profile_name,
            profile_revision,
            "profile.toml",
        )
        .read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    preset_path = (
        data_root
        / "preset_registry"
        / "mcremote-paper"
        / preset_revision
        / "preset.toml"
    )
    preset_path.parent.mkdir(parents=True)
    preset_path.write_text(
        _preset_source(
            revision=preset_revision,
            allowed_channel=channel,
        ),
        encoding="utf-8",
    )
    _write_policy(
        data_root,
        [
            {
                "ref": f"mcremote-paper@{preset_revision}",
                "status": "active",
                "available_since": "2026-07-24",
            }
        ],
    )
    (data_root / "preset_catalog.toml").write_bytes(build_preset_catalog(data_root=data_root))

    artifact_store = tmp_path / "artifact-store"
    digest_store = artifact_store / "sha256"
    digest_store.mkdir(parents=True)
    (digest_store / PAPER_SHA256).write_bytes(PAPER_BYTES)
    (digest_store / PLUGIN_SHA256).write_bytes(PLUGIN_BYTES)

    runtime_volumes = {"minecraft-data": f"{identity}-minecraft-data"}
    if profile_name == "home-server" and profile_revision == "3":
        runtime_volumes.update(
            {
                "credential-store": f"{identity}-credential-store",
                "credential-revocations": f"{identity}-credential-revocations",
            }
        )

    project = init_toml_project(
        tmp_path / identity,
        deployment_name=deployment_name,
        profile=f"{profile_name}@{profile_revision}",
        environment_identity=identity,
        channel=channel,
        exposure=exposure,
        purpose="integration",
        preset=f"mcremote-paper@{preset_revision}",
        artifact_store=str(artifact_store),
        runtime_volumes=runtime_volumes,
        world_identity=f"{identity}-world",
        bind_address=bind_address,
        java_port=25565,
        mcremote_port=25575,
        minecraft_eula=True,
    )
    _acknowledge(project.root, "unverified")
    resolve_project(
        project.root,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=FIRST_RESOLVED_AT,
    )
    return project.root, data_root, artifact_store


def test_public_vps_profile_renders_exact_public_runtime(tmp_path: Path) -> None:
    project, data_root, _ = _render_fixture(
        tmp_path,
        deployment_name="official-public-beta",
        identity="official-public-beta",
        profile_name="vps-server",
        profile_revision="1",
        exposure="public",
        bind_address="0.0.0.0",
    )
    output = project / "generated"

    result = render_toml_project(project, output, data_root=data_root)

    compose = yaml.safe_load((output / "compose.yaml").read_text(encoding="utf-8"))
    assert result.status == "created"
    assert compose["name"] == "official-public-beta"
    assert compose["services"]["minecraft"]["ports"] == [
        "0.0.0.0:25565:25565/tcp",
        "0.0.0.0:25575:25575/tcp",
    ]


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_toml_compose_renderer_uses_only_locked_artifacts_and_instance_contract(
    tmp_path: Path,
) -> None:
    project, data_root, artifact_store = _render_fixture(tmp_path)
    output = project / "generated"

    result = render_toml_project(project, output, data_root=data_root)

    assert result.status == "created"
    compose = yaml.safe_load((output / "compose.yaml").read_text(encoding="utf-8"))
    minecraft = compose["services"]["minecraft"]
    assert compose["name"] == "home"
    assert minecraft["image"] == f"registry.example/minecraft:fixture-java21@{OCI_DIGEST}"
    assert minecraft["environment"]["EULA"] == "TRUE"
    assert minecraft["environment"]["TYPE"] == "PAPER"
    assert minecraft["environment"]["VERSION"] == "1.21.11"
    assert minecraft["environment"]["PAPER_CUSTOM_JAR"] == "/artifacts/paper-fixture.jar"
    assert minecraft["environment"]["ONLINE_MODE"] == "true"
    assert minecraft["environment"]["ENABLE_RCON"] == "false"
    assert minecraft["environment"]["LEVEL"] == "home-beta-world"
    assert minecraft["ports"] == [
        "127.0.0.1:25565:25565/tcp",
        "127.0.0.1:25575:25575/tcp",
    ]
    assert minecraft["volumes"] == [
        {
            "type": "volume",
            "source": "minecraft-data",
            "target": "/data",
        },
        {
            "type": "bind",
            "source": "./minecraft",
            "target": "/config",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": f"{artifact_store}/sha256/{PAPER_SHA256}",
            "target": "/artifacts/paper-fixture.jar",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": f"{artifact_store}/sha256/{PLUGIN_SHA256}",
            "target": "/plugins/mcremote-fixture.jar",
            "read_only": True,
        },
    ]
    assert compose["volumes"]["minecraft-data"] == {
        "name": "home-beta-minecraft-data",
        "external": True,
    }
    assert minecraft["labels"]["io.mc-remote.world"] == "home-beta-world"
    assert minecraft["labels"]["io.mc-remote.lock"] == result.lock_identity

    properties = (output / "minecraft" / "server.properties").read_text(encoding="utf-8")
    assert "enable-rcon=false\n" in properties
    assert "online-mode=true\n" in properties
    assert "server-port=25565\n" in properties
    assert "level-name=home-beta-world\n" in properties


def test_credential_storage_renderer_mounts_security_state_outside_data(
    tmp_path: Path,
) -> None:
    project, data_root, _ = _render_fixture(
        tmp_path,
        profile_revision="3",
    )
    output = project / "generated"

    result = render_toml_project(project, output, data_root=data_root)

    assert result.status == "created"
    compose = yaml.safe_load((output / "compose.yaml").read_text(encoding="utf-8"))
    minecraft = compose["services"]["minecraft"]
    assert minecraft["environment"]["UID"] == "1000"
    assert minecraft["environment"]["GID"] == "1000"
    credential_mounts = [
        mount
        for mount in minecraft["volumes"]
        if mount.get("source") in {"credential-store", "credential-revocations"}
    ]
    assert credential_mounts == [
        {
            "type": "volume",
            "source": "credential-store",
            "target": "/mcremote/credential-store",
        },
        {
            "type": "volume",
            "source": "credential-revocations",
            "target": "/mcremote/credential-revocations",
        },
    ]
    assert all(not mount["target"].startswith("/data") for mount in credential_mounts)
    assert compose["volumes"] == {
        "minecraft-data": {"name": "home-beta-minecraft-data", "external": True},
        "credential-store": {
            "name": "home-beta-credential-store",
            "external": True,
        },
        "credential-revocations": {
            "name": "home-beta-credential-revocations",
            "external": True,
        },
    }
    lock = load_lock(project, data_root=data_root)
    assert lock["render_plan"]["volume_roles"][-1] == {
        "id": "credential-revocations",
        "kind": "security-state",
    }

    config = (output / "minecraft" / "plugins" / "McRemote" / "config.yml").read_text(
        encoding="utf-8"
    )
    assert "auth:\n  enforcement: true\n" in config
    assert 'credential_store_path: "/mcremote/credential-store/snapshot.json"\n' in config
    assert 'revocation_authority_path: "/mcremote/credential-revocations"\n' in config
    manifest = json.loads((output / "render-manifest.json").read_text(encoding="utf-8"))
    assert manifest["adapter_revision"] == "5"
    assert [entry["path"] for entry in manifest["files"]] == [
        "compose.yaml",
        "minecraft/server.properties",
        "minecraft/plugins/McRemote/config.yml",
    ]


def test_current_home_renderer_enforces_b2_authentication(tmp_path: Path) -> None:
    project, data_root, _ = _render_fixture(
        tmp_path,
        deployment_name="home-alpha",
        identity="home-alpha",
        channel="alpha",
        preset_revision="2",
        profile_revision="4",
    )
    output = project / "generated"

    render_toml_project(project, output, data_root=data_root)

    config = (output / "minecraft" / "plugins" / "McRemote" / "config.yml").read_text(
        encoding="utf-8"
    )
    assert "# Generated by mcrctl compose@6. Do not edit.\n" in config
    assert "auth:\n  enforcement: true\n" in config
    manifest = json.loads((output / "render-manifest.json").read_text(encoding="utf-8"))
    assert manifest["adapter_revision"] == "6"
    assert [entry["path"] for entry in manifest["files"]] == [
        "compose.yaml",
        "minecraft/server.properties",
        "minecraft/plugins/McRemote/config.yml",
    ]


def test_toml_render_manifest_is_deterministic_and_second_render_is_noop(tmp_path: Path) -> None:
    project, data_root, _ = _render_fixture(tmp_path)
    output = project / "generated"

    first = render_toml_project(project, output, data_root=data_root)
    before = _tree_bytes(output)
    before_mtimes = {
        path.relative_to(output).as_posix(): path.stat().st_mtime_ns
        for path in output.rglob("*")
        if path.is_file()
    }
    second = render_toml_project(project, output, data_root=data_root)
    manifest = json.loads((output / "render-manifest.json").read_text(encoding="utf-8"))

    assert first.status == "created"
    assert second.status == "unchanged"
    assert _tree_bytes(output) == before
    assert {
        path.relative_to(output).as_posix(): path.stat().st_mtime_ns
        for path in output.rglob("*")
        if path.is_file()
    } == before_mtimes
    assert manifest["schema_version"] == 1
    assert manifest["adapter"] == "compose"
    assert manifest["adapter_revision"] == "6"
    assert manifest["lock_identity"] == first.lock_identity
    assert [entry["path"] for entry in manifest["files"]] == [
        "compose.yaml",
        "minecraft/server.properties",
        "minecraft/plugins/McRemote/config.yml",
    ]


def test_toml_render_projects_only_locked_minecraft_motd_semantics(
    tmp_path: Path,
) -> None:
    project, data_root, _ = _render_fixture(tmp_path)
    order_path = project / "mc-remote.toml"
    order_path.write_text(
        order_path.read_text(encoding="utf-8")
        + """
[[operator_inputs]]
role = "minecraft-motd"
adapter = "minecraft-motd@1"
path = "operator/minecraft-motd/server.properties"
""",
        encoding="utf-8",
    )
    source_path = project / "operator" / "minecraft-motd" / "server.properties"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "# lexical comment is not runtime output\nmotd = McRemote home beta\n",
        encoding="utf-8",
    )
    resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=SECOND_RESOLVED_AT,
    )

    output = project / "generated"
    render_toml_project(project, output, data_root=data_root)

    properties = (output / "minecraft" / "server.properties").read_text(encoding="utf-8")
    assert "motd=McRemote home beta\n" in properties
    assert "lexical comment" not in properties


def test_toml_render_rejects_stale_lock_without_changing_managed_output(tmp_path: Path) -> None:
    project, data_root, _ = _render_fixture(tmp_path)
    output = project / "generated"
    render_toml_project(project, output, data_root=data_root)
    before = _tree_bytes(output)
    update_order_scalar(project, ("network", "java_port"), 25566)

    with pytest.raises(RenderContractError) as exc_info:
        render_toml_project(project, output, data_root=data_root)

    assert exc_info.value.reason == "stale_lock"
    assert _tree_bytes(output) == before


@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_toml_render_verifies_artifact_store_before_publishing(
    tmp_path: Path,
    mutation: str,
) -> None:
    project, data_root, artifact_store = _render_fixture(tmp_path)
    output = project / "generated"
    render_toml_project(project, output, data_root=data_root)
    before = _tree_bytes(output)
    artifact = artifact_store / "sha256" / PAPER_SHA256
    if mutation == "missing":
        artifact.unlink()
    else:
        artifact.write_bytes(b"tampered\n")

    with pytest.raises(RenderContractError) as exc_info:
        render_toml_project(project, output, data_root=data_root)

    assert exc_info.value.reason == f"artifact_{mutation}"
    assert _tree_bytes(output) == before


def test_toml_render_refuses_unmanaged_nonempty_output(tmp_path: Path) -> None:
    project, data_root, _ = _render_fixture(tmp_path)
    output = project / "generated"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("operator-owned\n", encoding="utf-8")

    with pytest.raises(RenderContractError) as exc_info:
        render_toml_project(project, output, data_root=data_root)

    assert exc_info.value.reason == "render_output_unmanaged"
    assert sentinel.read_text(encoding="utf-8") == "operator-owned\n"


def test_toml_render_refuses_modified_managed_output_without_overwriting_it(tmp_path: Path) -> None:
    project, data_root, _ = _render_fixture(tmp_path)
    output = project / "generated"
    render_toml_project(project, output, data_root=data_root)
    compose_path = output / "compose.yaml"
    compose_path.write_text("tampered: true\n", encoding="utf-8")
    before = _tree_bytes(output)

    with pytest.raises(RenderContractError) as exc_info:
        render_toml_project(project, output, data_root=data_root)

    assert exc_info.value.reason == "render_output_tampered"
    assert _tree_bytes(output) == before


def test_toml_render_replaces_only_valid_previous_managed_output(tmp_path: Path) -> None:
    project, data_root, _ = _render_fixture(tmp_path)
    output = project / "generated"
    first = render_toml_project(project, output, data_root=data_root)
    update_order_scalar(project, ("network", "java_port"), 25566)
    resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=SECOND_RESOLVED_AT,
    )

    replacement = render_toml_project(project, output, data_root=data_root)
    compose = yaml.safe_load((output / "compose.yaml").read_text(encoding="utf-8"))

    assert replacement.status == "replaced"
    assert replacement.lock_identity != first.lock_identity
    assert compose["services"]["minecraft"]["ports"][0] == "127.0.0.1:25566:25565/tcp"


def test_toml_render_publish_failure_rolls_back_previous_managed_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, data_root, _ = _render_fixture(tmp_path)
    output = project / "generated"
    render_toml_project(project, output, data_root=data_root)
    before = _tree_bytes(output)
    update_order_scalar(project, ("network", "java_port"), 25566)
    resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=SECOND_RESOLVED_AT,
    )
    real_replace = os.replace

    def fail_staging_publish(source: Path, destination: Path) -> None:
        if Path(source).name.endswith(".render") and Path(destination) == output:
            raise OSError("simulated staging publish failure")
        real_replace(source, destination)

    monkeypatch.setattr("mc_remote_stack.render.os.replace", fail_staging_publish)

    with pytest.raises(OSError, match="simulated staging publish failure"):
        render_toml_project(project, output, data_root=data_root)

    assert _tree_bytes(output) == before
    assert not list(output.parent.glob(".generated.*.backup"))
    assert not list(output.parent.glob(".generated.*.render"))


@pytest.mark.parametrize("unsafe_output", ["project", "ancestor", "artifact-store"])
def test_toml_render_rejects_output_that_overlaps_owned_input(
    tmp_path: Path,
    unsafe_output: str,
) -> None:
    project, data_root, artifact_store = _render_fixture(tmp_path)
    targets = {
        "project": project,
        "ancestor": project.parent,
        "artifact-store": artifact_store,
    }

    with pytest.raises(RenderContractError) as exc_info:
        render_toml_project(project, targets[unsafe_output], data_root=data_root)

    assert exc_info.value.reason == "render_output_unsafe"


def test_cli_render_routes_toml_project_to_compose_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, data_root, _ = _render_fixture(tmp_path)
    output = project / "generated"
    monkeypatch.setattr("mc_remote_stack.cli._preset_data_root", lambda: data_root)

    assert main(["render", "--project", str(project), "--output", str(output)]) == 0

    rendered = capsys.readouterr().out
    assert "OK render status=created adapter=compose@6 lock=sha256:" in rendered
    assert f"output={output.resolve()}" in rendered


def test_cli_render_reports_stale_toml_lock_with_stable_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, data_root, _ = _render_fixture(tmp_path)
    output = project / "generated"
    update_order_scalar(project, ("world", "identity"), "home-beta-other-world")
    monkeypatch.setattr("mc_remote_stack.cli._preset_data_root", lambda: data_root)

    assert main(["render", "--project", str(project), "--output", str(output)]) == 2

    assert "FAIL render reason=stale_lock" in capsys.readouterr().out
    assert not output.exists()


def test_toml_render_surfaces_lock_tamper_instead_of_reclassifying_it(
    tmp_path: Path,
) -> None:
    project, data_root, _ = _render_fixture(tmp_path)
    lock_path = project / "mc-remote.lock.toml"
    lock_path.write_text(
        lock_path.read_text(encoding="utf-8").replace(
            'identity = "home-beta-world"',
            'identity = "tampered-world"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ResolutionError) as exc_info:
        render_toml_project(project, project / "generated", data_root=data_root)

    assert exc_info.value.reason == "lock_identity_mismatch"


def test_compose_v2_projects_public_edge_and_private_backends(tmp_path: Path) -> None:
    data_root = files("mc_remote_stack").joinpath("data")
    artifact_store = tmp_path / "artifacts"
    project = init_toml_project(
        tmp_path / "official-public-beta",
        deployment_name="official-public-beta",
        profile="vps-server@5",
        environment_identity="official-public-beta",
        channel="beta",
        exposure="public",
        purpose="integration",
        preset="public-web-paper@1",
        artifact_store=str(artifact_store),
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

[[operator_inputs]]
role = "minecraft-server"
adapter = "minecraft-server@1"
path = "operator/minecraft-server/server.toml"
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
    server = project.root / "operator" / "minecraft-server" / "server.toml"
    server.parent.mkdir(parents=True)
    server.write_text(
        """
allow_flight = false
difficulty = "hard"
enable_query = false
enable_status = true
force_gamemode = true
gamemode = "creative"
hardcore = true
log_ips = true
management_server_enabled = false
max_players = 18
max_tick_time = -1
max_world_size = 9984
motd = "McRemote Sandbox Server"
network_compression_threshold = -1
simulation_distance = 6
spawn_protection = 150
view_distance = 10
white_list = false
""".lstrip(),
        encoding="utf-8",
    )
    _acknowledge(project.root, "unverified")
    resolve_project(
        project.root,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=FIRST_RESOLVED_AT,
    )
    lock = load_lock(project.root, data_root=data_root)
    fixture_artifacts = {
        "paper-jar": (PAPER_SHA256, PAPER_BYTES),
        "mcremote-jar": (PLUGIN_SHA256, PLUGIN_BYTES),
    }
    for artifact in lock["artifacts"]:
        if artifact["id"] in fixture_artifacts:
            artifact["sha256"] = fixture_artifacts[artifact["id"]][0]
    for digest, content in fixture_artifacts.values():
        path = artifact_store / "sha256" / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    compose, rendered_files = render_module._compose_v7(lock)

    assert compose["services"]["caddy"]["ports"] == [
        "0.0.0.0:80:80/tcp",
        "0.0.0.0:443:443/tcp",
    ]
    assert "ports" not in compose["services"]["scratch"]
    assert "ports" not in compose["services"]["bridge"]
    assert compose["services"]["minecraft"]["ports"] == [
        "0.0.0.0:25565:25565/tcp",
        "0.0.0.0:25565:19132/udp",
        "0.0.0.0:25575:25575/tcp",
    ]
    assert compose["networks"]["app"] == {
        "internal": True,
        "enable_ipv6": False,
    }
    assert compose["networks"]["egress"] == {
        "internal": False,
        "enable_ipv6": False,
    }
    assert compose["services"]["caddy"]["networks"] == ["edge", "app"]
    assert compose["services"]["scratch"]["networks"] == ["app"]
    assert compose["services"]["bridge"]["networks"] == ["app"]
    assert compose["services"]["minecraft"]["networks"] == {
        "app": {
            "aliases": ["sb.mc-remote.example"],
        },
        "egress": {
            "gw_priority": 1,
        },
    }
    assert set(rendered_files) == {
        "Caddyfile",
        "runtime/scratch.json",
        "minecraft/server.properties",
        "minecraft/plugins/McRemote/config.yml",
    }
    assert "reverse_proxy scratch:8080" in rendered_files["Caddyfile"]
    runtime_config = json.loads(rendered_files["runtime/scratch.json"])
    assert "connection_targets" not in runtime_config
    assert compose["services"]["bridge"]["environment"]["BRIDGE_SANDBOX_ALLOWLIST"] == (
        "sb.mc-remote.example"
    )
    assert "auth:\n  enforcement: true\n" in rendered_files[
        "minecraft/plugins/McRemote/config.yml"
    ]
    assert rendered_files["minecraft/server.properties"].splitlines() == [
        "# Generated by mcrctl compose@7. Do not edit.",
        "allow-flight=false",
        "difficulty=hard",
        "enable-query=false",
        "enable-rcon=false",
        "enable-status=true",
        "enforce-secure-profile=true",
        "force-gamemode=true",
        "gamemode=creative",
        "hardcore=true",
        "level-name=official-public-beta-world",
        "log-ips=true",
        "management-server-enabled=false",
        "max-players=18",
        "max-tick-time=-1",
        "max-world-size=9984",
        "motd=McRemote Sandbox Server",
        "network-compression-threshold=-1",
        "online-mode=true",
        "server-port=25565",
        "simulation-distance=6",
        "spawn-protection=150",
        "view-distance=10",
        "white-list=false",
    ]

    staging = tmp_path / "staging"
    staging.mkdir()
    rendered_paths = render_module._stage_compose_v7(lock, staging)
    manifest = json.loads((staging / "render-manifest.json").read_text(encoding="utf-8"))

    assert rendered_paths == (
        "compose.yaml",
        "Caddyfile",
        "runtime/scratch.json",
        "minecraft/server.properties",
        "minecraft/plugins/McRemote/config.yml",
    )
    assert manifest["adapter_revision"] == "7"
    assert yaml.safe_load((staging / "compose.yaml").read_text(encoding="utf-8")) == compose


def test_compose_v8_keeps_b3_public_beta_session_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_compose = {
        "services": {
            "minecraft": {
                "volumes": [
                    {
                        "type": "bind",
                        "source": "./minecraft",
                        "target": "/config",
                        "read_only": True,
                    }
                ]
            }
        }
    }
    base_files = {
        "minecraft/server.properties": (
            "# Generated by mcrctl compose@4. Do not edit.\n"
        )
    }
    monkeypatch.setattr(
        render_module,
        "_compose_v4",
        lambda _lock: (base_compose, base_files),
    )
    lock = {
        "components": [
            {
                "id": "paper-server",
                "role": "paper-server",
                "minecraft_version": "1.21.11",
            }
        ],
        "lock_identity": f"sha256:{1:064x}",
        "render_plan": {"semantic_sha256": f"{2:064x}"},
    }

    compose, rendered_files = render_module._compose_v8(lock)
    config = render_module._mcremote_b3_session_only_config(
        adapter="compose@8",
        minecraft_version="1.21.11",
    )

    assert config.startswith("# Generated by mcrctl compose@8. Do not edit.\n")
    assert "auth:\n  enforcement: true\n" in config
    assert 'credential_store_path: "/config/mcremote-session-only/store/snapshot.json"' in config
    assert (
        'revocation_authority_path: "/config/mcremote-session-only/authority"'
        in config
    )
    assert "player_token" not in config
    assert "/mcremote/credential-store" not in config
    assert "/mcremote/credential-revocations" not in config
    assert compose["services"]["minecraft"]["volumes"][0] == {
        "type": "bind",
        "source": "./minecraft",
        "target": "/config",
        "read_only": True,
    }
    assert rendered_files == {
        "minecraft/server.properties": (
            "# Generated by mcrctl compose@8. Do not edit.\n"
        ),
        "minecraft/plugins/McRemote/config.yml": config,
    }

    staging = tmp_path / "staging"
    staging.mkdir()
    rendered_paths = render_module._stage_compose_v8(lock, staging)
    manifest = json.loads(
        (staging / "render-manifest.json").read_text(encoding="utf-8")
    )

    assert rendered_paths == (
        "compose.yaml",
        "minecraft/server.properties",
        "minecraft/plugins/McRemote/config.yml",
    )
    assert manifest["adapter_revision"] == "8"


def test_compose_v9_requires_explicit_scratch_target_and_emits_empty_notices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_runtime = {
        "bridge_url": "wss://bridge-beta.mc-remote.example",
        "default_sandbox": "sb-beta.mc-remote.example",
        "connection_targets": [
            {
                "id": "beta",
                "label": "公開ベータ",
                "sandbox": "sb-beta.mc-remote.example",
            }
        ],
        "connection_enabled": True,
        "release_identity": "scratch-b3",
    }
    monkeypatch.setattr(
        render_module,
        "_compose_v8",
        lambda _lock: (
            {"services": {}},
            {"runtime/scratch.json": json.dumps(base_runtime, ensure_ascii=False) + "\n"},
        ),
    )

    _compose, rendered_files = render_module._compose_v9(
        {"environment": {"channel": "beta"}}
    )
    runtime = json.loads(rendered_files["runtime/scratch.json"])

    assert runtime["connection_targets"] == base_runtime["connection_targets"]
    assert runtime["default_sandbox"] == "sb-beta.mc-remote.example"
    assert runtime["notices"] == []


def test_compose_v9_projects_typed_public_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = {
        "bridge_url": "wss://bridge-beta.mc-remote.example",
        "default_sandbox": "sb-beta.mc-remote.example",
        "connection_targets": [
            {
                "id": "beta",
                "label": "Beta",
                "sandbox": "sb-beta.mc-remote.example",
            }
        ],
        "connection_enabled": True,
        "release_identity": "scratch-b4",
    }
    notices = [
        {
            "heading": "WireScope beta",
            "body": "Observe Scratch and Minecraft traffic.",
            "link": {
                "href": "https://wirescope-beta.mc-remote.example/",
                "label": "Open WireScope",
            },
        }
    ]
    monkeypatch.setattr(
        render_module,
        "_compose_v8",
        lambda _lock: (
            {"services": {}},
            {"runtime/scratch.json": json.dumps(runtime) + "\n"},
        ),
    )
    monkeypatch.setattr(
        render_module,
        "_locked_connection_notices",
        lambda _lock: notices,
        raising=False,
    )

    _compose, rendered = render_module._compose_v9({"environment": {"channel": "beta"}})

    assert json.loads(rendered["runtime/scratch.json"])["notices"] == notices


def test_compose_v13_appends_preset_release_notice_after_operator_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator_notices = [
        {
            "heading": "今後のリリース予定",
            "body": "10月RC版、年内に安定版リリース予定です。",
            "link": {"href": "https://mc-remote.com", "label": "公式サイトを見る"},
        },
        {
            "heading": "WireScope（ワイヤースコープ）ライブ画面",
            "body": "ScratchとMinecraftの通信を観察できます。",
            "link": {
                "href": "https://wirescope-beta.mc-remote.com/",
                "label": "WireScopeを見る",
            },
        },
    ]
    release_notice = {
        "heading": "マイクラリモコンScratchクライアント ver.2100.0.0b4",
        "body": "リリース情報は「こちら」。",
        "link": {
            "href": "https://github.com/Naohiro2g/scratch-editor/releases#release-v2100.0.0b4",
            "label": "こちら",
        },
    }
    monkeypatch.setattr(
        render_module,
        "_compose_v12",
        lambda _lock: (
            {"services": {}},
            {
                "runtime/scratch.json": json.dumps(
                    {"notices": operator_notices}, ensure_ascii=False
                )
                + "\n"
            },
        ),
    )

    _compose, rendered = render_module._compose_v13(
        {
            "presentation": {"scratch_release_notice": release_notice},
            "render_plan": {
                "presentation": {"scratch_release_notice": release_notice}
            },
        }
    )

    assert json.loads(rendered["runtime/scratch.json"])["notices"] == [
        *operator_notices,
        release_notice,
    ]


def test_compose_v13_rejects_release_notice_projection_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        render_module,
        "_compose_v12",
        lambda _lock: (
            {"services": {}},
            {"runtime/scratch.json": json.dumps({"notices": [{}]}) + "\n"},
        ),
    )

    with pytest.raises(RenderContractError) as exc_info:
        render_module._compose_v13(
            {
                "presentation": {"scratch_release_notice": {"heading": "one"}},
                "render_plan": {
                    "presentation": {"scratch_release_notice": {"heading": "two"}}
                },
            }
        )

    assert exc_info.value.reason == "render_plan_invalid"


def test_compose_v10_uses_writable_world_scoped_session_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        render_module,
        "_compose_v9",
        lambda _lock: (
            {"services": {"minecraft": {}}},
            {
                "minecraft/plugins/McRemote/config.yml": "old\n",
                "runtime/scratch.json": "{}\n",
            },
        ),
    )

    _compose, rendered_files = render_module._compose_v10(
        {"components": [{"role": "paper-server", "minecraft_version": "1.21.11"}]}
    )

    config = rendered_files["minecraft/plugins/McRemote/config.yml"]
    assert "# Generated by mcrctl compose@10." in config
    assert (
        'credential_store_path: "/data/plugins/McRemote/session-only/store/snapshot.json"'
        in config
    )
    assert (
        'revocation_authority_path: "/data/plugins/McRemote/session-only/authority"'
        in config
    )
    assert "/config/mcremote-session-only" not in config


def test_compose_v11_projects_exact_public_wirescope_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "wirescope-app.zip"
    assets = {
        "index.html": b"<!doctype html><title>WireScope</title>\n",
        "assets/app.js": b"export const ready = true;\n",
    }
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, content in assets.items():
            bundle.writestr(name, content)
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest_value = {
        "manifest_schema": "mcremote.wirescope.app-manifest",
        "manifest_version": 1,
        "archive": {
            "file": "wirescope-app.zip",
            "format": "zip",
            "format_version": 1,
            "sha256": archive_sha256,
        },
        "protocols": {
            "observer_schema": {"name": "mcremote.observer", "version": 1},
            "observer_session": 1,
            "scratch_handoff": 1,
            "station_attach": 1,
        },
        "assets": [
            {
                "path": name,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, content in assets.items()
        ],
    }
    manifest = tmp_path / "wirescope-app.manifest.json"
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    store = tmp_path / "store" / "sha256"
    store.mkdir(parents=True)
    (store / archive_sha256).write_bytes(archive.read_bytes())
    (store / manifest_sha256).write_bytes(manifest.read_bytes())

    routes = {
        "homepage": "mc-remote.example",
        "homepage_aliases": [],
        "scratch": "scratch-beta.mc-remote.example",
        "bridge": "bridge-beta.mc-remote.example",
        "minecraft": "sb-beta.mc-remote.example",
        "wirescope": "wirescope-beta.mc-remote.example",
    }
    operator_input = {
        "role": "public-routes",
        "adapter": "public-routes@2",
        "path": "operator/public-routes/routes.toml",
        "semantic_sha256": render_module.semantic_sha256(routes),
        "semantic": routes,
    }
    lock = {
        "lock_identity": f"sha256:{1:064x}",
        "runtime": {"artifact_store": str(tmp_path / "store")},
        "components": [
            {"id": "wirescope", "role": "wirescope-app", "artifact": "wirescope-zip"},
            {
                "id": "wirescope-manifest",
                "role": "wirescope-manifest",
                "artifact": "wirescope-manifest",
            },
        ],
        "artifacts": [
            {
                "id": "wirescope-zip",
                "kind": "https-file",
                "version": "2100.0.0b4",
                "filename": "wirescope-app.zip",
                "sha256": archive_sha256,
                "origin": "https://example.invalid/wirescope-app.zip",
            },
            {
                "id": "wirescope-manifest",
                "kind": "https-file",
                "version": "2100.0.0b4",
                "filename": "wirescope-app.manifest.json",
                "sha256": manifest_sha256,
                "origin": "https://example.invalid/wirescope-app.manifest.json",
            },
        ],
        "operator_inputs": [operator_input],
        "render_plan": {
            "operator_inputs": [operator_input],
            "semantic_sha256": f"{2:064x}",
        },
    }
    monkeypatch.setattr(
        render_module,
        "_compose_v10",
        lambda _lock: (
            {"services": {"caddy": {"volumes": []}}},
            {
                "Caddyfile": (
                    "# Generated by mcrctl compose@10. Do not edit.\n"
                    "scratch-beta.mc-remote.example {\n"
                    "    reverse_proxy scratch:8080\n"
                    "}\n"
                ),
                "runtime/scratch.json": json.dumps({"notices": []}) + "\n",
            },
        ),
    )

    compose, rendered_files = render_module._compose_v11(lock)

    runtime = json.loads(rendered_files["runtime/scratch.json"])
    assert runtime["wirescope_url"] == "https://wirescope-beta.mc-remote.example/"
    assert "wirescope-beta.mc-remote.example" in rendered_files["Caddyfile"]
    assert "Cross-Origin-Opener-Policy \"unsafe-none\"" in rendered_files["Caddyfile"]
    assert "Cache-Control \"no-store\"" in rendered_files["Caddyfile"]
    assert "Referrer-Policy \"strict-origin-when-cross-origin\"" in rendered_files["Caddyfile"]
    assert {
        "type": "bind",
        "source": "./wirescope",
        "target": "/srv/wirescope",
        "read_only": True,
    } in compose["services"]["caddy"]["volumes"]

    staging = tmp_path / "staging"
    staging.mkdir()
    rendered_paths = render_module._stage_compose_v11(lock, staging)
    assert (staging / "wirescope" / "index.html").read_bytes() == assets["index.html"]
    assert (staging / "wirescope" / "assets" / "app.js").read_bytes() == assets[
        "assets/app.js"
    ]
    assert "wirescope/index.html" in rendered_paths
    assert "wirescope/assets/app.js" in rendered_paths


def test_compose_v9_rejects_missing_connection_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = {
        "bridge_url": "wss://bridge-beta.mc-remote.example",
        "default_sandbox": "sb-beta.mc-remote.example",
        "connection_enabled": True,
        "release_identity": "scratch-b3",
    }
    monkeypatch.setattr(
        render_module,
        "_compose_v8",
        lambda _lock: (
            {"services": {}},
            {"runtime/scratch.json": json.dumps(runtime) + "\n"},
        ),
    )

    with pytest.raises(render_module.RenderContractError) as exc_info:
        render_module._compose_v9({"environment": {"channel": "beta"}})

    assert exc_info.value.reason == "scratch_runtime_config_invalid"
    assert exc_info.value.path == "runtime/scratch.json.connection_targets"


def test_compose_v9_rejects_default_outside_connection_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = {
        "bridge_url": "wss://bridge-beta.mc-remote.example",
        "default_sandbox": "sb-beta.mc-remote.example",
        "connection_targets": [
            {
                "id": "stable",
                "label": "安定版",
                "sandbox": "sb.mc-remote.example",
            }
        ],
        "connection_enabled": True,
        "release_identity": "scratch-b3",
    }
    monkeypatch.setattr(
        render_module,
        "_compose_v8",
        lambda _lock: (
            {"services": {}},
            {"runtime/scratch.json": json.dumps(runtime, ensure_ascii=False) + "\n"},
        ),
    )

    with pytest.raises(render_module.RenderContractError) as exc_info:
        render_module._compose_v9({"environment": {"channel": "beta"}})

    assert exc_info.value.reason == "scratch_runtime_config_invalid"
    assert exc_info.value.path == "runtime/scratch.json.default_sandbox"


def test_compose_v9_projects_required_targets_notice_and_shared_bridge_allowlist(
    tmp_path: Path,
) -> None:
    data_root = files("mc_remote_stack").joinpath("data")
    artifact_store = tmp_path / "artifacts"
    project = init_toml_project(
        tmp_path / "official-public-beta",
        deployment_name="official-public-beta",
        profile="vps-server@7",
        environment_identity="official-public-beta",
        channel="beta",
        exposure="public",
        purpose="integration",
        preset="public-web-paper@2",
        artifact_store=str(artifact_store),
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

[[operator_inputs]]
role = "minecraft-server"
adapter = "minecraft-server@1"
path = "operator/minecraft-server/server.toml"
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
    server = project.root / "operator" / "minecraft-server" / "server.toml"
    server.parent.mkdir(parents=True)
    server.write_text(
        """
allow_flight = false
difficulty = "hard"
enable_query = false
enable_status = true
force_gamemode = true
gamemode = "creative"
hardcore = true
log_ips = true
management_server_enabled = false
max_players = 18
max_tick_time = -1
max_world_size = 9984
motd = "McRemote Sandbox Server"
network_compression_threshold = -1
simulation_distance = 6
spawn_protection = 150
view_distance = 10
white_list = false
""".lstrip(),
        encoding="utf-8",
    )
    _acknowledge(project.root, "unverified")
    with pytest.raises(ResolutionError) as exc_info:
        resolve_project(
            project.root,
            data_root=data_root,
            allow_unverified=True,
            resolved_at=FIRST_RESOLVED_AT,
        )
    assert exc_info.value.reason == "operator_input_required"
    assert "connection-targets" in str(exc_info.value)

    project.order.write_text(
        project.order.read_text(encoding="utf-8")
        + """
[[operator_inputs]]
role = "connection-targets"
adapter = "connection-targets@1"
path = "operator/connection-targets/targets.toml"
""",
        encoding="utf-8",
    )
    targets = project.root / "operator" / "connection-targets" / "targets.toml"
    targets.parent.mkdir(parents=True)
    targets.write_text(
        """
[[targets]]
id = "stable"
label = "Stable"
sandbox = "sb.mc-remote.example"

[[targets]]
id = "beta"
label = "Beta"
sandbox = "beta.sb.mc-remote.example"
""".lstrip(),
        encoding="utf-8",
    )
    resolve_project(
        project.root,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=FIRST_RESOLVED_AT,
    )
    lock = load_lock(project.root, data_root=data_root)
    fixture_artifacts = {
        "paper-jar": (PAPER_SHA256, PAPER_BYTES),
        "mcremote-jar": (PLUGIN_SHA256, PLUGIN_BYTES),
    }
    for artifact in lock["artifacts"]:
        if artifact["id"] in fixture_artifacts:
            artifact["sha256"] = fixture_artifacts[artifact["id"]][0]
    for digest, content in fixture_artifacts.values():
        path = artifact_store / "sha256" / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    _, rendered_files = render_module._compose_v9(lock)

    runtime_config = json.loads(rendered_files["runtime/scratch.json"])
    assert runtime_config["connection_targets"] == [
        {"id": "stable", "label": "Stable", "sandbox": "sb.mc-remote.example"},
        {"id": "beta", "label": "Beta", "sandbox": "beta.sb.mc-remote.example"},
    ]
    assert runtime_config["default_sandbox"] == "sb.mc-remote.example"
    assert runtime_config["notices"] == []

    compose, _ = render_module._compose_v9(lock)
    assert compose["services"]["bridge"]["environment"]["BRIDGE_SANDBOX_ALLOWLIST"] == (
        "beta.sb.mc-remote.example,sb.mc-remote.example"
    )

    staging = tmp_path / "compose-v9-staging"
    staging.mkdir()
    rendered_paths = render_module._stage_compose_v9(lock, staging)
    manifest = json.loads(
        (staging / "render-manifest.json").read_text(encoding="utf-8")
    )
    assert "runtime/scratch.json" in rendered_paths
    assert manifest["adapter_revision"] == "9"


def test_compose_v12_projects_exact_plugins_and_homepage_without_overlays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "artifacts"
    plugin_bytes = b"peripheral plugin\n"
    plugin_sha256 = hashlib.sha256(plugin_bytes).hexdigest()
    plugin_path = store / "sha256" / plugin_sha256
    plugin_path.parent.mkdir(parents=True)
    plugin_path.write_bytes(plugin_bytes)
    homepage_source = tmp_path / "homepage-source"
    homepage_source.mkdir()
    (homepage_source / "index.html").write_text("homepage\n", encoding="utf-8")
    homepage = import_homepage_tree(homepage_source, store)

    routes_semantic = {
        "homepage": "mc-remote.example",
        "homepage_aliases": ["www.mc-remote.example"],
        "scratch": "scratch-beta.mc-remote.example",
        "bridge": "bridge-beta.mc-remote.example",
        "minecraft": "sb-beta.mc-remote.example",
        "wirescope": "wirescope-beta.mc-remote.example",
    }
    plugins_semantic = {
        "plugins": [{"filename": "WorldEdit.jar", "sha256": plugin_sha256}]
    }
    homepage_semantic = {
        "tree_sha256": homepage.tree_sha256,
        "file_count": homepage.file_count,
        "total_bytes": homepage.total_bytes,
    }
    backup_path = tmp_path / "backup"
    backup_path.mkdir()
    backup_semantic = {"host_path": str(backup_path)}
    operator_inputs = [
        {
            "role": "public-routes",
            "adapter": "public-routes@2",
            "path": "operator/public-routes/routes.toml",
            "semantic_sha256": semantic_sha256(routes_semantic),
            "semantic": routes_semantic,
        },
        {
            "role": "minecraft-plugins",
            "adapter": "minecraft-plugins@1",
            "path": "operator/minecraft-plugins/plugins.toml",
            "semantic_sha256": semantic_sha256(plugins_semantic),
            "semantic": plugins_semantic,
        },
        {
            "role": "homepage-static",
            "adapter": "homepage-static@1",
            "path": "operator/homepage-static/homepage.toml",
            "semantic_sha256": semantic_sha256(homepage_semantic),
            "semantic": homepage_semantic,
        },
        {
            "role": "minecraft-backup",
            "adapter": "minecraft-backup@1",
            "path": "operator/minecraft-backup/backup.toml",
            "semantic_sha256": semantic_sha256(backup_semantic),
            "semantic": backup_semantic,
        },
    ]
    lock = {
        "runtime": {"artifact_store": str(store)},
        "operator_inputs": operator_inputs,
        "render_plan": {"operator_inputs": operator_inputs},
    }
    homepage_domains = "mc-remote.example, www.mc-remote.example"
    base_compose = {
        "services": {
            "minecraft": {
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/artifacts/mcremote",
                        "target": "/plugins/McRemote.jar",
                        "read_only": True,
                    }
                ],
                "labels": {},
            },
            "caddy": {"volumes": [], "labels": {}},
        }
    }
    base_files = {
        "Caddyfile": f'''# Generated by mcrctl compose@11. Do not edit.
{homepage_domains} {{
    encode zstd gzip
    respond "McRemote public edge is healthy; homepage content is not installed." 200
}}
'''
    }
    monkeypatch.setattr(
        render_module,
        "_compose_v11",
        lambda _lock: (copy.deepcopy(base_compose), dict(base_files)),
    )

    homepage.path.chmod(0o700)
    with pytest.raises(RenderContractError) as exc_info:
        render_module._compose_v12(lock)
    assert exc_info.value.reason == "runtime_content_permissions_invalid"
    homepage.path.chmod(0o755)

    compose, rendered = render_module._compose_v12(lock)

    assert {
        "type": "bind",
        "source": str(plugin_path),
        "target": "/plugins/WorldEdit.jar",
        "read_only": True,
    } in compose["services"]["minecraft"]["volumes"]
    assert {
        "type": "bind",
        "source": str(homepage.path),
        "target": "/srv/homepage",
        "read_only": True,
    } in compose["services"]["caddy"]["volumes"]
    assert {
        "type": "bind",
        "source": str(backup_path),
        "target": "/backup",
    } in compose["services"]["minecraft"]["volumes"]
    assert "root * /srv/homepage" in rendered["Caddyfile"]
    assert "file_server" in rendered["Caddyfile"]
    assert "content is not installed" not in rendered["Caddyfile"]
    assert "compose@12" in rendered["Caddyfile"]


def test_compose_v12_rejects_missing_peripheral_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins = {"plugins": [{"filename": "WorldEdit.jar", "sha256": "a" * 64}]}
    homepage = {"tree_sha256": "b" * 64, "file_count": 1, "total_bytes": 1}
    backup = {"host_path": str(tmp_path / "backup")}
    inputs = [
        {
            "role": "minecraft-plugins",
            "adapter": "minecraft-plugins@1",
            "path": "operator/minecraft-plugins/plugins.toml",
            "semantic_sha256": semantic_sha256(plugins),
            "semantic": plugins,
        },
        {
            "role": "homepage-static",
            "adapter": "homepage-static@1",
            "path": "operator/homepage-static/homepage.toml",
            "semantic_sha256": semantic_sha256(homepage),
            "semantic": homepage,
        },
        {
            "role": "minecraft-backup",
            "adapter": "minecraft-backup@1",
            "path": "operator/minecraft-backup/backup.toml",
            "semantic_sha256": semantic_sha256(backup),
            "semantic": backup,
        },
    ]
    monkeypatch.setattr(
        render_module,
        "_compose_v11",
        lambda _lock: (
            {
                "services": {
                    "minecraft": {"volumes": [], "labels": {}},
                    "caddy": {"volumes": [], "labels": {}},
                }
            },
            {"Caddyfile": ""},
        ),
    )

    with pytest.raises(RenderContractError) as exc_info:
        render_module._compose_v12(
            {
                "runtime": {"artifact_store": str(tmp_path / "store")},
                "operator_inputs": inputs,
                "render_plan": {"operator_inputs": inputs},
            }
        )

    assert exc_info.value.reason == "runtime_content_missing"
