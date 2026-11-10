"""Permissions for owned render trees; never apply this to operator/live data."""

import os
import stat
from pathlib import Path, PurePosixPath

_CONTAINER_FILES = frozenset({
    "Caddyfile",
    "runtime/scratch.json",
    "minecraft/server.properties",
    "minecraft/plugins/McRemote/config.yml",
    "runtime/minecraft/plugins/McRemote/config.yml",
})


def _set_mode(path: Path, mode: int) -> None:
    if stat.S_IMODE(path.stat().st_mode) == mode:
        return
    path.chmod(mode)
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def rendered_file_mode(relative: str) -> int:
    """Only known, credential-free container inputs are world-readable."""
    path = PurePosixPath(relative)
    if relative in _CONTAINER_FILES:
        return 0o644
    if len(path.parts) > 1 and path.parts[0] == "wirescope":
        return 0o644  # Contents of the verified public application bundle.
    if len(path.parts) == 3 and path.parts[:2] == ("runtime", "web-pairs") and path.suffix == ".json":
        return 0o644  # Validated public browser runtime configuration.
    return 0o600  # Compose, manifests, locks, and any future unclassified file.


def set_render_modes(root: Path) -> None:
    """Normalize a generated tree while retaining a private outer boundary."""
    paths = [root, *root.rglob("*")]
    if any(path.is_symlink() for path in paths):
        raise ValueError("symlinks are forbidden in a generated render tree")
    public_directories: set[Path] = set()
    for path in paths[1:]:
        if path.is_file():
            mode = rendered_file_mode(path.relative_to(root).as_posix())
            _set_mode(path, mode)
            if mode == 0o644:
                parent = path.parent
                while parent != root:
                    public_directories.add(parent)
                    parent = parent.parent
    for path in paths:
        if path.is_dir():
            _set_mode(path, 0o755 if path in public_directories else 0o700)


def render_modes_equal(expected: Path, actual: Path) -> bool:
    """Compare permissions separately from the byte-only lock verification."""
    def inventory(root: Path) -> dict[str, int]:
        return {str(path.relative_to(root)): stat.S_IMODE(path.stat().st_mode)
                for path in [root, *root.rglob("*")]}

    return inventory(expected) == inventory(actual)
