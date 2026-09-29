"""Test-owned public VPS deployment fixture for vps-server@12 / compose@13.

Mechanism tests (render, apply, doctor, world restore, artifact fetch) build on
this fixture instead of copying product presets, so adding or retiring a
bundled preset does not ripple through them. Only the profile under test and
the immutable Scratch contract it pins are copied from package data.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import zipfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from mc_remote_stack.preset_registry import build_preset_catalog
from mc_remote_stack.resolver import resolve_project
from mc_remote_stack.runtime_content import import_homepage_tree
from mc_remote_stack.toml_project import init_toml_project

from .test_preset_registry import _data_root, _write_policy
from .test_resolver import FIRST_RESOLVED_AT, _acknowledge

PROFILE = "vps-server@12"
PRESET = "fixture-web-paper@1"
SCRATCH_CONTRACT_COMMIT = "f133fc95ed7b23109cc1908dc4f0dae066510258"
SCRATCH_IMAGE_DIGEST = (
    "sha256:738dae72706df0e6d781c7413a50054586ec824db0a7c73b938c47d990d2fc56"
)

PAPER_BYTES = b"deterministic paper fixture\n"
MCREMOTE_BYTES = b"deterministic mcremote fixture\n"
PLUGIN_BYTES = b"deterministic peripheral plugin fixture\n"
WIRESCOPE_ASSETS = {
    "index.html": b"<!doctype html><title>WireScope</title>\n",
    "assets/app.js": b"export const ready = true;\n",
}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _oci_digest(seed: int) -> str:
    return f"sha256:{seed:064x}"


def _wirescope_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, content in WIRESCOPE_ASSETS.items():
            bundle.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), content)
    return buffer.getvalue()


WIRESCOPE_ZIP_BYTES = _wirescope_zip()
WIRESCOPE_MANIFEST_BYTES = json.dumps(
    {
        "manifest_schema": "mcremote.wirescope.app-manifest",
        "manifest_version": 1,
        "archive": {
            "file": "wirescope-app.zip",
            "format": "zip",
            "format_version": 1,
            "sha256": _sha256(WIRESCOPE_ZIP_BYTES),
        },
        "protocols": {
            "observer_schema": {"name": "mcremote.observer", "version": 1},
            "observer_session": 1,
            "scratch_handoff": 1,
            "station_attach": 1,
        },
        "assets": [
            {"path": name, "bytes": len(content), "sha256": _sha256(content)}
            for name, content in WIRESCOPE_ASSETS.items()
        ],
    },
    sort_keys=True,
).encode("utf-8")

FILE_ARTIFACTS = {
    "paper-jar": ("paper-fixture.jar", PAPER_BYTES),
    "mcremote-jar": ("mcremote-fixture.jar", MCREMOTE_BYTES),
    "wirescope-zip": ("wirescope-app.zip", WIRESCOPE_ZIP_BYTES),
    "wirescope-manifest": ("wirescope-app.manifest.json", WIRESCOPE_MANIFEST_BYTES),
}


# Identity of the pinned Scratch contract directory copied into the data root.
SCRATCH_CONTRACT = f'''[scratch_runtime_contract]
source_commit = "{SCRATCH_CONTRACT_COMMIT}"
source_directory = "packages/scratch-gui/contracts/runtime-config"
directory_tree_sha = "ecb669a02ac6c8e502b44850e6dd28260c5adad4"
schema_sha256 = "4e1f8489dc6ea03800f5cf0fefd2f078fd6d71c8efda581f1711f68e384f99e4"
container_mount_path = "/usr/share/nginx/html/mc-remote-runtime-config.json"
image_digest = "{SCRATCH_IMAGE_DIGEST}"
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
"fixtures/invalid/schema-version.json" = "9e0d32fc83501a2b73095ae59675e27ace5d11fb31e19443b78e49341ba3e766"
"fixtures/invalid/unknown-field.json" = "35f1f21562e237cce722f5a1f93723f00d927d07769f8b12174d3ff9f73d5e3d"'''


def _preset_source(mcremote_artifact: str | None = None) -> str:
    name, revision = PRESET.split("@")
    scratch_contract = SCRATCH_CONTRACT
    file_artifacts = "\n\n".join(
        f'''[[artifacts]]
id = "{artifact_id}"
kind = "https-file"
version = "fixture"
filename = "{filename}"
sha256 = "{_sha256(content)}"
origin = "https://example.invalid/{filename}"'''
        for artifact_id, (filename, content) in FILE_ARTIFACTS.items()
        if not (mcremote_artifact and artifact_id == "mcremote-jar")
    )
    if mcremote_artifact:
        file_artifacts += "\n\n" + mcremote_artifact
    return f"""schema_version = 1

[preset]
name = "{name}"
revision = "{revision}"
description = "Test-owned public VPS deployment fixture"

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
allowed_channels = ["beta"]
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
protocol = "23.1.0"

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
protocol = "23.1.0"

[[artifacts]]
id = "caddy-image"
kind = "oci"
version = "fixture"
locator = "registry.example/caddy"
digest = "{_oci_digest(21)}"

[[artifacts]]
id = "scratch-image"
kind = "oci"
version = "fixture"
locator = "registry.example/scratch"
digest = "{SCRATCH_IMAGE_DIGEST}"

[[artifacts]]
id = "bridge-image"
kind = "oci"
version = "fixture"
locator = "registry.example/bridge"
digest = "{_oci_digest(23)}"

[[artifacts]]
id = "minecraft-image"
kind = "oci"
version = "fixture-java21"
locator = "registry.example/minecraft"
digest = "{_oci_digest(24)}"

{file_artifacts}
"""


OPERATOR_INPUTS = {
    "public-routes": (
        "public-routes@2",
        "operator/public-routes/routes.toml",
        """homepage = "mc-remote.example"
homepage_aliases = ["www.mc-remote.example"]
scratch = "scratch-beta.mc-remote.example"
bridge = "bridge-beta.mc-remote.example"
minecraft = "sb-beta.mc-remote.example"
wirescope = "wirescope-beta.mc-remote.example"
""",
    ),
    "minecraft-server": (
        "minecraft-server@1",
        "operator/minecraft-server/server.toml",
        """allow_flight = false
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
motd = "McRemote Fixture Server"
network_compression_threshold = -1
simulation_distance = 6
spawn_protection = 150
view_distance = 10
white_list = false
""",
    ),
    "connection-targets": (
        "connection-targets@3",
        "operator/connection-targets/targets.toml",
        """[[targets]]
id = "beta"
label = "公開ベータ"
sandbox = "sb-beta.mc-remote.example"

[[notices]]
heading = "お知らせ"
body = "fixture notice"
""",
    ),
}


@dataclass(frozen=True)
class VpsFixture:
    project: Path
    data_root: Path
    artifact_store: Path
    backup: Path

    @property
    def output(self) -> Path:
        return self.project / "generated"


def build_vps_fixture(
    tmp_path: Path,
    *,
    identity: str = "official-vps",
    mcremote_artifact: str | None = None,
) -> VpsFixture:
    """Create one resolved vps-server@12 project with test-owned artifacts.

    ``mcremote_artifact`` replaces the McRemote artifact record, for example with a
    reviewed git-build record.
    """

    data_root = _data_root(tmp_path, "vps-data")
    profile_name, profile_revision = PROFILE.split("@")
    profile_path = data_root / "profiles" / profile_name / profile_revision / "profile.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        files("mc_remote_stack")
        .joinpath("data", "profiles", profile_name, profile_revision, "profile.toml")
        .read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    contract_source = files("mc_remote_stack").joinpath(
        "data", "scratch-contracts", SCRATCH_CONTRACT_COMMIT
    )
    shutil.copytree(
        str(contract_source), data_root / "scratch-contracts" / SCRATCH_CONTRACT_COMMIT
    )
    preset_name, preset_revision = PRESET.split("@")
    preset_path = data_root / "preset_registry" / preset_name / preset_revision / "preset.toml"
    preset_path.parent.mkdir(parents=True)
    preset_path.write_text(_preset_source(mcremote_artifact), encoding="utf-8")
    _write_policy(data_root, [{"ref": PRESET, "status": "active", "available_since": "2026-09-29"}])
    (data_root / "preset_catalog.toml").write_bytes(build_preset_catalog(data_root=data_root))

    artifact_store = tmp_path / "artifact-store"
    digest_store = artifact_store / "sha256"
    digest_store.mkdir(parents=True)
    for _filename, content in [*FILE_ARTIFACTS.values(), ("plugin", PLUGIN_BYTES)]:
        (digest_store / _sha256(content)).write_bytes(content)
    homepage_source = tmp_path / "homepage-source"
    homepage_source.mkdir()
    (homepage_source / "index.html").write_text("homepage fixture\n", encoding="utf-8")
    homepage = import_homepage_tree(homepage_source, artifact_store)
    backup = tmp_path / "backup"
    backup.mkdir()
    # The live homepage directory `mcrctl homepage sync` maintains beside the store.
    live_homepage = artifact_store.parent / "homepage"
    live_homepage.mkdir()
    (live_homepage / "index.html").write_text("homepage fixture\n", encoding="utf-8")

    project = init_toml_project(
        tmp_path / identity,
        deployment_name=identity,
        profile=PROFILE,
        environment_identity=identity,
        channel="beta",
        exposure="public",
        purpose="integration",
        preset=PRESET,
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

    inputs = dict(OPERATOR_INPUTS)
    inputs["minecraft-plugins"] = (
        "minecraft-plugins@1",
        "operator/minecraft-plugins/plugins.toml",
        f'[[plugins]]\nfilename = "WorldEdit.jar"\nsha256 = "{_sha256(PLUGIN_BYTES)}"\n',
    )
    inputs["homepage-static"] = (
        "homepage-static@1",
        "operator/homepage-static/homepage.toml",
        f'tree_sha256 = "{homepage.tree_sha256}"\n'
        f"file_count = {homepage.file_count}\n"
        f"total_bytes = {homepage.total_bytes}\n",
    )
    inputs["minecraft-backup"] = (
        "minecraft-backup@1",
        "operator/minecraft-backup/backup.toml",
        f'host_path = "{backup}"\n',
    )
    order = project / "mc-remote.toml"
    with order.open("a", encoding="utf-8") as stream:
        for role, (adapter, relative, content) in inputs.items():
            target = project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            stream.write(
                f'\n[[operator_inputs]]\nrole = "{role}"\nadapter = "{adapter}"\npath = "{relative}"\n'
            )

    _acknowledge(project, "unverified")
    resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=FIRST_RESOLVED_AT,
    )
    return VpsFixture(project, data_root, artifact_store, backup)


def rendered_containers(output: Path, *, health: str = "healthy") -> list[dict]:
    """Docker inspect records of a runtime that runs exactly the canonical render."""

    import yaml

    compose = yaml.safe_load((output / "compose.yaml").read_text(encoding="utf-8"))
    output = output.resolve()
    records = []
    for service_id, service in compose["services"].items():
        ports: dict[str, list[dict[str, str]]] = {}
        for specification in service.get("ports", []):
            mapping, _, protocol = str(specification).partition("/")
            address, host_port, container_port = mapping.split(":")
            ports.setdefault(f"{container_port}/{protocol or 'tcp'}", []).append(
                {"HostIp": address, "HostPort": host_port}
            )
        mounts = []
        for volume in service.get("volumes", []):
            mount = {"Type": volume["type"], "Destination": volume["target"]}
            mount["RW"] = volume.get("read_only") is not True
            if volume["type"] == "volume":
                mount["Name"] = compose["volumes"][volume["source"]]["name"]
            else:
                mount["Source"] = str((output / volume["source"]).resolve())
            mounts.append(mount)
        records.append(
            {
                "Id": f"container-{service_id}",
                "Config": {
                    "Labels": {
                        **service.get("labels", {}),
                        "com.docker.compose.project": compose["name"],
                        "com.docker.compose.service": service_id,
                        "com.docker.compose.project.config_files": str(output / "compose.yaml"),
                        "com.docker.compose.project.working_dir": str(output),
                    }
                },
                "State": {"Running": True, "Health": {"Status": health}},
                "NetworkSettings": {"Ports": ports},
                "Mounts": mounts,
            }
        )
    return records
