import subprocess
from pathlib import Path

import pytest

from mc_remote_stack.homepage_sync import HomepageSyncError, sync_homepage


def test_sync_homepage_fetches_main_replaces_public_directory_and_recreates_only_caddy(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    artifact_store = tmp_path / "artifacts"
    source_root = tmp_path / "clone"
    homepage = source_root / "30-広告宣伝" / "homepage"
    homepage.mkdir(parents=True)
    (homepage / "index.html").write_text("new homepage\n", encoding="utf-8")
    public = tmp_path / "homepage"
    public.mkdir()
    (public / "old.html").write_text("old\n", encoding="utf-8")
    commands = []

    monkeypatch.setattr(
        "mc_remote_stack.homepage_sync.load_order",
        lambda _project: type("Loaded", (), {"order": {"runtime": {"artifact_store": str(artifact_store)}}})(),
    )
    monkeypatch.setattr(
        "mc_remote_stack.homepage_sync.load_lock",
        lambda *_args, **_kwargs: {
            "deployment": {"name": "official-public-beta"},
            "render_plan": {"adapter_revision": "13"},
            "operator_inputs": [
                {"role": "public-routes", "semantic": {"homepage": "mc-remote.com"}}
            ],
        },
    )
    monkeypatch.setattr(
        "mc_remote_stack.homepage_sync.render_toml_project",
        lambda *_args, **_kwargs: None,
    )

    def runner(command, timeout):
        commands.append((command, timeout))
        if command[:2] == ["git", "clone"]:
            destination = Path(command[-1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            source_root.rename(destination)
        return subprocess.CompletedProcess(command, 0, "", "")

    result = sync_homepage(
        project,
        project / "generated",
        data_root=tmp_path,
        runner=runner,
    )

    assert result.public_directory == public
    assert (public / "index.html").read_text(encoding="utf-8") == "new homepage\n"
    assert not (public / "old.html").exists()
    assert commands[0][0][:5] == [
        "git",
        "clone",
        "--depth",
        "1",
        "--branch",
    ]
    assert commands[-1][0][-8:] == [
        "up",
        "-d",
        "--force-recreate",
        "--no-deps",
        "caddy",
        "--wait",
        "--wait-timeout",
        "60",
    ]
    assert "minecraft" not in commands[-1][0]


@pytest.mark.parametrize(
    ("renderer_revision", "homepage_domain"),
    [("14", None), ("13", "other.example")],
)
def test_sync_homepage_rejects_other_deployment_before_changing_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    renderer_revision: str,
    homepage_domain: str | None,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    artifact_store = tmp_path / "artifacts"
    monkeypatch.setattr(
        "mc_remote_stack.homepage_sync.load_order",
        lambda _project: type(
            "Loaded", (), {"order": {"runtime": {"artifact_store": str(artifact_store)}}}
        )(),
    )
    monkeypatch.setattr(
        "mc_remote_stack.homepage_sync.load_lock",
        lambda *_args, **_kwargs: {
            "deployment": {"name": "home-trial"},
            "render_plan": {"adapter_revision": renderer_revision},
            "operator_inputs": (
                []
                if homepage_domain is None
                else [
                    {
                        "role": "public-routes",
                        "semantic": {"homepage": homepage_domain},
                    }
                ]
            ),
        },
    )

    def runner(_command: list[str], _timeout: int) -> subprocess.CompletedProcess[str]:
        pytest.fail("a non-homepage deployment must fail before cloning or Docker")

    with pytest.raises(HomepageSyncError) as exc_info:
        sync_homepage(
            project,
            project / "generated",
            data_root=tmp_path,
            runner=runner,
        )

    assert exc_info.value.reason == "homepage_target_invalid"
    assert not (tmp_path / "homepage").exists()


def test_homepage_sync_cli_needs_only_the_project(monkeypatch, tmp_path: Path, capsys) -> None:
    public = tmp_path / "homepage"
    public.mkdir()
    monkeypatch.setattr(
        "mc_remote_stack.cli.sync_homepage",
        lambda *_args, **_kwargs: type("Result", (), {"public_directory": public})(),
    )

    from mc_remote_stack.cli import main

    assert main(["homepage", "sync", "--project", str(tmp_path)]) == 0
    assert f"OK homepage sync path={public}" in capsys.readouterr().out
