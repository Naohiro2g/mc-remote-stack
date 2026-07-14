from pathlib import Path

from mc_remote_stack.cli import main

from .helpers import enable_renderable_staging, make_renderable_project


def test_cli_init_and_plan_stops_on_unresolved_lock(tmp_path: Path, capsys) -> None:
    project = tmp_path / "deployment"

    assert main(["init", str(project), "--profile", "official-vps"]) == 0
    assert main(["repo", "check", "--project", str(project)]) == 0
    assert main(["plan", "--project", str(project)]) == 2

    output = capsys.readouterr().out
    assert "PLAN rcon=disabled" in output
    assert "exact image@sha256 digest is required" in output


def test_cli_requires_explicit_eula_confirmation(tmp_path: Path, capsys) -> None:
    project = tmp_path / "deployment"
    assert main(["init", str(project)]) == 0

    assert main(["accept-eula", "--project", str(project)]) == 2
    assert main(["accept-eula", "--project", str(project), "--yes"]) == 0

    output = capsys.readouterr().out
    assert "requires --yes" in output
    assert "recorded explicit EULA acceptance" in output


def test_cli_plan_reports_enabled_staging_as_dormant_with_reserved_ports(tmp_path: Path, capsys) -> None:
    project = make_renderable_project(tmp_path)
    enable_renderable_staging(project)

    assert main(["plan", "--project", str(project.root)]) in (0, 1)

    output = capsys.readouterr().out
    assert "PLAN staging=enabled activation=compose-profile:staging default=dormant" in output
    assert "PLAN staging-public-ports=25566/tcp,25566/udp,25576/tcp" in output
