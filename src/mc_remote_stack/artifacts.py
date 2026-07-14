"""Content-addressed import of exact runtime artifacts."""

import hashlib
import os
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .project import ProjectPaths
from .yamlio import load_mapping

MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class ImportedArtifact:
    name: str
    filename: str
    sha256: str
    path: Path
    status: str


def default_artifact_store() -> Path:
    configured = os.environ.get("MC_REMOTE_ARTIFACT_HOME")
    if configured:
        return Path(configured).expanduser().resolve() / "sha256"
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return (cache_home / "mc-remote" / "artifacts" / "sha256").resolve()


def _sha256_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _append_recovery_artifacts(artifacts: list[tuple[str, dict]], locked: dict, prefix: str = "") -> None:
    minecraft = locked.get("minecraft")
    if isinstance(minecraft, dict):
        paper = minecraft.get("paper")
        if isinstance(paper, dict) and isinstance(paper.get("origin"), dict):
            if paper["origin"].get("kind") == "recovery_archive":
                artifacts.append((f"{prefix}Paper", paper))
    plugins = locked.get("plugins")
    if isinstance(plugins, dict):
        for name, artifact in plugins.items():
            if not isinstance(name, str) or not isinstance(artifact, dict):
                continue
            origin = artifact.get("origin")
            if isinstance(origin, dict) and origin.get("kind") == "recovery_archive":
                artifacts.append((f"{prefix}{name}", artifact))


def _recovery_artifacts(lock: dict) -> list[tuple[str, dict]]:
    artifacts: list[tuple[str, dict]] = []
    _append_recovery_artifacts(artifacts, lock)
    staging = lock.get("staging")
    if isinstance(staging, dict):
        _append_recovery_artifacts(artifacts, staging, "staging/")
    return artifacts


def _import_member(
    archive: zipfile.ZipFile,
    infos_by_name: dict[str, list[zipfile.ZipInfo]],
    name: str,
    artifact: dict,
    store: Path,
) -> ImportedArtifact:
    origin = artifact["origin"]
    member = origin["member"]
    entries = infos_by_name.get(member, [])
    if len(entries) != 1 or entries[0].is_dir():
        raise ValueError(f"{name}: archive member must exist exactly once: {member}")
    entry = entries[0]
    if entry.file_size <= 0 or entry.file_size > MAX_ARTIFACT_BYTES:
        raise ValueError(f"{name}: archive member size is outside the artifact limit: {member}")

    expected_sha256 = artifact["sha256"]
    destination = store / expected_sha256
    if destination.exists():
        with destination.open("rb") as existing:
            if _sha256_stream(existing) != expected_sha256:
                raise ValueError(f"{name}: content-addressed store entry has the wrong SHA-256")
        return ImportedArtifact(name, artifact["filename"], expected_sha256, destination, "present")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=store, prefix=".import-", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            digest = hashlib.sha256()
            with archive.open(entry) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                    temporary.write(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())
        if digest.hexdigest() != expected_sha256:
            raise ValueError(f"{name}: artifact SHA-256 does not match lock: {member}")
        temporary_path.chmod(0o644)
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return ImportedArtifact(name, artifact["filename"], expected_sha256, destination, "imported")


def import_recovery_archive(
    project_root: Path,
    archive_path: Path,
    store: Path | None = None,
) -> list[ImportedArtifact]:
    """Import only lock-named JARs from a verified recovery ZIP."""
    paths = ProjectPaths(project_root.resolve())
    lock = load_mapping(paths.lock)
    archive_path = archive_path.resolve()
    with archive_path.open("rb") as stream:
        archive_sha256 = _sha256_stream(stream)

    candidates = _recovery_artifacts(lock)
    expected_archives = {artifact["origin"].get("archive_sha256") for _, artifact in candidates}
    if archive_sha256 not in expected_archives:
        raise ValueError(f"archive SHA-256 is not referenced by the lock: {archive_sha256}")
    selected = [
        (name, artifact)
        for name, artifact in candidates
        if artifact["origin"].get("archive_sha256") == archive_sha256
    ]

    artifact_store = (store or default_artifact_store()).resolve()
    artifact_store.mkdir(mode=0o755, parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        infos_by_name: dict[str, list[zipfile.ZipInfo]] = {}
        for info in archive.infolist():
            infos_by_name.setdefault(info.filename, []).append(info)
        return [
            _import_member(archive, infos_by_name, name, artifact, artifact_store)
            for name, artifact in selected
        ]
