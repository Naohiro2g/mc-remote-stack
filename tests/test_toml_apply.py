import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from mc_remote_stack.apply import (
    ApplyContractError,
    TomlApplyResult,
    _safe_command_failure_detail,
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


def _prepared_alpha_project(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    project, data_root, _ = _render_fixture(
        tmp_path,
        deployment_name="home-alpha",
        identity="home-alpha",
        channel="alpha",
        preset_revision="2",
    )
    output = project / "generated"
    render_toml_project(project, output, data_root=data_root)
    return project, data_root, output, load_lock(project, data_root=data_root)


def _prepared_public_project(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    project, data_root, _ = _render_fixture(
        tmp_path,
        deployment_name="official-public-beta",
        identity="official-public-beta",
        profile_name="vps-server",
        profile_revision="1",
        exposure="public",
        bind_address="0.0.0.0",
    )
    output = project / "generated"
    render_toml_project(project, output, data_root=data_root)
    return project, data_root, output, load_lock(project, data_root=data_root)


def test_public_vps_bootstrap_contract_is_supported(tmp_path: Path) -> None:
    project, data_root, output, lock = _prepared_public_project(tmp_path)
    runner = FakeDocker({})

    with pytest.raises(AssertionError, match="docker.*context.*inspect"):
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

    assert runner.calls[0][0] == ("docker", "context", "inspect", "default")


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


def _managed_container(
    lock: dict,
    output: Path,
    *,
    running: bool = True,
) -> dict:
    return {
        "Id": "container-current",
        "Config": {
            "Labels": {
                "com.docker.compose.project": "home",
                "com.docker.compose.service": "minecraft",
                "com.docker.compose.project.config_files": str(
                    output.resolve() / "compose.yaml"
                ),
                "com.docker.compose.project.working_dir": str(
                    output.resolve()
                ),
                "io.mc-remote.deployment": "home",
                "io.mc-remote.environment": "home-beta",
                "io.mc-remote.world": "home-beta-world",
                "io.mc-remote.lock": lock["lock_identity"],
            }
        },
        "State": {"Running": running},
    }


def _read_only_responses(
    output: Path,
    *,
    project_containers: list[str] | None = None,
    volume_names: list[str] | None = None,
) -> dict[tuple[str, ...], list[subprocess.CompletedProcess[str]]]:
    base = _compose_base(output)
    container_stdout = "".join(f"{value}\n" for value in (project_containers or []))
    volume_stdout = "".join(f"{value}\n" for value in (volume_names or []))
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
            "label=com.docker.compose.project=home",
        ): [_result(("docker",), stdout=container_stdout)],
        _docker(
            "volume",
            "ls",
            "--quiet",
            "--filter",
            "name=^home\\-beta\\-minecraft\\-data$",
        ): [_result(("docker",), stdout=volume_stdout)],
        _docker(
            "ps",
            "--all",
            "--quiet",
            "--filter",
            "publish=25565",
        ): [_result(("docker",))],
        _docker(
            "ps",
            "--all",
            "--quiet",
            "--filter",
            "publish=25575",
        ): [_result(("docker",))],
    }
    return commands


def test_bootstrap_apply_is_bound_to_current_lock_and_verified_render(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    base = _compose_base(output)
    responses = _read_only_responses(output)
    responses.update(
        {
            base + ("pull", "--policy", "always", "--quiet", "minecraft"): [
                _result(base)
            ],
            _docker(
                "volume",
                "create",
                "--driver",
                "local",
                "--label",
                "io.mc-remote.owner=mcrctl",
                "--label",
                "io.mc-remote.deployment=home",
                "--label",
                "io.mc-remote.environment=home-beta",
                "--label",
                "io.mc-remote.world=home-beta-world",
                "--label",
                f"io.mc-remote.created-by-lock={lock['lock_identity']}",
                "home-beta-minecraft-data",
            ): [_result(("docker",), stdout="home-beta-minecraft-data\n")],
            _docker("volume", "inspect", "home-beta-minecraft-data"): [
                _result(
                    ("docker",),
                    stdout=json.dumps([_managed_volume(lock)]) + "\n",
                )
            ],
            base
            + (
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "300",
                "--no-build",
                "--pull",
                "never",
                "minecraft",
            ): [_result(base)],
            _docker(
                "ps",
                "--all",
                "--quiet",
                "--filter",
                "label=com.docker.compose.project=home",
            ): [
                responses[
                    _docker(
                        "ps",
                        "--all",
                        "--quiet",
                        "--filter",
                        "label=com.docker.compose.project=home",
                    )
                ][0],
                _result(("docker",), stdout="container-current\n"),
            ],
            _docker("inspect", "container-current"): [
                _result(
                    ("docker",),
                    stdout=json.dumps([_managed_container(lock, output)])
                    + "\n",
                )
            ],
        }
    )
    runner = FakeDocker(responses)
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
    assert result.compose_project == "home"
    assert result.volume == "home-beta-minecraft-data"
    assert probed == [("127.0.0.1", 25565), ("127.0.0.1", 25575)]
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
    assert commands.index(base + ("pull", "--policy", "always", "--quiet", "minecraft")) < commands.index(
        _docker(
            "volume",
            "create",
            "--driver",
            "local",
            "--label",
            "io.mc-remote.owner=mcrctl",
            "--label",
            "io.mc-remote.deployment=home",
            "--label",
            "io.mc-remote.environment=home-beta",
            "--label",
            "io.mc-remote.world=home-beta-world",
            "--label",
            f"io.mc-remote.created-by-lock={lock['lock_identity']}",
            "home-beta-minecraft-data",
        )
    )


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"bootstrap": False, "confirmed": True, "allow_unverified": True}, "bootstrap_confirmation_required"),
        ({"bootstrap": True, "confirmed": False, "allow_unverified": True}, "apply_confirmation_required"),
        ({"bootstrap": True, "confirmed": True, "allow_unverified": False}, "unverified_not_acknowledged"),
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
    responses = _read_only_responses(
        output,
        volume_names=["home-beta-minecraft-data"],
    )
    responses[_docker("volume", "inspect", "home-beta-minecraft-data")] = [
        _result(
            ("docker",),
            stdout=json.dumps(
                [
                    {
                        "Name": "home-beta-minecraft-data",
                        "Driver": "local",
                        "Labels": {},
                    }
                ]
            )
            + "\n",
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


def test_alpha_bootstrap_contract_reaches_docker_preflight(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_alpha_project(tmp_path)
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
    responses = _read_only_responses(output)
    responses[
        _docker(
            "ps",
            "--all",
            "--quiet",
            "--filter",
            "publish=25565",
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


def test_failed_compose_up_rolls_back_containers_but_retains_world_volume(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_project(tmp_path)
    base = _compose_base(output)
    responses = _read_only_responses(output)
    responses.update(
        {
            base + ("pull", "--policy", "always", "--quiet", "minecraft"): [
                _result(base)
            ],
            _docker(
                "volume",
                "create",
                "--driver",
                "local",
                "--label",
                "io.mc-remote.owner=mcrctl",
                "--label",
                "io.mc-remote.deployment=home",
                "--label",
                "io.mc-remote.environment=home-beta",
                "--label",
                "io.mc-remote.world=home-beta-world",
                "--label",
                f"io.mc-remote.created-by-lock={lock['lock_identity']}",
                "home-beta-minecraft-data",
            ): [_result(("docker",), stdout="home-beta-minecraft-data\n")],
            _docker("volume", "inspect", "home-beta-minecraft-data"): [
                _result(
                    ("docker",),
                    stdout=json.dumps([_managed_volume(lock)]) + "\n",
                )
            ],
            base
            + (
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "300",
                "--no-build",
                "--pull",
                "never",
                "minecraft",
            ): [
                _result(
                    base,
                    returncode=1,
                    stdout="less useful stdout",
                    stderr=(
                        "startup failed token=supersecret "
                        "container-environment"
                    ),
                )
            ],
            base + ("down", "--timeout", "120"): [_result(base)],
        }
    )
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
        project_containers=["container-current"],
        volume_names=["home-beta-minecraft-data"],
    )
    responses[_docker("inspect", "container-current")] = [
        _result(
            ("docker",),
            stdout=json.dumps([_managed_container(lock, output)]) + "\n",
        )
    ]
    responses[_docker("volume", "inspect", "home-beta-minecraft-data")] = [
        _result(("docker",), stdout=json.dumps([_managed_volume(lock)]) + "\n")
    ]
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
        project_containers=["container-current"],
        volume_names=["home-beta-minecraft-data"],
    )
    container = _managed_container(lock, output)
    labels = container["Config"]["Labels"]
    labels["com.docker.compose.project.config_files"] = (
        f"{output.resolve() / 'compose.yaml'},"
        f"{tmp_path / 'recovery.override.yaml'}"
    )
    responses[_docker("inspect", "container-current")] = [
        _result(("docker",), stdout=json.dumps([container]) + "\n")
    ]
    responses[_docker("volume", "inspect", "home-beta-minecraft-data")] = [
        _result(
            ("docker",),
            stdout=json.dumps([_managed_volume(lock)]) + "\n",
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
    assert "WARN live bootstrap used the one-shot unverified acknowledgement" in output_text


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
