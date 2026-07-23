"""Content-addressed import of exact runtime artifacts."""

import hashlib
import http.client
import os
import tempfile
import urllib.error
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .project import ProjectPaths
from .resolver import inspect_lock, load_lock
from .yamlio import load_mapping

MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class ImportedArtifact:
    name: str
    filename: str
    sha256: str
    path: Path
    status: str


class ArtifactFetchError(ValueError):
    """Stable, fail-closed diagnostic for lock-backed artifact acquisition."""

    def __init__(self, reason: str, path: Path | str, message: str) -> None:
        self.reason = reason
        self.path = path
        super().__init__(f"{reason}: {path}: {message}")


@dataclass(frozen=True)
class FetchedArtifact:
    id: str
    filename: str
    sha256: str
    path: Path
    status: str


class _InsecureArtifactRedirect(ValueError):
    pass


class _HttpsOnlyRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: BinaryIO,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        parsed = urlsplit(newurl)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise _InsecureArtifactRedirect(
                "artifact redirect requires a credential-free HTTPS URL"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_HTTPS_ONLY_OPENER = build_opener(_HttpsOnlyRedirectHandler())


def _fetch_fail(reason: str, path: Path | str, message: str) -> None:
    raise ArtifactFetchError(reason, path, message)


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


def _verify_store_entry(
    destination: Path,
    *,
    artifact_id: str,
    expected_sha256: str,
) -> bool:
    if not destination.exists() and not destination.is_symlink():
        return False
    if destination.is_symlink() or not destination.is_file():
        _fetch_fail(
            "artifact_store_tampered",
            destination,
            f"{artifact_id}: content-addressed entry must be a regular file",
        )
    try:
        with destination.open("rb") as existing:
            actual_sha256 = _sha256_stream(existing)
    except OSError as exc:
        _fetch_fail(
            "artifact_store_tampered",
            destination,
            f"{artifact_id}: cannot verify content-addressed entry: {exc}",
        )
    if actual_sha256 != expected_sha256:
        _fetch_fail(
            "artifact_store_tampered",
            destination,
            f"{artifact_id}: expected SHA-256 {expected_sha256}, found {actual_sha256}",
        )
    return True


def _validate_https_url(value: str, *, logical_path: str, redirect: bool) -> None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        reason = "artifact_redirect_insecure" if redirect else "artifact_origin_invalid"
        _fetch_fail(reason, logical_path, "artifact acquisition requires an HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        reason = "artifact_redirect_insecure" if redirect else "artifact_origin_invalid"
        _fetch_fail(reason, logical_path, "credentials in artifact URLs are forbidden")


def _default_open_url(request: Request, *, timeout: int) -> Any:
    return _HTTPS_ONLY_OPENER.open(request, timeout=timeout)


def _response_content_length(response: Any, *, logical_path: str) -> int | None:
    raw_length = response.headers.get("Content-Length")
    if raw_length is None:
        return None
    try:
        length = int(raw_length)
    except (TypeError, ValueError):
        _fetch_fail(
            "artifact_download_failed",
            logical_path,
            "response Content-Length is not a valid integer",
        )
    if length < 0:
        _fetch_fail(
            "artifact_download_failed",
            logical_path,
            "response Content-Length must not be negative",
        )
    if length > MAX_ARTIFACT_BYTES:
        _fetch_fail(
            "artifact_too_large",
            logical_path,
            f"response exceeds the {MAX_ARTIFACT_BYTES}-byte artifact limit",
        )
    return length


def _download_locked_artifact(
    artifact: dict[str, Any],
    *,
    digest_store: Path,
    open_url: Callable[..., Any],
) -> FetchedArtifact:
    artifact_id = artifact["id"]
    expected_sha256 = artifact["sha256"]
    destination = digest_store / expected_sha256
    if _verify_store_entry(
        destination,
        artifact_id=artifact_id,
        expected_sha256=expected_sha256,
    ):
        return FetchedArtifact(
            artifact_id,
            artifact["filename"],
            expected_sha256,
            destination,
            "present",
        )

    logical_path = f"artifacts.{artifact_id}.origin"
    origin = artifact["origin"]
    _validate_https_url(origin, logical_path=logical_path, redirect=False)
    request = Request(
        origin,
        headers={"User-Agent": f"mcrctl/{__version__}"},
        method="GET",
    )
    try:
        response = open_url(request, timeout=60)
    except _InsecureArtifactRedirect as exc:
        _fetch_fail("artifact_redirect_insecure", logical_path, str(exc))
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError) as exc:
        _fetch_fail("artifact_download_failed", logical_path, str(exc))

    temporary_path: Path | None = None
    try:
        with response as source:
            final_url = source.geturl()
            _validate_https_url(final_url, logical_path=logical_path, redirect=True)
            _response_content_length(source, logical_path=logical_path)

            with tempfile.NamedTemporaryFile(
                dir=digest_store,
                prefix=".fetch-",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                digest = hashlib.sha256()
                size = 0
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_ARTIFACT_BYTES:
                        _fetch_fail(
                            "artifact_too_large",
                            logical_path,
                            f"response exceeds the {MAX_ARTIFACT_BYTES}-byte artifact limit",
                        )
                    digest.update(chunk)
                    temporary.write(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != expected_sha256:
            _fetch_fail(
                "artifact_digest_mismatch",
                logical_path,
                f"{artifact_id}: expected SHA-256 {expected_sha256}, found {actual_sha256}",
            )
        temporary_path.chmod(0o644)
        try:
            os.link(temporary_path, destination)
        except FileExistsError:
            _verify_store_entry(
                destination,
                artifact_id=artifact_id,
                expected_sha256=expected_sha256,
            )
            status = "present"
        else:
            directory_descriptor = os.open(digest_store, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            status = "fetched"
    except ArtifactFetchError:
        raise
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError) as exc:
        _fetch_fail("artifact_download_failed", logical_path, str(exc))
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return FetchedArtifact(
        artifact_id,
        artifact["filename"],
        expected_sha256,
        destination,
        status,
    )


def fetch_locked_artifacts(
    project_root: Path,
    *,
    data_root: Traversable,
    open_url: Callable[..., Any] | None = None,
) -> list[FetchedArtifact]:
    """Fetch exact HTTPS file artifacts from one current, self-verifying TOML lock."""

    project_root = project_root.resolve()
    inspection = inspect_lock(project_root, data_root=data_root)
    lock_path = project_root / "mc-remote.lock.toml"
    if inspection.status == "missing":
        _fetch_fail(
            "lock_missing",
            lock_path,
            "resolve the project before fetching artifacts",
        )
    if inspection.status == "stale":
        _fetch_fail(
            "stale_lock",
            lock_path,
            "order or exact bundled input changed; run mcrctl resolve explicitly",
        )
    lock = load_lock(project_root, data_root=data_root)
    artifacts = [
        artifact
        for artifact in lock["artifacts"]
        if artifact["kind"] == "https-file"
    ]
    if not artifacts:
        return []

    digest_store = Path(lock["runtime"]["artifact_store"]).resolve() / "sha256"
    try:
        digest_store.mkdir(mode=0o755, parents=True, exist_ok=True)
    except OSError as exc:
        _fetch_fail("artifact_store_write_failed", digest_store, str(exc))
    if digest_store.is_symlink() or not digest_store.is_dir():
        _fetch_fail(
            "artifact_store_write_failed",
            digest_store,
            "content-addressed store must be a directory",
        )

    opener = open_url or _default_open_url
    return [
        _download_locked_artifact(
            artifact,
            digest_store=digest_store,
            open_url=opener,
        )
        for artifact in artifacts
    ]


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
    beta = lock.get("beta")
    if isinstance(beta, dict):
        _append_recovery_artifacts(artifacts, beta, "beta/")
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
        (name, artifact) for name, artifact in candidates if artifact["origin"].get("archive_sha256") == archive_sha256
    ]

    artifact_store = (store or default_artifact_store()).resolve()
    artifact_store.mkdir(mode=0o755, parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        infos_by_name: dict[str, list[zipfile.ZipInfo]] = {}
        for info in archive.infolist():
            infos_by_name.setdefault(info.filename, []).append(info)
        return [_import_member(archive, infos_by_name, name, artifact, artifact_store) for name, artifact in selected]
