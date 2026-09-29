from pathlib import Path

import mc_remote_stack.cli as cli
from mc_remote_stack.cli import main


def test_cli_project_commands_require_toml_project(tmp_path: Path, capsys) -> None:
    project = tmp_path / "deployment"
    project.mkdir()

    for command in ("validate", "plan"):
        assert main([command, "--project", str(project)]) == 2

    output = capsys.readouterr().out
    assert "FAIL validate reason=toml_project_required" in output
    assert "FAIL plan reason=toml_project_required" in output


def test_cli_requires_explicit_eula_confirmation(tmp_path: Path, capsys) -> None:
    project = tmp_path / "deployment"
    project.mkdir()

    assert main(["accept-eula", "--project", str(project)]) == 2

    assert "requires --yes" in capsys.readouterr().out


def test_cli_rejects_sudo_mcrctl_for_project_commands(tmp_path: Path, capsys, monkeypatch) -> None:
    project = tmp_path / "deployment"
    project.mkdir()
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)

    assert main(["plan", "--project", str(project)]) == 2

    output = capsys.readouterr().out
    assert "reason=operator_root_forbidden" in output
    assert "run mcrctl as the project-owning operator" in output


def test_cli_rejects_sudo_mcrctl_init_before_creating_root_owned_project(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    project = tmp_path / "deployment"
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)

    assert main(["init", str(project)]) == 2

    assert not project.exists()
    output = capsys.readouterr().out
    assert "reason=operator_root_forbidden" in output


def test_project_commands_use_the_current_project_directory(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    project = tmp_path / "deployment"
    project.mkdir()
    (project / "mc-remote.toml").write_text("schema_version = 1\n", encoding="utf-8")
    monkeypatch.chdir(project)

    assert main(["plan"]) == 2

    output = capsys.readouterr().out
    assert "reason=project_required" not in output
    assert f"path={project}" in output


def test_project_commands_outside_a_project_ask_for_one(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["plan"]) == 2
    assert main(["doctor"]) == 2

    output = capsys.readouterr().out
    assert "FAIL plan reason=project_required" in output
    assert "reason=doctor_deployment_required" in output
