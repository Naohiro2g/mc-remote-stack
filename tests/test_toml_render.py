import hashlib
import json
import os
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from mc_remote_stack.cli import main
from mc_remote_stack.preset_registry import build_preset_catalog
from mc_remote_stack.render import RenderContractError, render_toml_project
from mc_remote_stack.resolver import ResolutionError, resolve_project
from mc_remote_stack.toml_project import init_toml_project, update_order_scalar

from .test_preset_registry import _data_root, _write_policy
from .test_resolver import FIRST_RESOLVED_AT, SECOND_RESOLVED_AT, _acknowledge

PAPER_BYTES = b"deterministic paper fixture\n"
PLUGIN_BYTES = b"deterministic mcremote fixture\n"
PAPER_SHA256 = hashlib.sha256(PAPER_BYTES).hexdigest()
PLUGIN_SHA256 = hashlib.sha256(PLUGIN_BYTES).hexdigest()
OCI_DIGEST = f"sha256:{11:064x}"


def _preset_source() -> str:
    return f"""schema_version = 1

[preset]
name = "mcremote-paper"
revision = "1"
description = "Deterministic compose renderer fixture"

[requirements]
profile_capabilities = ["compose", "paper", "persistent-world"]
allowed_channels = ["beta"]
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


def _render_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    data_root = _data_root(tmp_path, "render-data")
    profile_path = data_root / "profiles" / "home-server" / "1" / "profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        files("mc_remote_stack")
        .joinpath("data", "profiles", "home-server", "1", "profile.toml")
        .read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    preset_path = data_root / "preset_registry" / "mcremote-paper" / "1" / "preset.toml"
    preset_path.parent.mkdir(parents=True)
    preset_path.write_text(_preset_source(), encoding="utf-8")
    _write_policy(
        data_root,
        [
            {
                "ref": "mcremote-paper@1",
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

    project = init_toml_project(
        tmp_path / "home-beta",
        deployment_name="home",
        profile="home-server@1",
        environment_identity="home-beta",
        channel="beta",
        exposure="isolated",
        purpose="integration",
        preset="mcremote-paper@1",
        artifact_store=str(artifact_store),
        runtime_volumes={"minecraft-data": "home-beta-minecraft-data"},
        world_identity="home-beta-world",
        bind_address="127.0.0.1",
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
    assert manifest["adapter_revision"] == "1"
    assert manifest["lock_identity"] == first.lock_identity
    assert [entry["path"] for entry in manifest["files"]] == [
        "compose.yaml",
        "minecraft/server.properties",
    ]


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
    assert "OK render status=created adapter=compose@1 lock=sha256:" in rendered
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
