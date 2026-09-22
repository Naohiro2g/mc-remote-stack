import subprocess
from pathlib import Path

from mc_remote_stack.homepage_sync import sync_homepage


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
        lambda *_args, **_kwargs: {"deployment": {"name": "official-public-beta"}},
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
