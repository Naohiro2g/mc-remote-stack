import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import mc_remote_stack.cli as cli_module
from mc_remote_stack.cli import main
from mc_remote_stack.deployment_interface import (
    DeploymentInterfaceError,
    apply_interface_order,
    detect_apply_mode,
    doctor_interface_deployment,
    prepare_interface_deployment,
    validate_interface_runtime,
)

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


def _fixture(tmp_path: Path, *, order_suffix: str = "") -> tuple[Path, Path]:
    data_root = tmp_path / "data"
    contract = data_root / "scratch-contracts" / ("1" * 40)
    contract.mkdir(parents=True)
    schema_source = json.dumps(RUNTIME_SCHEMA, sort_keys=True).encode()
    (contract / "schema.json").write_bytes(schema_source)

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
schema_sha256 = "{hashlib.sha256(schema_source).hexdigest()}"
container_mount_path = "/usr/share/nginx/html/mc-remote-runtime-config.json"
image_digest = "sha256:{'2' * 64}"

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
    assert prepared.compose["services"]["scratch"]["volumes"] == [
        {
            "type": "bind",
            "source": "./runtime/scratch.json",
            "target": "/usr/share/nginx/html/mc-remote-runtime-config.json",
            "read_only": True,
        }
    ]


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
        ("\nrelease_identity = \"forbidden\"\n", "unknown_order_key"),
    ],
)
def test_single_order_fails_closed(order_suffix: str, reason: str, tmp_path: Path) -> None:
    order, data_root = _fixture(tmp_path, order_suffix=order_suffix)

    with pytest.raises(DeploymentInterfaceError, match=reason):
        prepare_interface_deployment(order, data_root=data_root)


def test_renderer_uses_handoff_schema_not_a_stack_owned_field_list(tmp_path: Path) -> None:
    order, data_root = _fixture(tmp_path)
    schema_path = data_root / "scratch-contracts" / ("1" * 40) / "schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["required"].append("scratch_owned_future_field")
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    preset_path = data_root / "preset_registry" / "classroom" / "1" / "preset.toml"
    source = preset_path.read_text(encoding="utf-8")
    source = source.replace(
        'schema_sha256 = "' + hashlib.sha256(json.dumps(RUNTIME_SCHEMA, sort_keys=True).encode()).hexdigest() + '"',
        'schema_sha256 = "' + hashlib.sha256(schema_path.read_bytes()).hexdigest() + '"',
    )
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


def test_apply_mode_is_derived_from_managed_runtime_state() -> None:
    assert detect_apply_mode([], expected_services={"scratch", "bridge", "minecraft"}) == "create"
    records = [
        {"service": "scratch", "managed": True},
        {"service": "bridge", "managed": True},
        {"service": "minecraft", "managed": True},
    ]
    assert detect_apply_mode(records, expected_services={"scratch", "bridge", "minecraft"}) == "update"
    with pytest.raises(DeploymentInterfaceError, match="deployment_runtime_unmanaged"):
        detect_apply_mode(records[:-1], expected_services={"scratch", "bridge", "minecraft"})


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


def _docker_runner(*, existing: bool):
    calls: list[list[str]] = []

    def run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:4] == ["context", "inspect", "default"]:
            stdout = '[{"Endpoints":{"docker":{"Host":"unix:///var/run/docker.sock"}}}]'
        elif "ps" in command and "--quiet" in command:
            stdout = "scratch-id\nbridge-id\nminecraft-id\n" if existing else ""
        elif "inspect" in command:
            container_id = command[-1]
            service = container_id.removesuffix("-id")
            stdout = json.dumps(
                [
                    {
                        "Config": {
                            "Labels": {
                                "com.docker.compose.service": service,
                                "io.mc-remote.interface": "2026-08-31-01",
                            }
                        }
                    }
                ]
            )
        else:
            stdout = "ok\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    return run, calls


@pytest.mark.parametrize(("existing", "expected_mode"), [(False, "create"), (True, "update")])
def test_apply_automatically_selects_create_or_update(
    existing: bool, expected_mode: str, tmp_path: Path
) -> None:
    order, data_root = _fixture(tmp_path)
    runner, calls = _docker_runner(existing=existing)

    result = apply_interface_order(
        order,
        data_root=data_root,
        state_root=tmp_path / "state",
        artifact_store=tmp_path / "artifacts",
        runner=runner,
        artifact_fetcher=lambda _prepared: None,
    )

    assert result.mode == expected_mode
    assert result.lock_identity.startswith("sha256:")
    assert (tmp_path / "state" / "school-a" / "current.json").is_file()
    flattened = [part for call in calls for part in call]
    assert "--bootstrap" not in flattened
    assert [part for part in flattened if part == "update"] == []
    assert any(call[-4:] == ["up", "--detach", "--remove-orphans", "--wait"] for call in calls)


def test_doctor_reads_locked_contract_and_live_bridge_allowlist(tmp_path: Path) -> None:
    order, data_root = _fixture(tmp_path)
    runner, _calls = _docker_runner(existing=True)
    applied = apply_interface_order(
        order,
        data_root=data_root,
        state_root=tmp_path / "state",
        artifact_store=tmp_path / "artifacts",
        runner=runner,
        artifact_fetcher=lambda _prepared: None,
    )
    runtime = json.loads(applied.runtime_config)

    result = doctor_interface_deployment(
        "school-a",
        data_root=data_root,
        state_root=tmp_path / "state",
        runner=runner,
        runtime_probe=lambda _url, _timeout: runtime,
        bridge_environment_probe=lambda _container, _runner: {
            "BRIDGE_SANDBOX_ALLOWLIST": "minecraft.example.org"
        },
    )

    assert result.lock_identity == applied.lock_identity
    assert result.scratch_runtime_status == "current"
    assert result.bridge_allowlist_status == "current"


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
        )

    monkeypatch.setattr(cli_module, "doctor_interface_deployment", doctor)

    assert main(["doctor", "school-a"]) == 0
    assert calls == ["school-a"]
    assert "OK doctor deployment=school-a scratch-runtime=current bridge-allowlist=current" in (
        capsys.readouterr().out
    )


def test_normal_apply_help_does_not_expose_legacy_bootstrap_or_update(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit, match="0"):
        main(["apply", "--help"])

    output = capsys.readouterr().out
    assert "mc-remote.toml" in output
    assert "--bootstrap" not in output
    assert "--expected-lock-identity" not in output
