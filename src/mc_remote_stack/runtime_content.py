"""Content-addressed storage for deployment-owned runtime composition bytes."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .preset_registry import semantic_sha256

MAX_RUNTIME_FILE_BYTES = 512 * 1024 * 1024
MAX_HOMEPAGE_FILES = 4096
MAX_HOMEPAGE_BYTES = 128 * 1024 * 1024


class RuntimeContentError(ValueError):
    """Stable fail-closed diagnostic for deployment-owned runtime content."""

    def __init__(self, reason: str, path: Path | str, message: str) -> None:
        self.reason = reason
        self.path = str(path)
        super().__init__(f"{reason}: {path}: {message}")


@dataclass(frozen=True)
class ImportedRuntimeFile:
    path: Path
    sha256: str
    status: str


@dataclass(frozen=True)
class HomepageTree:
    path: Path
    tree_sha256: str
    file_count: int
    total_bytes: int
    status: str


def _fail(reason: str, path: Path | str, message: str) -> None:
    raise RuntimeContentError(reason, path, message)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        _fail("runtime_content_read_failed", path, str(exc))
    return digest.hexdigest()


def _require_regular_file(path: Path) -> None:
    if path.is_symlink():
        _fail("runtime_content_symlink_forbidden", path, "symbolic links are forbidden")
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        _fail("runtime_content_read_failed", path, str(exc))
    if not stat.S_ISREG(mode):
        _fail("runtime_content_not_regular", path, "content must be one regular file")
    if path.stat().st_size <= 0 or path.stat().st_size > MAX_RUNTIME_FILE_BYTES:
        _fail(
            "runtime_content_size_invalid",
            path,
            f"file size must be between 1 and {MAX_RUNTIME_FILE_BYTES} bytes",
        )


def import_runtime_file(
    source: Path,
    store: Path,
    *,
    expected_sha256: str,
) -> ImportedRuntimeFile:
    """Import one exact regular file without overwriting a digest collision."""

    source = source.expanduser().absolute()
    _require_regular_file(source)
    source = source.resolve()
    actual = _sha256_file(source)
    if actual != expected_sha256:
        _fail(
            "runtime_content_digest_mismatch",
            source,
            f"expected SHA-256 {expected_sha256}, found {actual}",
        )
    digest_store = store.resolve() / "sha256"
    digest_store.mkdir(mode=0o755, parents=True, exist_ok=True)
    destination = digest_store / expected_sha256
    if destination.exists() or destination.is_symlink():
        _require_regular_file(destination)
        if _sha256_file(destination) != expected_sha256:
            _fail(
                "runtime_content_store_tampered",
                destination,
                "content-addressed entry has the wrong SHA-256",
            )
        return ImportedRuntimeFile(destination, expected_sha256, "present")

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=digest_store,
            prefix=".runtime-import-",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            with source.open("rb") as source_stream:
                shutil.copyfileobj(source_stream, stream, 1024 * 1024)
            stream.flush()
            os.fsync(stream.fileno())
        copied_sha256 = _sha256_file(temporary)
        if copied_sha256 != expected_sha256:
            _fail(
                "runtime_content_digest_mismatch",
                temporary,
                f"copied bytes changed: expected {expected_sha256}, found {copied_sha256}",
            )
        temporary.chmod(0o644)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            _require_regular_file(destination)
            if _sha256_file(destination) != expected_sha256:
                _fail(
                    "runtime_content_store_tampered",
                    destination,
                    "content-addressed entry has the wrong SHA-256",
                )
            status = "present"
        else:
            status = "imported"
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return ImportedRuntimeFile(destination, expected_sha256, status)


def _tree_inventory(root: Path) -> tuple[list[dict[str, object]], int]:
    if root.is_symlink():
        _fail("runtime_content_symlink_forbidden", root, "symbolic links are forbidden")
    if not root.is_dir():
        _fail("runtime_content_not_directory", root, "homepage content must be a directory")
    records: list[dict[str, object]] = []
    total_bytes = 0
    for candidate in sorted(root.rglob("*")):
        if candidate.is_symlink():
            _fail("runtime_content_symlink_forbidden", candidate, "symbolic links are forbidden")
        mode = candidate.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            _fail(
                "runtime_content_not_regular",
                candidate,
                "homepage entries must be regular files",
            )
        relative = candidate.relative_to(root).as_posix()
        path = PurePosixPath(relative)
        if any(part in {"", ".", ".."} for part in path.parts):
            _fail("runtime_content_path_invalid", candidate, "homepage path is unsafe")
        size = candidate.stat().st_size
        total_bytes += size
        records.append(
            {"path": relative, "bytes": size, "sha256": _sha256_file(candidate)}
        )
        if len(records) > MAX_HOMEPAGE_FILES or total_bytes > MAX_HOMEPAGE_BYTES:
            _fail(
                "runtime_content_size_invalid",
                root,
                "homepage tree exceeds its file-count or byte limit",
            )
    if not records or not any(record["path"] == "index.html" for record in records):
        _fail(
            "runtime_content_index_missing",
            root,
            "homepage tree must contain a top-level index.html",
        )
    return records, total_bytes


def verify_homepage_tree(root: Path, expected_sha256: str) -> HomepageTree:
    records, total_bytes = _tree_inventory(root)
    actual = semantic_sha256({"schema_version": 1, "files": records})
    if actual != expected_sha256:
        _fail(
            "runtime_content_digest_mismatch",
            root,
            f"expected tree SHA-256 {expected_sha256}, found {actual}",
        )
    return HomepageTree(root, actual, len(records), total_bytes, "present")


def import_homepage_tree(source: Path, store: Path) -> HomepageTree:
    """Copy one safe static tree into a content-addressed immutable directory."""

    source = source.expanduser().absolute()
    if source.is_symlink():
        _fail("runtime_content_symlink_forbidden", source, "symbolic links are forbidden")
    source = source.resolve(strict=False)
    records, total_bytes = _tree_inventory(source)
    tree_sha256 = semantic_sha256({"schema_version": 1, "files": records})
    tree_store = store.resolve() / "trees" / "sha256"
    tree_store.mkdir(mode=0o755, parents=True, exist_ok=True)
    destination = tree_store / tree_sha256
    if destination.exists() or destination.is_symlink():
        verified = verify_homepage_tree(destination, tree_sha256)
        return HomepageTree(
            destination,
            tree_sha256,
            verified.file_count,
            verified.total_bytes,
            "present",
        )

    temporary = Path(tempfile.mkdtemp(prefix=".homepage-import-", dir=tree_store))
    moved = False
    try:
        for record in records:
            relative = PurePosixPath(str(record["path"]))
            target = temporary / relative
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            shutil.copyfile(source / relative, target)
            target.chmod(0o644)
        verify_homepage_tree(temporary, tree_sha256)
        try:
            os.rename(temporary, destination)
        except FileExistsError:
            verify_homepage_tree(destination, tree_sha256)
        else:
            moved = True
    finally:
        if not moved and temporary.exists():
            shutil.rmtree(temporary)
    verified = verify_homepage_tree(destination, tree_sha256)
    return HomepageTree(
        destination,
        tree_sha256,
        verified.file_count,
        verified.total_bytes,
        "imported",
    )
