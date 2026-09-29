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
