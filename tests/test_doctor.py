import json
from pathlib import Path

import pytest

from mc_remote_stack.cli import main
from mc_remote_stack.doctor import (
    DoctorContractError,
    ProtocolHelloResult,
    TomlDoctorResult,
    doctor_toml_project,
    probe_protocol_hello,
)

from .test_toml_apply import FakeDocker, _prepared_project, _result


def _docker(*arguments: str) -> tuple[str, ...]:
    return ("docker", "--context", "default", *arguments)


def _compose_base(output: Path) -> tuple[str, ...]:
    return _docker(
        "compose",
        "--ansi",
        "never",
        "--project-directory",
        str(output.resolve()),
        "--file",
        str((output / "compose.yaml").resolve()),
    )


def _managed_container(lock: dict, *, health: str = "healthy") -> dict:
    return {
        "Id": "container-current",
        "Config": {
            "Labels": {
                "com.docker.compose.project": "home",
                "com.docker.compose.service": "minecraft",
                "io.mc-remote.deployment": "home",
                "io.mc-remote.environment": "home-beta",
                "io.mc-remote.world": "home-beta-world",
                "io.mc-remote.lock": lock["lock_identity"],
            }
        },
        "State": {
            "Running": True,
            "Health": {"Status": health},
        },
        "NetworkSettings": {
            "Ports": {
                "25565/tcp": [{"HostIp": "127.0.0.1", "HostPort": "25565"}],
                "25575/tcp": [{"HostIp": "127.0.0.1", "HostPort": "25575"}],
            }
        },
    }


def _managed_volume(lock: dict) -> dict:
    return {
        "Name": "home-beta-minecraft-data",
        "Driver": "local",
        "Labels": {
            "io.mc-remote.owner": "mcrctl",
            "io.mc-remote.deployment": "home",
            "io.mc-remote.environment": "home-beta",
            "io.mc-remote.world": "home-beta-world",
            "io.mc-remote.created-by-lock": lock["lock_identity"],
        },
    }


def _doctor_responses(output: Path, lock: dict, *, health: str = "healthy") -> dict:
    base = _compose_base(output)
    return {
        ("docker", "context", "inspect", "default"): [
            _result(
                ("docker",),
                stdout=json.dumps(
                    [{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}]
                )
                + "\n",
            )
        ],
        _docker("version", "--format", "{{.Server.Version}}"): [
            _result(("docker",), stdout="29.1.3\n")
        ],
        _docker("compose", "version", "--short"): [
            _result(("docker",), stdout="2.40.3\n")
        ],
        base + ("config", "--quiet"): [_result(base)],
        _docker(
            "ps",
            "--all",
            "--quiet",
            "--filter",
            "label=com.docker.compose.project=home",
        ): [_result(("docker",), stdout="container-current\n")],
        _docker("inspect", "container-current"): [
            _result(
                ("docker",),
                stdout=json.dumps([_managed_container(lock, health=health)]) + "\n",
            )
        ],
        _docker("volume", "inspect", "home-beta-minecraft-data"): [
            _result(
                ("docker",),
                stdout=json.dumps([_managed_volume(lock)]) + "\n",
            )
        ],
    }


def test_doctor_checks_current_render_runtime_and_protocol_without_mutation(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    runner = FakeDocker(_doctor_responses(output, lock))
    hello_calls: list[tuple[str, int, str, str, str, int]] = []

    def hello_probe(
        address: str,
        port: int,
        protocol: str,
        minecraft_version: str,
        world: str,
        timeout: int,
    ) -> ProtocolHelloResult:
        hello_calls.append((address, port, protocol, minecraft_version, world, timeout))
        return ProtocolHelloResult(
            status="ok",
            protocol="21.0.0",
            minecraft_version="1.21.11",
        )

    result = doctor_toml_project(
        project,
        output,
        docker_context="default",
        data_root=data_root,
        timeout=5,
        runner=runner,
        hello_probe=hello_probe,
    )

    assert result == TomlDoctorResult(
        deployment="home",
        environment="home-beta",
        lock_identity=lock["lock_identity"],
        docker_context="default",
        runtime_status="healthy",
        network_scope="loopback",
        bind_address="127.0.0.1",
        java_port=25565,
        mcremote_port=25575,
        protocol_status="ok",
        protocol="21.0.0",
        minecraft_version="1.21.11",
        compatibility_status="unverified",
    )
    assert hello_calls == [
        ("127.0.0.1", 25575, "21.0.0", "1.21.11", "home-beta-world", 5)
    ]
    mutation_words = {"pull", "up", "down", "create", "rm", "start", "stop", "restart"}
    assert all(not mutation_words.intersection(command) for command, _ in runner.calls)


def test_doctor_rejects_unhealthy_runtime_before_protocol_probe(tmp_path: Path) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    runner = FakeDocker(_doctor_responses(output, lock, health="unhealthy"))
    hello_called = False

    def hello_probe(*_args: object) -> ProtocolHelloResult:
        nonlocal hello_called
        hello_called = True
        raise AssertionError("protocol probe must not run for an unhealthy container")

    with pytest.raises(DoctorContractError) as exc_info:
        doctor_toml_project(
            project,
            output,
            docker_context="default",
            data_root=data_root,
            runner=runner,
            hello_probe=hello_probe,
        )

    assert exc_info.value.reason == "doctor_runtime_unhealthy"
    assert hello_called is False


def test_doctor_rejects_live_port_drift_before_protocol_probe(tmp_path: Path) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    responses = _doctor_responses(output, lock)
    container = _managed_container(lock)
    container["NetworkSettings"]["Ports"]["25575/tcp"][0]["HostIp"] = "0.0.0.0"
    responses[_docker("inspect", "container-current")] = [
        _result(("docker",), stdout=json.dumps([container]) + "\n")
    ]
    runner = FakeDocker(responses)

    with pytest.raises(DoctorContractError) as exc_info:
        doctor_toml_project(
            project,
            output,
            docker_context="default",
            data_root=data_root,
            runner=runner,
            hello_probe=lambda *_args: pytest.fail("hello probe must not run"),
        )

    assert exc_info.value.reason == "doctor_network_mismatch"


def test_doctor_rejects_noncurrent_render_before_contacting_docker(tmp_path: Path) -> None:
    project, data_root, output, _ = _prepared_project(tmp_path)
    (output / "compose.yaml").write_text(
        (output / "compose.yaml")
        .read_text(encoding="utf-8")
        .replace("restart: unless-stopped", "restart: no"),
        encoding="utf-8",
    )
    runner = FakeDocker({})

    with pytest.raises(DoctorContractError) as exc_info:
        doctor_toml_project(
            project,
            output,
            docker_context="default",
            data_root=data_root,
            runner=runner,
        )

    assert exc_info.value.reason == "render_output_tampered"
    assert runner.calls == []


def test_doctor_rejects_remote_context_before_daemon_contact(tmp_path: Path) -> None:
    project, data_root, output, _ = _prepared_project(tmp_path)
    command = ("docker", "context", "inspect", "remote")
    runner = FakeDocker(
        {
            command: [
                _result(
                    command,
                    stdout=json.dumps(
                        [{"Endpoints": {"docker": {"Host": "ssh://private-host"}}}]
                    )
                    + "\n",
                )
            ]
        }
    )

    with pytest.raises(DoctorContractError) as exc_info:
        doctor_toml_project(
            project,
            output,
            docker_context="remote",
            data_root=data_root,
            runner=runner,
        )

    assert exc_info.value.reason == "docker_context_not_local"
    assert runner.calls == [(command, 30)]


class _FakeSocket:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.sent = b""

    def __enter__(self) -> "_FakeSocket":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def sendall(self, value: bytes) -> None:
        self.sent += value

    def recv(self, _size: int) -> bytes:
        value, self.response = self.response, b""
        return value


def test_protocol_hello_uses_a_real_lf_and_validates_the_public_response() -> None:
    response = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocol": "21.0.0",
            "mc_version": "1.21.11",
            "supported_mc_versions": ["1.21.11"],
            "catalogHash": None,
            "world_constants": {"y_sea": 63},
            "world": "home-beta-world",
            "origin": [200, 0, 200],
            "session": "must-not-be-returned",
            "player": "must-not-be-returned",
        },
    }
    fake_socket = _FakeSocket(
        json.dumps(response, separators=(",", ":")).encode() + b"\n"
    )

    def connect(address: tuple[str, int], timeout: int) -> _FakeSocket:
        assert address == ("127.0.0.1", 25575)
        assert timeout == 5
        return fake_socket

    result = probe_protocol_hello(
        "127.0.0.1",
        25575,
        "21.0.0",
        "1.21.11",
        "home-beta-world",
        5,
        connector=connect,
    )

    assert result == ProtocolHelloResult(
        status="ok",
        protocol="21.0.0",
        minecraft_version="1.21.11",
    )
    assert fake_socket.sent.endswith(b"\n")
    assert not fake_socket.sent.endswith(b"\\n")
    sent = json.loads(fake_socket.sent)
    assert sent == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "hello",
        "params": {"protocol": "21.0.0"},
    }


def test_protocol_hello_accepts_auth_required_as_a_responsive_server() -> None:
    response = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": -32001,
            "message": "Authentication required",
            "data": {"reason": "auth_required"},
        },
    }
    fake_socket = _FakeSocket(json.dumps(response).encode() + b"\n")

    result = probe_protocol_hello(
        "127.0.0.1",
        25575,
        "21.0.0",
        "1.21.11",
        "home-beta-world",
        5,
        connector=lambda _address, _timeout: fake_socket,
    )

    assert result == ProtocolHelloResult(
        status="auth-required",
        protocol=None,
        minecraft_version=None,
    )


def test_protocol_hello_rejects_a_lock_mismatch_without_echoing_the_response() -> None:
    response = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocol": "22.0.0",
            "mc_version": "1.21.11",
            "supported_mc_versions": ["1.21.11"],
            "catalogHash": None,
            "world_constants": {"y_sea": 63},
            "world": "home-beta-world",
            "origin": [200, 0, 200],
            "session": "do-not-leak-this-session",
        },
    }
    fake_socket = _FakeSocket(json.dumps(response).encode() + b"\n")

    with pytest.raises(DoctorContractError) as exc_info:
        probe_protocol_hello(
            "127.0.0.1",
            25575,
            "21.0.0",
            "1.21.11",
            "home-beta-world",
            5,
            connector=lambda _address, _timeout: fake_socket,
        )

    assert exc_info.value.reason == "protocol_hello_mismatch"
    assert "do-not-leak" not in str(exc_info.value)


def test_cli_doctor_uses_simple_local_defaults_and_does_not_echo_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "home-beta"
    project.mkdir()
    (project / "mc-remote.toml").write_text("schema_version = 1\n")
    received: dict[str, object] = {}

    def fake_doctor(
        project_root: Path,
        output: Path,
        **kwargs: object,
    ) -> TomlDoctorResult:
        received.update({"project": project_root, "output": output, **kwargs})
        return TomlDoctorResult(
            deployment="home",
            environment="home-beta",
            lock_identity=f"sha256:{'1' * 64}",
            docker_context="default",
            runtime_status="healthy",
            network_scope="loopback",
            bind_address="127.0.0.1",
            java_port=25565,
            mcremote_port=25575,
            protocol_status="ok",
            protocol="21.0.0",
            minecraft_version="1.21.11",
            compatibility_status="unverified",
        )

    monkeypatch.setattr("mc_remote_stack.cli.doctor_toml_project", fake_doctor)

    assert main(["doctor", "--project", str(project)]) == 0

    assert received["project"] == project
    assert received["output"] == project / "generated"
    assert received["docker_context"] == "default"
    output = capsys.readouterr().out
    assert "OK doctor runtime=healthy deployment=home environment=home-beta" in output
    assert "OK doctor network=loopback bind=127.0.0.1 java-port=25565 mcremote-port=25575" in output
    assert "OK doctor protocol=21.0.0 mc-version=1.21.11 auth=not-required" in output
    assert "WARN doctor compatibility=unverified" in output
    assert "token" not in output
    assert "session" not in output
    assert "player" not in output
