"""Synchronize the Git-managed homepage to its live Caddy directory."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path

from .apply import CommandRunner, _default_runner
from .render import render_toml_project
from .resolver import load_lock
from .toml_project import load_order

KNOWLEDGE_REPOSITORY = "https://github.com/Naohiro2g/mc-remote-knowledge.git"
HOMEPAGE_RELATIVE = Path("30-広告宣伝/homepage")


class HomepageSyncError(ValueError):
    def __init__(self, reason: str, path: object, message: str) -> None:
        self.reason = reason
        self.path = str(path)
        super().__init__(f"{reason}: {path}: {message}")


@dataclass(frozen=True)
class HomepageSyncResult:
    public_directory: Path


def _run(runner: CommandRunner, command: list[str], *, timeout: int) -> None:
    result = runner(command, timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise HomepageSyncError("homepage_sync_command_failed", command[0], detail)


def _publish_modes(root: Path) -> None:
    root.chmod(0o755)
    for path in root.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)


def sync_homepage(
    project_root: Path,
    output: Path,
    *,
    data_root: Traversable,
    runner: CommandRunner = _default_runner,
) -> HomepageSyncResult:
    """Fetch knowledge main, replace the public directory, and recreate only Caddy."""

    project_root = project_root.resolve()
    loaded = load_order(project_root)
    artifact_store = Path(loaded.order["runtime"]["artifact_store"]).resolve()
    public_directory = artifact_store.parent / "homepage"
    public_directory.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="mcrctl-homepage-") as temporary_name:
        temporary = Path(temporary_name)
        checkout = temporary / "knowledge"
        _run(
            runner,
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                "main",
                KNOWLEDGE_REPOSITORY,
                str(checkout),
            ],
            timeout=120,
        )
        source = checkout / HOMEPAGE_RELATIVE
        if not source.is_dir() or not (source / "index.html").is_file():
            raise HomepageSyncError(
                "homepage_source_missing",
                source,
                "knowledge main does not contain the homepage directory",
            )
        staging = Path(
            tempfile.mkdtemp(prefix=".homepage.next.", dir=public_directory.parent)
        )
        shutil.copytree(source, staging, dirs_exist_ok=True)
        _publish_modes(staging)

    previous: Path | None = None
    if public_directory.exists():
        previous = Path(
            tempfile.mkdtemp(prefix=".homepage.previous.", dir=public_directory.parent)
        )
        previous.rmdir()
        os.rename(public_directory, previous)
    os.rename(staging, public_directory)

    render_toml_project(project_root, output, data_root=data_root)
    lock = load_lock(project_root, data_root=data_root)
    compose = output / "compose.yaml"
    _run(
        runner,
        [
            "docker",
            "--context",
            "default",
            "compose",
            "--project-name",
            lock["deployment"]["name"],
            "-f",
            str(compose),
            "up",
            "-d",
            "--force-recreate",
            "--no-deps",
            "caddy",
            "--wait",
            "--wait-timeout",
            "60",
        ],
        timeout=120,
    )
    if previous is not None:
        shutil.rmtree(previous)
    return HomepageSyncResult(public_directory)
