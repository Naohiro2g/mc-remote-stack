import copy
import hashlib
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import zipfile
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomlkit
import yaml

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
from mc_remote_stack.doctor import DoctorContractError
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
    started = False
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
    if "wirescope_port" in prepared.lock["network"]:
        ports["caddy"]["8445/tcp"] = [{"HostIp": "127.0.0.1", "HostPort": "8445"}]
    if "bedrock_port" in prepared.lock["network"]:
        ports["minecraft"]["19132/udp"] = [{
            "HostIp": prepared.lock["network"]["bedrock_bind_address"], "HostPort": "19132",
        }]

    def run(command, timeout):
        nonlocal started
        calls.append(command)
        if "up" in command:
            started = True
        if command[1:4] == ["context", "inspect", "default"]:
            stdout = '[{"Endpoints":{"docker":{"Host":"unix:///var/run/docker.sock"}}}]'
        elif command[-3:-1] == ["volume", "inspect"]:
            return subprocess.CompletedProcess(command, 0 if volume_exists else 1, "[]", "")
        elif "ps" in command:
            stdout = "caddy-id\nscratch-id\nbridge-id\nminecraft-id\n" if existing or started else ""
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


@pytest.mark.parametrize("preview", [False, True])
def test_container_readable_public_configs_and_private_state_modes(tmp_path, preview):
    prepared = prepare_interface_deployment(_order(tmp_path))
    old_umask = os.umask(0o077)
    try:
        if preview:
            root = interface_module.write_interface_preview(prepared, tmp_path / "preview")
        else:
            root = interface_module._publish_render(prepared, tmp_path / "state")
    finally:
        os.umask(old_umask)
    for relative in ("Caddyfile", "runtime/scratch.json"):
        assert stat.S_IMODE((root / relative).stat().st_mode) == 0o644
    for relative in ("mc-remote.lock.json", "runtime/minecraft/plugins/McRemote/config.yml"):
        assert stat.S_IMODE((root / relative).stat().st_mode) == 0o600


@pytest.mark.parametrize("relative", ["Caddyfile", "runtime/scratch.json"])
def test_same_content_permission_repair_reaches_an_existing_bind_mount(tmp_path, relative):
    prepared = prepare_interface_deployment(_order(tmp_path))
    root = interface_module._publish_render(prepared, tmp_path / "state")
    path = root / relative
    path.chmod(0o600)
    with path.open("rb") as mounted_inode:
        inode = os.fstat(mounted_inode.fileno()).st_ino
        interface_module._publish_render(prepared, tmp_path / "state")
        assert path.stat().st_ino == inode
        assert stat.S_IMODE(os.fstat(mounted_inode.fileno()).st_mode) == 0o644
        assert mounted_inode.read() == path.read_bytes()


def _restarting_caddy(service, record):
    if service == "caddy":
        # Docker reports Running=True even while a restart-policy container restarts.
        record["State"]["Restarting"] = True
        record["NetworkSettings"]["Ports"] = {}


def test_doctor_names_a_restart_loop_before_the_missing_ports(tmp_path):
    prepared, _ = _applied(tmp_path)
    runner, _ = _runner(prepared, existing=True, volume_exists=True, mutation=_restarting_caddy)
    with pytest.raises(DeploymentInterfaceError, match="deployment_runtime_restarting.*caddy"):
        doctor_interface_deployment("home-trial", state_root=tmp_path / "state", runner=runner)


def test_apply_rechecks_the_started_containers_before_recording_success(tmp_path):
    order = _order(tmp_path)
    prepared = prepare_interface_deployment(order)
    runner, calls = _runner(prepared, mutation=_restarting_caddy)
    stages = []
    with pytest.raises(DeploymentInterfaceError, match="deployment_runtime_restarting.*caddy"):
        apply_interface_order(
            order, state_root=tmp_path / "state", runner=runner,
            artifact_fetcher=lambda _: None, port_probe=lambda *_: True, progress=stages.append,
        )
    assert any("up" in command for command in calls)
    assert stages[-1] == "verify" and "record" not in stages
    assert not (tmp_path / "state/home-trial/current.json").exists()


@pytest.mark.parametrize("container_ids", ["", "scratch-id\nbridge-id\nminecraft-id\n"])
def test_apply_rejects_missing_services_after_compose_reports_success(tmp_path, container_ids):
    order = _order(tmp_path)
    prepared = prepare_interface_deployment(order)
    base_runner, _ = _runner(prepared)

    def runner(command, timeout):
        result = base_runner(command, timeout)
        if "ps" in command:
            return subprocess.CompletedProcess(command, 0, container_ids if result.stdout else "", "")
        return result

    with pytest.raises(DeploymentInterfaceError, match="deployment_runtime_unmanaged"):
        apply_interface_order(
            order, state_root=tmp_path / "state", runner=runner,
            artifact_fetcher=lambda _: None, port_probe=lambda *_: True,
        )
    assert not (tmp_path / "state/home-trial/current.json").exists()


def test_failed_update_preserves_the_previous_current_record(tmp_path):
    prepared, _ = _applied(tmp_path)
    current = tmp_path / "state/home-trial/current.json"
    previous = current.read_bytes()
    with prepared.order_path.open("a") as order:
        order.write('\n[[notices]]\nheading="Changed"\nbody="New notice"\n')
    desired = prepare_interface_deployment(prepared.order_path, artifact_store=tmp_path / "artifacts")
    runner, _ = _runner(desired, existing=True, volume_exists=True, mutation=_restarting_caddy)
    with pytest.raises(DeploymentInterfaceError, match="deployment_runtime_restarting"):
        apply_interface_order(
            prepared.order_path, artifact_store=tmp_path / "artifacts", state_root=tmp_path / "state",
            runner=runner, artifact_fetcher=lambda _: None, port_probe=lambda *_: True,
        )
    assert current.read_bytes() == previous


def _wirescope_order(tmp_path):
    order = _order(tmp_path)
    order.write_text(order.read_text().replace("home-alpha-full@2", "home-alpha-full@3").replace(
        'bridge_url = "wss://home.example.org:8444/"',
        'bridge_url = "wss://home.example.org:8444/"\nwirescope_url = "https://home.example.org:8445/"',
    ))
    return order


def _wirescope_fixture(tmp_path):
    data_root = tmp_path / "data"
    shutil.copytree(str(files("mc_remote_stack").joinpath("data")), data_root)
    resource = data_root / "preset_registry/home-alpha-full/3/preset.toml"
    preset = tomlkit.parse(resource.read_text())
    assets = {"index.html": b"<html>WireScope fixture</html>", "assets/icon.png": b"\x89PNG\r\n\x1a\n"}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        for name, content in assets.items():
            zipped.writestr(name, content)
    archive_source = archive.getvalue()
    manifest = {
        "manifest_schema": "mcremote.wirescope.app-manifest", "manifest_version": 1,
        "archive": {"file": "wirescope-app.zip", "format": "zip", "format_version": 1,
                    "sha256": hashlib.sha256(archive_source).hexdigest()},
        "protocols": {"observer_schema": {"name": "mcremote.observer", "version": 1},
                      "observer_session": 1, "scratch_handoff": 1, "station_attach": 1},
        "assets": [{"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                   for name, content in assets.items()],
    }
    store = tmp_path / "artifacts/sha256"
    store.mkdir(parents=True)
    for artifact, source in (("wirescope-zip", archive_source),
                             ("wirescope-manifest", json.dumps(manifest).encode())):
        digest = hashlib.sha256(source).hexdigest()
        next(item for item in preset["artifacts"] if item["id"] == artifact)["sha256"] = digest
        (store / digest).write_bytes(source)
    resource.write_text(tomlkit.dumps(preset))
    return _wirescope_order(tmp_path), data_root, assets


def test_home_wirescope_revision_locks_the_same_release_and_separate_origin(tmp_path):
    prepared = prepare_interface_deployment(_wirescope_order(tmp_path))
    public = {a["id"]: a for a in load_preset("public-web-paper@11").data["artifacts"]}
    assert {a["id"]: a for a in prepared.lock["artifacts"]} == public
    assert set(prepared.compose["services"]) == {"caddy", "scratch", "bridge", "minecraft"}
    assert prepared.lock["runtime_config"]["wirescope_url"] == "https://home.example.org:8445/"
    assert "127.0.0.1:8445:8445/tcp" in prepared.compose["services"]["caddy"]["ports"]
    assert {"type": "bind", "source": "./wirescope", "target": "/srv/wirescope", "read_only": True} in (
        prepared.compose["services"]["caddy"]["volumes"]
    )
    assert 'Referrer-Policy "strict-origin-when-cross-origin"' in prepared.files["Caddyfile"]
    assert 'Cross-Origin-Opener-Policy "unsafe-none"' in prepared.files["Caddyfile"]


def test_home_wirescope_compose_file_publishes_the_port_and_static_directory(tmp_path):
    prepared = prepare_interface_deployment(_wirescope_order(tmp_path))
    deployed = yaml.safe_load(prepared.files["compose.yaml"])
    assert "127.0.0.1:8445:8445/tcp" in deployed["services"]["caddy"]["ports"]
    assert {"type": "bind", "source": "./wirescope", "target": "/srv/wirescope", "read_only": True} in (
        deployed["services"]["caddy"]["volumes"]
    )


@pytest.mark.parametrize("url", [None, "https://home.example.org:8443/"])
def test_home_wirescope_requires_a_configured_separate_origin(tmp_path, url):
    order = _wirescope_order(tmp_path)
    source = order.read_text()
    source = source.replace('wirescope_url = "https://home.example.org:8445/"\n',
                            "" if url is None else f'wirescope_url = "{url}"\n')
    order.write_text(source)
    with pytest.raises(DeploymentInterfaceError, match="wirescope_surface_invalid"):
        prepare_interface_deployment(order)


def test_home_apply_publishes_verified_wirescope_assets_and_can_upgrade_existing_world(tmp_path):
    order, data_root, assets = _wirescope_fixture(tmp_path)
    order.write_text(order.read_text().replace("home-alpha-full@3", "home-alpha-full@2"))
    previous = prepare_interface_deployment(order, data_root=data_root, artifact_store=tmp_path / "artifacts")
    runner, _ = _runner(previous)
    created = apply_interface_order(
        order, data_root=data_root, artifact_store=tmp_path / "artifacts", state_root=tmp_path / "state",
        runner=runner, artifact_fetcher=lambda _: None, port_probe=lambda *_: True,
    )
    order.write_text(order.read_text().replace("home-alpha-full@2", "home-alpha-full@3"))
    requested = prepare_interface_deployment(order, data_root=data_root, artifact_store=tmp_path / "artifacts")
    runner, _ = _runner(requested, existing=True, volume_exists=True)
    updated = apply_interface_order(
        order, data_root=data_root, artifact_store=tmp_path / "artifacts", state_root=tmp_path / "state",
        runner=runner, artifact_fetcher=lambda _: None, port_probe=lambda *_: True,
    )
    assert created.mode == "create" and updated.mode == "update"
    assert requested.compose["volumes"] == previous.compose["volumes"]
    for name, content in assets.items():
        path = updated.render_root / "wirescope" / name
        assert path.read_bytes() == content
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert (updated.render_root / "wirescope/wirescope-app.manifest.json").is_file()


def test_home_wirescope_tampered_archive_fails_before_start_and_current(tmp_path):
    order, data_root, _ = _wirescope_fixture(tmp_path)
    prepared = prepare_interface_deployment(order, data_root=data_root, artifact_store=tmp_path / "artifacts")
    digest = next(a["sha256"] for a in prepared.lock["artifacts"] if a["id"] == "wirescope-zip")
    (tmp_path / "artifacts/sha256" / digest).write_bytes(b"tampered")
    runner, calls = _runner(prepared)
    with pytest.raises(DeploymentInterfaceError, match="artifact_tampered"):
        apply_interface_order(
            order, data_root=data_root, artifact_store=tmp_path / "artifacts", state_root=tmp_path / "state",
            runner=runner, artifact_fetcher=lambda _: None, port_probe=lambda *_: True,
        )
    assert not any("up" in command for command in calls)
    assert not (tmp_path / "state/home-trial/current.json").exists()


def test_home_wirescope_checks_its_port_before_fetch(tmp_path):
    prepared = prepare_interface_deployment(_wirescope_order(tmp_path))
    runner, _ = _runner(prepared)
    fetched = []
    with pytest.raises(DeploymentInterfaceError, match="deployment_port_in_use.*8445"):
        apply_interface_order(
            prepared.order_path, state_root=tmp_path / "state", runner=runner,
            artifact_fetcher=lambda _: fetched.append(True), port_probe=lambda _address, port: port != 8445,
        )
    assert fetched == []


@pytest.mark.parametrize("failure", [None, "doctor_wirescope_header_mismatch", "doctor_wirescope_content_mismatch"])
def test_home_doctor_checks_wirescope_public_content_and_handoff_headers(tmp_path, failure):
    order, data_root, assets = _wirescope_fixture(tmp_path)
    prepared = prepare_interface_deployment(order, data_root=data_root, artifact_store=tmp_path / "artifacts")
    runner, _ = _runner(prepared)
    apply_interface_order(
        order, data_root=data_root, artifact_store=tmp_path / "artifacts", state_root=tmp_path / "state",
        runner=runner, artifact_fetcher=lambda _: None, port_probe=lambda *_: True,
    )
    calls = []

    def probe(scratch_url, wirescope_url, **kwargs):
        calls.append((scratch_url, wirescope_url, kwargs))
        if failure:
            raise DoctorContractError(failure, wirescope_url, "fixture mismatch")

    runner, _ = _runner(prepared, existing=True, volume_exists=True)

    def doctor():
        return doctor_interface_deployment(
            "home-trial", state_root=tmp_path / "state", data_root=data_root, runner=runner,
            runtime_probe=lambda *_: prepared.lock["runtime_config"], bridge_upstream_probe=lambda *_: None,
            hello_probe=lambda *_: SimpleNamespace(status="auth-required"), wirescope_probe=probe,
        )

    if failure:
        with pytest.raises(DeploymentInterfaceError, match=failure):
            doctor()
    else:
        assert doctor().wirescope_status == "current"
    assert calls == [("https://home.example.org:8443/", "https://home.example.org:8445/",
                      {"expected_index_sha256": hashlib.sha256(assets["index.html"]).hexdigest(), "timeout": 5})]


def _standard_order(order):
    order.write_text(order.read_text().replace("home-alpha-full@3", "home-alpha-full@4").replace(
        '[surfaces]', '[surfaces]\nbedrock_bind_address = "192.0.2.30"',
    ))
    return order


def _standard_fixture(tmp_path):
    order, data_root, assets = _wirescope_fixture(tmp_path)
    resource = data_root / "preset_registry/home-alpha-full/4/preset.toml"
    preset = tomlkit.parse(resource.read_text())
    old = tomlkit.parse((data_root / "preset_registry/home-alpha-full/3/preset.toml").read_text())
    for artifact in preset["artifacts"]:
        if artifact["id"] in {"wirescope-zip", "wirescope-manifest"}:
            artifact["sha256"] = next(a["sha256"] for a in old["artifacts"] if a["id"] == artifact["id"])
    resource.write_text(tomlkit.dumps(preset))
    return _standard_order(order), data_root, assets


def _standard_runner(prepared, *, plugin_failure=None, existing=False, mutation=None):
    base, calls = _runner(prepared, existing=existing, volume_exists=existing, mutation=mutation)
    artifacts = {a["filename"]: a for a in prepared.lock["artifacts"] if a["kind"] == "https-file"}

    def run(command, timeout):
        if "sha256sum" in command:
            calls.append(command)
            name = Path(command[-1]).name
            if plugin_failure == "missing":
                return subprocess.CompletedProcess(command, 1, "", "No such file")
            digest = "0" * 64 if plugin_failure == "changed" else artifacts[name]["sha256"]
            return subprocess.CompletedProcess(command, 0, f"{digest}  {command[-1]}\n", "")
        return base(command, timeout)

    return run, calls


def test_home_standard_set_pins_and_mounts_all_five_plugins_in_the_written_compose(tmp_path):
    prepared = prepare_interface_deployment(_standard_order(_wirescope_order(tmp_path)))
    previous = prepare_interface_deployment(_wirescope_order(tmp_path))
    assert prepared.compose["volumes"] == previous.compose["volumes"]
    assert set(prepared.compose["services"]) == {"caddy", "scratch", "bridge", "minecraft"}
    compose = yaml.safe_load(prepared.files["compose.yaml"])
    minecraft = compose["services"]["minecraft"]
    assert "192.0.2.30:19132:19132/udp" in minecraft["ports"]
    roles = {c["role"]: c for c in prepared.lock["components"]}
    for role in ("luckperms-plugin", "geyser-plugin", "floodgate-plugin", "viaversion-plugin", "viabackwards-plugin"):
        artifact = next(a for a in prepared.lock["artifacts"] if a["id"] == roles[role]["artifact"])
        assert "/latest/" not in artifact["origin"]
        assert any(m["target"] == f"/plugins/{artifact['filename']}" and m["read_only"]
                   and m["source"].endswith(artifact["sha256"]) for m in minecraft["volumes"])


def test_standard_set_requires_the_explicit_bedrock_host_interface(tmp_path):
    order = _wirescope_order(tmp_path)
    order.write_text(order.read_text().replace("home-alpha-full@3", "home-alpha-full@4"))
    with pytest.raises(DeploymentInterfaceError, match="bedrock_bind_address_required"):
        prepare_interface_deployment(order)


def test_standard_set_requires_luckperms_in_its_preset(tmp_path):
    order, data_root, _ = _standard_fixture(tmp_path)
    resource = data_root / "preset_registry/home-alpha-full/4/preset.toml"
    preset = tomlkit.parse(resource.read_text())
    preset["components"] = [c for c in preset["components"] if c["role"] != "luckperms-plugin"]
    resource.write_text(tomlkit.dumps(preset))
    with pytest.raises(DeploymentInterfaceError, match="preset_component_invalid.*luckperms-plugin"):
        prepare_interface_deployment(order, data_root=data_root)


@pytest.mark.parametrize("failure", ["missing", "changed"])
def test_standard_plugin_verification_failure_preserves_the_previous_current(tmp_path, failure):
    _, previous = _applied(tmp_path)
    current = tmp_path / "state/home-trial/current.json"
    saved = current.read_bytes()
    order, data_root, _ = _standard_fixture(tmp_path)
    prepared = prepare_interface_deployment(order, data_root=data_root, artifact_store=tmp_path / "artifacts")
    runner, _ = _standard_runner(prepared, existing=True, plugin_failure=failure)
    stages = []
    with pytest.raises(DeploymentInterfaceError, match="deployment_plugin_(missing|mismatch)"):
        apply_interface_order(
            order, data_root=data_root, artifact_store=tmp_path / "artifacts", state_root=tmp_path / "state",
            runner=runner, artifact_fetcher=lambda _: None, port_probe=lambda *_: True,
            udp_port_probe=lambda *_: True, progress=stages.append,
        )
    assert stages[-1] == "verify" and "record" not in stages
    assert current.read_bytes() == saved
    assert previous.mode == "create"


def test_standard_doctor_reports_plugin_hashes_without_claiming_account_permissions(tmp_path):
    order, data_root, _ = _standard_fixture(tmp_path)
    prepared = prepare_interface_deployment(order, data_root=data_root, artifact_store=tmp_path / "artifacts")
    runner, _ = _standard_runner(prepared)
    apply_interface_order(
        order, data_root=data_root, artifact_store=tmp_path / "artifacts", state_root=tmp_path / "state",
        runner=runner, artifact_fetcher=lambda _: None, port_probe=lambda *_: True, udp_port_probe=lambda *_: True,
    )
    runner, _ = _standard_runner(prepared, existing=True)
    result = doctor_interface_deployment(
        "home-trial", data_root=data_root, state_root=tmp_path / "state", runner=runner,
        runtime_probe=lambda *_: prepared.lock["runtime_config"], bridge_upstream_probe=lambda *_: None,
        hello_probe=lambda *_: SimpleNamespace(status="auth-required"), wirescope_probe=lambda *_a, **_k: None,
    )
    assert result.plugins_status == "current"


def test_standard_udp_preflight_does_not_treat_an_owned_tcp_port_as_udp(tmp_path):
    _applied(tmp_path)
    order = _standard_order(_wirescope_order(tmp_path))
    prepared = prepare_interface_deployment(order)

    def mutate(service, record):
        if service == "minecraft":
            record["NetworkSettings"]["Ports"].pop("19132/udp")
            record["NetworkSettings"]["Ports"]["19132/tcp"] = [{"HostIp": "192.0.2.30", "HostPort": "19132"}]

    runner, calls = _standard_runner(prepared, existing=True, mutation=mutate)
    with pytest.raises(DeploymentInterfaceError, match="deployment_port_in_use.*19132/udp"):
        apply_interface_order(
            order, runner=runner, port_probe=lambda *_: True, udp_port_probe=lambda *_: False,
            state_root=tmp_path / "state", artifact_fetcher=lambda _: pytest.fail("must stop before fetch"),
        )
    assert not any("up" in command for command in calls)


def test_udp_port_probe_detects_an_existing_udp_listener_even_when_tcp_is_free():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        assert interface_module._default_port_probe("127.0.0.1", port)
        assert not interface_module._default_udp_port_probe("127.0.0.1", port)
