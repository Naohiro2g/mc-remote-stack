import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import mc_remote_stack.cli as cli_module
import mc_remote_stack.deployment_interface as interface_module
from mc_remote_stack.cli import main
from mc_remote_stack.deployment_interface import (
    DeploymentInterfaceError,
    apply_interface_order,
    doctor_interface_deployment,
    prepare_interface_deployment,
    validate_interface_runtime,
)
from mc_remote_stack.preset_registry import load_preset


def _order(tmp_path: Path) -> Path:
    path = tmp_path / "mc-remote.toml"
    path.write_text('''schema_version = 1
deployment = "home-trial"
preset = "home-alpha-full@2"

[surfaces]
scratch_url = "https://home.example.org:8443/"
bridge_url = "wss://home.example.org:8444/"

[[targets]]
id = "trial"
label = "Home trial"
sandbox = "home.example.org"
default = true
''')
    return path


def _runner(prepared, *, existing=False, volume_exists=False, mutation=None):
    calls = []
    ports = {
        "caddy": {
            "8443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8443"}],
            "8444/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8444"}],
        },
        "scratch": {}, "bridge": {},
        "minecraft": {
            "25565/tcp": [{"HostIp": "127.0.0.1", "HostPort": "25565"}],
            "25575/tcp": [{"HostIp": "127.0.0.1", "HostPort": "25575"}],
        },
    }

    def run(command, timeout):
        calls.append(command)
        if command[1:4] == ["context", "inspect", "default"]:
            stdout = '[{"Endpoints":{"docker":{"Host":"unix:///var/run/docker.sock"}}}]'
        elif command[-3:-1] == ["volume", "inspect"]:
            return subprocess.CompletedProcess(command, 0 if volume_exists else 1, "[]", "")
        elif "ps" in command:
            stdout = "caddy-id\nscratch-id\nbridge-id\nminecraft-id\n" if existing else ""
        elif "inspect" in command and command[-1].endswith("-id"):
            service = command[-1].removesuffix("-id")
            record = {
                "Config": {
                    "Image": prepared.compose["services"][service]["image"],
                    "Labels": {
                        "com.docker.compose.service": service,
                        "io.mc-remote.interface": "2026-08-31-01",
                    },
                    "Env": [
                        "BRIDGE_SANDBOX_ALLOWLIST=home.example.org",
                        "BRIDGE_DEFAULT_SANDBOX=home.example.org",
                        "BRIDGE_SANDBOX_PORT=25575",
                    ] if service == "bridge" else [],
                },
                "State": {"Running": True, "Health": {"Status": "healthy"}},
                "NetworkSettings": {"Ports": copy.deepcopy(ports[service])},
            }
            if mutation is not None:
                mutation(service, record)
            stdout = json.dumps([record])
        else:
            stdout = "ok\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    return run, calls


def _applied(tmp_path):
    order = _order(tmp_path)
    prepared = prepare_interface_deployment(order, artifact_store=tmp_path / "artifacts")
    runner, _ = _runner(prepared)
    result = apply_interface_order(
        order, artifact_store=tmp_path / "artifacts", state_root=tmp_path / "state",
        runner=runner, artifact_fetcher=lambda _: None, port_probe=lambda *_: True,
    )
    return prepared, result


def test_home_post2_uses_the_published_contract_and_shared_target_generation(tmp_path):
    prepared = prepare_interface_deployment(_order(tmp_path))
    public = load_preset("public-web-paper@11").data
    public_artifacts = {a["id"]: a for a in public["artifacts"]}
    for artifact in prepared.lock["artifacts"]:
        assert artifact == public_artifacts[artifact["id"]]
    assert set(prepared.compose["services"]) == {"caddy", "scratch", "bridge", "minecraft"}
    assert prepared.compose["services"]["caddy"]["ports"] == [
        "127.0.0.1:8443:8443/tcp", "127.0.0.1:8444:8444/tcp",
    ]
    assert "ports" not in prepared.compose["services"]["scratch"]
    assert "ports" not in prepared.compose["services"]["bridge"]
    assert "reverse_proxy scratch:8080" in prepared.files["Caddyfile"]
    assert "reverse_proxy bridge:8080" in prepared.files["Caddyfile"]
    assert prepared.lock["scratch_contract"]["commit"] == public["scratch_runtime_contract"]["source_commit"]
    assert prepared.lock["runtime_config"]["schema_version"] == 1
    assert "release_identity" not in prepared.lock["runtime_config"]
    validate_interface_runtime(
        prepared.lock["runtime_config"], lock=prepared.lock, bridge_allowlist="home.example.org",
    )
    assert prepared.compose["services"]["bridge"]["environment"]["BRIDGE_SANDBOX_ALLOWLIST"] == "home.example.org"
    assert prepared.compose["services"]["minecraft"]["networks"]["app"]["aliases"] == ["home.example.org"]
    assert 'credential_store_path: "/data/plugins/McRemote/session-only/store/snapshot.json"' in (
        prepared.files["runtime/minecraft/plugins/McRemote/config.yml"]
    )


@pytest.mark.parametrize("invalid", ["missing-schema-version", "release-identity"])
def test_home_doctor_validates_the_served_runtime_against_the_locked_schema(tmp_path, invalid):
    prepared, _ = _applied(tmp_path)
    runtime = copy.deepcopy(prepared.lock["runtime_config"])
    if invalid == "missing-schema-version":
        runtime.pop("schema_version")
    else:
        runtime["release_identity"] = "old-format"
    runner, _ = _runner(prepared, existing=True, volume_exists=True)
    with pytest.raises(DeploymentInterfaceError, match="scratch_runtime_schema_invalid"):
        doctor_interface_deployment(
            "home-trial", state_root=tmp_path / "state", runner=runner,
            runtime_probe=lambda *_: runtime, bridge_upstream_probe=lambda *_: None,
            hello_probe=lambda *_: SimpleNamespace(status="auth-required"),
        )


def test_home_apply_selects_update_for_a_stopped_existing_world_and_doctor_checks_four_services(tmp_path):
    prepared, created = _applied(tmp_path)
    runner, _ = _runner(prepared, volume_exists=True)
    updated = apply_interface_order(
        prepared.order_path, artifact_store=tmp_path / "artifacts", state_root=tmp_path / "state",
        runner=runner, artifact_fetcher=lambda _: None, port_probe=lambda *_: True,
    )
    assert created.mode == "create" and updated.mode == "update"
    runner, _ = _runner(prepared, existing=True, volume_exists=True)
    result = doctor_interface_deployment(
        "home-trial", state_root=tmp_path / "state", runner=runner,
        runtime_probe=lambda *_: prepared.lock["runtime_config"],
        bridge_upstream_probe=lambda *_: None,
        hello_probe=lambda *_: SimpleNamespace(status="auth-required"),
    )
    assert result.scratch_runtime_status == "current" and result.auth_status == "enforced"


def test_home_doctor_rejects_publication_of_a_direct_scratch_port(tmp_path):
    prepared, _ = _applied(tmp_path)

    def mutate(service, record):
        if service == "scratch":
            record["NetworkSettings"]["Ports"] = {
                "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18080"}],
            }

    runner, _ = _runner(prepared, existing=True, volume_exists=True, mutation=mutate)
    with pytest.raises(DeploymentInterfaceError, match="deployment_network_mismatch"):
        doctor_interface_deployment("home-trial", state_root=tmp_path / "state", runner=runner)


def test_home_apply_checks_caddy_ports_before_fetch_or_publishing(tmp_path):
    order = _order(tmp_path)
    prepared = prepare_interface_deployment(order)
    runner, _ = _runner(prepared)
    fetched = []
    with pytest.raises(DeploymentInterfaceError, match="deployment_port_in_use"):
        apply_interface_order(
            order, state_root=tmp_path / "state", runner=runner,
            artifact_fetcher=lambda _: fetched.append(True),
            port_probe=lambda address, port: port != 8443,
        )
    assert fetched == []
    assert not (tmp_path / "state").exists()


def test_home_dry_run_exports_a_reviewable_lock_and_compose_without_docker(tmp_path, monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        raise AssertionError("dry-run must not apply or start Docker")

    monkeypatch.setattr(cli_module, "apply_interface_order", unexpected)
    monkeypatch.setattr(subprocess, "run", unexpected)
    output = tmp_path / "preview"
    assert main(["apply", str(_order(tmp_path)), "--dry-run", "--output", str(output)]) == 0
    lock = json.loads((output / "mc-remote.lock.json").read_text())
    assert lock["preset"]["ref"] == "home-alpha-full@2"
    assert (output / "compose.yaml").is_file()
    assert (output / "Caddyfile").is_file()
    assert not (output / "current.json").exists()
    assert "PLAN apply deployment=home-trial" in capsys.readouterr().out


def test_dry_run_requires_an_order_and_never_enters_project_apply(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli_module, "_uses_toml_project", lambda _: True)

    def unexpected(*args, **kwargs):
        raise AssertionError("dry-run must not enter project apply")

    monkeypatch.setattr(cli_module, "apply_toml_project", unexpected)
    assert main(["apply", "--project", str(tmp_path), "--dry-run"]) == 2
    assert "apply_order_required" in capsys.readouterr().out


def test_dry_run_preserves_existing_review_files(tmp_path, capsys):
    output = tmp_path / "preview"
    output.mkdir()
    sentinel = output / "operator-note.txt"
    sentinel.write_text("keep this note")
    assert main(["apply", str(_order(tmp_path)), "--dry-run", "--output", str(output)]) == 2
    assert "preview_output_not_empty" in capsys.readouterr().out
    assert sentinel.read_text() == "keep this note"


def test_apply_reports_the_stage_before_a_docker_failure(tmp_path):
    order = _order(tmp_path)
    prepared = prepare_interface_deployment(order)
    base_runner, _ = _runner(prepared)
    stages = []

    def runner(command, timeout):
        if "pull" in command:
            return subprocess.CompletedProcess(command, 1, "", "registry unavailable")
        return base_runner(command, timeout)

    with pytest.raises(DeploymentInterfaceError, match="registry unavailable"):
        apply_interface_order(
            order, state_root=tmp_path / "state", runner=runner,
            artifact_fetcher=lambda _: None, port_probe=lambda *_: True,
            progress=stages.append,
        )
    assert stages[-1] == "pull"
    assert "start" not in stages
    assert not (tmp_path / "state/home-trial/current.json").exists()


def test_docker_error_includes_a_bounded_diagnostic():
    def runner(command, timeout):
        return subprocess.CompletedProcess(command, 1, "", "x" * 3000 + "\naddress already in use")

    with pytest.raises(DeploymentInterfaceError) as failure:
        interface_module._run(runner, ["docker", "compose", "up"], 30, "deployment_apply_failed")
    assert "address already in use" in str(failure.value)
    assert len(str(failure.value)) < 2200
