import json
import subprocess
from pathlib import Path

import pytest

from mc_remote_stack.operator_environment import (
    OperatorEnvironmentError,
    check_operator_environment,
)


def _completed(command: list[str], stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def _healthy_runner(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    del timeout
    if command == ["git", "--version"]:
        return _completed(command, "git version 2.43.0\n")
    if command == ["uv", "--version"]:
        return _completed(command, "uv 0.12.3\n")
    if command == ["docker", "context", "inspect", "default"]:
        return _completed(
            command,
            json.dumps([{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}]),
        )
    if command == ["docker", "--context", "default", "version", "--format", "{{.Server.Version}}"]:
        return _completed(command, "29.7.2\n")
    if command == ["docker", "--context", "default", "compose", "version", "--short"]:
        return _completed(command, "2.39.1\n")
    raise AssertionError(command)


def test_operator_check_accepts_one_unprivileged_owner_with_complete_toolchain(tmp_path: Path) -> None:
    project = tmp_path / "deployment"
    project.mkdir()

    result = check_operator_environment(
        project,
        docker_context="default",
        effective_uid=project.stat().st_uid,
        effective_user="operator",
        runner=_healthy_runner,
        python_version=(3, 11, 9),
    )

    assert result.status == "ready"
    assert result.operator == "operator"
    assert result.docker_context == "default"
    assert result.compose_version == "2.39.1"


def test_operator_check_uses_the_bootstrap_uv_path_when_login_path_omits_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "deployment"
    project.mkdir()
    uv = tmp_path / ".local/bin/uv"
    uv.parent.mkdir(parents=True)
    uv.write_text("fixture\n", encoding="utf-8")
    uv.chmod(0o700)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("mc_remote_stack.operator_environment.Path.home", lambda: tmp_path)

    def runner(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        if command == [str(uv), "--version"]:
            return _completed(command, "uv 0.12.3\n")
        if command == ["uv", "--version"]:
            pytest.fail("operator check must use the bootstrap-resolved uv path")
        return _healthy_runner(command, timeout=timeout)

    result = check_operator_environment(
        project,
        docker_context="default",
        effective_uid=project.stat().st_uid,
        effective_user="operator",
        runner=runner,
        python_version=(3, 11, 9),
    )

    assert result.status == "ready"
    assert result.uv_version == "uv 0.12.3"


def test_operator_check_rejects_root_before_running_tools(tmp_path: Path) -> None:
    project = tmp_path / "deployment"
    project.mkdir()

    with pytest.raises(OperatorEnvironmentError) as caught:
        check_operator_environment(
            project,
            docker_context="default",
            effective_uid=0,
            effective_user="root",
            runner=lambda command, timeout: pytest.fail(command),
            python_version=(3, 11, 9),
        )

    assert caught.value.reason == "operator_root_forbidden"
    assert caught.value.path == "operator.uid"


def test_operator_check_rejects_project_not_owned_by_operator(tmp_path: Path) -> None:
    project = tmp_path / "deployment"
    project.mkdir()

    with pytest.raises(OperatorEnvironmentError) as caught:
        check_operator_environment(
            project,
            docker_context="default",
            effective_uid=project.stat().st_uid + 1,
            effective_user="operator",
            runner=lambda command, timeout: pytest.fail(command),
            python_version=(3, 11, 9),
        )

    assert caught.value.reason == "operator_project_owner_mismatch"
    assert caught.value.path == project.resolve()


def test_operator_check_rejects_nested_project_entry_without_write_access(
    tmp_path: Path,
) -> None:
    project = tmp_path / "deployment"
    project.mkdir()
    nested = project / "recovery.yaml"
    nested.write_text("services: {}\n", encoding="utf-8")
    nested.chmod(0o400)
    try:
        with pytest.raises(OperatorEnvironmentError) as caught:
            check_operator_environment(
                project,
                docker_context="default",
                effective_uid=project.stat().st_uid,
                effective_user="operator",
                runner=_healthy_runner,
                python_version=(3, 11, 9),
            )
    finally:
        nested.chmod(0o600)

    assert caught.value.reason == "operator_project_entry_not_writable"
    assert caught.value.path == nested


def test_operator_check_rejects_an_unwritable_declared_artifact_store(
    tmp_path: Path,
) -> None:
    project = tmp_path / "deployment"
    project.mkdir()
    artifact_store = tmp_path / "artifact-store"
    artifact_store.mkdir()
    (project / "mc-remote.toml").write_text(
        f'[runtime]\nartifact_store = "{artifact_store}"\n',
        encoding="utf-8",
    )
    artifact_store.chmod(0o500)
    try:
        with pytest.raises(OperatorEnvironmentError) as caught:
            check_operator_environment(
                project,
                docker_context="default",
                effective_uid=project.stat().st_uid,
                effective_user="operator",
                runner=_healthy_runner,
                python_version=(3, 11, 9),
            )
    finally:
        artifact_store.chmod(0o700)

    assert caught.value.reason == "operator_artifact_store_not_writable"
    assert caught.value.path == artifact_store


def test_operator_check_reports_direct_docker_permission_failure(tmp_path: Path) -> None:
    project = tmp_path / "deployment"
    project.mkdir()

    def runner(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        if command in (["git", "--version"], ["uv", "--version"]):
            return _healthy_runner(command, timeout=timeout)
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="permission denied while trying to connect to the Docker daemon socket\n",
        )

    with pytest.raises(OperatorEnvironmentError) as caught:
        check_operator_environment(
            project,
            docker_context="default",
            effective_uid=project.stat().st_uid,
            effective_user="operator",
            runner=runner,
            python_version=(3, 11, 9),
        )

    assert caught.value.reason == "operator_docker_access_missing"
    assert "docker group" in str(caught.value)


def test_operator_check_requires_compose_with_gw_priority_support(tmp_path: Path) -> None:
    project = tmp_path / "deployment"
    project.mkdir()

    def runner(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        if command[-3:] == ["compose", "version", "--short"]:
            return _completed(command, "2.32.4\n")
        return _healthy_runner(command, timeout=timeout)

    with pytest.raises(OperatorEnvironmentError) as caught:
        check_operator_environment(
            project,
            docker_context="default",
            effective_uid=project.stat().st_uid,
            effective_user="operator",
            runner=runner,
            python_version=(3, 11, 9),
        )

    assert caught.value.reason == "operator_compose_too_old"
    assert "2.33.1" in str(caught.value)
