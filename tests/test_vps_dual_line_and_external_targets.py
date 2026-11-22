"""Tests for vps-server@21 / compose@23 dual-line (Latest v2 + LTS v1) and external targets."""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.request import Request

import pytest
import yaml

from mc_remote_stack.artifacts import fetch_locked_artifacts
from mc_remote_stack.cli import main
from mc_remote_stack.deployment_update import (
    apply_deployment_update,
    plan_deployment_update,
)
from mc_remote_stack.doctor import (
    DoctorContractError,
    ProtocolHelloResult,
    doctor_toml_project,
    probe_external_mcremote_auth,
    validate_scratch_runtime_config,
)
from mc_remote_stack.operator_inputs import (
    OperatorInputError,
    _parse_connection_targets_v5,
    _parse_lts_web,
)
from mc_remote_stack.preset_registry import build_preset_catalog
from mc_remote_stack.render import RenderContractError, render_toml_project
from mc_remote_stack.resolver import ResolutionError, load_lock, resolve_project
from mc_remote_stack.runtime_content import import_homepage_tree
from mc_remote_stack.scratch_contract import load_runtime_config_schema
from mc_remote_stack.toml_project import init_toml_project

from .test_doctor import _vps_doctor_responses
from .test_preset_registry import _data_root, _write_policy
from .test_resolver import FIRST_RESOLVED_AT, _acknowledge
from .test_toml_apply import FakeDocker, _result
from .vps_fixture import (
    MCREMOTE_BYTES,
    OPERATOR_INPUTS,
    PAPER_BYTES,
    PLUGIN_BYTES,
    PUBLIC_ROUTES_V3,
    VpsFixture,
    _oci_digest,
    _sha256,
    rendered_containers,
)
from .vps_fixture import (
    SCRATCH_CONTRACT as SCRATCH_CONTRACT_V1,
)
from .vps_fixture import (
    SCRATCH_CONTRACT_COMMIT as SCRATCH_CONTRACT_V1_COMMIT,
)
from .vps_fixture import (
    SCRATCH_IMAGE_DIGEST as SCRATCH_V1_IMAGE_DIGEST,
)
from .vps_fixture import (
    WIRESCOPE_MANIFEST_BYTES as WIRESCOPE_V1_MANIFEST_BYTES,
)
from .vps_fixture import (
    WIRESCOPE_ZIP_BYTES as WIRESCOPE_V1_ZIP_BYTES,
)

SCRATCH_CONTRACT_V2_COMMIT = "0ae8af418d095bac734f37bfca35804810ec1cae"
SCRATCH_V2_IMAGE_DIGEST = _oci_digest(122)
UNITY_LTS_DIGEST = f"sha256:{'b' * 64}"
UNITY_LATEST_DIGEST = f"sha256:{'c' * 64}"

WIRESCOPE_V2_ASSETS = {
    "index.html": b"<!doctype html><title>WireScope 2400</title>\n",
    "assets/app.js": b"export const series = 2400;\n",
}


def _make_zip(assets: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, content in assets.items():
            bundle.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), content)
    return buffer.getvalue()


WIRESCOPE_V2_ZIP_BYTES = _make_zip(WIRESCOPE_V2_ASSETS)
WIRESCOPE_V2_MANIFEST_BYTES = json.dumps(
    {
        "manifest_schema": "mcremote.wirescope.app-manifest",
        "manifest_version": 1,
        "archive": {
            "file": "wirescope-app.zip",
            "format": "zip",
            "format_version": 1,
            "sha256": _sha256(WIRESCOPE_V2_ZIP_BYTES),
        },
        "protocols": {
            "observer_schema": {"name": "mcremote.observer", "version": 1},
            "observer_session": 1,
            "scratch_handoff": 1,
            "station_attach": 1,
        },
        "assets": [
            {"path": name, "bytes": len(content), "sha256": _sha256(content)}
            for name, content in WIRESCOPE_V2_ASSETS.items()
        ],
    },
    sort_keys=True,
).encode("utf-8")

SCRATCH_CONTRACT_V2 = f'''[scratch_runtime_contract]
source_commit = "{SCRATCH_CONTRACT_V2_COMMIT}"
source_directory = "packages/scratch-gui/contracts/runtime-config"
directory_tree_sha = "11b998a78c185a290c6c0d730d008af34c838036"
schema_sha256 = "1720bc180b31f0275e4b2d264f149dcbe1c74b4dc151e63aafbb851303c3c1e3"
container_mount_path = "/usr/share/nginx/html/mc-remote-runtime-config.json"
image_digest = "{SCRATCH_V2_IMAGE_DIGEST}"
accepted_fixtures = ["fixtures/disabled.json", "fixtures/valid.json"]
rejected_fixtures = [
  "fixtures/invalid/enabled-missing-targets.json",
  "fixtures/invalid/nested-unknown-field.json",
  "fixtures/invalid/schema-version.json",
  "fixtures/invalid/unknown-field.json",
]

[scratch_runtime_contract.fixture_sha256]
"fixtures/disabled.json" = "bec0cf2c31fbca7d3bd603e29c1d145d82b6ecf7b4661b9adafde31dfa2eec2d"
"fixtures/valid.json" = "cc2282144e1e87b42d2e31229461f9aeead26eeb446ae817571eb96935360e20"
"fixtures/invalid/enabled-missing-targets.json" = "f5392059e42431b45fb8f7f03aa09e734fb8b9e79f8fb92913082ff05842736c"
"fixtures/invalid/nested-unknown-field.json" = "ed0923793b8713ec67615b6f14bc9f8fb342041b0cdeed5d7b9b10110a3c62b7"
"fixtures/invalid/schema-version.json" = "0b2b8fd116adf66c362e8950519da05324816c3b30c2c76842e398c8ede82e17"
"fixtures/invalid/unknown-field.json" = "35f1f21562e237cce722f5a1f93723f00d927d07769f8b12174d3ff9f73d5e3d"'''


def _preset_toml(
    *,
    revision: str,
    scratch_contract: str,
    scratch_digest: str,
    bridge_seed: int,
    wirescope_zip: bytes,
    wirescope_manifest: bytes,
    wirescope_zip_name: str,
    wirescope_manifest_name: str,
    protocol: str,
    version_tag: str,
) -> str:
    return f"""schema_version = 1

[preset]
name = "fixture-web-paper"
revision = "{revision}"
description = "Test-owned public VPS preset ({version_tag})"

[requirements]
profile_capabilities = [
  "compose",
  "paper",
  "persistent-world",
  "public-web",
  "scratch-runtime",
  "websocket-bridge",
  "wirescope-browser",
  "mcremote-auth-enforced",
  "mcremote-session-only",
]
allowed_channels = ["beta", "stable"]
required_claims = ["profile-render"]

{scratch_contract}

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
protocol = "{protocol}"

[[components]]
id = "wirescope"
role = "wirescope-app"
artifact = "wirescope-zip"

[[components]]
id = "wirescope-manifest"
role = "wirescope-manifest"
artifact = "wirescope-manifest"

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
protocol = "{protocol}"

[[artifacts]]
id = "caddy-image"
kind = "oci"
version = "fixture"
locator = "registry.example/caddy"
digest = "{_oci_digest(21)}"

[[artifacts]]
id = "scratch-image"
kind = "oci"
version = "{version_tag}"
locator = "registry.example/scratch"
digest = "{scratch_digest}"

[[artifacts]]
id = "bridge-image"
kind = "oci"
version = "{version_tag}"
locator = "registry.example/bridge"
digest = "{_oci_digest(bridge_seed)}"

[[artifacts]]
id = "minecraft-image"
kind = "oci"
version = "fixture-java21"
locator = "registry.example/minecraft"
digest = "{_oci_digest(24)}"

[[artifacts]]
id = "paper-jar"
kind = "https-file"
version = "fixture"
filename = "paper-fixture.jar"
sha256 = "{_sha256(PAPER_BYTES)}"
origin = "https://example.invalid/paper-fixture.jar"

[[artifacts]]
id = "mcremote-jar"
kind = "https-file"
version = "{version_tag}"
filename = "mcremote-{version_tag}.jar"
sha256 = "{_sha256(MCREMOTE_BYTES)}"
origin = "https://example.invalid/mcremote-{version_tag}.jar"

[[artifacts]]
id = "wirescope-zip"
kind = "https-file"
version = "{version_tag}"
filename = "{wirescope_zip_name}"
sha256 = "{_sha256(wirescope_zip)}"
origin = "https://example.invalid/{version_tag}/{wirescope_zip_name}"

[[artifacts]]
id = "wirescope-manifest"
kind = "https-file"
version = "{version_tag}"
filename = "{wirescope_manifest_name}"
sha256 = "{_sha256(wirescope_manifest)}"
origin = "https://example.invalid/{version_tag}/{wirescope_manifest_name}"
"""


REHEARSAL_ROUTES = """homepage = "mc-remote.example"
homepage_aliases = ["www.mc-remote.example"]
scratch = "scratch-beta.mc-remote.example"
bridge = "bridge-beta.mc-remote.example"
minecraft = "sb-beta.mc-remote.example"
wirescope = "wirescope-beta.mc-remote.example"
web_pairs = []
"""

REHEARSAL_TARGETS = """[[targets]]
id = "beta"
label = "公開ベータ（2400 rc1）"
host = "sb-beta.mc-remote.example"
port = 25575
external = false

[[targets]]
id = "unity-latest"
label = "Unity箱庭（2400 rc1）"
host = "unity-sb.mc-remote.example"
port = 25575
external = false

[[targets]]
id = "stable"
label = "標準（GAMEs）"
host = "sb.mc-remote.example"
port = 25575
external = true

[[targets]]
id = "unity-lts"
label = "Unity箱庭（LTS）"
host = "unity-sb-lts.mc-remote.example"
port = 25575
external = false

[[targets]]
id = "unused-ext"
label = "未使用外部"
host = "unused-ext.mc-remote.example"
port = 25579
external = true

[[notices]]
heading = "お知らせ"
body = "rehearsal notice"

[[selections]]
scratch = "scratch-beta.mc-remote.example"
target_ids = ["beta", "unity-latest"]
default_target = "beta"
"""

REHEARSAL_LTS_WEB = """preset = "fixture-web-paper@1"
scratch = "scratch.mc-remote.example"
bridge = "bridge.mc-remote.example"
wirescope = "wirescope.mc-remote.example"
target_ids = ["stable", "unity-lts"]
default_target = "stable"
"""

PRODUCTION_ROUTES = """homepage = "mc-remote.example"
homepage_aliases = ["www.mc-remote.example"]
scratch = "scratch.mc-remote.example"
bridge = "bridge.mc-remote.example"
minecraft = "sb.mc-remote.example"
wirescope = "wirescope.mc-remote.example"

[[web_pairs]]
scratch = "scratch-beta.mc-remote.example"
wirescope = "wirescope-beta.mc-remote.example"
"""

PRODUCTION_TARGETS = """[[targets]]
id = "stable"
label = "標準（Minecraft）"
host = "sb.mc-remote.example"
port = 25575
external = false

[[targets]]
id = "unity-latest"
label = "Unity箱庭"
host = "unity-sb.mc-remote.example"
port = 25575
external = false

[[targets]]
id = "lts"
label = "長期固定（GAMEs）"
host = "sb-lts.mc-remote.example"
port = 25575
external = true

[[targets]]
id = "unity-lts"
label = "Unity箱庭（LTS）"
host = "unity-sb-lts.mc-remote.example"
port = 25575
external = false

[[notices]]
heading = "お知らせ"
body = "production notice"

[[selections]]
scratch = "scratch.mc-remote.example"
target_ids = ["stable", "unity-latest"]
default_target = "stable"

[[selections]]
scratch = "scratch-beta.mc-remote.example"
target_ids = ["stable", "unity-latest"]
default_target = "stable"
"""

PRODUCTION_LTS_WEB = """preset = "fixture-web-paper@1"
scratch = "scratch-lts.mc-remote.example"
bridge = "bridge-lts.mc-remote.example"
wirescope = "wirescope-lts.mc-remote.example"
target_ids = ["lts", "unity-lts"]
default_target = "lts"
"""

UNITY_SANDBOXES_SOURCE = f"""[[sandboxes]]
id = "lts"
hostname = "unity-sb-lts.mc-remote.example"
tcp_port = 25585
image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox:v2321.2.0@{UNITY_LTS_DIGEST}"

[[sandboxes]]
id = "latest"
hostname = "unity-sb.mc-remote.example"
tcp_port = 25586
image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox:2400.0.0rc1@{UNITY_LATEST_DIGEST}"
"""


def _copy_bundled_profile(data_root: Path, revision: str) -> None:
    target = data_root / "profiles" / "vps-server" / revision / "profile.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        files("mc_remote_stack")
        .joinpath("data", "profiles", "vps-server", revision, "profile.toml")
        .read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def _setup_dual_line_data_root(
    tmp_path: Path,
    *,
    lts_policy_status: str = "active",
) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    data_root = _data_root(tmp_path, "vps-data")
    for revision in ("20", "21"):
        _copy_bundled_profile(data_root, revision)
    for commit in (SCRATCH_CONTRACT_V1_COMMIT, SCRATCH_CONTRACT_V2_COMMIT):
        src = files("mc_remote_stack").joinpath("data", "scratch-contracts", commit)
        shutil.copytree(str(src), data_root / "scratch-contracts" / commit)

    p1 = data_root / "preset_registry" / "fixture-web-paper" / "1" / "preset.toml"
    p1.parent.mkdir(parents=True)
    p1.write_text(
        _preset_toml(
            revision="1",
            scratch_contract=SCRATCH_CONTRACT_V1,
            scratch_digest=SCRATCH_V1_IMAGE_DIGEST,
            bridge_seed=23,
            wirescope_zip=WIRESCOPE_V1_ZIP_BYTES,
            wirescope_manifest=WIRESCOPE_V1_MANIFEST_BYTES,
            wirescope_zip_name="wirescope-app.zip",
            wirescope_manifest_name="wirescope-app.manifest.json",
            protocol="23.3.0",
            version_tag="2321.1.0",
        ),
        encoding="utf-8",
    )
    p2 = data_root / "preset_registry" / "fixture-web-paper" / "2" / "preset.toml"
    p2.parent.mkdir(parents=True)
    p2.write_text(
        _preset_toml(
            revision="2",
            scratch_contract=SCRATCH_CONTRACT_V2,
            scratch_digest=SCRATCH_V2_IMAGE_DIGEST,
            bridge_seed=123,
            wirescope_zip=WIRESCOPE_V2_ZIP_BYTES,
            wirescope_manifest=WIRESCOPE_V2_MANIFEST_BYTES,
            wirescope_zip_name="wirescope-app.zip",
            wirescope_manifest_name="wirescope-app.manifest.json",
            protocol="24.0.0",
            version_tag="2400.0.0",
        ),
        encoding="utf-8",
    )
    p3 = data_root / "preset_registry" / "fixture-web-paper" / "3" / "preset.toml"
    p3.parent.mkdir(parents=True)
    p3.write_text(
        _preset_toml(
            revision="3",
            scratch_contract=SCRATCH_CONTRACT_V2,
            scratch_digest=SCRATCH_V2_IMAGE_DIGEST,
            bridge_seed=123,
            wirescope_zip=WIRESCOPE_V2_ZIP_BYTES,
            wirescope_manifest=WIRESCOPE_V2_MANIFEST_BYTES,
            wirescope_zip_name="wirescope-app.zip",
            wirescope_manifest_name="wirescope-app.manifest.json",
            protocol="24.0.0",
            version_tag="2400.0.1",
        ),
        encoding="utf-8",
    )
    p1_policy: dict[str, Any] = {
        "ref": "fixture-web-paper@1",
        "status": lts_policy_status,
        "available_since": "2026-09-29",
    }
    if lts_policy_status == "eol":
        p1_policy.update(
            {
                "deprecated_since": "2026-11-01",
                "eol_since": "2026-11-15",
                "reason": "superseded",
            }
        )
    _write_policy(
        data_root,
        [
            p1_policy,
            {"ref": "fixture-web-paper@2", "status": "active", "available_since": "2026-11-22"},
            {"ref": "fixture-web-paper@3", "status": "active", "available_since": "2026-12-01"},
        ],
    )
    (data_root / "preset_catalog.toml").write_bytes(build_preset_catalog(data_root=data_root))

    artifact_store = tmp_path / "artifact-store"
    digest_store = artifact_store / "sha256"
    digest_store.mkdir(parents=True)
    for blob in (
        PAPER_BYTES,
        MCREMOTE_BYTES,
        PLUGIN_BYTES,
        WIRESCOPE_V1_ZIP_BYTES,
        WIRESCOPE_V1_MANIFEST_BYTES,
        WIRESCOPE_V2_ZIP_BYTES,
        WIRESCOPE_V2_MANIFEST_BYTES,
    ):
        (digest_store / _sha256(blob)).write_bytes(blob)

    homepage_source = tmp_path / "homepage-source"
    homepage_source.mkdir()
    (homepage_source / "index.html").write_text("homepage fixture\n", encoding="utf-8")
    homepage = import_homepage_tree(homepage_source, artifact_store)
    live_homepage = artifact_store.parent / "homepage"
    live_homepage.mkdir()
    (live_homepage / "index.html").write_text("homepage fixture\n", encoding="utf-8")
    (artifact_store.parent / "candidates").mkdir()
    backup = tmp_path / "backup"
    backup.mkdir()
    (artifact_store.parent / "homepage-meta.json").write_text(
        json.dumps(
            {
                "tree_sha256": homepage.tree_sha256,
                "file_count": homepage.file_count,
                "total_bytes": homepage.total_bytes,
            }
        ),
        encoding="utf-8",
    )
    return data_root, artifact_store, backup


def _build_vps21_fixture(
    tmp_path: Path,
    *,
    preset: str = "fixture-web-paper@2",
    routes_source: str = REHEARSAL_ROUTES,
    targets_source: str = REHEARSAL_TARGETS,
    lts_web_source: str | None = REHEARSAL_LTS_WEB,
    unity_source: str | None = UNITY_SANDBOXES_SOURCE,
    lts_policy_status: str = "active",
    resolve_now: bool = True,
    allow_eol: bool = False,
) -> VpsFixture:
    data_root, artifact_store, backup = _setup_dual_line_data_root(
        tmp_path, lts_policy_status=lts_policy_status
    )
    homepage_meta = json.loads(
        (artifact_store.parent / "homepage-meta.json").read_text(encoding="utf-8")
    )
    identity = "official-vps"
    project = init_toml_project(
        tmp_path / identity,
        deployment_name=identity,
        profile="vps-server@21",
        environment_identity=identity,
        channel="beta",
        exposure="public",
        purpose="integration",
        preset=preset,
        artifact_store=str(artifact_store),
        runtime_volumes={
            "minecraft-data": f"{identity}-minecraft-data",
            "caddy-data": f"{identity}-caddy-data",
            "caddy-config": f"{identity}-caddy-config",
        },
        world_identity=f"{identity}-world",
        bind_address="0.0.0.0",
        java_port=25565,
        mcremote_port=25575,
        minecraft_eula=True,
    ).root
    order_path = project / "mc-remote.toml"
    order_path.write_text(
        order_path.read_text(encoding="utf-8").replace(
            "[world]", '[minecraft]\nheap = "3G"\n\n[world]'
        ),
        encoding="utf-8",
    )
    inputs: list[tuple[str, str, str, str]] = [
        ("public-routes", "public-routes@3", "operator/public-routes/routes.toml", routes_source),
        (
            "minecraft-server",
            "minecraft-server@1",
            "operator/minecraft-server/server.toml",
            OPERATOR_INPUTS["minecraft-server"][2],
        ),
        (
            "connection-targets",
            "connection-targets@5",
            "operator/connection-targets/targets.toml",
            targets_source,
        ),
        (
            "minecraft-plugins",
            "minecraft-plugins@1",
            "operator/minecraft-plugins/plugins.toml",
            f'[[plugins]]\nfilename = "WorldEdit.jar"\nsha256 = "{_sha256(PLUGIN_BYTES)}"\n',
        ),
        (
            "homepage-static",
            "homepage-static@1",
            "operator/homepage-static/homepage.toml",
            f'tree_sha256 = "{homepage_meta["tree_sha256"]}"\n'
            f'file_count = {homepage_meta["file_count"]}\n'
            f'total_bytes = {homepage_meta["total_bytes"]}\n',
        ),
        (
            "minecraft-backup",
            "minecraft-backup@1",
            "operator/minecraft-backup/backup.toml",
            f'host_path = "{backup}"\n',
        ),
    ]
    if unity_source is not None:
        inputs.append(
            (
                "unity-sandboxes",
                "unity-sandboxes@2",
                "operator/unity-sandboxes/sandboxes.toml",
                unity_source,
            )
        )
    if lts_web_source is not None:
        inputs.append(
            ("lts-web", "lts-web@1", "operator/lts-web/lts.toml", lts_web_source)
        )

    with order_path.open("a", encoding="utf-8") as stream:
        for role, adapter, relative, content in inputs:
            target = project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            stream.write(
                f'\n[[operator_inputs]]\nrole = "{role}"\nadapter = "{adapter}"\npath = "{relative}"\n'
            )

    _acknowledge(project, "unverified")
    if resolve_now:
        resolve_project(
            project,
            data_root=data_root,
            allow_unverified=True,
            allow_eol=allow_eol,
            resolved_at=FIRST_RESOLVED_AT,
        )
    return VpsFixture(project, data_root, artifact_store, backup)


# ---------------------------------------------------------------------------
# 1. Input parsers: connection-targets@5 & lts-web@1
# ---------------------------------------------------------------------------


def test_connection_targets_v5_accepts_host_sandbox_ports_and_external(tmp_path: Path) -> None:
    source = """[[targets]]
id = "local-v4"
label = "互換"
sandbox = "sb-beta.mc-remote.example"

[[targets]]
id = "games-main"
label = "外部"
host = "games.mc-remote.example"
port = 25575
external = true

[[targets]]
id = "games-alt"
label = "外部別ポート"
host = "games.mc-remote.example"
port = 25585
external = true

[[notices]]
heading = "お知らせ"
body = "v5 test"

[[selections]]
scratch = "scratch-beta.mc-remote.example"
target_ids = ["local-v4", "games-main", "games-alt"]
default_target = "local-v4"
"""
    parsed = _parse_connection_targets_v5(tmp_path / "targets.toml", source.encode("utf-8"))
    assert parsed["targets"] == [
        {
            "id": "local-v4",
            "label": "互換",
            "host": "sb-beta.mc-remote.example",
            "port": 25575,
            "external": False,
        },
        {
            "id": "games-main",
            "label": "外部",
            "host": "games.mc-remote.example",
            "port": 25575,
            "external": True,
        },
        {
            "id": "games-alt",
            "label": "外部別ポート",
            "host": "games.mc-remote.example",
            "port": 25585,
            "external": True,
        },
    ]


@pytest.mark.parametrize(
    "target_block",
    [
        'id = "bad"\nlabel = "Both"\nhost = "a.example"\nsandbox = "a.example"\n',
        'id = "bad"\nlabel = "Neither"\n',
        'id = "bad"\nlabel = "Zero"\nhost = "a.example"\nport = 0\n',
        'id = "bad"\nlabel = "High"\nhost = "a.example"\nport = 65536\n',
        'id = "bad"\nlabel = "Bool"\nhost = "a.example"\nport = true\n',
        'id = "bad"\nlabel = "Str"\nhost = "a.example"\nport = "25575"\n',
        'id = "bad"\nlabel = "ExtStr"\nhost = "a.example"\nexternal = "true"\n',
        (
            'id = "dup"\nlabel = "One"\nhost = "a.example"\n\n'
            '[[targets]]\nid = "dup"\nlabel = "Two"\nhost = "b.example"\n'
        ),
        (
            'id = "one"\nlabel = "One"\nhost = "a.example"\nport = 25575\n\n'
            '[[targets]]\nid = "two"\nlabel = "Two"\nhost = "a.example"\nport = 25575\n'
        ),
    ],
    ids=[
        "both_host_and_sandbox",
        "neither_host_nor_sandbox",
        "port_zero",
        "port_overflow",
        "port_bool",
        "port_string",
        "external_non_bool",
        "duplicate_id",
        "duplicate_host_port",
    ],
)
def test_connection_targets_v5_rejects_invalid_entries(tmp_path: Path, target_block: str) -> None:
    source = f"""[[targets]]
{target_block}
[[notices]]
heading = "お知らせ"
body = "v5 invalid"

[[selections]]
scratch = "scratch.mc-remote.example"
target_ids = ["{target_block.splitlines()[0].split('"')[1]}"]
default_target = "{target_block.splitlines()[0].split('"')[1]}"
"""
    with pytest.raises(OperatorInputError) as exc_info:
        _parse_connection_targets_v5(tmp_path / "targets.toml", source.encode("utf-8"))
    assert exc_info.value.reason == "operator_input_parse_failed"


def test_lts_web_v1_parser_accepts_valid_and_rejects_invalid(tmp_path: Path) -> None:
    parsed = _parse_lts_web(tmp_path / "lts.toml", REHEARSAL_LTS_WEB.encode("utf-8"))
    assert parsed == {
        "preset": "fixture-web-paper@1",
        "scratch": "scratch.mc-remote.example",
        "bridge": "bridge.mc-remote.example",
        "wirescope": "wirescope.mc-remote.example",
        "target_ids": ["stable", "unity-lts"],
        "default_target": "stable",
    }

    with pytest.raises(OperatorInputError) as secret_exc:
        _parse_lts_web(
            tmp_path / "lts.toml",
            REHEARSAL_LTS_WEB.replace("fixture-web-paper@1", "secret://preset").encode("utf-8"),
        )
    assert secret_exc.value.reason == "operator_input_secret_forbidden"

    with pytest.raises(OperatorInputError) as env_exc:
        _parse_lts_web(
            tmp_path / "lts.toml",
            REHEARSAL_LTS_WEB.replace("fixture-web-paper@1", "${PRESET}").encode("utf-8"),
        )
    assert env_exc.value.reason == "operator_input_secret_forbidden"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda s: s.replace('default_target = "stable"\n', ""),
        lambda s: s + 'extra = "no"\n',
        lambda s: s.replace("fixture-web-paper@1", "fixture-web-paper@latest"),
        lambda s: s.replace("bridge.mc-remote.example", "scratch.mc-remote.example"),
        lambda s: s.replace('["stable", "unity-lts"]', '["stable", "stable"]'),
        lambda s: s.replace('["stable", "unity-lts"]', "[]"),
        lambda s: s.replace('["stable", "unity-lts"]', '["Bad_ID"]'),
        lambda s: s.replace('default_target = "stable"', 'default_target = "missing"'),
    ],
    ids=[
        "missing_key",
        "extra_key",
        "non_exact_preset",
        "colliding_hostnames",
        "duplicate_target_id",
        "empty_target_ids",
        "invalid_target_id",
        "default_not_in_target_ids",
    ],
)
def test_lts_web_v1_parser_rejects_malformed_documents(tmp_path: Path, mutator) -> None:
    with pytest.raises(OperatorInputError) as exc_info:
        _parse_lts_web(tmp_path / "lts.toml", mutator(REHEARSAL_LTS_WEB).encode("utf-8"))
    assert exc_info.value.reason == "operator_input_parse_failed"


# ---------------------------------------------------------------------------
# 2. Resolver & lock validation (vps-server@21)
# ---------------------------------------------------------------------------


def test_vps_server_21_resolves_without_and_with_lts_web(tmp_path: Path) -> None:
    single = _build_vps21_fixture(
        tmp_path / "single",
        lts_web_source=None,
        unity_source=None,
    )
    single_lock = load_lock(single.project, data_root=single.data_root)
    assert "lts_web" not in single_lock
    assert "lts_web" not in single_lock["render_plan"]
    assert [s["id"] for s in single_lock["render_plan"]["services"]] == [
        "caddy",
        "scratch",
        "bridge",
        "minecraft",
    ]

    dual = _build_vps21_fixture(tmp_path / "dual", unity_source=None)
    dual_lock = load_lock(dual.project, data_root=dual.data_root)
    assert dual_lock["lts_web"]["preset"]["ref"] == "fixture-web-paper@1"
    assert dual_lock["render_plan"]["lts_web"] == dual_lock["lts_web"]
    assert [s["id"] for s in dual_lock["render_plan"]["services"]] == [
        "caddy",
        "scratch",
        "bridge",
        "minecraft",
        "scratch-lts",
        "bridge-lts",
    ]
    assert dual_lock["lock_identity"] != single_lock["lock_identity"]


def test_vps_server_21_resolver_rejects_collisions_and_bad_lts_presets(tmp_path: Path) -> None:
    # 1. lts-web collides with public-routes
    f1 = _build_vps21_fixture(
        tmp_path / "coll-route",
        lts_web_source=REHEARSAL_LTS_WEB.replace(
            "scratch.mc-remote.example", "scratch-beta.mc-remote.example"
        ),
        resolve_now=False,
    )
    with pytest.raises(ResolutionError) as e1:
        resolve_project(f1.project, data_root=f1.data_root, allow_unverified=True, resolved_at=FIRST_RESOLVED_AT)
    assert e1.value.reason == "operator_input_parse_failed"

    # 2. lts-web collides with unity-sandboxes
    f2 = _build_vps21_fixture(
        tmp_path / "coll-unity",
        lts_web_source=REHEARSAL_LTS_WEB.replace(
            "wirescope.mc-remote.example", "unity-sb.mc-remote.example"
        ),
        resolve_now=False,
    )
    with pytest.raises(ResolutionError) as e2:
        resolve_project(f2.project, data_root=f2.data_root, allow_unverified=True, resolved_at=FIRST_RESOLVED_AT)
    assert e2.value.reason == "operator_input_parse_failed"

    # 3. external = true target collides with local route/lts-web host
    f3 = _build_vps21_fixture(
        tmp_path / "coll-ext",
        targets_source=REHEARSAL_TARGETS.replace(
            'host = "sb.mc-remote.example"', 'host = "scratch.mc-remote.example"'
        ),
        resolve_now=False,
    )
    with pytest.raises(ResolutionError) as e3:
        resolve_project(f3.project, data_root=f3.data_root, allow_unverified=True, resolved_at=FIRST_RESOLVED_AT)
    assert e3.value.reason == "operator_input_parse_failed"

    # 4. unknown target_id in lts-web
    f4 = _build_vps21_fixture(
        tmp_path / "bad-tid",
        lts_web_source=REHEARSAL_LTS_WEB.replace('"unity-lts"', '"no-such-id"'),
        resolve_now=False,
    )
    with pytest.raises(ResolutionError) as e4:
        resolve_project(f4.project, data_root=f4.data_root, allow_unverified=True, resolved_at=FIRST_RESOLVED_AT)
    assert e4.value.reason == "operator_input_parse_failed"

    # 5. EOL lts-web preset requires order + CLI --allow-eol
    f5 = _build_vps21_fixture(
        tmp_path / "eol-lts",
        lts_policy_status="eol",
        resolve_now=False,
    )
    with pytest.raises(ResolutionError) as e5:
        resolve_project(
            f5.project,
            data_root=f5.data_root,
            allow_unverified=True,
            allow_eol=True,
            resolved_at=FIRST_RESOLVED_AT,
        )
    assert e5.value.reason == "preset_eol"
    _acknowledge(f5.project, "eol")
    res_eol = resolve_project(
        f5.project,
        data_root=f5.data_root,
        allow_unverified=True,
        allow_eol=True,
        resolved_at=FIRST_RESOLVED_AT,
    )
    assert res_eol.status == "created"

    # 6. lts-web preset missing required web component
    f6 = _build_vps21_fixture(tmp_path / "missing-comp", resolve_now=False)
    preset1_path = f6.data_root / "preset_registry" / "fixture-web-paper" / "1" / "preset.toml"
    preset1_path.write_text(
        preset1_path.read_text(encoding="utf-8").replace(
            'role = "websocket-bridge"', 'role = "unused-role"'
        ),
        encoding="utf-8",
    )
    (f6.data_root / "preset_catalog.toml").write_bytes(
        build_preset_catalog(data_root=f6.data_root)
    )
    with pytest.raises(ResolutionError) as e6:
        resolve_project(f6.project, data_root=f6.data_root, allow_unverified=True, resolved_at=FIRST_RESOLVED_AT)
    assert e6.value.reason == "preset_incompatible"


# ---------------------------------------------------------------------------
# 3. Artifact fetch across primary and lts_web (with deduplication)
# ---------------------------------------------------------------------------


def test_fetch_locked_artifacts_fetches_and_deduplicates_lts_web_artifacts(
    tmp_path: Path,
) -> None:
    fixture = _build_vps21_fixture(tmp_path)
    digest_dir = fixture.artifact_store / "sha256"
    for entry in digest_dir.iterdir():
        if entry.name != _sha256(PLUGIN_BYTES):
            entry.unlink()

    content_by_url = {
        "https://example.invalid/paper-fixture.jar": PAPER_BYTES,
        "https://example.invalid/mcremote-2400.0.0.jar": MCREMOTE_BYTES,
        "https://example.invalid/2400.0.0/wirescope-app.zip": WIRESCOPE_V2_ZIP_BYTES,
        "https://example.invalid/2400.0.0/wirescope-app.manifest.json": WIRESCOPE_V2_MANIFEST_BYTES,
        "https://example.invalid/2321.1.0/wirescope-app.zip": WIRESCOPE_V1_ZIP_BYTES,
        "https://example.invalid/2321.1.0/wirescope-app.manifest.json": WIRESCOPE_V1_MANIFEST_BYTES,
    }
    fetched_urls: list[str] = []

    class _FakeResp(io.BytesIO):
        def __init__(self, content: bytes, url: str) -> None:
            super().__init__(content)
            self._url = url
            self.headers = {"Content-Length": str(len(content))}

        def geturl(self) -> str:
            return self._url

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

    def fake_urlopen(request: Request, *, timeout: int) -> _FakeResp:
        assert timeout == 60
        fetched_urls.append(request.full_url)
        return _FakeResp(content_by_url[request.full_url], request.full_url)

    results = fetch_locked_artifacts(
        fixture.project, data_root=fixture.data_root, open_url=fake_urlopen
    )

    assert sorted(fetched_urls) == sorted(content_by_url)
    assert len(results) == 6

    # Deduplication: when LTS shares the exact same wirescope sha256+filename, fetch once.
    fixture_dup = _build_vps21_fixture(
        tmp_path / "dedup",
        lts_web_source=REHEARSAL_LTS_WEB.replace("fixture-web-paper@1", "fixture-web-paper@2"),
    )
    for entry in (fixture_dup.artifact_store / "sha256").iterdir():
        if entry.name != _sha256(PLUGIN_BYTES):
            entry.unlink()
    fetched_urls.clear()
    dedup_results = fetch_locked_artifacts(
        fixture_dup.project, data_root=fixture_dup.data_root, open_url=fake_urlopen
    )
    assert len(fetched_urls) == 4
    assert len(dedup_results) == 4


# ---------------------------------------------------------------------------
# 4. Render (compose@23): 11/25 rehearsal, 12/1 production, ports & regression
# ---------------------------------------------------------------------------


def test_compose_23_rehearsal_layout_renders_v2_primary_v1_lts_and_external_egress(
    tmp_path: Path,
) -> None:
    fixture = _build_vps21_fixture(tmp_path)
    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)

    primary_runtime = json.loads((fixture.output / "runtime/scratch.json").read_text(encoding="utf-8"))
    v2_schema = load_runtime_config_schema(
        load_lock(fixture.project, data_root=fixture.data_root)["scratch_runtime_contract"],
        data_root=fixture.data_root,
    )
    validate_scratch_runtime_config(primary_runtime, expected=primary_runtime, schema=v2_schema)
    assert primary_runtime["schema_version"] == 2
    assert primary_runtime["default_target_id"] == "beta"
    assert primary_runtime["bridge_url"] == "wss://bridge-beta.mc-remote.example"
    assert primary_runtime["wirescope_url"] == "https://wirescope-beta.mc-remote.example/"
    assert primary_runtime["connection_targets"] == [
        {
            "id": "beta",
            "label": "公開ベータ（2400 rc1）",
            "host": "sb-beta.mc-remote.example",
            "port": 25575,
        },
        {
            "id": "unity-latest",
            "label": "Unity箱庭（2400 rc1）",
            "host": "unity-sb.mc-remote.example",
            "port": 25575,
        },
    ]

    bridge_targets = json.loads(
        (fixture.output / "runtime/bridge-targets.json").read_text(encoding="utf-8")
    )
    assert bridge_targets == {
        "schema_version": 1,
        "default_target_id": "beta",
        "targets": [
            {
                "id": "beta",
                "label": "公開ベータ（2400 rc1）",
                "host": "sb-beta.mc-remote.example",
                "port": 25575,
            },
            {
                "id": "unity-latest",
                "label": "Unity箱庭（2400 rc1）",
                "host": "unity-sb.mc-remote.example",
                "port": 25575,
            },
        ],
    }

    lts_runtime = json.loads((fixture.output / "runtime/scratch-lts.json").read_text(encoding="utf-8"))
    assert lts_runtime["schema_version"] == 1
    assert lts_runtime["default_sandbox"] == "sb.mc-remote.example"
    assert lts_runtime["bridge_url"] == "wss://bridge.mc-remote.example"
    assert lts_runtime["wirescope_url"] == "https://wirescope.mc-remote.example/"
    assert lts_runtime["connection_targets"] == [
        {"id": "stable", "label": "標準（GAMEs）", "sandbox": "sb.mc-remote.example"},
        {"id": "unity-lts", "label": "Unity箱庭（LTS）", "sandbox": "unity-sb-lts.mc-remote.example"},
    ]

    compose = yaml.safe_load((fixture.output / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert list(services) == [
        "caddy",
        "scratch",
        "bridge",
        "minecraft",
        "scratch-lts",
        "bridge-lts",
        "unity-sandbox-latest",
        "unity-sandbox-lts",
    ]
    bridge = services["bridge"]
    assert bridge["environment"]["BRIDGE_TARGETS_FILE"] == "/etc/mc-remote/bridge-targets.json"
    assert bridge["environment"]["BRIDGE_ORIGIN_ALLOWLIST"] == "https://scratch-beta.mc-remote.example"
    assert not any(k.startswith("BRIDGE_SANDBOX_") for k in bridge["environment"])
    assert bridge["volumes"] == [
        {
            "type": "bind",
            "source": "./runtime/bridge-targets.json",
            "target": "/etc/mc-remote/bridge-targets.json",
            "read_only": True,
        }
    ]
    assert bridge["networks"] == ["app"]

    bridge_lts = services["bridge-lts"]
    assert bridge_lts["environment"]["BRIDGE_SANDBOX_ALLOWLIST"] == (
        "sb.mc-remote.example,unity-sb-lts.mc-remote.example"
    )
    assert bridge_lts["environment"]["BRIDGE_DEFAULT_SANDBOX"] == "sb.mc-remote.example"
    assert bridge_lts["environment"]["BRIDGE_SANDBOX_PORT"] == "25575"
    assert bridge_lts["environment"]["BRIDGE_ORIGIN_ALLOWLIST"] == "https://scratch.mc-remote.example"
    assert bridge_lts["networks"] == {"app": {}, "egress": {"gw_priority": 1}}

    assert services["minecraft"]["networks"]["app"]["aliases"] == ["sb-beta.mc-remote.example"]
    assert {
        "type": "bind",
        "source": "./wirescope-lts",
        "target": "/srv/wirescope-lts",
        "read_only": True,
    } in services["caddy"]["volumes"]

    caddyfile = (fixture.output / "Caddyfile").read_text(encoding="utf-8")
    assert (
        "scratch.mc-remote.example {\n"
        '    header Referrer-Policy "strict-origin-when-cross-origin"\n'
        "    reverse_proxy scratch-lts:8080\n}"
    ) in caddyfile
    assert "bridge.mc-remote.example {\n    reverse_proxy bridge-lts:8080\n}" in caddyfile
    assert "wirescope.mc-remote.example {\n    root * /srv/wirescope-lts" in caddyfile

    for rel in ("runtime/scratch-lts.json", "runtime/bridge-targets.json", "wirescope-lts/index.html"):
        assert (fixture.output / rel).stat().st_mode & 0o777 == 0o644


def test_compose_23_production_layout_with_extra_web_pair_renders_both_v2_and_lts(
    tmp_path: Path,
) -> None:
    fixture = _build_vps21_fixture(
        tmp_path,
        routes_source=PRODUCTION_ROUTES,
        targets_source=PRODUCTION_TARGETS,
        lts_web_source=PRODUCTION_LTS_WEB,
    )
    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)

    main_rt = json.loads((fixture.output / "runtime/scratch.json").read_text(encoding="utf-8"))
    beta_rt = json.loads(
        (fixture.output / "runtime/web-pairs/scratch-beta.mc-remote.example.json").read_text(
            encoding="utf-8"
        )
    )
    lts_rt = json.loads((fixture.output / "runtime/scratch-lts.json").read_text(encoding="utf-8"))
    assert main_rt["schema_version"] == 2
    assert beta_rt["schema_version"] == 2
    assert main_rt["wirescope_url"] == "https://wirescope.mc-remote.example/"
    assert beta_rt["wirescope_url"] == "https://wirescope-beta.mc-remote.example/"
    assert lts_rt["schema_version"] == 1
    assert lts_rt["default_sandbox"] == "sb-lts.mc-remote.example"

    compose = yaml.safe_load((fixture.output / "compose.yaml").read_text(encoding="utf-8"))
    assert compose["services"]["bridge"]["environment"]["BRIDGE_ORIGIN_ALLOWLIST"] == (
        "https://scratch.mc-remote.example,https://scratch-beta.mc-remote.example"
    )
    assert compose["services"]["minecraft"]["networks"]["app"]["aliases"] == ["sb.mc-remote.example"]


def test_compose_23_external_port_and_local_port_constraints(tmp_path: Path) -> None:
    # External non-25575 port accepted on v2 primary line and attaches egress to bridge.
    ext_targets = REHEARSAL_TARGETS.replace(
        'target_ids = ["beta", "unity-latest"]',
        'target_ids = ["beta", "unused-ext"]',
    )
    f_v2 = _build_vps21_fixture(
        tmp_path / "v2-ext-port",
        targets_source=ext_targets,
        lts_web_source=None,
        unity_source=None,
    )
    render_toml_project(f_v2.project, f_v2.output, data_root=f_v2.data_root)
    compose = yaml.safe_load((f_v2.output / "compose.yaml").read_text(encoding="utf-8"))
    assert compose["services"]["bridge"]["networks"] == {"app": {}, "egress": {"gw_priority": 1}}
    bridge_targets = json.loads(
        (f_v2.output / "runtime/bridge-targets.json").read_text(encoding="utf-8")
    )
    assert [t["port"] for t in bridge_targets["targets"]] == [25575, 25579]

    # External non-25575 port rejected on v1 LTS line.
    f_v1_port = _build_vps21_fixture(
        tmp_path / "v1-ext-port",
        lts_web_source=REHEARSAL_LTS_WEB.replace('"unity-lts"', '"unused-ext"'),
        unity_source=None,
    )
    with pytest.raises(RenderContractError) as err_v1:
        render_toml_project(f_v1_port.project, f_v1_port.output, data_root=f_v1_port.data_root)
    assert err_v1.value.reason == "scratch_runtime_config_invalid"

    # Local container target (external = false) with port != 25575 rejected.
    f_local_port = _build_vps21_fixture(
        tmp_path / "local-port",
        targets_source=REHEARSAL_TARGETS.replace("port = 25575\nexternal = false", "port = 25599\nexternal = false", 1),
        lts_web_source=None,
        unity_source=None,
    )
    with pytest.raises(RenderContractError) as err_local:
        render_toml_project(f_local_port.project, f_local_port.output, data_root=f_local_port.data_root)
    assert err_local.value.reason == "scratch_runtime_config_invalid"


def test_compose_22_regression_remains_legacy_v1(tmp_path: Path) -> None:
    data_root, artifact_store, backup = _setup_dual_line_data_root(tmp_path)
    homepage_meta = json.loads(
        (artifact_store.parent / "homepage-meta.json").read_text(encoding="utf-8")
    )
    project = init_toml_project(
        tmp_path / "vps20",
        deployment_name="vps20",
        profile="vps-server@20",
        environment_identity="vps20",
        channel="beta",
        exposure="public",
        purpose="integration",
        preset="fixture-web-paper@1",
        artifact_store=str(artifact_store),
        runtime_volumes={
            "minecraft-data": "vps20-mc",
            "caddy-data": "vps20-cd",
            "caddy-config": "vps20-cc",
        },
        world_identity="vps20-world",
        bind_address="0.0.0.0",
        java_port=25565,
        mcremote_port=25575,
        minecraft_eula=True,
    ).root
    order_path = project / "mc-remote.toml"
    order_path.write_text(
        order_path.read_text(encoding="utf-8").replace(
            "[world]", '[minecraft]\nheap = "3G"\n\n[world]'
        ),
        encoding="utf-8",
    )
    v4_targets = """[[targets]]
id = "beta"
label = "公開ベータ"
sandbox = "sb-beta.mc-remote.example"

[[notices]]
heading = "お知らせ"
body = "v20"

[[selections]]
scratch = "scratch-beta.mc-remote.example"
target_ids = ["beta"]
default_target = "beta"
"""
    for role, adapter, rel, content in (
        ("public-routes", "public-routes@3", "operator/public-routes/routes.toml", PUBLIC_ROUTES_V3[2]),
        (
            "minecraft-server",
            "minecraft-server@1",
            "operator/minecraft-server/server.toml",
            OPERATOR_INPUTS["minecraft-server"][2],
        ),
        ("connection-targets", "connection-targets@4", "operator/connection-targets/targets.toml", v4_targets),
        (
            "minecraft-plugins",
            "minecraft-plugins@1",
            "operator/minecraft-plugins/plugins.toml",
            f'[[plugins]]\nfilename = "WorldEdit.jar"\nsha256 = "{_sha256(PLUGIN_BYTES)}"\n',
        ),
        (
            "homepage-static",
            "homepage-static@1",
            "operator/homepage-static/homepage.toml",
            f'tree_sha256 = "{homepage_meta["tree_sha256"]}"\n'
            f'file_count = {homepage_meta["file_count"]}\n'
            f'total_bytes = {homepage_meta["total_bytes"]}\n',
        ),
        (
            "minecraft-backup",
            "minecraft-backup@1",
            "operator/minecraft-backup/backup.toml",
            f'host_path = "{backup}"\n',
        ),
    ):
        p = project / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        with order_path.open("a", encoding="utf-8") as stream:
            stream.write(f'\n[[operator_inputs]]\nrole = "{role}"\nadapter = "{adapter}"\npath = "{rel}"\n')

    # Adjust selection to match single pair when using PRODUCTION_ROUTES or add second selection
    (project / "operator/connection-targets/targets.toml").write_text(
        v4_targets
        + '\n[[selections]]\nscratch = "scratch.mc-remote.example"\ntarget_ids = ["beta"]\ndefault_target = "beta"\n',
        encoding="utf-8",
    )
    _acknowledge(project, "unverified")
    resolve_project(project, data_root=data_root, allow_unverified=True, resolved_at=FIRST_RESOLVED_AT)
    output = project / "generated"
    render_toml_project(project, output, data_root=data_root)

    compose = yaml.safe_load((output / "compose.yaml").read_text(encoding="utf-8"))
    assert "BRIDGE_TARGETS_FILE" not in compose["services"]["bridge"]["environment"]
    assert "BRIDGE_SANDBOX_ALLOWLIST" in compose["services"]["bridge"]["environment"]
    assert json.loads((output / "runtime/scratch.json").read_text(encoding="utf-8"))["schema_version"] == 1


# ---------------------------------------------------------------------------
# 5. Doctor, external TCP probe, v2 runtime schema check, and CLI
# ---------------------------------------------------------------------------


def test_doctor_validates_dual_line_extra_pairs_and_external_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = _build_vps21_fixture(
        tmp_path,
        routes_source=PRODUCTION_ROUTES,
        targets_source=PRODUCTION_TARGETS,
        lts_web_source=PRODUCTION_LTS_WEB,
    )
    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)
    lock = load_lock(fixture.project, data_root=fixture.data_root)

    containers = rendered_containers(fixture.output)
    assert len(containers) == 8
    runner = FakeDocker(_vps_doctor_responses(fixture.output, lock, containers=containers))
    observed_runtimes = {
        "https://scratch.mc-remote.example/mc-remote-runtime-config.json": json.loads(
            (fixture.output / "runtime/scratch.json").read_text(encoding="utf-8")
        ),
        "https://scratch-beta.mc-remote.example/mc-remote-runtime-config.json": json.loads(
            (fixture.output / "runtime/web-pairs/scratch-beta.mc-remote.example.json").read_text(
                encoding="utf-8"
            )
        ),
        "https://scratch-lts.mc-remote.example/mc-remote-runtime-config.json": json.loads(
            (fixture.output / "runtime/scratch-lts.json").read_text(encoding="utf-8")
        ),
    }
    external_calls: list[tuple[str, int, int]] = []

    result = doctor_toml_project(
        fixture.project,
        fixture.output,
        docker_context="default",
        data_root=fixture.data_root,
        runner=runner,
        hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
        homepage_probe=lambda *_a, **_k: None,
        scratch_runtime_probe=lambda url, _timeout: observed_runtimes[url],
        wirescope_probe=lambda *_a, **_k: None,
        unity_health_probe=lambda *_a, **_k: None,
        unity_tcp_probe=lambda *_a, **_k: None,
        external_target_probe=lambda host, port, timeout: external_calls.append((host, port, timeout)),
    )

    assert result.web_pair_count == 3
    assert result.lts_web_status == "current"
    assert result.external_target_status == "healthy"
    assert result.external_target_count == 1
    assert external_calls == [("sb-lts.mc-remote.example", 25575, 5)]

    monkeypatch.setattr("mc_remote_stack.cli.doctor_toml_project", lambda *_a, **_k: result)
    assert main(["doctor", "--project", str(fixture.project)]) == 0
    doctor_out = capsys.readouterr().out
    assert "OK doctor scratch-runtime=current web-pairs=3" in doctor_out
    assert "OK doctor wirescope=current handoff=cross-origin web-pairs=3" in doctor_out
    assert "OK doctor lts-web=current" in doctor_out
    assert "OK doctor external-targets=healthy count=1" in doctor_out

    monkeypatch.setattr("mc_remote_stack.cli._preset_data_root", lambda: fixture.data_root)
    assert main(["plan", "--project", str(fixture.project)]) == 0
    plan_out = capsys.readouterr().out
    assert "PLAN lts-web-preset=fixture-web-paper@1" in plan_out
    assert "PLAN lts-web-artifact=wirescope-zip kind=https-file" in plan_out

    # Doctor rejects missing /srv/wirescope-lts mount on Caddy.
    bad_containers = rendered_containers(fixture.output)
    for container in bad_containers:
        if container["Config"]["Labels"]["com.docker.compose.service"] == "caddy":
            container["Mounts"] = [
                m for m in container["Mounts"] if m.get("Destination") != "/srv/wirescope-lts"
            ]
    with pytest.raises(DoctorContractError) as exc_info:
        doctor_toml_project(
            fixture.project,
            fixture.output,
            docker_context="default",
            data_root=fixture.data_root,
            runner=FakeDocker(_vps_doctor_responses(fixture.output, lock, containers=bad_containers)),
            hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
            homepage_probe=lambda *_a, **_k: None,
            scratch_runtime_probe=lambda url, _timeout: observed_runtimes[url],
            wirescope_probe=lambda *_a, **_k: None,
            unity_health_probe=lambda *_a, **_k: None,
            unity_tcp_probe=lambda *_a, **_k: None,
            external_target_probe=lambda *_a, **_k: None,
        )
    assert exc_info.value.reason == "doctor_composition_mount_mismatch"


class _FakeSocket:
    def __init__(self, payloads: list[dict[str, Any]], sent: list[dict[str, Any]]) -> None:
        self._payload = (json.dumps(payloads.pop(0)) + "\n").encode("utf-8")
        self._sent = sent

    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def sendall(self, value: bytes) -> None:
        self._sent.append(json.loads(value))

    def recv(self, _size: int) -> bytes:
        chunk, self._payload = self._payload, b""
        return chunk


def test_probe_external_mcremote_auth_handles_mismatch_retry_and_failures() -> None:
    sent: list[dict[str, Any]] = []
    responses = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32000,
                "message": "protocol mismatch",
                "data": {"reason": "protocol_mismatch", "server": "24.0.0"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32000,
                "message": "auth required",
                "data": {"reason": "auth_required"},
            },
        },
    ]
    probe_external_mcremote_auth(
        "sb-lts.mc-remote.example",
        25575,
        5,
        connector=lambda _addr, _timeout: _FakeSocket(responses, sent),
    )
    assert [req["params"]["protocol"] for req in sent] == ["23.3.0", "24.0.0"]

    with pytest.raises(DoctorContractError) as unauth_exc:
        probe_external_mcremote_auth(
            "sb-lts.mc-remote.example",
            25575,
            5,
            connector=lambda _addr, _timeout: _FakeSocket(
                [{"jsonrpc": "2.0", "id": 1, "result": {"protocol": "23.3.0"}}], []
            ),
        )
    assert unauth_exc.value.reason == "doctor_auth_not_enforced"

    def fail_connect(_addr: tuple[str, int], _timeout: int) -> _FakeSocket:
        raise OSError("refused")

    with pytest.raises(DoctorContractError) as down_exc:
        probe_external_mcremote_auth("sb-lts.mc-remote.example", 25575, 5, connector=fail_connect)
    assert down_exc.value.reason == "doctor_external_target_unavailable"

    with pytest.raises(DoctorContractError) as bad_err:
        probe_external_mcremote_auth(
            "sb-lts.mc-remote.example",
            25575,
            5,
            connector=lambda _addr, _timeout: _FakeSocket(
                [{"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "data": {"reason": "other"}}}],
                [],
            ),
        )
    assert bad_err.value.reason == "doctor_external_target_unhealthy"


def test_validate_scratch_runtime_config_v2_rejects_invalid_shapes(tmp_path: Path) -> None:
    data_root, _store, _backup = _setup_dual_line_data_root(tmp_path)
    schema = load_runtime_config_schema(
        {
            "source_commit": SCRATCH_CONTRACT_V2_COMMIT,
            "source_directory": "packages/scratch-gui/contracts/runtime-config",
            "directory_tree_sha": "11b998a78c185a290c6c0d730d008af34c838036",
            "schema_sha256": "1720bc180b31f0275e4b2d264f149dcbe1c74b4dc151e63aafbb851303c3c1e3",
            "container_mount_path": "/usr/share/nginx/html/mc-remote-runtime-config.json",
            "image_digest": SCRATCH_V2_IMAGE_DIGEST,
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
                "fixtures/invalid/schema-version.json": (
                    "0b2b8fd116adf66c362e8950519da05324816c3b30c2c76842e398c8ede82e17"
                ),
                "fixtures/invalid/unknown-field.json": (
                    "35f1f21562e237cce722f5a1f93723f00d927d07769f8b12174d3ff9f73d5e3d"
                ),
            },
        },
        data_root=data_root,
    )
    base_v2 = {
        "schema_version": 2,
        "connection_enabled": True,
        "bridge_url": "wss://bridge-beta.mc-remote.example",
        "default_target_id": "beta",
        "connection_targets": [
            {"id": "beta", "label": "Beta", "host": "sb-beta.mc-remote.example", "port": 25575}
        ],
        "wirescope_url": "https://wirescope-beta.mc-remote.example/",
        "notices": [{"heading": "お知らせ", "body": "ok"}],
    }

    bad_default = {**base_v2, "default_target_id": "missing"}
    bad_dup = {
        **base_v2,
        "connection_targets": [
            {"id": "one", "label": "One", "host": "sb.example", "port": 25575},
            {"id": "two", "label": "Two", "host": "sb.example", "port": 25575},
        ],
        "default_target_id": "one",
    }
    bad_port = {
        **base_v2,
        "connection_targets": [
            {"id": "beta", "label": "Beta", "host": "sb-beta.mc-remote.example", "port": 70000}
        ],
    }
    bad_mixed = {**base_v2, "default_sandbox": "sb-beta.mc-remote.example"}

    for candidate in (bad_default, bad_dup, bad_port, bad_mixed):
        with pytest.raises(DoctorContractError) as exc_info:
            validate_scratch_runtime_config(candidate, expected=candidate, schema=schema)
        assert exc_info.value.reason == "doctor_scratch_runtime_invalid"


# ---------------------------------------------------------------------------
# 6. In-place deployment update (vps-server@20 -> @21, and @21 -> @21)
# ---------------------------------------------------------------------------


def _dynamic_update_runner(
    output: Path, source_lock: dict[str, Any], source_containers: list[dict[str, Any]]
):
    state: dict[str, Any] = {"containers": source_containers}

    def runner(command: list[str], timeout: int):
        del timeout
        cmd = tuple(command)
        if cmd == ("docker", "context", "inspect", "default"):
            return _result(
                cmd,
                stdout=json.dumps([{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}])
                + "\n",
            )
        if cmd == ("docker", "--context", "default", "version", "--format", "{{.Server.Version}}"):
            return _result(cmd, stdout="29.1.3\n")
        if cmd == ("docker", "--context", "default", "compose", "version", "--short"):
            return _result(cmd, stdout="2.40.3\n")
        if cmd[:4] == ("docker", "--context", "default", "ps"):
            return _result(cmd, stdout="".join(f"{c['Id']}\n" for c in state["containers"]))
        if cmd[:4] == ("docker", "--context", "default", "inspect"):
            cid = cmd[4]
            for container in state["containers"]:
                if container["Id"] == cid:
                    return _result(cmd, stdout=json.dumps([container]) + "\n")
            raise AssertionError(f"unknown container id {cid}")
        if cmd[:5] == ("docker", "--context", "default", "volume", "inspect"):
            vol = cmd[5]
            return _result(
                cmd,
                stdout=json.dumps(
                    [
                        {
                            "Name": vol,
                            "Driver": "local",
                            "Labels": {
                                "io.mc-remote.owner": "mcrctl",
                                "io.mc-remote.deployment": source_lock["deployment"]["name"],
                                "io.mc-remote.environment": source_lock["environment"]["identity"],
                                "io.mc-remote.world": source_lock["world"]["identity"],
                                "io.mc-remote.created-by-lock": source_lock["lock_identity"],
                            },
                        }
                    ]
                )
                + "\n",
            )
        if "compose" in cmd and "config" in cmd:
            if "--format" in cmd and "json" in cmd:
                compose_path = Path(cmd[cmd.index("--file") + 1])
                compose_obj = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
                for service in compose_obj.get("services", {}).values():
                    for volume in service.get("volumes", []):
                        if isinstance(volume, dict) and volume.get("type") == "bind":
                            src = Path(volume["source"])
                            if not src.is_absolute():
                                volume["source"] = str((compose_path.parent / src).resolve())
                return _result(cmd, stdout=json.dumps(compose_obj))
            return _result(cmd)
        if "compose" in cmd and ("pull" in cmd or "down" in cmd):
            return _result(cmd)
        if "compose" in cmd and "up" in cmd:
            state["containers"] = rendered_containers(output)
            return _result(cmd)
        raise AssertionError(f"unexpected command: {command!r}")

    return runner


def _patch_doctor_probes(monkeypatch: pytest.MonkeyPatch, output: Path) -> list[tuple[str, int]]:
    external_calls: list[tuple[str, int]] = []

    def fake_scratch_probe(url: str, timeout: int):
        del timeout
        host = url.split("/")[2]
        pair_file = output / "runtime/web-pairs" / f"{host}.json"
        if pair_file.exists():
            return json.loads(pair_file.read_text(encoding="utf-8"))
        caddyfile = (output / "Caddyfile").read_text(encoding="utf-8")
        lts_block = (
            f"{host} {{\n"
            '    header Referrer-Policy "strict-origin-when-cross-origin"\n'
            "    reverse_proxy scratch-lts:8080\n}"
        )
        if lts_block in caddyfile:
            return json.loads((output / "runtime/scratch-lts.json").read_text(encoding="utf-8"))
        return json.loads((output / "runtime/scratch.json").read_text(encoding="utf-8"))

    monkeypatch.setattr("mc_remote_stack.doctor.probe_homepage_public", lambda *_a, **_k: None)
    monkeypatch.setattr("mc_remote_stack.doctor.probe_scratch_runtime_config", fake_scratch_probe)
    monkeypatch.setattr("mc_remote_stack.doctor.probe_wirescope_public_handoff", lambda *_a, **_k: None)
    monkeypatch.setattr("mc_remote_stack.doctor.probe_unity_sandbox_health", lambda *_a, **_k: None)
    monkeypatch.setattr("mc_remote_stack.doctor.probe_unity_sandbox_tcp_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "mc_remote_stack.doctor.probe_external_mcremote_auth",
        lambda host, port, _timeout: external_calls.append((host, port)),
    )
    return external_calls


def test_deployment_update_upgrades_vps20_to_vps21_with_and_without_v4_targets_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root, artifact_store, backup = _setup_dual_line_data_root(tmp_path)
    homepage_meta = json.loads(
        (artifact_store.parent / "homepage-meta.json").read_text(encoding="utf-8")
    )
    project = init_toml_project(
        tmp_path / "official-vps",
        deployment_name="official-vps",
        profile="vps-server@20",
        environment_identity="official-vps",
        channel="beta",
        exposure="public",
        purpose="integration",
        preset="fixture-web-paper@1",
        artifact_store=str(artifact_store),
        runtime_volumes={
            "minecraft-data": "official-vps-mc",
            "caddy-data": "official-vps-cd",
            "caddy-config": "official-vps-cc",
        },
        world_identity="official-vps-world",
        bind_address="0.0.0.0",
        java_port=25565,
        mcremote_port=25575,
        minecraft_eula=True,
    ).root
    order_path = project / "mc-remote.toml"
    order_path.write_text(
        order_path.read_text(encoding="utf-8").replace(
            "[world]", '[minecraft]\nheap = "3G"\n\n[world]'
        ),
        encoding="utf-8",
    )
    v4_targets = """[[targets]]
id = "beta"
label = "公開ベータ"
sandbox = "sb-beta.mc-remote.example"

[[notices]]
heading = "お知らせ"
body = "v20 notice"

[[selections]]
scratch = "scratch-beta.mc-remote.example"
target_ids = ["beta"]
default_target = "beta"
"""
    for role, adapter, rel, content in (
        ("public-routes", "public-routes@3", "operator/public-routes/routes.toml", REHEARSAL_ROUTES),
        (
            "minecraft-server",
            "minecraft-server@1",
            "operator/minecraft-server/server.toml",
            OPERATOR_INPUTS["minecraft-server"][2],
        ),
        ("connection-targets", "connection-targets@4", "operator/connection-targets/targets.toml", v4_targets),
        (
            "minecraft-plugins",
            "minecraft-plugins@1",
            "operator/minecraft-plugins/plugins.toml",
            f'[[plugins]]\nfilename = "WorldEdit.jar"\nsha256 = "{_sha256(PLUGIN_BYTES)}"\n',
        ),
        (
            "homepage-static",
            "homepage-static@1",
            "operator/homepage-static/homepage.toml",
            f'tree_sha256 = "{homepage_meta["tree_sha256"]}"\n'
            f'file_count = {homepage_meta["file_count"]}\n'
            f'total_bytes = {homepage_meta["total_bytes"]}\n',
        ),
        (
            "minecraft-backup",
            "minecraft-backup@1",
            "operator/minecraft-backup/backup.toml",
            f'host_path = "{backup}"\n',
        ),
    ):
        p = project / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        with order_path.open("a", encoding="utf-8") as stream:
            stream.write(f'\n[[operator_inputs]]\nrole = "{role}"\nadapter = "{adapter}"\npath = "{rel}"\n')

    _acknowledge(project, "unverified")
    resolve_project(project, data_root=data_root, allow_unverified=True, resolved_at=FIRST_RESOLVED_AT)
    output = project / "generated"
    render_toml_project(project, output, data_root=data_root)
    source_lock = load_lock(project, data_root=data_root)
    source_containers = rendered_containers(output)

    # First verify vps-server@20 -> vps-server@21 works with an UNTOUCHED v4 targets.toml.
    _patch_doctor_probes(monkeypatch, output)
    runner = _dynamic_update_runner(output, source_lock, source_containers)
    plan_untouched = plan_deployment_update(
        project,
        output,
        target_profile="vps-server@21",
        target_preset="fixture-web-paper@2",
        input_overrides={},
        docker_context="default",
        data_root=data_root,
        allow_unverified=True,
        runner=runner,
        hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
    )
    res_untouched = apply_deployment_update(
        project,
        plan_id=plan_untouched.plan_id,
        confirmed=True,
        data_root=data_root,
        runner=runner,
        hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
    )
    assert res_untouched.phase == "complete"
    assert json.loads((output / "runtime/scratch.json").read_text(encoding="utf-8"))["schema_version"] == 2

    # Next verify upgrading with lts-web + connection-targets@5 + unity-sandboxes@2 on vps-server@21.
    targets_file = tmp_path / "rehearsal-targets.toml"
    targets_file.write_text(REHEARSAL_TARGETS, encoding="utf-8")
    lts_file = tmp_path / "rehearsal-lts.toml"
    lts_file.write_text(REHEARSAL_LTS_WEB, encoding="utf-8")
    unity_file = tmp_path / "unity.toml"
    unity_file.write_text(UNITY_SANDBOXES_SOURCE, encoding="utf-8")

    # Reset current containers / lock state for clean @20 -> @21 direct upgrade with lts-web
    f20 = _build_vps21_fixture(
        tmp_path / "direct20",
        lts_web_source=None,
        unity_source=None,
        resolve_now=False,
    )
    # Build as @20 first
    f20_order = f20.project / "mc-remote.toml"
    f20_order.write_text(
        f20_order.read_text(encoding="utf-8")
        .replace('profile = "vps-server@21"', 'profile = "vps-server@20"')
        .replace('preset = "fixture-web-paper@2"', 'preset = "fixture-web-paper@1"')
        .replace('adapter = "connection-targets@5"', 'adapter = "connection-targets@4"'),
        encoding="utf-8",
    )
    (f20.project / "operator/connection-targets/targets.toml").write_text(v4_targets, encoding="utf-8")
    resolve_project(f20.project, data_root=f20.data_root, allow_unverified=True, resolved_at=FIRST_RESOLVED_AT)
    render_toml_project(f20.project, f20.output, data_root=f20.data_root)
    f20_lock = load_lock(f20.project, data_root=f20.data_root)
    f20_containers = rendered_containers(f20.output)

    ext_calls = _patch_doctor_probes(monkeypatch, f20.output)
    f20_runner = _dynamic_update_runner(f20.output, f20_lock, f20_containers)
    plan_dual = plan_deployment_update(
        f20.project,
        f20.output,
        target_profile="vps-server@21",
        target_preset="fixture-web-paper@2",
        input_overrides={},
        input_files={
            "connection-targets": targets_file,
            "lts-web": lts_file,
            "unity-sandboxes": unity_file,
        },
        docker_context="default",
        data_root=f20.data_root,
        allow_unverified=True,
        runner=f20_runner,
        hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
    )
    assert "scratch-lts" in plan_dual.services
    assert "bridge-lts" in plan_dual.services

    res_dual = apply_deployment_update(
        f20.project,
        plan_id=plan_dual.plan_id,
        confirmed=True,
        data_root=f20.data_root,
        runner=f20_runner,
        hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
    )
    assert res_dual.phase == "complete"
    assert ext_calls == [("sb.mc-remote.example", 25575)]

    # Finally update vps-server@21 (11/25 rehearsal) -> vps-server@21 (12/1 production).
    prod_routes = tmp_path / "prod-routes.toml"
    prod_routes.write_text(PRODUCTION_ROUTES, encoding="utf-8")
    prod_targets = tmp_path / "prod-targets.toml"
    prod_targets.write_text(PRODUCTION_TARGETS, encoding="utf-8")
    prod_lts = tmp_path / "prod-lts.toml"
    prod_lts.write_text(PRODUCTION_LTS_WEB, encoding="utf-8")

    ext_calls.clear()
    plan_prod = plan_deployment_update(
        f20.project,
        f20.output,
        target_profile="vps-server@21",
        target_preset="fixture-web-paper@3",
        input_overrides={},
        input_files={
            "public-routes": prod_routes,
            "connection-targets": prod_targets,
            "lts-web": prod_lts,
        },
        docker_context="default",
        data_root=f20.data_root,
        allow_unverified=True,
        runner=f20_runner,
        hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
    )
    res_prod = apply_deployment_update(
        f20.project,
        plan_id=plan_prod.plan_id,
        confirmed=True,
        data_root=f20.data_root,
        runner=f20_runner,
        hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
    )
    assert res_prod.phase == "complete"
    # plan_deployment_update doctors the live source (sb) before apply
    # doctors the target (sb-lts).
    assert ext_calls == [
        ("sb.mc-remote.example", 25575),
        ("sb-lts.mc-remote.example", 25575),
    ]
    prod_lock = load_lock(f20.project, data_root=f20.data_root)
    assert prod_lock["input"]["preset"]["ref"] == "fixture-web-paper@3"
    assert (f20.output / "runtime/web-pairs/scratch-beta.mc-remote.example.json").is_file()
