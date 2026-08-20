import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from mc_remote_stack.apply import (
    ApplyContractError,
    TomlApplyResult,
    _initialize_created_credential_volumes,
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


def _prepared_legacy_beta_project(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    project, data_root, _ = _render_fixture(tmp_path, profile_revision="2")
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
        profile_revision="2",
    )
    output = project / "generated"
    render_toml_project(project, output, data_root=data_root)
    return project, data_root, output, load_lock(project, data_root=data_root)


def _prepared_current_alpha_project(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    project, data_root, _ = _render_fixture(
        tmp_path,
        deployment_name="home-alpha",
        identity="home-alpha",
        channel="alpha",
        preset_revision="2",
        profile_revision="4",
    )
    output = project / "generated"
    render_toml_project(project, output, data_root=data_root)
    return project, data_root, output, load_lock(project, data_root=data_root)


def _prepared_credential_project(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    project, data_root, _ = _render_fixture(
        tmp_path,
        deployment_name="home-alpha",
        identity="home-alpha",
        channel="alpha",
        preset_revision="2",
        profile_revision="3",
    )
    output = project / "generated"
    render_toml_project(project, output, data_root=data_root)
    return project, data_root, output, load_lock(project, data_root=data_root)


def _prepared_b3_credential_project(
    tmp_path: Path,
) -> tuple[Path, Path, Path, dict]:
    project, data_root, _ = _render_fixture(
        tmp_path,
        deployment_name="home-b3-alpha",
        identity="home-b3-alpha",
        channel="alpha",
        preset_revision="3",
        profile_revision="3",
    )
    output = project / "generated"
    render_toml_project(project, output, data_root=data_root)
    return project, data_root, output, load_lock(project, data_root=data_root)


def _prepared_b4_persistent_credential_project(
    tmp_path: Path,
) -> tuple[Path, Path, Path, dict]:
    project, data_root, _ = _render_fixture(
        tmp_path,
        deployment_name="home-alpha",
        identity="home-alpha",
        channel="alpha",
        preset_revision="6",
        profile_revision="3",
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


def test_legacy_public_vps_bootstrap_contract_is_rejected(tmp_path: Path) -> None:
    project, data_root, output, lock = _prepared_public_project(tmp_path)
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

    assert exc_info.value.reason == "bootstrap_contract_unsupported"
    assert runner.calls == []


def test_legacy_home_beta_bootstrap_contract_is_rejected(tmp_path: Path) -> None:
    project, data_root, output, lock = _prepared_legacy_beta_project(tmp_path)
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

    assert exc_info.value.reason == "bootstrap_contract_unsupported"
    assert runner.calls == []


def test_current_public_vps_bootstrap_contract_is_supported(tmp_path: Path) -> None:
    _project, _data_root, _output, lock = _prepared_public_project(tmp_path)
    lock["input"]["profile"]["ref"] = "vps-server@7"
    lock["input"]["preset"]["ref"] = "public-web-paper@2"

    _validate_bootstrap_contract(
        lock,
        allow_unverified=True,
        allow_eol=False,
    )


def test_public_wirescope_vps_bootstrap_contract_is_supported(tmp_path: Path) -> None:
    _project, _data_root, _output, lock = _prepared_public_project(tmp_path)
    lock["input"]["profile"]["ref"] = "vps-server@9"
    lock["input"]["preset"]["ref"] = "public-web-paper@4"

    _validate_bootstrap_contract(
        lock,
        allow_unverified=True,
        allow_eol=False,
    )


def test_canonical_public_vps_bootstrap_contract_is_supported(tmp_path: Path) -> None:
    _project, _data_root, _output, lock = _prepared_public_project(tmp_path)
    lock["input"]["profile"]["ref"] = "vps-server@10"
    lock["input"]["preset"]["ref"] = "public-web-paper@4"

    _validate_bootstrap_contract(
        lock,
        allow_unverified=True,
        allow_eol=False,
    )


def test_typed_notice_public_vps_bootstrap_contract_is_supported(tmp_path: Path) -> None:
    _project, _data_root, _output, lock = _prepared_public_project(tmp_path)
    lock["input"]["profile"]["ref"] = "vps-server@11"
    lock["input"]["preset"]["ref"] = "public-web-paper@4"

    _validate_bootstrap_contract(
        lock,
        allow_unverified=True,
        allow_eol=False,
    )


def test_previous_public_vps_contract_is_rejected(tmp_path: Path) -> None:
    _project, _data_root, _output, lock = _prepared_public_project(tmp_path)
    lock["input"]["profile"]["ref"] = "vps-server@4"
    lock["input"]["preset"]["ref"] = "public-web-paper@1"

    with pytest.raises(ApplyContractError) as exc_info:
        _validate_bootstrap_contract(
            lock,
            allow_unverified=True,
            allow_eol=False,
        )

    assert exc_info.value.reason == "bootstrap_contract_unsupported"


def test_credential_profile_rejects_old_plugin_preset_before_docker(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_credential_project(tmp_path)
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

    assert exc_info.value.reason == "bootstrap_contract_unsupported"
    assert runner.calls == []


def test_b3_credential_alpha_bootstrap_contract_reaches_docker_preflight(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_b3_credential_project(tmp_path)
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


def test_b4_persistent_credential_bootstrap_contract_reaches_docker_preflight(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_b4_persistent_credential_project(
        tmp_path
    )
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


def test_b3_credential_alpha_requires_one_shot_unverified_allowance(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_b3_credential_project(tmp_path)
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
            allow_unverified=False,
            runner=runner,
        )

    assert exc_info.value.reason == "unverified_not_acknowledged"
    assert runner.calls == []


def test_fresh_credential_volumes_are_initialized_for_pinned_runtime_user() -> None:
    image = "registry.example/minecraft:fixture-java21@sha256:" + "a" * 64
    command = _docker(
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--user",
        "0:0",
        "--mount",
        (
            "type=volume,source=home-b3-alpha-credential-store,"
            "target=/credential-store,volume-nocopy"
        ),
        "--mount",
        (
            "type=volume,source=home-b3-alpha-credential-revocations,"
            "target=/credential-revocations,volume-nocopy"
        ),
        "--entrypoint",
        "chown",
        image,
        "1000:1000",
        "/credential-store",
        "/credential-revocations",
    )
    runner = FakeDocker({command: [_result(command)]})

    _initialize_created_credential_volumes(
        runner,
        ["docker", "--context", "default"],
        image=image,
        volume_assignments={
            "minecraft-data": "home-b3-alpha-minecraft-data",
            "credential-store": "home-b3-alpha-credential-store",
            "credential-revocations": "home-b3-alpha-credential-revocations",
        },
        created_volumes={
            "home-b3-alpha-minecraft-data",
            "home-b3-alpha-credential-store",
            "home-b3-alpha-credential-revocations",
        },
    )

    assert runner.calls == [(command, 120)]


def test_credential_volume_initializer_does_not_touch_existing_state() -> None:
    runner = FakeDocker({})

    _initialize_created_credential_volumes(
        runner,
        ["docker", "--context", "default"],
        image="registry.example/minecraft:fixture-java21@sha256:" + "a" * 64,
        volume_assignments={
            "minecraft-data": "home-b3-alpha-minecraft-data",
            "credential-store": "home-b3-alpha-credential-store",
            "credential-revocations": "home-b3-alpha-credential-revocations",
        },
        created_volumes={"home-b3-alpha-minecraft-data"},
    )

    assert runner.calls == []


def test_credential_volume_initializer_failure_is_fail_closed() -> None:
    image = "registry.example/minecraft:fixture-java21@sha256:" + "a" * 64
    calls: list[tuple[tuple[str, ...], int]] = []

    def failing_runner(
        command: list[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        calls.append((tuple(command), timeout))
        return _result(tuple(command), returncode=1, stderr="chown failed\n")

    with pytest.raises(ApplyContractError) as exc_info:
        _initialize_created_credential_volumes(
            failing_runner,
            ["docker", "--context", "default"],
            image=image,
            volume_assignments={
                "credential-store": "home-b3-alpha-credential-store",
                "credential-revocations": "home-b3-alpha-credential-revocations",
            },
            created_volumes={"home-b3-alpha-credential-store"},
        )

    assert exc_info.value.reason == "bootstrap_volume_initialize_failed"
    assert all("credential-revocations" not in part for part in calls[0][0])


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


def _managed_volume(lock: dict, name: str = "home-beta-minecraft-data") -> dict:
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
    running: bool = True,
) -> dict:
    return {
        "Id": "container-current",
        "Config": {
            "Labels": {
                "com.docker.compose.project": lock["deployment"]["name"],
                "com.docker.compose.service": "minecraft",
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


def test_b3_apply_initializes_fresh_credential_volumes_before_compose_up(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_b3_credential_project(tmp_path)
    base = _compose_base(output)
    responses = _read_only_responses(output, lock=lock)
    labels = _managed_volume(lock)["Labels"]
    volumes = {
        assignment["role"]: assignment["identity"]
        for assignment in lock["runtime"]["volumes"]
    }
    runtime_component = next(
        component
        for component in lock["components"]
        if component["role"] == "minecraft-runtime"
    )
    runtime_artifact = next(
        artifact
        for artifact in lock["artifacts"]
        if artifact["id"] == runtime_component["artifact"]
    )
    image = (
        f"{runtime_artifact['locator']}:{runtime_artifact['version']}"
        f"@{runtime_artifact['digest']}"
    )
    responses[base + ("pull", "--policy", "always", "--quiet", "minecraft")] = [
        _result(base)
    ]
    for volume in volumes.values():
        create_arguments = ["volume", "create", "--driver", "local"]
        for key, value in labels.items():
            create_arguments.extend(["--label", f"{key}={value}"])
        create_arguments.append(volume)
        create_command = _docker(*create_arguments)
        responses[create_command] = [_result(create_command, stdout=f"{volume}\n")]
        inspect_command = _docker("volume", "inspect", volume)
        responses[inspect_command] = [
            _result(
                inspect_command,
                stdout=json.dumps([_managed_volume(lock, volume)]) + "\n",
            )
        ]
    initialize_command = _docker(
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--user",
        "0:0",
        "--mount",
        (
            f"type=volume,source={volumes['credential-store']},"
            "target=/credential-store,volume-nocopy"
        ),
        "--mount",
        (
            f"type=volume,source={volumes['credential-revocations']},"
            "target=/credential-revocations,volume-nocopy"
        ),
        "--entrypoint",
        "chown",
        image,
        "1000:1000",
        "/credential-store",
        "/credential-revocations",
    )
    responses[initialize_command] = [_result(initialize_command)]
    up_command = base + (
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        "300",
        "--no-build",
        "--pull",
        "never",
        "minecraft",
    )
    responses[up_command] = [_result(up_command)]
    project_ps = _docker(
        "ps",
        "--all",
        "--quiet",
        "--filter",
        "label=com.docker.compose.project=home-b3-alpha",
    )
    responses[project_ps] = [
        responses[project_ps][0],
        _result(project_ps, stdout="container-current\n"),
    ]
    inspect_container = _docker("inspect", "container-current")
    responses[inspect_container] = [
        _result(
            inspect_container,
            stdout=json.dumps([_managed_container(lock, output)]) + "\n",
        )
    ]
    runner = FakeDocker(responses)
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
        port_probe=lambda _address, _port: None,
        progress=progress.append,
    )

    assert result.status == "created"
    assert "initialize-credential-volumes" in progress
    commands = [command for command, _timeout in runner.calls]
    assert commands.index(initialize_command) < commands.index(up_command)


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


def test_apply_accepts_managed_volume_with_older_creation_lock(
    tmp_path: Path,
) -> None:
    _project, _data_root, _output, lock = _prepared_project(tmp_path)
    volume = _managed_volume(lock)
    volume["Labels"]["io.mc-remote.created-by-lock"] = "sha256:" + "0" * 64
    runner = FakeDocker(
        {
            _docker("volume", "inspect", "home-beta-minecraft-data"): [
                _result(("docker",), stdout=json.dumps([volume]) + "\n")
            ]
        }
    )

    _inspect_managed_volume(
        runner,
        ["docker", "--context", "default"],
        "home-beta-minecraft-data",
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


def test_legacy_alpha_bootstrap_contract_is_rejected_before_docker(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_alpha_project(tmp_path)
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

    assert exc_info.value.reason == "bootstrap_contract_unsupported"
    assert runner.calls == []


def test_current_alpha_bootstrap_contract_reaches_docker_preflight(
    tmp_path: Path,
) -> None:
    project, data_root, output, lock = _prepared_current_alpha_project(tmp_path)
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
