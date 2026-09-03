import hashlib
import json
import subprocess
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest

import mc_remote_stack.cli as cli_module
import mc_remote_stack.deployment_interface as deployment_interface_module
from mc_remote_stack.cli import main
from mc_remote_stack.deployment_interface import (
    DeploymentInterfaceError,
    apply_interface_order,
    detect_apply_mode,
    doctor_interface_deployment,
    prepare_interface_deployment,
    validate_interface_runtime,
)
from mc_remote_stack.preset_registry import load_catalog_policy, load_preset, load_preset_catalog

RUNTIME_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "connection_enabled",
        "bridge_url",
        "default_sandbox",
        "connection_targets",
    ],
    "properties": {
        "schema_version": {"const": 1},
        "connection_enabled": {"const": True},
        "bridge_url": {"type": "string", "pattern": "^wss://"},
        "default_sandbox": {"type": "string", "minLength": 1},
        "connection_targets": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "label", "sandbox"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "label": {"type": "string", "minLength": 1},
                    "sandbox": {"type": "string", "minLength": 1},
                },
            },
        },
        "wirescope_url": {"type": "string", "pattern": "^https://"},
        "notices": {
            "type": "array",
            "maxItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["heading", "body"],
                "properties": {
                    "heading": {"type": "string", "minLength": 1},
                    "body": {"type": "string", "minLength": 1},
                    "link": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["href", "label"],
                        "properties": {
                            "href": {"type": "string", "pattern": "^https://"},
                            "label": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
        },
    },
}


def _git_blob_sha(source: bytes) -> bytes:
    return hashlib.sha1(b"blob " + str(len(source)).encode() + b"\0" + source).digest()


def _git_tree_sha(root: Path) -> str:
    entries: list[tuple[str, bool, bytes]] = []
    for path in root.iterdir():
        if path.is_dir():
            identity = bytes.fromhex(_git_tree_sha(path))
            entries.append((path.name, True, identity))
        else:
            identity = _git_blob_sha(path.read_bytes())
            entries.append((path.name, False, identity))
    entries.sort(key=lambda item: (item[0] + ("/" if item[1] else "")).encode())
    body = b"".join(
        (b"40000" if is_directory else b"100644")
        + b" "
        + name.encode()
        + b"\0"
        + identity
        for name, is_directory, identity in entries
    )
    return hashlib.sha1(b"tree " + str(len(body)).encode() + b"\0" + body).hexdigest()


def _fixture(tmp_path: Path, *, order_suffix: str = "") -> tuple[Path, Path]:
    data_root = tmp_path / "data"
    contract = data_root / "scratch-contracts" / ("1" * 40)
    (contract / "fixtures" / "invalid").mkdir(parents=True)
    schema_source = json.dumps(RUNTIME_SCHEMA, sort_keys=True).encode()
    (contract / "schema.json").write_bytes(schema_source)
    (contract / "README.md").write_text("runtime contract\n", encoding="utf-8")
    accepted_source = json.dumps(
        {
            "schema_version": 1,
            "connection_enabled": True,
            "bridge_url": "wss://bridge.example.org/",
            "default_sandbox": "minecraft.example.org",
            "connection_targets": [
                {
                    "id": "classroom",
                    "label": "Classroom",
                    "sandbox": "minecraft.example.org",
                }
            ],
        },
        sort_keys=True,
    ).encode()
    rejected_source = json.dumps(
        {"schema_version": 1, "connection_enabled": True, "unknown": "forbidden"},
        sort_keys=True,
    ).encode()
    (contract / "fixtures" / "valid.json").write_bytes(accepted_source)
    (contract / "fixtures" / "invalid" / "unknown-field.json").write_bytes(rejected_source)
    contract_tree_sha = _git_tree_sha(contract)

    preset = data_root / "preset_registry" / "classroom" / "1" / "preset.toml"
    preset.parent.mkdir(parents=True)
    preset.write_text(
        f'''schema_version = 1

[preset]
name = "classroom"
revision = "1"
description = "deployment interface test preset"

[requirements]
profile_capabilities = ["compose"]
allowed_channels = ["alpha"]
required_claims = ["profile-render"]

[deployment_interface]
renderer_revision = "1"
bind_address = "127.0.0.1"
scratch_port = 18080
bridge_port = 18081
java_port = 25565
mcremote_port = 25575

[deployment_interface.scratch_contract]
commit = "{'1' * 40}"
source_directory = "packages/scratch-gui/contracts/runtime-config"
directory_tree_sha = "{contract_tree_sha}"
schema_sha256 = "{hashlib.sha256(schema_source).hexdigest()}"
container_mount_path = "/usr/share/nginx/html/mc-remote-runtime-config.json"
image_digest = "sha256:{'2' * 64}"
accepted_fixtures = ["fixtures/valid.json"]
rejected_fixtures = ["fixtures/invalid/unknown-field.json"]

[deployment_interface.scratch_contract.fixture_sha256]
"fixtures/valid.json" = "{hashlib.sha256(accepted_source).hexdigest()}"
"fixtures/invalid/unknown-field.json" = "{hashlib.sha256(rejected_source).hexdigest()}"

[[components]]
id = "scratch"
role = "scratch-runtime"
artifact = "scratch-image"

[[components]]
id = "bridge"
role = "websocket-bridge"
artifact = "bridge-image"
protocol = "23.0.0"

[[components]]
id = "minecraft-runtime"
role = "minecraft-runtime"
artifact = "minecraft-image"

[[components]]
id = "paper"
role = "paper-server"
artifact = "paper-jar"
minecraft_version = "1.21.11"

[[components]]
id = "mcremote"
role = "mcremote-plugin"
artifact = "mcremote-jar"
protocol = "23.0.0"

[[artifacts]]
id = "scratch-image"
kind = "oci"
version = "sha-{'1' * 40}"
locator = "registry.example/scratch"
digest = "sha256:{'2' * 64}"

[[artifacts]]
id = "bridge-image"
kind = "oci"
version = "sha-{'1' * 40}"
locator = "registry.example/bridge"
digest = "sha256:{'3' * 64}"

[[artifacts]]
id = "minecraft-image"
kind = "oci"
version = "java21"
locator = "registry.example/minecraft"
digest = "sha256:{'4' * 64}"

[[artifacts]]
id = "paper-jar"
kind = "https-file"
version = "1.21.11-1"
filename = "paper.jar"
sha256 = "{'5' * 64}"
origin = "https://example.invalid/paper.jar"

[[artifacts]]
id = "mcremote-jar"
kind = "https-file"
version = "2300.0.0b6"
filename = "mcremote.jar"
sha256 = "{'6' * 64}"
origin = "https://example.invalid/mcremote.jar"
''',
        encoding="utf-8",
    )

    order = tmp_path / "mc-remote.toml"
    order.write_text(
        '''schema_version = 1
deployment = "school-a"
preset = "classroom@1"

[surfaces]
scratch_url = "https://scratch.example.org/"
bridge_url = "wss://bridge.example.org/"
wirescope_url = "https://wirescope.example.org/"

[[targets]]
id = "classroom"
label = "Classroom"
sandbox = "minecraft.example.org"
default = true

[[notices]]
heading = "授業のお知らせ"
body = "本日の利用時間は16時までです。"
''' + order_suffix,
        encoding="utf-8",
    )
    return order, data_root


def test_single_order_resolves_exact_preset_and_renders_one_target(tmp_path: Path) -> None:
    order, data_root = _fixture(tmp_path)

    prepared = prepare_interface_deployment(order, data_root=data_root)

    assert prepared.lock["preset"]["ref"] == "classroom@1"
    assert prepared.lock["artifacts"][0] == {
        "id": "scratch-image",
        "kind": "oci",
        "version": "sha-" + "1" * 40,
        "locator": "registry.example/scratch",
        "digest": "sha256:" + "2" * 64,
    }
    runtime = json.loads(prepared.files["runtime/scratch.json"])
    assert runtime == {
        "schema_version": 1,
        "connection_enabled": True,
        "bridge_url": "wss://bridge.example.org/",
        "default_sandbox": "minecraft.example.org",
        "connection_targets": [
            {"id": "classroom", "label": "Classroom", "sandbox": "minecraft.example.org"}
        ],
        "wirescope_url": "https://wirescope.example.org/",
        "notices": [
            {"heading": "授業のお知らせ", "body": "本日の利用時間は16時までです。"}
        ],
    }
    assert "release_identity" not in runtime
    bridge = prepared.compose["services"]["bridge"]["environment"]
    assert bridge["BRIDGE_SANDBOX_ALLOWLIST"] == "minecraft.example.org"
    assert bridge["BRIDGE_DEFAULT_SANDBOX"] == "minecraft.example.org"
    assert prepared.compose["services"]["minecraft"]["networks"] == {
        "app": {"aliases": ["minecraft.example.org"]}
    }
    assert prepared.compose["services"]["scratch"]["volumes"] == [
        {
            "type": "bind",
            "source": "./runtime/scratch.json",
            "target": "/usr/share/nginx/html/mc-remote-runtime-config.json",
            "read_only": True,
        }
    ]

    repeated = prepare_interface_deployment(order, data_root=data_root)
    assert repeated.lock == prepared.lock
    assert repeated.files == prepared.files


@pytest.mark.parametrize(
    ("order_suffix", "reason"),
    [
        (
            '''\n[[targets]]\nid = "second"\nlabel = "Second"\nsandbox = "second.example.org"\ndefault = true\n''',
            "default_target_count_invalid",
        ),
        (
            '''\n[[targets]]\nid = "second"\nlabel = "Second"\nsandbox = "minecraft.example.org"\ndefault = false\n''',
            "target_sandbox_duplicate",
        ),
        (
            '''
[[targets]]
id = "second"
label = "Second"
sandbox = "one.example.org,two.example.org"
default = false
''',
            "order_schema_invalid",
        ),
        ("\nrelease_identity = \"forbidden\"\n", "unknown_order_key"),
    ],
)
def test_single_order_fails_closed(order_suffix: str, reason: str, tmp_path: Path) -> None:
    order, data_root = _fixture(tmp_path, order_suffix=order_suffix)

    with pytest.raises(DeploymentInterfaceError, match=reason):
        prepare_interface_deployment(order, data_root=data_root)


def test_renderer_uses_handoff_schema_not_a_stack_owned_field_list(tmp_path: Path) -> None:
    order, data_root = _fixture(tmp_path)
    contract_path = data_root / "scratch-contracts" / ("1" * 40)
    schema_path = contract_path / "schema.json"
    fixture_path = contract_path / "fixtures" / "valid.json"
    old_tree_sha = _git_tree_sha(contract_path)
    old_schema_sha = hashlib.sha256(schema_path.read_bytes()).hexdigest()
    old_fixture_sha = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["required"].append("scratch_owned_future_field")
    schema["properties"]["scratch_owned_future_field"] = {"const": "contract-owned"}
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture["scratch_owned_future_field"] = "contract-owned"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    preset_path = data_root / "preset_registry" / "classroom" / "1" / "preset.toml"
    source = preset_path.read_text(encoding="utf-8")
    source = source.replace(old_tree_sha, _git_tree_sha(contract_path))
    source = source.replace(old_schema_sha, hashlib.sha256(schema_path.read_bytes()).hexdigest())
    source = source.replace(old_fixture_sha, hashlib.sha256(fixture_path.read_bytes()).hexdigest())
    preset_path.write_text(source, encoding="utf-8")

    with pytest.raises(DeploymentInterfaceError, match="scratch_runtime_schema_invalid"):
        prepare_interface_deployment(order, data_root=data_root)


def test_contract_handoff_digest_and_scratch_image_must_match(tmp_path: Path) -> None:
    order, data_root = _fixture(tmp_path)
    preset_path = data_root / "preset_registry" / "classroom" / "1" / "preset.toml"
    preset_path.write_text(
        preset_path.read_text(encoding="utf-8").replace(
            'image_digest = "sha256:' + "2" * 64 + '"',
            'image_digest = "sha256:' + "9" * 64 + '"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeploymentInterfaceError, match="scratch_image_digest_mismatch"):
        prepare_interface_deployment(order, data_root=data_root)


def test_contract_directory_tree_and_fixture_digests_must_match(tmp_path: Path) -> None:
    order, data_root = _fixture(tmp_path)
    fixture_path = (
        data_root
        / "scratch-contracts"
        / ("1" * 40)
        / "fixtures"
        / "valid.json"
    )
    fixture_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(DeploymentInterfaceError, match="scratch_contract_tree_mismatch"):
        prepare_interface_deployment(order, data_root=data_root)


def test_contract_fixture_accept_reject_expectations_are_verified(tmp_path: Path) -> None:
    order, data_root = _fixture(tmp_path)
    preset_path = data_root / "preset_registry" / "classroom" / "1" / "preset.toml"
    source = preset_path.read_text(encoding="utf-8")
    source = source.replace(
        'accepted_fixtures = ["fixtures/valid.json"]',
        'accepted_fixtures = ["fixtures/invalid/unknown-field.json"]',
    ).replace(
        'rejected_fixtures = ["fixtures/invalid/unknown-field.json"]',
        'rejected_fixtures = ["fixtures/valid.json"]',
    )
    preset_path.write_text(source, encoding="utf-8")

    with pytest.raises(DeploymentInterfaceError, match="scratch_contract_fixture_result_mismatch"):
        prepare_interface_deployment(order, data_root=data_root)


def test_bundled_classroom_preset_locks_authoritative_handoff_2(tmp_path: Path) -> None:
    order = tmp_path / "mc-remote.toml"
    order.write_text(
        '''schema_version = 1
deployment = "school-a"
preset = "classroom@1"

[surfaces]
scratch_url = "https://scratch.example.org/"
bridge_url = "wss://bridge.example.org/"

[[targets]]
id = "classroom"
label = "Classroom"
sandbox = "minecraft.example.org"
default = true
''',
        encoding="utf-8",
    )

    prepared = prepare_interface_deployment(order, artifact_store=tmp_path / "artifacts")

    contract = prepared.lock["scratch_contract"]
    assert contract["commit"] == "4c893bd532002d9216665c5c9b9825e09ede1e7c"
    assert contract["directory_tree_sha"] == "ecb669a02ac6c8e502b44850e6dd28260c5adad4"
    artifacts = {item["id"]: item for item in prepared.lock["artifacts"]}
    assert artifacts["scratch-image"]["digest"] == (
        "sha256:c2693fd5078ba547ced1cfe6b1732d5e9c283ffc44aaf8356bce6967e5aa7f2c"
    )
    assert artifacts["bridge-image"]["digest"] == (
        "sha256:42e9a25cdf6af922219544dbefd74a251eafb1dd78d88d4819c5a7418142b66c"
    )
    assert artifacts["mcremote-jar"] == {
        "id": "mcremote-jar",
        "kind": "https-file",
        "version": "2301.0.0b7",
        "filename": "mc-remote-1.21.11-2301.0.0b7.jar",
        "sha256": "f08388cf393e02db1eb605e707dfaec890792e7a475de5a51caacbc940028ee9",
        "origin": (
            "https://github.com/Naohiro2g/McRemote/releases/download/"
            "v1.21.11-2301.0.0b7/mc-remote-1.21.11-2301.0.0b7.jar"
        ),
    }
    components = {item["id"]: item for item in prepared.lock["components"]}
    assert components["bridge"]["protocol"] == "23.1.0"
    assert components["mcremote-paper"]["protocol"] == "23.1.0"
    assert load_preset("classroom@1").data["deployment_interface"]["renderer_revision"] == "1"
    policy_entry = next(
        item for item in load_catalog_policy()["presets"] if item["ref"] == "classroom@1"
    )
    assert policy_entry == {
        "ref": "classroom@1",
        "status": "active",
        "available_since": "2026-09-04",
    }
    catalog_entry = next(
        item
        for item in load_preset_catalog()["preset_catalog"]["presets"]
        if item["ref"] == "classroom@1"
    )
    assert catalog_entry["compatibility_status"] == "unverified"

    contract_root = Path(
        str(
            files("mc_remote_stack").joinpath(
                "data",
                "scratch-contracts",
                "4c893bd532002d9216665c5c9b9825e09ede1e7c",
            )
        )
    )
    assert _git_tree_sha(contract_root) == "ecb669a02ac6c8e502b44850e6dd28260c5adad4"
    expected_sha256 = {
        "schema.json": "4e1f8489dc6ea03800f5cf0fefd2f078fd6d71c8efda581f1711f68e384f99e4",
        "fixtures/valid.json": "cc2282144e1e87b42d2e31229461f9aeead26eeb446ae817571eb96935360e20",
        "fixtures/disabled.json": "bec0cf2c31fbca7d3bd603e29c1d145d82b6ecf7b4661b9adafde31dfa2eec2d",
        "fixtures/invalid/schema-version.json": (
            "9e0d32fc83501a2b73095ae59675e27ace5d11fb31e19443b78e49341ba3e766"
        ),
        "fixtures/invalid/enabled-missing-targets.json": (
            "f5392059e42431b45fb8f7f03aa09e734fb8b9e79f8fb92913082ff05842736c"
        ),
        "fixtures/invalid/unknown-field.json": (
            "35f1f21562e237cce722f5a1f93723f00d927d07769f8b12174d3ff9f73d5e3d"
        ),
        "fixtures/invalid/nested-unknown-field.json": (
            "ed0923793b8713ec67615b6f14bc9f8fb342041b0cdeed5d7b9b10110a3c62b7"
        ),
    }
    for relative, expected in expected_sha256.items():
        assert hashlib.sha256((contract_root / relative).read_bytes()).hexdigest() == expected


def test_apply_mode_is_derived_from_managed_runtime_state() -> None:
    services = {"scratch", "bridge", "minecraft"}
    assert (
        detect_apply_mode(
            [], expected_services=services, state_exists=False, volume_exists=False
        )
        == "create"
    )
    records = [
        {"service": "scratch", "managed": True},
        {"service": "bridge", "managed": True},
        {"service": "minecraft", "managed": True},
    ]
    assert (
        detect_apply_mode(
            records, expected_services=services, state_exists=True, volume_exists=True
        )
        == "update"
    )
    assert (
        detect_apply_mode(
            [], expected_services=services, state_exists=True, volume_exists=True
        )
        == "update"
    )
    with pytest.raises(DeploymentInterfaceError, match="deployment_runtime_unmanaged"):
        detect_apply_mode(
            records[:-1], expected_services=services, state_exists=True, volume_exists=True
        )
    with pytest.raises(DeploymentInterfaceError, match="deployment_state_incomplete"):
        detect_apply_mode(
            [], expected_services=services, state_exists=False, volume_exists=True
        )


def test_preset_revision_order_is_checked_only_for_an_existing_world() -> None:
    previous = {"preset": {"ref": "classroom@2"}}

    with pytest.raises(DeploymentInterfaceError, match="stateful_preset_downgrade"):
        deployment_interface_module._validate_stateful_transition(
            previous, {"preset": {"ref": "classroom@1"}}
        )
    with pytest.raises(DeploymentInterfaceError, match="stateful_preset_family_change"):
        deployment_interface_module._validate_stateful_transition(
            previous, {"preset": {"ref": "public-web-paper@9"}}
        )

    assert (
        detect_apply_mode(
            [],
            expected_services={"scratch", "bridge", "minecraft"},
            state_exists=False,
            volume_exists=False,
        )
        == "create"
    )


def test_doctor_validation_uses_locked_schema_and_exact_allowlist(tmp_path: Path) -> None:
    order, data_root = _fixture(tmp_path)
    prepared = prepare_interface_deployment(order, data_root=data_root)
    runtime = json.loads(prepared.files["runtime/scratch.json"])

    validate_interface_runtime(
        runtime,
        lock=prepared.lock,
        bridge_allowlist="minecraft.example.org",
        data_root=data_root,
    )

    with pytest.raises(DeploymentInterfaceError, match="bridge_allowlist_mismatch"):
        validate_interface_runtime(
            runtime,
            lock=prepared.lock,
            bridge_allowlist="extra.example.org,minecraft.example.org",
            data_root=data_root,
        )

    invalid = {**runtime, "release_identity": "forbidden"}
    with pytest.raises(DeploymentInterfaceError, match="scratch_runtime_schema_invalid"):
        validate_interface_runtime(
            invalid,
            lock=prepared.lock,
            bridge_allowlist="minecraft.example.org",
            data_root=data_root,
        )


def _docker_runner(
    *,
    existing: bool,
    volume_exists: bool | None = None,
    images: dict[str, str] | None = None,
    running: bool = True,
    minecraft_healthy: bool = True,
):
    calls: list[list[str]] = []
    if volume_exists is None:
        volume_exists = existing
    if images is None:
        images = {
            "scratch": "registry.example/scratch@sha256:" + "2" * 64,
            "bridge": "registry.example/bridge@sha256:" + "3" * 64,
            "minecraft": "registry.example/minecraft@sha256:" + "4" * 64,
        }

    def run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:4] == ["context", "inspect", "default"]:
            stdout = '[{"Endpoints":{"docker":{"Host":"unix:///var/run/docker.sock"}}}]'
        elif command[-3:-1] == ["volume", "inspect"]:
            return subprocess.CompletedProcess(command, 0 if volume_exists else 1, "[]", "")
        elif "ps" in command and "--quiet" in command:
            stdout = "scratch-id\nbridge-id\nminecraft-id\n" if existing else ""
        elif "inspect" in command and command[-1].endswith("-id"):
            container_id = command[-1]
            service = container_id.removesuffix("-id")
            ports = {
                "scratch": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}]},
                "bridge": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18081"}]},
                "minecraft": {
                    "25565/tcp": [{"HostIp": "127.0.0.1", "HostPort": "25565"}],
                    "25575/tcp": [{"HostIp": "127.0.0.1", "HostPort": "25575"}],
                },
            }[service]
            environment = (
                [
                    "BRIDGE_SANDBOX_ALLOWLIST=minecraft.example.org",
                    "BRIDGE_DEFAULT_SANDBOX=minecraft.example.org",
                    "BRIDGE_SANDBOX_PORT=25575",
                ]
                if service == "bridge"
                else []
            )
            state: dict[str, object] = {"Running": running}
            if service == "minecraft":
                state["Health"] = {
                    "Status": "healthy" if minecraft_healthy else "unhealthy"
                }
            stdout = json.dumps(
                [
                    {
                        "Config": {
                            "Image": images[service],
                            "Env": environment,
                            "Labels": {
                                "com.docker.compose.project": "school-a",
                                "com.docker.compose.service": service,
                                "io.mc-remote.interface": "2026-08-31-01",
                                "io.mc-remote.deployment": "school-a",
                            }
                        },
                        "State": state,
                        "NetworkSettings": {"Ports": ports},
                    }
                ]
            )
        else:
            stdout = "ok\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    return run, calls


def test_bundled_handoff_2_runs_through_apply_and_doctor(tmp_path: Path) -> None:
    order = tmp_path / "mc-remote.toml"
    order.write_text(
        '''schema_version = 1
deployment = "school-a"
preset = "classroom@1"

[surfaces]
scratch_url = "https://scratch.example.org/"
bridge_url = "wss://bridge.example.org/"

[[targets]]
id = "classroom"
label = "Classroom"
sandbox = "minecraft.example.org"
default = true
''',
        encoding="utf-8",
    )
    prepared = prepare_interface_deployment(order, artifact_store=tmp_path / "artifacts")
    images = {
        service: config["image"]
        for service, config in prepared.compose["services"].items()
    }
    runner, _calls = _docker_runner(existing=False, volume_exists=False)

    applied = apply_interface_order(
        order,
        state_root=tmp_path / "state",
        artifact_store=tmp_path / "artifacts",
        runner=runner,
        artifact_fetcher=lambda _prepared: None,
        port_probe=lambda _address, _port: True,
    )
    runtime = json.loads(applied.runtime_config)
    doctor_runner, _doctor_calls = _docker_runner(
        existing=True, volume_exists=True, images=images
    )
    result = doctor_interface_deployment(
        "school-a",
        state_root=tmp_path / "state",
        runner=doctor_runner,
        runtime_probe=lambda _url, _timeout: runtime,
        bridge_environment_probe=lambda _container, _runner: {
            "BRIDGE_SANDBOX_ALLOWLIST": "minecraft.example.org",
            "BRIDGE_DEFAULT_SANDBOX": "minecraft.example.org",
            "BRIDGE_SANDBOX_PORT": "25575",
        },
        bridge_upstream_probe=lambda *_args: None,
        hello_probe=lambda *_args: SimpleNamespace(status="auth-required"),
    )

    assert applied.mode == "create"
    assert result.scratch_runtime_status == "current"
    assert result.bridge_allowlist_status == "current"


def test_apply_automatically_selects_create_then_stopped_update(tmp_path: Path) -> None:
    order, data_root = _fixture(tmp_path)
    create_runner, create_calls = _docker_runner(existing=False, volume_exists=False)

    created = apply_interface_order(
        order,
        data_root=data_root,
        state_root=tmp_path / "state",
        artifact_store=tmp_path / "artifacts",
        runner=create_runner,
        artifact_fetcher=lambda _prepared: None,
        port_probe=lambda _address, _port: True,
    )
    stopped_runner, update_calls = _docker_runner(existing=False, volume_exists=True)
    updated = apply_interface_order(
        order,
        data_root=data_root,
        state_root=tmp_path / "state",
        artifact_store=tmp_path / "artifacts",
        runner=stopped_runner,
        artifact_fetcher=lambda _prepared: None,
        port_probe=lambda _address, _port: True,
    )

    assert created.mode == "create"
    assert updated.mode == "update"
    assert updated.lock_identity.startswith("sha256:")
    assert (tmp_path / "state" / "school-a" / "current.json").is_file()
    flattened = [part for call in create_calls + update_calls for part in call]
    assert "--bootstrap" not in flattened
    assert [part for part in flattened if part == "update"] == []
    assert any(
        call[-4:] == ["up", "--detach", "--remove-orphans", "--wait"]
        for call in create_calls + update_calls
    )


def test_apply_rejects_host_port_collision_before_fetch_or_render(tmp_path: Path) -> None:
    order, data_root = _fixture(tmp_path)
    runner, calls = _docker_runner(existing=False, volume_exists=False)
    fetched: list[str] = []

    with pytest.raises(DeploymentInterfaceError, match="deployment_port_in_use"):
        apply_interface_order(
            order,
            data_root=data_root,
            state_root=tmp_path / "state",
            artifact_store=tmp_path / "artifacts",
            runner=runner,
            artifact_fetcher=lambda _prepared: fetched.append("fetched"),
            port_probe=lambda _address, port: port != 25565,
        )

    assert fetched == []
    assert not (tmp_path / "state" / "school-a" / "renders").exists()
    assert not any("compose" in call for call in calls)


def test_doctor_reads_locked_contract_and_live_bridge_allowlist(tmp_path: Path) -> None:
    order, data_root = _fixture(tmp_path)
    runner, _calls = _docker_runner(existing=False, volume_exists=False)
    applied = apply_interface_order(
        order,
        data_root=data_root,
        state_root=tmp_path / "state",
        artifact_store=tmp_path / "artifacts",
        runner=runner,
        artifact_fetcher=lambda _prepared: None,
        port_probe=lambda _address, _port: True,
    )
    runtime = json.loads(applied.runtime_config)
    prepared = prepare_interface_deployment(order, data_root=data_root)
    images = {
        service: config["image"]
        for service, config in prepared.compose["services"].items()
    }
    doctor_runner, _doctor_calls = _docker_runner(
        existing=True, volume_exists=True, images=images
    )

    result = doctor_interface_deployment(
        "school-a",
        data_root=data_root,
        state_root=tmp_path / "state",
        runner=doctor_runner,
        runtime_probe=lambda _url, _timeout: runtime,
        bridge_environment_probe=lambda _container, _runner: {
            "BRIDGE_SANDBOX_ALLOWLIST": "minecraft.example.org",
            "BRIDGE_DEFAULT_SANDBOX": "minecraft.example.org",
            "BRIDGE_SANDBOX_PORT": "25575",
        },
        bridge_upstream_probe=lambda *_args: None,
        hello_probe=lambda *_args: SimpleNamespace(status="auth-required"),
    )

    assert result.lock_identity == applied.lock_identity
    assert result.scratch_runtime_status == "current"
    assert result.bridge_allowlist_status == "current"


def test_doctor_checks_exact_runtime_bridge_reachability_and_auth(tmp_path: Path) -> None:
    order, data_root = _fixture(tmp_path)
    prepared = prepare_interface_deployment(order, data_root=data_root)
    create_runner, _calls = _docker_runner(existing=False, volume_exists=False)
    applied = apply_interface_order(
        order,
        data_root=data_root,
        state_root=tmp_path / "state",
        artifact_store=tmp_path / "artifacts",
        runner=create_runner,
        artifact_fetcher=lambda _prepared: None,
        port_probe=lambda _address, _port: True,
    )
    images = {
        service: config["image"]
        for service, config in prepared.compose["services"].items()
    }
    doctor_runner, _doctor_calls = _docker_runner(
        existing=True,
        volume_exists=True,
        images=images,
    )
    bridge_probes: list[tuple[str, str, int]] = []
    hello_probes: list[tuple[str, int, str, str]] = []

    result = doctor_interface_deployment(
        "school-a",
        data_root=data_root,
        state_root=tmp_path / "state",
        runner=doctor_runner,
        runtime_probe=lambda _url, _timeout: json.loads(applied.runtime_config),
        bridge_upstream_probe=lambda container, sandbox, port, _runner, _context, _timeout: (
            bridge_probes.append((container, sandbox, port))
        ),
        hello_probe=lambda address, port, protocol, minecraft_version, timeout: (
            hello_probes.append((address, port, protocol, minecraft_version))
            or SimpleNamespace(status="auth-required")
        ),
    )

    assert result.runtime_status == "healthy"
    assert result.image_status == "current"
    assert result.network_status == "current"
    assert result.bridge_upstream_status == "reachable"
    assert result.auth_status == "enforced"
    assert bridge_probes == [("bridge-id", "minecraft.example.org", 25575)]
    assert hello_probes == [("127.0.0.1", 25575, "23.0.0", "1.21.11")]


def test_doctor_rejects_unhealthy_minecraft_container(tmp_path: Path) -> None:
    order, data_root = _fixture(tmp_path)
    create_runner, _calls = _docker_runner(existing=False, volume_exists=False)
    applied = apply_interface_order(
        order,
        data_root=data_root,
        state_root=tmp_path / "state",
        artifact_store=tmp_path / "artifacts",
        runner=create_runner,
        artifact_fetcher=lambda _prepared: None,
        port_probe=lambda _address, _port: True,
    )
    prepared = prepare_interface_deployment(order, data_root=data_root)
    images = {
        service: config["image"]
        for service, config in prepared.compose["services"].items()
    }
    doctor_runner, _doctor_calls = _docker_runner(
        existing=True,
        volume_exists=True,
        images=images,
        minecraft_healthy=False,
    )

    with pytest.raises(DeploymentInterfaceError, match="deployment_runtime_unhealthy"):
        doctor_interface_deployment(
            "school-a",
            data_root=data_root,
            state_root=tmp_path / "state",
            runner=doctor_runner,
            runtime_probe=lambda _url, _timeout: json.loads(applied.runtime_config),
            bridge_upstream_probe=lambda *_args: None,
            hello_probe=lambda *_args: SimpleNamespace(status="auth-required"),
        )


def test_cli_exposes_compact_apply_without_bootstrap_or_update_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    order = tmp_path / "mc-remote.toml"
    order.write_text("schema_version = 1\n", encoding="utf-8")
    calls: list[Path] = []

    def apply(path: Path, **_kwargs: object) -> SimpleNamespace:
        calls.append(path)
        return SimpleNamespace(
            deployment="school-a",
            mode="update",
            lock_identity="sha256:" + "a" * 64,
        )

    monkeypatch.setattr(cli_module, "apply_interface_order", apply)

    assert main(["apply", str(order)]) == 0
    assert calls == [order]
    assert "OK apply deployment=school-a mode=update" in capsys.readouterr().out


def test_cli_exposes_doctor_by_deployment_identity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def doctor(deployment: str, **_kwargs: object) -> SimpleNamespace:
        calls.append(deployment)
        return SimpleNamespace(
            deployment=deployment,
            lock_identity="sha256:" + "a" * 64,
            scratch_runtime_status="current",
            bridge_allowlist_status="current",
            runtime_status="healthy",
            image_status="current",
            network_status="current",
            bridge_upstream_status="reachable",
            auth_status="enforced",
        )

    monkeypatch.setattr(cli_module, "doctor_interface_deployment", doctor)

    assert main(["doctor", "school-a"]) == 0
    assert calls == ["school-a"]
    output = capsys.readouterr().out
    assert "OK doctor deployment=school-a runtime=healthy images=current network=current" in output
    assert "scratch-runtime=current bridge-allowlist=current" in output
    assert "bridge-upstream=reachable auth=enforced" in output


def test_normal_apply_help_does_not_expose_legacy_bootstrap_or_update(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit, match="0"):
        main(["apply", "--help"])

    output = capsys.readouterr().out
    assert "mc-remote.toml" in output
    assert "--bootstrap" not in output
    assert "--expected-lock-identity" not in output
