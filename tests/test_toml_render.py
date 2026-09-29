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
from mc_remote_stack.runtime_content import import_homepage_tree
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
            {
                "services": {
                    "scratch": {
                        "volumes": [
                            {
                                "type": "bind",
                                "source": "./runtime/scratch.json",
                                "target": "/usr/share/nginx/html/mc-remote-runtime-config.json",
                                "read_only": True,
                            }
                        ]
                    }
                }
            },
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


def test_compose_v13_skips_release_notice_when_preset_has_no_presentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEC 2026-09-05-02: product-config's {version} notice is now the sole
    developer-notice source, so a preset may omit `[presentation]` entirely."""

    operator_notices = [
        {
            "heading": "今後のリリース予定",
            "body": "10月RC版、年内に安定版リリース予定です。",
            "link": {"href": "https://mc-remote.com", "label": "公式サイトを見る"},
        },
    ]
    monkeypatch.setattr(
        render_module,
        "_compose_v12",
        lambda _lock: (
            {
                "services": {
                    "scratch": {
                        "volumes": [
                            {
                                "type": "bind",
                                "source": "./runtime/scratch.json",
                                "target": "/usr/share/nginx/html/mc-remote-runtime-config.json",
                                "read_only": True,
                            }
                        ]
                    }
                }
            },
            {
                "runtime/scratch.json": json.dumps(
                    {"notices": operator_notices}, ensure_ascii=False
                )
                + "\n"
            },
        ),
    )

    _compose, rendered = render_module._compose_v13({"render_plan": {}})

    assert json.loads(rendered["runtime/scratch.json"])["notices"] == operator_notices


def test_compose_v13_projects_locked_b7_runtime_contract_without_release_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = {
        "bridge_url": "wss://bridge-beta.mc-remote.com",
        "default_sandbox": "sb.mc-remote.com",
        "connection_targets": [
            {"id": "official", "label": "Official", "sandbox": "sb.mc-remote.com"}
        ],
        "connection_enabled": True,
        "release_identity": "sha-obsolete",
        "notices": [{"heading": "Operator", "body": "Maintenance notice"}],
    }
    release_notice = {
        "heading": "McRemote b7",
        "body": "Release information",
    }
    contract = {
        "source_commit": "4c893bd532002d9216665c5c9b9825e09ede1e7c",
        "source_directory": "packages/scratch-gui/contracts/runtime-config",
        "directory_tree_sha": "ecb669a02ac6c8e502b44850e6dd28260c5adad4",
        "schema_sha256": "4e1f8489dc6ea03800f5cf0fefd2f078fd6d71c8efda581f1711f68e384f99e4",
        "container_mount_path": "/usr/share/nginx/html/mc-remote-runtime-config.json",
        "image_digest": "sha256:"
        "c2693fd5078ba547ced1cfe6b1732d5e9c283ffc44aaf8356bce6967e5aa7f2c",
        "accepted_fixtures": ["fixtures/disabled.json", "fixtures/valid.json"],
        "rejected_fixtures": [
            "fixtures/invalid/enabled-missing-targets.json",
            "fixtures/invalid/nested-unknown-field.json",
            "fixtures/invalid/schema-version.json",
            "fixtures/invalid/unknown-field.json",
        ],
        "fixture_sha256": {
            "fixtures/disabled.json": "bec0cf2c31fbca7d3bd603e29c1d145d82b6ecf7b4661b9adafde31dfa2eec2d",
            "fixtures/valid.json": "cc2282144e1e87b42d2e31229461f9aeead26eeb446ae817571eb96935360e20",
            "fixtures/invalid/enabled-missing-targets.json": (
                "f5392059e42431b45fb8f7f03aa09e734fb8b9e79f8fb92913082ff05842736c"
            ),
            "fixtures/invalid/nested-unknown-field.json": (
                "ed0923793b8713ec67615b6f14bc9f8fb342041b0cdeed5d7b9b10110a3c62b7"
            ),
            "fixtures/invalid/schema-version.json": "9e0d32fc83501a2b73095ae59675e27ace5d11fb31e19443b78e49341ba3e766",
            "fixtures/invalid/unknown-field.json": "35f1f21562e237cce722f5a1f93723f00d927d07769f8b12174d3ff9f73d5e3d",
        },
    }
    monkeypatch.setattr(
        render_module,
        "_compose_v12",
        lambda _lock: (
            {
                "services": {
                    "scratch": {
                        "volumes": [
                            {
                                "type": "bind",
                                "source": "./runtime/scratch.json",
                                "target": contract["container_mount_path"],
                                "read_only": True,
                            }
                        ]
                    }
                }
            },
            {"runtime/scratch.json": json.dumps(runtime, ensure_ascii=False) + "\n"},
        ),
    )

    _compose, rendered = render_module._compose_v13(
        {
            "components": [
                {
                    "id": "scratch",
                    "role": "scratch-runtime",
                    "artifact": "scratch-image",
                }
            ],
            "artifacts": [
                {
                    "id": "scratch-image",
                    "kind": "oci",
                    "digest": contract["image_digest"],
                }
            ],
            "scratch_runtime_contract": contract,
            "presentation": {"scratch_release_notice": release_notice},
            "render_plan": {
                "scratch_runtime_contract": contract,
                "presentation": {"scratch_release_notice": release_notice},
            },
        }
    )

    projected = json.loads(rendered["runtime/scratch.json"])
    assert projected["schema_version"] == 1
    assert "release_identity" not in projected
    assert projected["notices"] == [runtime["notices"][0], release_notice]


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

    compose, rendered = render_module._compose_v12(lock)

    assert {
        "type": "bind",
        "source": str(plugin_path),
        "target": "/plugins/WorldEdit.jar",
        "read_only": True,
    } in compose["services"]["minecraft"]["volumes"]
    assert {
        "type": "bind",
        "source": str(store.parent / "homepage"),
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
