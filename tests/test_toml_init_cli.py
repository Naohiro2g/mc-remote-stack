import tomllib
from pathlib import Path

import pytest

from mc_remote_stack.cli import main


def _toml_init_args(project: Path, artifact_store: Path) -> list[str]:
    return [
        "init",
        str(project),
        "--format",
        "toml",
        "--deployment-name",
        "home",
        "--profile",
        "home-server@1",
        "--environment-identity",
        "home-beta",
        "--channel",
        "beta",
        "--exposure",
        "isolated",
        "--purpose",
        "integration",
        "--preset",
        "mcremote-paper@1",
        "--artifact-store",
        str(artifact_store),
        "--volume",
        "minecraft-data=home-beta-minecraft-data",
        "--world-identity",
        "home-beta-world",
        "--bind-address",
        "127.0.0.1",
        "--java-port",
        "25565",
        "--mcremote-port",
        "25575",
    ]


def test_cli_toml_init_creates_explicit_unresolved_project(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "arbitrary-directory-name"
    artifact_store = tmp_path / "artifact-store"

    assert main(_toml_init_args(project, artifact_store)) == 0

    order = tomllib.loads((project / "mc-remote.toml").read_text(encoding="utf-8"))
    assert order["deployment"] == {
        "name": "home",
        "profile": "home-server@1",
    }
    assert order["environment"] == {
        "identity": "home-beta",
        "channel": "beta",
        "exposure": "isolated",
        "purpose": "integration",
        "preset": "mcremote-paper@1",
    }
    assert order["runtime"] == {
        "artifact_store": str(artifact_store),
        "volumes": [
            {
                "role": "minecraft-data",
                "identity": "home-beta-minecraft-data",
            }
        ],
    }
    assert order["world"] == {"identity": "home-beta-world"}
    assert order["network"] == {
        "bind_address": "127.0.0.1",
        "java_port": 25565,
        "mcremote_port": 25575,
    }
    assert order["agreements"] == {"minecraft_eula": False}
    assert not (project / "mc-remote.lock.toml").exists()
    assert not artifact_store.exists()

    output = capsys.readouterr().out
    assert f"OK initialized format=toml project={project.resolve()}" in output
    assert f"NEXT mcrctl accept-eula --project {project.resolve()} --yes" in output
    assert f"NEXT mcrctl resolve --project {project.resolve()}" in output


def test_cli_toml_init_requires_every_instance_argument_without_creating_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "home-beta"

    assert main(["init", str(project), "--format", "toml"]) == 2

    output = capsys.readouterr().out
    assert "FAIL init reason=missing_toml_init_argument" in output
    assert "--deployment-name" in output
    assert "--mcremote-port" in output
    assert not project.exists()


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        (["--volume", "missing-separator"], "invalid_volume_assignment"),
        (
            [
                "--volume",
                "minecraft-data=other-volume",
            ],
            "duplicate_volume_assignment",
        ),
    ],
)
def test_cli_toml_init_rejects_invalid_volume_assignments_before_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    reason: str,
) -> None:
    project = tmp_path / "home-beta"
    args = _toml_init_args(project, tmp_path / "artifact-store")
    args.extend(extra)

    assert main(args) == 2

    assert f"FAIL init reason={reason}" in capsys.readouterr().out
    assert not project.exists()


def test_cli_toml_init_reports_cross_field_validation_without_partial_project(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "home-beta"
    args = _toml_init_args(project, tmp_path / "artifact-store")
    bind_index = args.index("--bind-address") + 1
    args[bind_index] = "192.168.1.10"

    assert main(args) == 2

    assert "reason=unsupported_environment_combination" in capsys.readouterr().out
    assert not project.exists()


def test_cli_legacy_init_rejects_toml_only_arguments_instead_of_ignoring_them(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "deployment"

    assert main(["init", str(project), "--deployment-name", "ignored"]) == 2

    output = capsys.readouterr().out
    assert "reason=toml_init_argument_requires_format" in output
    assert "--format toml" in output
    assert not project.exists()
