import copy
import hashlib
import json
import os
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

import mc_remote_stack.render as render_module
from mc_remote_stack.cli import main
from mc_remote_stack.preset_registry import build_preset_catalog, semantic_sha256
from mc_remote_stack.render import RenderContractError, render_toml_project
from mc_remote_stack.resolver import ResolutionError, load_lock, resolve_project
from mc_remote_stack.toml_project import init_toml_project, update_order_scalar

from .test_preset_registry import _data_root, _write_policy
from .test_resolver import FIRST_RESOLVED_AT, SECOND_RESOLVED_AT, _acknowledge
from .vps_fixture import build_vps_fixture

PAPER_BYTES = b"deterministic paper fixture\n"
PLUGIN_BYTES = b"deterministic mcremote fixture\n"
PAPER_SHA256 = hashlib.sha256(PAPER_BYTES).hexdigest()
PLUGIN_SHA256 = hashlib.sha256(PLUGIN_BYTES).hexdigest()
OCI_DIGEST = f"sha256:{11:064x}"


def _render_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Resolved public VPS project, data root, and artifact store."""

    fixture = build_vps_fixture(tmp_path)
    return fixture.project, fixture.data_root, fixture.artifact_store


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_toml_compose_renderer_uses_only_locked_artifacts_and_instance_contract(
    tmp_path: Path,
) -> None:
    project, data_root, _ = _render_fixture(tmp_path)
    output = project / "generated"

    result = render_toml_project(project, output, data_root=data_root)

    assert result.status == "created"
    compose = yaml.safe_load((output / "compose.yaml").read_text(encoding="utf-8"))
    minecraft = compose["services"]["minecraft"]
    assert compose["name"] == "official-vps"
    assert minecraft["environment"]["LEVEL"] == "official-vps-world"
    assert minecraft["environment"]["SYNC_SKIP_NEWER_IN_DESTINATION"] == "true", (
        "server.properties / plugin config must be seed-once, not "
        "force-reasserted over an operator's live edit on every restart "
        "(docs/operator-editable-runtime-config-design_ja.md)"
    )
    assert compose["volumes"] == {
        role: {"name": f"official-vps-{role}", "external": True}
        for role in ("caddy-config", "caddy-data", "minecraft-data")
    }
    for service in compose["services"].values():
        assert service["labels"]["io.mc-remote.lock"] == result.lock_identity


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
    assert manifest["adapter_revision"] == "13"
    assert manifest["lock_identity"] == first.lock_identity
    assert [entry["path"] for entry in manifest["files"]] == [
        "compose.yaml",
        "Caddyfile",
        "runtime/scratch.json",
        "minecraft/server.properties",
        "minecraft/plugins/McRemote/config.yml",
        "wirescope/assets/app.js",
        "wirescope/index.html",
        "wirescope/wirescope-app.manifest.json",
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
    assert compose["services"]["minecraft"]["ports"][0] == "0.0.0.0:25566:25565/tcp"


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
    assert "OK render status=created adapter=compose@13 lock=sha256:" in rendered
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
            'identity = "official-vps-world"',
            'identity = "tampered-world"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ResolutionError) as exc_info:
        render_toml_project(project, project / "generated", data_root=data_root)

    assert exc_info.value.reason == "lock_identity_mismatch"


def _write_home_alpha_full_preset(data_root: Path, *, revision: str = "1") -> None:
    preset_path = (
        data_root / "preset_registry" / "home-alpha-full" / revision / "preset.toml"
    )
    preset_path.parent.mkdir(parents=True)
    preset_path.write_text(
        f"""schema_version = 1

[preset]
name = "home-alpha-full"
revision = "{revision}"
description = "Deterministic home-server@6 compose renderer fixture"

[requirements]
profile_capabilities = [
  "compose",
  "paper",
  "persistent-world",
  "scratch-runtime",
  "websocket-bridge",
  "mcremote-auth-enforced",
]
allowed_channels = ["alpha"]
required_claims = ["profile-render"]

[[components]]
id = "caddy"
role = "caddy-edge"
artifact = "caddy-image"

[[components]]
id = "scratch"
role = "scratch-runtime"
artifact = "scratch-image"

[[components]]
id = "bridge"
role = "websocket-bridge"
artifact = "bridge-image"

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
id = "caddy-image"
kind = "oci"
version = "fixture-caddy"
locator = "registry.example/caddy"
digest = "{OCI_DIGEST}"

[[artifacts]]
id = "scratch-image"
kind = "oci"
version = "sha-{"a" * 40}"
locator = "registry.example/scratch"
digest = "{OCI_DIGEST}"

[[artifacts]]
id = "bridge-image"
kind = "oci"
version = "sha-{"a" * 40}"
locator = "registry.example/bridge"
digest = "{OCI_DIGEST}"

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
""",
        encoding="utf-8",
    )
    _write_policy(
        data_root,
        [
            {
                "ref": f"home-alpha-full@{revision}",
                "status": "active",
                "available_since": "2026-08-29",
            }
        ],
    )
    (data_root / "preset_catalog.toml").write_bytes(build_preset_catalog(data_root=data_root))


def test_compose_v14_projects_tailnet_lan_edge_on_loopback(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path, "home-alpha-full-data")
    profile_path = data_root / "profiles" / "home-server" / "6" / "profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        files("mc_remote_stack")
        .joinpath("data", "profiles", "home-server", "6", "profile.toml")
        .read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _write_home_alpha_full_preset(data_root)

    artifact_store = tmp_path / "artifacts"
    project = init_toml_project(
        tmp_path / "home-alpha",
        deployment_name="home-alpha",
        profile="home-server@6",
        environment_identity="home-alpha",
        channel="alpha",
        exposure="isolated",
        purpose="integration",
        preset="home-alpha-full@1",
        artifact_store=str(artifact_store),
        runtime_volumes={
            "minecraft-data": "home-alpha-minecraft-data",
            "caddy-data": "home-alpha-caddy-data",
            "caddy-config": "home-alpha-caddy-config",
        },
        world_identity="home-alpha-world",
        bind_address="127.0.0.1",
        java_port=25566,
        mcremote_port=25576,
        minecraft_eula=True,
    )
    project.order.write_text(
        project.order.read_text(encoding="utf-8")
        + """
[[operator_inputs]]
role = "lan-routes"
adapter = "lan-routes@1"
path = "operator/lan-routes/routes.toml"
""",
        encoding="utf-8",
    )
    routes = project.root / "operator" / "lan-routes" / "routes.toml"
    routes.parent.mkdir(parents=True)
    routes.write_text(
        """
hostname = "m720s1.example-tailnet.ts.net"
scratch_port = 8443
bridge_port = 8444
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

    compose, rendered_files = render_module._compose_v14(lock)

    assert compose["services"]["caddy"]["ports"] == [
        "127.0.0.1:8443:8443/tcp",
        "127.0.0.1:8444:8444/tcp",
    ]
    assert compose["services"]["minecraft"]["ports"] == [
        "127.0.0.1:25566:25565/tcp",
        "127.0.0.1:25566:25565/udp",
        "127.0.0.1:25576:25575/tcp",
    ]
    assert compose["services"]["minecraft"]["networks"] == {
        "app": {"aliases": ["m720s1.example-tailnet.ts.net"]},
        "egress": {"gw_priority": 1},
    }
    assert compose["networks"]["egress"] == {
        "internal": False,
        "enable_ipv6": False,
    }, "minecraft needs internet egress to download the vanilla jar from Mojang at first boot"
    assert compose["services"]["minecraft"]["environment"][
        "SYNC_SKIP_NEWER_IN_DESTINATION"
    ] == "true", (
        "server.properties and McRemote's config.yml must be seed-once, not "
        "force-reasserted over an operator's live edit on every restart"
    )
    assert all(
        "tls" not in str(mount) for mount in compose["services"]["caddy"]["volumes"]
    )
    caddyfile = rendered_files["Caddyfile"]
    assert ":8443 {" in caddyfile
    assert ":8444 {" in caddyfile
    assert "m720s1.example-tailnet.ts.net" not in caddyfile
    assert "tls " not in caddyfile
    runtime_config = json.loads(rendered_files["runtime/scratch.json"])
    assert runtime_config["bridge_url"] == "wss://m720s1.example-tailnet.ts.net:8444"
    assert runtime_config["default_sandbox"] == "m720s1.example-tailnet.ts.net"
    # Regression: scratch-editor's client-side loader requires a non-empty
    # connection_targets array containing default_sandbox, or it throws and
    # silently falls back to the embedded sb.mc-remote.com default — this is
    # exactly what happened on the real m720s1 bootstrap.
    assert runtime_config["connection_targets"] == [
        {"id": "alpha", "label": "Alpha", "sandbox": "m720s1.example-tailnet.ts.net"},
    ]
    assert len(runtime_config["notices"]) == 1
    assert runtime_config["notices"][0]["heading"] == "McRemote home private alpha"
    assert compose["services"]["bridge"]["environment"]["BRIDGE_ORIGIN_ALLOWLIST"] == (
        "https://m720s1.example-tailnet.ts.net:8443"
    )
    assert "auth:\n  enforcement: true\n" in rendered_files[
        "minecraft/plugins/McRemote/config.yml"
    ]

    staging = tmp_path / "staging"
    staging.mkdir()
    rendered_paths = render_module._stage_compose_v14(lock, staging)
    manifest = json.loads((staging / "render-manifest.json").read_text(encoding="utf-8"))
    assert manifest["adapter_revision"] == "14"
    assert set(rendered_paths) == {
        "compose.yaml",
        "Caddyfile",
        "runtime/scratch.json",
        "minecraft/server.properties",
        "minecraft/plugins/McRemote/config.yml",
    }


def test_compose_v14_renders_through_the_real_toml_render_dispatch(tmp_path: Path) -> None:
    """Regression test: calling _compose_v14 directly does not exercise the
    separate adapter-revision allowlist inside _load_current_toml_render_lock
    (used by render_toml_project and apply); compose@14 must be accepted there too.
    """
    data_root = _data_root(tmp_path, "home-alpha-full-dispatch-data")
    profile_path = data_root / "profiles" / "home-server" / "6" / "profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        files("mc_remote_stack")
        .joinpath("data", "profiles", "home-server", "6", "profile.toml")
        .read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _write_home_alpha_full_preset(data_root)

    artifact_store = tmp_path / "artifacts"
    project = init_toml_project(
        tmp_path / "home-alpha",
        deployment_name="home-alpha",
        profile="home-server@6",
        environment_identity="home-alpha",
        channel="alpha",
        exposure="isolated",
        purpose="integration",
        preset="home-alpha-full@1",
        artifact_store=str(artifact_store),
        runtime_volumes={
            "minecraft-data": "home-alpha-minecraft-data",
            "caddy-data": "home-alpha-caddy-data",
            "caddy-config": "home-alpha-caddy-config",
        },
        world_identity="home-alpha-world",
        bind_address="127.0.0.1",
        java_port=25566,
        mcremote_port=25576,
        minecraft_eula=True,
    )
    project.order.write_text(
        project.order.read_text(encoding="utf-8")
        + """
[[operator_inputs]]
role = "lan-routes"
adapter = "lan-routes@1"
path = "operator/lan-routes/routes.toml"
""",
        encoding="utf-8",
    )
    routes = project.root / "operator" / "lan-routes" / "routes.toml"
    routes.parent.mkdir(parents=True)
    routes.write_text(
        """
hostname = "m720s1.example-tailnet.ts.net"
scratch_port = 8443
bridge_port = 8444
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
    # _write_home_alpha_full_preset already declares PAPER_SHA256/PLUGIN_SHA256
    # as the paper-jar/mcremote-jar artifact hashes, so populating the
    # content-addressed store under those exact digests is enough; the lock
    # itself needs no post-resolve mutation.
    (artifact_store / "sha256" / PAPER_SHA256).parent.mkdir(parents=True, exist_ok=True)
    (artifact_store / "sha256" / PAPER_SHA256).write_bytes(PAPER_BYTES)
    (artifact_store / "sha256" / PLUGIN_SHA256).write_bytes(PLUGIN_BYTES)

    output = project.root / "generated"
    result = render_toml_project(project.root, output, data_root=data_root)

    assert result.status == "created"
    assert result.adapter == "compose"
    assert result.adapter_revision == "14"
    compose = yaml.safe_load((output / "compose.yaml").read_text(encoding="utf-8"))
    assert compose["services"]["caddy"]["ports"] == [
        "127.0.0.1:8443:8443/tcp",
        "127.0.0.1:8444:8444/tcp",
    ]
    # Regression: apply's pre-apply verification re-reads render-manifest.json
    # through _load_managed_manifest, which has its own adapter_revision
    # allowlist separate from _stage_current's and
    # _load_current_toml_render_lock's. A fresh render into an empty
    # directory never exercises this path (it returns None early), so only a
    # *second* look at a now-populated output catches a missing "14" here.
    manifest = render_module._load_managed_manifest(output)
    assert manifest is not None
    assert manifest["adapter_revision"] == "14"


def _public_lock(tmp_path: Path) -> dict:
    fixture = build_vps_fixture(tmp_path)
    return load_lock(fixture.project, data_root=fixture.data_root)


def _replace_connection_targets(lock: dict, semantic: dict) -> None:
    for inputs in (lock["operator_inputs"], lock["render_plan"]["operator_inputs"]):
        for item in inputs:
            if item["role"] == "connection-targets":
                item["semantic"] = copy.deepcopy(semantic)
                item["semantic_sha256"] = semantic_sha256(semantic)


def test_public_renderer_appends_preset_release_notice_after_operator_feed(
    tmp_path: Path,
) -> None:
    lock = _public_lock(tmp_path)
    release_notice = {
        "heading": "マイクラリモコンScratchクライアント ver.2100.0.0b4",
        "body": "リリース情報は「こちら」。",
        "link": {"href": "https://example.invalid/releases", "label": "こちら"},
    }
    lock["presentation"] = {"scratch_release_notice": release_notice}
    lock["render_plan"]["presentation"] = copy.deepcopy(lock["presentation"])

    _compose, rendered = render_module._compose_public(lock)

    notices = json.loads(rendered["runtime/scratch.json"])["notices"]
    assert notices == [{"heading": "お知らせ", "body": "fixture notice"}, release_notice]


def test_public_renderer_rejects_release_notice_projection_mismatch(tmp_path: Path) -> None:
    lock = _public_lock(tmp_path)
    lock["presentation"] = {"scratch_release_notice": {"heading": "one"}}
    lock["render_plan"]["presentation"] = {"scratch_release_notice": {"heading": "two"}}

    with pytest.raises(RenderContractError) as exc_info:
        render_module._compose_public(lock)

    assert exc_info.value.reason == "render_plan_invalid"


@pytest.mark.parametrize(
    ("targets", "path"),
    [
        ([], "runtime/scratch.json.connection_targets"),
        (
            [{"id": "other", "label": "Other", "sandbox": "other.mc-remote.example"}],
            "runtime/scratch.json.default_sandbox",
        ),
    ],
)
def test_public_renderer_rejects_unusable_connection_targets(
    tmp_path: Path,
    targets: list,
    path: str,
) -> None:
    lock = _public_lock(tmp_path)
    _replace_connection_targets(
        lock,
        {"targets": targets, "notices": [{"heading": "お知らせ", "body": "fixture notice"}]},
    )

    with pytest.raises(RenderContractError) as exc_info:
        render_module._compose_public(lock)

    assert exc_info.value.reason == "scratch_runtime_config_invalid"
    assert exc_info.value.path == path


def test_public_renderer_rejects_missing_peripheral_plugin(tmp_path: Path) -> None:
    lock = _public_lock(tmp_path)
    plugins = next(
        item for item in lock["operator_inputs"] if item["role"] == "minecraft-plugins"
    )
    for plugin in plugins["semantic"]["plugins"]:
        (Path(lock["runtime"]["artifact_store"]) / "sha256" / plugin["sha256"]).unlink()

    with pytest.raises(RenderContractError) as exc_info:
        render_module._compose_public(lock)

    assert exc_info.value.reason == "runtime_content_missing"
