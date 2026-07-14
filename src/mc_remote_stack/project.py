"""Deployment project creation and path conventions."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .defaults import SECRETS_EXAMPLE, config_for_profile, unresolved_lock
from .yamlio import dump_mapping


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / "mc-remote.yml"

    @property
    def lock(self) -> Path:
        return self.root / "mc-remote.lock.yml"


PROJECT_GITIGNORE = """/generated/
/secrets/
/.env
*.secret
*.zip
*.tar
*.tar.gz
"""


PROJECT_README = """# McRemote deployment project

This directory contains desired configuration and immutable artifact identities for one deployment.

- Secrets are stored with `mcrctl secret set`, outside this Git directory.
- Generated Compose, environment, and plugin configuration must not be committed.
- Runtime backups must not be committed.
"""


def init_project(root: Path, profile: str) -> ProjectPaths:
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"refusing to initialize non-empty directory: {root}")

    root.mkdir(parents=True, exist_ok=True)
    (root / "overrides").mkdir()
    (root / "records").mkdir()
    (root / ".gitignore").write_text(PROJECT_GITIGNORE, encoding="utf-8")
    (root / "README.md").write_text(PROJECT_README, encoding="utf-8")
    dump_mapping(root / "mc-remote.yml", config_for_profile(profile))
    dump_mapping(root / "mc-remote.lock.yml", unresolved_lock())
    dump_mapping(root / "secrets.example.yml", SECRETS_EXAMPLE)
    return ProjectPaths(root)


def accept_eula(root: Path) -> ProjectPaths:
    paths = ProjectPaths(root.resolve())
    from .yamlio import load_mapping

    config = load_mapping(paths.config)
    deployment = config.get("deployment")
    if not isinstance(deployment, dict):
        raise ValueError("mc-remote.yml: deployment must be a mapping")
    deployment["eula_accepted"] = True
    deployment["eula_accepted_at"] = datetime.now(UTC).isoformat()
    dump_mapping(paths.config, config)
    return paths
