import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from mc_remote_stack.apply import (
    ApplyContractError,
    TomlApplyResult,
    _inspect_managed_volume,
    _safe_command_failure_detail,
    _validate_bootstrap_contract,
    apply_toml_project,
)
from mc_remote_stack.cli import main
from mc_remote_stack.render import render_toml_project
from mc_remote_stack.resolver import load_lock

from .test_toml_render import _render_fixture


class FakeDocker:
    def __init__(
        self,
        responses: dict[tuple[str, ...], list[subprocess.CompletedProcess[str]]],
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], int]] = []

    def __call__(
        self,
        command: list[str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        key = tuple(command)
        self.calls.append((key, timeout))
        available = self.responses.get(key)
        if not available:
            raise AssertionError(f"unexpected command: {command!r}")
        return available.pop(0)


def _result(
    command: tuple[str, ...],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def _docker(*arguments: str) -> tuple[str, ...]:
    return ("docker", "--context", "default", *arguments)


@pytest.mark.parametrize(
    ("stderr", "expected", "hidden"),
    [
        (
            "\x1b[31mfailed token=supersecret trailing-value\x1b[0m\n",
            "failed token=<redacted>",
            "supersecret",
        ),
        (
            "denied https://alice:swordfish@example.invalid/image\n",
            "denied https://<redacted>@example.invalid/image",
            "swordfish",
        ),
        (
            'response {"authorization": "Bearer abc", "status": 401}\n',
            'response {"authorization": <redacted>, "status": 401}',
            "Bearer abc",
        ),
    ],
)
def test_command_failure_detail_redacts_sensitive_values(
    stderr: str,
    expected: str,
    hidden: str,
) -> None:
    detail = _safe_command_failure_detail(
        _result(
            ("docker",),
            returncode=1,
            stdout="less useful stdout\n",
            stderr=stderr,
        )
    )

    assert detail == expected
    assert hidden not in detail
    assert "less useful stdout" not in detail


def _prepared_project(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    project, data_root, _ = _render_fixture(tmp_path)
    output = project / "generated"
    render_toml_project(project, output, data_root=data_root)
    return project, data_root, output, load_lock(project, data_root=data_root)


def test_bootstrap_still_requires_explicit_eula_acceptance(tmp_path: Path) -> None:
    _project, _data_root, _output, lock = _prepared_project(tmp_path)
    lock["agreements"]["minecraft_eula"] = False

    with pytest.raises(ApplyContractError) as exc_info:
        _validate_bootstrap_contract(
            lock,
            allow_unverified=True,
            allow_eol=False,
        )

    assert exc_info.value.reason == "minecraft_eula_not_accepted"


def test_bootstrap_does_not_require_retired_compatibility_acknowledgement(tmp_path: Path) -> None:
    _project, _data_root, _output, lock = _prepared_project(tmp_path)
    lock["acknowledgements"]["allow_unverified"] = False

    _validate_bootstrap_contract(lock, allow_unverified=False, allow_eol=False)


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


def _world_volume(lock: dict) -> str:
    return next(
        assignment["identity"]
        for assignment in lock["runtime"]["volumes"]
        if assignment["role"] == "minecraft-data"
    )


def _volume_names(lock: dict) -> list[str]:
    return [assignment["identity"] for assignment in lock["runtime"]["volumes"]]


def _service_ids(lock: dict) -> list[str]:
    return [service["id"] for service in lock["render_plan"]["services"]]


def _volume_create(lock: dict, volume: str) -> tuple[str, ...]:
    labels = _managed_volume(lock, volume)["Labels"]
    command = ["volume", "create", "--driver", "local"]
    for key, value in labels.items():
        command.extend(["--label", f"{key}={value}"])
    return _docker(*command, volume)


def _project_ps(lock: dict) -> tuple[str, ...]:
    return _docker(
        "ps",
        "--all",
        "--quiet",
        "--filter",
        f"label=com.docker.compose.project={lock['deployment']['name']}",
    )


def _managed_volume(lock: dict, name: str | None = None) -> dict:
    name = name or _world_volume(lock)
    return {
        "Name": name,
        "Driver": "local",
        "Labels": {
            "io.mc-remote.owner": "mcrctl",
            "io.mc-remote.deployment": lock["deployment"]["name"],
            "io.mc-remote.environment": lock["environment"]["identity"],
            "io.mc-remote.world": lock["world"]["identity"],
            "io.mc-remote.created-by-lock": lock["lock_identity"],
        },
    }


def _managed_container(
    lock: dict,
    output: Path,
    *,
    service: str = "minecraft",
    running: bool = True,
) -> dict:
    return {
        "Id": f"container-{service}",
        "Config": {
            "Labels": {
                "com.docker.compose.project": lock["deployment"]["name"],
                "com.docker.compose.service": service,
                "com.docker.compose.project.config_files": str(
                    output.resolve() / "compose.yaml"
                ),
                "com.docker.compose.project.working_dir": str(
                    output.resolve()
                ),
                "io.mc-remote.deployment": lock["deployment"]["name"],
                "io.mc-remote.environment": lock["environment"]["identity"],
                "io.mc-remote.world": lock["world"]["identity"],
                "io.mc-remote.lock": lock["lock_identity"],
            }
        },
        "State": {"Running": running},
    }


def _running_project(
    responses: dict[tuple[str, ...], list[subprocess.CompletedProcess[str]]],
    lock: dict,
    output: Path,
    containers: list[dict] | None = None,
) -> None:
    """Answer inspections for an existing project with every locked service."""

    for container in containers or [
        _managed_container(lock, output, service=service) for service in _service_ids(lock)
    ]:
        responses[_docker("inspect", container["Id"])] = [
            _result(("docker",), stdout=json.dumps([container]) + "\n")
        ]
    for volume in _volume_names(lock):
        responses[_docker("volume", "inspect", volume)] = [
            _result(("docker",), stdout=json.dumps([_managed_volume(lock, volume)]) + "\n")
        ]


def _bootstrap_responses(
    output: Path,
    lock: dict,
    *,
    up_result: subprocess.CompletedProcess[str] | None = None,
) -> dict[tuple[str, ...], list[subprocess.CompletedProcess[str]]]:
    """Answer one fresh bootstrap of every locked service and volume."""

    base = _compose_base(output)
    services = _service_ids(lock)
    responses = _read_only_responses(output, lock=lock)
    responses[base + ("pull", "--policy", "always", "--quiet", *services)] = [_result(base)]
    responses[
        base
        + ("up", "--detach", "--wait", "--wait-timeout", "300", "--no-build", "--pull", "never", *services)
    ] = [up_result or _result(base)]
    responses[_project_ps(lock)].append(
        _result(("docker",), stdout="".join(f"container-{service}\n" for service in services))
    )
    for volume in _volume_names(lock):
        responses[_volume_create(lock, volume)] = [_result(("docker",), stdout=f"{volume}\n")]
    _running_project(responses, lock, output)
    return responses


def _read_only_responses(
    output: Path,
    *,
    lock: dict | None = None,
    project_containers: list[str] | None = None,
    volume_names: list[str] | None = None,
) -> dict[tuple[str, ...], list[subprocess.CompletedProcess[str]]]:
    base = _compose_base(output)
    compose_project = lock["deployment"]["name"] if lock else "home"
    expected_volumes = (
        [assignment["identity"] for assignment in lock["runtime"]["volumes"]]
        if lock
        else ["home-beta-minecraft-data"]
    )
    ports = (
        [str(lock["network"]["java_port"]), str(lock["network"]["mcremote_port"])]
        if lock
        else ["25565", "25575"]
    )
    if lock and "caddy" in _service_ids(lock):
        ports = ["80", "443", *ports]
    container_stdout = "".join(f"{value}\n" for value in (project_containers or []))
    commands = {
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
            _result(("docker",), stdout="28.0.0\n")
        ],
        _docker("compose", "version", "--short"): [
            _result(("docker",), stdout="2.39.0\n")
        ],
        base + ("config", "--quiet"): [_result(base)],
        _docker(
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={compose_project}",
        ): [_result(("docker",), stdout=container_stdout)],
    }
    existing_volumes = set(volume_names or [])
    for volume in expected_volumes:
        volume_stdout = f"{volume}\n" if volume in existing_volumes else ""
        escaped_volume = volume.replace("-", "\\-")
        commands[
            _docker(
                "volume",
                "ls",
                "--quiet",
                "--filter",
                f"name=^{escaped_volume}$",
            )
        ] = [_result(("docker",), stdout=volume_stdout)]
    for port in ports:
        commands[
            _docker(
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"publish={port}",
            )
        ] = [_result(("docker",))]
    return commands


def test_bootstrap_apply_is_bound_to_current_lock_and_verified_render(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    base = _compose_base(output)
    runner = FakeDocker(_bootstrap_responses(output, lock))
    probed: list[tuple[str, int]] = []
    progress: list[str] = []

    result = apply_toml_project(
        project,
        output,
        expected_lock_identity=lock["lock_identity"],
        docker_context="default",
        data_root=data_root,
        bootstrap=True,
        confirmed=True,
        allow_unverified=True,
        runner=runner,
        port_probe=lambda address, port: probed.append((address, port)),
        progress=progress.append,
    )

    assert result.status == "created"
    assert result.lock_identity == lock["lock_identity"]
    assert result.compose_project == "official-vps"
    assert result.service == "caddy,scratch,bridge,minecraft"
    assert sorted(result.volume.split(",")) == sorted(_volume_names(lock))
    # Every host port the canonical render publishes, including the public edge.
    assert probed == [
        ("0.0.0.0", 80),
        ("0.0.0.0", 443),
        ("0.0.0.0", 25565),
        ("0.0.0.0", 25575),
    ]
    assert progress == [
        "verify-render",
        "validate-lock",
        "docker-preflight",
        "runtime-preflight",
        "check-ports",
        "pull-images",
        "prepare-volumes",
        "start-services-and-wait timeout=300",
        "post-check",
        "complete",
    ]
    commands = [command for command, _ in runner.calls]
    pull = base + ("pull", "--policy", "always", "--quiet", *_service_ids(lock))
    assert all(
        commands.index(pull) < commands.index(_volume_create(lock, volume))
        for volume in _volume_names(lock)
    )


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"bootstrap": False, "confirmed": True, "allow_unverified": True}, "bootstrap_confirmation_required"),
        ({"bootstrap": True, "confirmed": False, "allow_unverified": True}, "apply_confirmation_required"),
    ],
)
def test_apply_gates_fail_before_contacting_docker(
    tmp_path: Path,
    kwargs: dict[str, bool],
    reason: str,
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    runner = FakeDocker({})

    with pytest.raises(ApplyContractError) as exc_info:
        apply_toml_project(
            project,
            output,
            expected_lock_identity=lock["lock_identity"],
            docker_context="default",
            data_root=data_root,
            runner=runner,
            **kwargs,
        )

    assert exc_info.value.reason == reason
    assert runner.calls == []


def test_apply_rejects_expected_lock_mismatch_before_contacting_docker(
    tmp_path: Path,
) -> None:
    project, data_root, output, _ = _prepared_project(tmp_path)
    runner = FakeDocker({})

    with pytest.raises(ApplyContractError) as exc_info:
        apply_toml_project(
            project,
            output,
            expected_lock_identity=f"sha256:{'0' * 64}",
            docker_context="default",
            data_root=data_root,
            bootstrap=True,
            confirmed=True,
            allow_unverified=True,
            runner=runner,
        )

    assert exc_info.value.reason == "apply_lock_identity_mismatch"
    assert runner.calls == []


def test_apply_rejects_self_consistent_but_noncanonical_render_before_docker(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    compose_path = output / "compose.yaml"
    compose_path.write_text(
        compose_path.read_text(encoding="utf-8").replace(
            "restart: unless-stopped",
            "restart: no",
        ),
        encoding="utf-8",
    )
    manifest_path = output / "render-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for record in manifest["files"]:
        if record["path"] == "compose.yaml":
            record["sha256"] = hashlib.sha256(compose_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    runner = FakeDocker({})

    with pytest.raises(ApplyContractError) as exc_info:
        apply_toml_project(
            project,
            output,
            expected_lock_identity=lock["lock_identity"],
            docker_context="default",
            data_root=data_root,
            bootstrap=True,
            confirmed=True,
            allow_unverified=True,
            runner=runner,
        )

    assert exc_info.value.reason == "render_output_not_current"
    assert runner.calls == []


def test_apply_rejects_unmanaged_existing_volume_before_pull(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    world = _world_volume(lock)
    responses = _read_only_responses(output, lock=lock, volume_names=[world])
    responses[_docker("volume", "inspect", world)] = [
        _result(
            ("docker",),
            stdout=json.dumps([{"Name": world, "Driver": "local", "Labels": {}}]) + "\n",
        )
    ]
    runner = FakeDocker(responses)

    with pytest.raises(ApplyContractError) as exc_info:
        apply_toml_project(
            project,
            output,
            expected_lock_identity=lock["lock_identity"],
            docker_context="default",
            data_root=data_root,
            bootstrap=True,
            confirmed=True,
            allow_unverified=True,
            runner=runner,
            port_probe=lambda _address, _port: None,
        )

    assert exc_info.value.reason == "bootstrap_volume_unmanaged"
    assert all("pull" not in command for command, _ in runner.calls)


def test_apply_accepts_managed_volume_with_older_creation_lock(
    tmp_path: Path,
) -> None:
    _project, _data_root, _output, lock = _prepared_project(tmp_path)
    world = _world_volume(lock)
    volume = _managed_volume(lock, world)
    volume["Labels"]["io.mc-remote.created-by-lock"] = "sha256:" + "0" * 64
    runner = FakeDocker(
        {
            _docker("volume", "inspect", world): [
                _result(("docker",), stdout=json.dumps([volume]) + "\n")
            ]
        }
    )

    _inspect_managed_volume(
        runner,
        ["docker", "--context", "default"],
        world,
        lock,
    )


def test_apply_rejects_remote_docker_context_before_daemon_contact(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    context_command = ("docker", "context", "inspect", "remote")
    runner = FakeDocker(
        {
            context_command: [
                _result(
                    context_command,
                    stdout=json.dumps(
                        [{"Endpoints": {"docker": {"Host": "ssh://private-host"}}}]
                    )
                    + "\n",
                )
            ]
        }
    )

    with pytest.raises(ApplyContractError) as exc_info:
        apply_toml_project(
            project,
            output,
            expected_lock_identity=lock["lock_identity"],
            docker_context="remote",
            data_root=data_root,
            bootstrap=True,
            confirmed=True,
            allow_unverified=True,
            runner=runner,
        )

    assert exc_info.value.reason == "docker_context_not_local"
    assert runner.calls == [(context_command, 30)]


def test_apply_rejects_published_port_collision_before_pull(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    responses = _read_only_responses(output, lock=lock)
    responses[
        _docker(
            "ps",
            "--all",
            "--quiet",
            "--filter",
            "publish=443",
        )
    ] = [_result(("docker",), stdout="other-container\n")]
    runner = FakeDocker(responses)

    with pytest.raises(ApplyContractError) as exc_info:
        apply_toml_project(
            project,
            output,
            expected_lock_identity=lock["lock_identity"],
            docker_context="default",
            data_root=data_root,
            bootstrap=True,
            confirmed=True,
            allow_unverified=True,
            runner=runner,
            port_probe=lambda _address, _port: None,
        )

    assert exc_info.value.reason == "host_port_in_use"
    assert all("pull" not in command for command, _ in runner.calls)


def test_apply_distinguishes_port_probe_permission_from_port_collision(tmp_path: Path) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    runner = FakeDocker(_read_only_responses(output, lock=lock))

    def denied(_address: str, _port: int) -> None:
        raise PermissionError(13, "Permission denied")

    with pytest.raises(ApplyContractError) as exc_info:
        apply_toml_project(
            project,
            output,
            expected_lock_identity=lock["lock_identity"],
            docker_context="default",
            data_root=data_root,
            bootstrap=True,
            confirmed=True,
            runner=runner,
            port_probe=denied,
        )

    assert exc_info.value.reason == "host_port_probe_permission_denied"
    assert all("pull" not in command and "up" not in command for command, _ in runner.calls)


def test_failed_compose_up_rolls_back_containers_but_retains_world_volume(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    base = _compose_base(output)
    responses = _bootstrap_responses(
        output,
        lock,
        up_result=_result(
            base,
            returncode=1,
            stdout="less useful stdout",
            stderr="startup failed token=supersecret container-environment",
        ),
    )
    responses[base + ("down", "--timeout", "120")] = [_result(base)]
    runner = FakeDocker(responses)

    with pytest.raises(ApplyContractError) as exc_info:
        apply_toml_project(
            project,
            output,
            expected_lock_identity=lock["lock_identity"],
            docker_context="default",
            data_root=data_root,
            bootstrap=True,
            confirmed=True,
            allow_unverified=True,
            runner=runner,
            port_probe=lambda _address, _port: None,
        )

    assert exc_info.value.reason == "compose_up_failed"
    assert "startup failed token=<redacted>" in str(exc_info.value)
    assert "supersecret" not in str(exc_info.value)
    assert "container-environment" not in str(exc_info.value)
    assert "less useful stdout" not in str(exc_info.value)
    assert (base + ("down", "--timeout", "120"), 180) in runner.calls
    assert all(command[:5] != _docker("volume", "rm") for command, _ in runner.calls)


def test_exact_running_bootstrap_is_an_apply_noop(tmp_path: Path) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    responses = _read_only_responses(
        output,
        lock=lock,
        project_containers=[f"container-{service}" for service in _service_ids(lock)],
        volume_names=_volume_names(lock),
    )
    _running_project(responses, lock, output)
    runner = FakeDocker(responses)

    result = apply_toml_project(
        project,
        output,
        expected_lock_identity=lock["lock_identity"],
        docker_context="default",
        data_root=data_root,
        bootstrap=True,
        confirmed=True,
        allow_unverified=True,
        runner=runner,
        port_probe=lambda _address, _port: None,
    )

    assert result.status == "unchanged"
    assert all("pull" not in command and "up" not in command for command, _ in runner.calls)


def test_exact_lock_with_additional_compose_file_is_not_apply_noop(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    responses = _read_only_responses(
        output,
        lock=lock,
        project_containers=[f"container-{service}" for service in _service_ids(lock)],
        volume_names=_volume_names(lock),
    )
    containers = [
        _managed_container(lock, output, service=service) for service in _service_ids(lock)
    ]
    containers[0]["Config"]["Labels"]["com.docker.compose.project.config_files"] = (
        f"{output.resolve() / 'compose.yaml'},{tmp_path / 'recovery.override.yaml'}"
    )
    _running_project(responses, lock, output, containers)
    runner = FakeDocker(responses)

    with pytest.raises(ApplyContractError) as exc_info:
        apply_toml_project(
            project,
            output,
            expected_lock_identity=lock["lock_identity"],
            docker_context="default",
            data_root=data_root,
            bootstrap=True,
            confirmed=True,
            allow_unverified=True,
            runner=runner,
        )

    assert exc_info.value.reason == "bootstrap_runtime_composition_mismatch"
    assert all(
        "pull" not in command and "up" not in command
        for command, _ in runner.calls
    )


def test_cli_apply_passes_explicit_bootstrap_and_lock_acknowledgements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    received: dict[str, object] = {}

    def fake_apply(
        project_root: Path,
        render_output: Path,
        **kwargs: object,
    ) -> TomlApplyResult:
        received.update(
            {
                "project": project_root,
                "output": render_output,
                **kwargs,
            }
        )
        progress = kwargs["progress"]
        assert callable(progress)
        progress("start-services-and-wait timeout=300")
        return TomlApplyResult(
            status="created",
            lock_identity=lock["lock_identity"],
            compose_project="home",
            service="minecraft",
            volume="home-beta-minecraft-data",
        )

    monkeypatch.setattr("mc_remote_stack.cli._preset_data_root", lambda: data_root)
    monkeypatch.setattr("mc_remote_stack.cli.apply_toml_project", fake_apply)

    assert (
        main(
            [
                "apply",
                "--project",
                str(project),
                "--output",
                str(output),
                "--expected-lock-identity",
                lock["lock_identity"],
                "--docker-context",
                "default",
                "--bootstrap",
                "--yes",
                "--allow-unverified",
            ]
        )
        == 0
    )

    assert received["project"] == project
    assert received["output"] == output
    assert received["expected_lock_identity"] == lock["lock_identity"]
    assert received["docker_context"] == "default"
    assert received["bootstrap"] is True
    assert received["confirmed"] is True
    assert received["allow_unverified"] is True
    output_text = capsys.readouterr().out
    assert (
        "PROGRESS apply step=start-services-and-wait timeout=300" in output_text
    )
    assert "OK apply status=created bootstrap=true" in output_text
    assert "unverified" not in output_text


@pytest.mark.parametrize("omitted", ["output", "docker-context", "both"])
def test_cli_bootstrap_apply_defaults_to_project_render_and_local_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    omitted: str,
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)

    def fake_apply(project_root: Path, render_output: Path, **kwargs: object) -> TomlApplyResult:
        assert project_root == project
        assert render_output == output
        assert kwargs["docker_context"] == "default"
        return TomlApplyResult(
            status="created",
            lock_identity=lock["lock_identity"],
            compose_project="home",
            service="minecraft",
            volume="home-beta-minecraft-data",
        )

    monkeypatch.setattr("mc_remote_stack.cli._preset_data_root", lambda: data_root)
    monkeypatch.setattr("mc_remote_stack.cli.apply_toml_project", fake_apply)
    args = ["apply", "--project", str(project), "--bootstrap", "--yes"]
    if omitted not in ("output", "both"):
        args += ["--output", str(output)]
    if omitted not in ("docker-context", "both"):
        args += ["--docker-context", "default"]

    assert main(args) == 0


def test_cli_apply_reports_stable_failure_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    monkeypatch.setattr("mc_remote_stack.cli._preset_data_root", lambda: data_root)

    def fail_apply(*_args: object, **_kwargs: object) -> TomlApplyResult:
        raise ApplyContractError(
            "bootstrap_volume_unmanaged",
            "home-beta-minecraft-data",
            "fixture",
        )

    monkeypatch.setattr("mc_remote_stack.cli.apply_toml_project", fail_apply)

    assert (
        main(
            [
                "apply",
                "--project",
                str(project),
                "--output",
                str(output),
                "--expected-lock-identity",
                lock["lock_identity"],
                "--docker-context",
                "default",
                "--bootstrap",
                "--yes",
                "--allow-unverified",
            ]
        )
        == 2
    )

    assert "FAIL apply reason=bootstrap_volume_unmanaged" in capsys.readouterr().out
