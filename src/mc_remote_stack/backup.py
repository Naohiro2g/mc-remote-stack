"""Encrypted off-host backup transfer with explicit FTPS."""

import ftplib
import hashlib
import json
import os
import ssl
import subprocess
import tempfile
import time
import tomllib
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .secrets import read_secret
from .validation import LoadedProject


class BackupTransferError(RuntimeError):
    """Raised when encryption or off-host verification cannot be completed."""


class FtpsClient(Protocol):
    def connect(self, host: str, port: int) -> Any: ...
    def login(self, username: str, password: str) -> Any: ...
    def prot_p(self) -> Any: ...
    def set_pasv(self, passive: bool) -> Any: ...
    def cwd(self, directory: str) -> Any: ...
    def storbinary(self, command: str, source, blocksize: int) -> Any: ...
    def voidcmd(self, command: str) -> Any: ...
    def size(self, filename: str) -> int | None: ...
    def rename(self, source: str, destination: str) -> Any: ...
    def retrbinary(self, command: str, callback, blocksize: int) -> Any: ...
    def nlst(self) -> list[str]: ...
    def mlsd(self) -> Any: ...
    def delete(self, filename: str) -> Any: ...
    def quit(self) -> Any: ...
    def close(self) -> Any: ...


@dataclass(frozen=True)
class TransferResult:
    status: str
    source_path: Path
    source_sha256: str
    encrypted_path: Path
    encrypted_sha256: str
    encrypted_size_bytes: int
    remote_name: str
    record_path: Path


@dataclass(frozen=True)
class BackupEndpoint:
    config: dict[str, Any]


@dataclass(frozen=True)
class RemoteArchive:
    name: str
    size_bytes: int
    record_present: bool


@dataclass(frozen=True)
class DownloadResult:
    status: str
    remote_name: str
    encrypted_path: Path
    encrypted_sha256: str
    encrypted_size_bytes: int
    record_path: Path


@dataclass(frozen=True)
class RecordDownloadResult:
    status: str
    remote_name: str
    remote_record_name: str
    record_path: Path


@dataclass(frozen=True)
class DecryptResult:
    status: str
    encrypted_path: Path
    archive_path: Path
    archive_sha256: str
    record_path: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _encrypt_with_age(source: Path, destination: Path, recipient: str) -> None:
    try:
        subprocess.run(
            ["age", "--encrypt", "--recipient", recipient, "--output", str(destination), str(source)],
            check=True,
        )
    except FileNotFoundError as exc:
        raise BackupTransferError("age executable is required") from exc
    except subprocess.CalledProcessError as exc:
        raise BackupTransferError(f"age encryption failed with exit status {exc.returncode}") from exc


def _decrypt_with_age(source: Path, destination: Path, identity: Path) -> None:
    try:
        subprocess.run(
            [
                "age",
                "--decrypt",
                "--identity",
                str(identity),
                "--output",
                str(destination),
                str(source),
            ],
            check=True,
        )
    except FileNotFoundError as exc:
        raise BackupTransferError("age executable is required") from exc
    except subprocess.CalledProcessError as exc:
        raise BackupTransferError(
            f"age decryption failed with exit status {exc.returncode}"
        ) from exc


def _write_record(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_record(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupTransferError(f"cannot read transfer record: {path}") from exc
    if not isinstance(value, dict):
        raise BackupTransferError(f"transfer record must contain an object: {path}")
    return value


def _record_sha256(record: dict[str, Any], key: str, record_path: Path) -> str:
    value = record.get(key)
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BackupTransferError(
            f"transfer record has invalid {key}: {record_path}"
        )
    return value


def _record_size(record: dict[str, Any], record_path: Path) -> int:
    value = record.get("encrypted_size_bytes")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BackupTransferError(
            f"transfer record has invalid encrypted_size_bytes: {record_path}"
        )
    return value


def _validated_record(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    record = _load_record(resolved)
    if record.get("schema_version") != 1:
        raise BackupTransferError(
            f"unsupported transfer record schema_version: {resolved}"
        )
    _record_sha256(record, "source_sha256", resolved)
    encrypted_sha256 = _record_sha256(
        record,
        "encrypted_sha256",
        resolved,
    )
    _record_size(record, resolved)
    remote_name = record.get("remote_name")
    if (
        not isinstance(remote_name, str)
        or not _safe_remote_name(remote_name)
        or not remote_name.endswith(".age")
    ):
        raise BackupTransferError(
            f"transfer record has invalid remote_name: {resolved}"
        )
    if not remote_name.endswith(f".{encrypted_sha256}.age"):
        raise BackupTransferError(
            f"transfer record remote_name does not embed encrypted_sha256: {resolved}"
        )
    return record


def _safe_remote_name(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and not value.startswith(".")
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
        and not any(ord(character) < 0x20 for character in value)
    )


def ready_outbox_archives(
    outbox: Path,
    *,
    activated_after: Path,
    now: float | None = None,
    minimum_age_seconds: int = 120,
) -> list[Path]:
    """Return complete ZIPs created after an explicit activation marker."""
    resolved_outbox = outbox.expanduser().resolve()
    marker = activated_after.expanduser().resolve()
    if not resolved_outbox.is_dir():
        raise BackupTransferError(
            f"backup outbox is not a directory: {resolved_outbox}"
        )
    if not marker.is_file():
        raise BackupTransferError(
            f"backup activation marker is not a file: {marker}"
        )
    if minimum_age_seconds < 120:
        raise BackupTransferError(
            "backup archive minimum age must be at least 120 seconds"
        )
    current_time = time.time() if now is None else now
    marker_mtime_ns = marker.stat().st_mtime_ns
    ready: list[tuple[int, str, Path]] = []
    for archive in resolved_outbox.iterdir():
        if (
            archive.is_symlink()
            or not archive.is_file()
            or not archive.name.endswith(".zip")
            or not _safe_remote_name(archive.name)
        ):
            continue
        before = archive.stat()
        if before.st_mtime_ns <= marker_mtime_ns:
            continue
        if current_time - before.st_mtime < minimum_age_seconds:
            continue

        record_path = archive.with_name(
            f"{archive.name}.age.transfer.json"
        )
        if record_path.exists():
            record = _validated_record(record_path)
            if (
                record.get("status") == "download-verified"
                and record.get("source_name") == archive.name
            ):
                continue

        try:
            with zipfile.ZipFile(archive) as source:
                corrupt_entry = source.testzip()
        except (OSError, zipfile.BadZipFile) as exc:
            raise BackupTransferError(
                f"backup archive is not a valid ZIP: {archive.name}"
            ) from exc
        if corrupt_entry is not None:
            raise BackupTransferError(
                f"backup archive failed CRC verification: {archive.name}"
            )
        after = archive.stat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise BackupTransferError(
                f"backup archive changed during verification: {archive.name}"
            )
        ready.append((after.st_mtime_ns, archive.name, archive))
    return [archive for _, _, archive in sorted(ready)]


def _transport(project: LoadedProject) -> dict[str, Any]:
    backup = project.config["backup"]
    transport = backup["transport"]
    if not isinstance(transport, dict) or transport.get("type") != "ftps-explicit":
        raise BackupTransferError(
            "backup transport must be configured as ftps-explicit"
        )
    if transport.get("tls_verify") is not True:
        raise BackupTransferError("backup transport requires TLS verification")
    return transport


def load_backup_endpoint(
    path: Path,
    *,
    deployment_name: str,
) -> BackupEndpoint:
    """Load a private transport-only file without accepting embedded secrets."""
    resolved = path.expanduser().resolve()
    try:
        mode = resolved.stat().st_mode & 0o777
        with resolved.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BackupTransferError(
            f"cannot read backup transport config: {resolved}"
        ) from exc
    if mode & 0o037:
        raise BackupTransferError(
            "backup transport config must not be group-writable or accessible "
            "to other users"
        )
    if set(value) != {"schema_version", "transport"}:
        raise BackupTransferError(
            "backup transport config has unknown keys or missing required keys"
        )
    if value.get("schema_version") != 1:
        raise BackupTransferError(
            "backup transport config schema_version must be 1"
        )
    transport = value.get("transport")
    if not isinstance(transport, dict):
        raise BackupTransferError("backup transport must be a table")
    allowed_transport = {
        "type",
        "host",
        "port",
        "passive",
        "tls_verify",
        "username",
        "credential",
        "remote_directory",
        "encryption",
    }
    if set(transport) != allowed_transport:
        raise BackupTransferError(
            "backup transport table has unknown keys or missing required keys"
        )
    if transport.get("type") != "ftps-explicit":
        raise BackupTransferError("backup transport type must be ftps-explicit")
    host = transport.get("host")
    if (
        not isinstance(host, str)
        or not host
        or host != host.lower()
        or any(character.isspace() for character in host)
    ):
        raise BackupTransferError("backup transport host is invalid")
    if transport.get("port") != 21:
        raise BackupTransferError("explicit FTPS requires port 21")
    if transport.get("passive") is not True:
        raise BackupTransferError("backup transport requires passive mode")
    if transport.get("tls_verify") is not True:
        raise BackupTransferError("backup transport requires TLS verification")
    username = transport.get("username")
    if not isinstance(username, str) or not username:
        raise BackupTransferError("backup transport username is required")
    credential = transport.get("credential")
    if (
        not isinstance(credential, str)
        or not credential.startswith("secret://")
        or len(credential) <= len("secret://")
    ):
        raise BackupTransferError(
            "backup transport requires a secret:// credential reference"
        )
    remote_directory = transport.get("remote_directory")
    if (
        not isinstance(remote_directory, str)
        or not remote_directory.startswith("/")
        or ".." in PurePosixPath(remote_directory).parts
    ):
        raise BackupTransferError(
            "backup transport remote_directory must be a safe absolute path"
        )
    encryption = transport.get("encryption")
    if not isinstance(encryption, dict) or set(encryption) != {
        "type",
        "recipient",
    }:
        raise BackupTransferError(
            "backup transport encryption table is invalid"
        )
    recipient = encryption.get("recipient")
    if (
        encryption.get("type") != "age"
        or not isinstance(recipient, str)
        or not recipient.startswith("age1")
        or len(recipient) < 24
    ):
        raise BackupTransferError(
            "backup transport requires a valid public age recipient"
        )
    if (
        not isinstance(deployment_name, str)
        or not deployment_name
        or "/" in deployment_name
        or "\\" in deployment_name
    ):
        raise BackupTransferError("backup secret deployment name is invalid")
    return BackupEndpoint(
        config={
            "deployment": {"name": deployment_name},
            "backup": {"transport": transport},
        }
    )


def _connected_ftps(
    project: LoadedProject,
    *,
    ftps_factory: Callable[..., FtpsClient],
) -> FtpsClient:
    transport = _transport(project)
    deployment_name = project.config["deployment"]["name"]
    password = read_secret(deployment_name, transport["credential"])
    client = ftps_factory(context=ssl.create_default_context(), timeout=30)
    try:
        client.connect(transport["host"], transport["port"])
        client.login(transport["username"], password)
        client.prot_p()
        client.set_pasv(transport["passive"])
        client.cwd(transport["remote_directory"])
        client.voidcmd("TYPE I")
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        raise
    return client


def _remote_size(client: FtpsClient, filename: str) -> int | None:
    try:
        return client.size(filename)
    except ftplib.error_perm as exc:
        if str(exc).startswith("550"):
            return None
        raise


def _download_sha256(client: FtpsClient, remote_name: str) -> str:
    digest = hashlib.sha256()
    client.retrbinary(f"RETR {remote_name}", digest.update, 1024 * 1024)
    return digest.hexdigest()


def _record_identity(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record.get(key)
        for key in (
            "schema_version",
            "source_name",
            "source_sha256",
            "recipient",
            "encrypted_name",
            "encrypted_sha256",
            "encrypted_size_bytes",
            "remote_name",
        )
    }


def _download_bytes(
    client: FtpsClient,
    remote_name: str,
    *,
    maximum_size: int,
) -> bytes:
    chunks: list[bytes] = []
    received = 0

    def receive(chunk: bytes) -> None:
        nonlocal received
        received += len(chunk)
        if received > maximum_size:
            raise BackupTransferError(
                f"remote metadata exceeds {maximum_size} bytes"
            )
        chunks.append(chunk)

    client.retrbinary(f"RETR {remote_name}", receive, 64 * 1024)
    return b"".join(chunks)


def _publish_record_sidecar(
    client: FtpsClient,
    *,
    remote_name: str,
    record_path: Path,
    record: dict[str, Any],
) -> None:
    remote_record = f"{remote_name}.transfer.json"
    existing_size = _remote_size(client, remote_record)
    if existing_size is not None:
        if existing_size > 1024 * 1024:
            raise BackupTransferError(
                "remote transfer record exceeds the metadata size limit"
            )
        try:
            existing = json.loads(
                _download_bytes(
                    client,
                    remote_record,
                    maximum_size=1024 * 1024,
                )
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupTransferError(
                "remote transfer record is not valid JSON"
            ) from exc
        if (
            not isinstance(existing, dict)
            or _record_identity(existing) != _record_identity(record)
        ):
            raise BackupTransferError(
                "remote transfer record does not match the local record"
            )
        return

    temporary_remote = f".{remote_record}.{uuid.uuid4().hex}.uploading"
    uploaded_temporary = False
    try:
        with record_path.open("rb") as source:
            client.storbinary(f"STOR {temporary_remote}", source, 64 * 1024)
        uploaded_temporary = True
        if _remote_size(client, temporary_remote) != record_path.stat().st_size:
            raise BackupTransferError(
                "temporary remote transfer record size does not match"
            )
        client.rename(temporary_remote, remote_record)
        uploaded_temporary = False
        if _remote_size(client, remote_record) != record_path.stat().st_size:
            raise BackupTransferError(
                "final remote transfer record size does not match"
            )
    except Exception:
        if uploaded_temporary:
            try:
                client.delete(temporary_remote)
            except Exception:
                pass
        raise


def list_remote_archives(
    project: LoadedProject,
    *,
    ftps_factory: Callable[..., FtpsClient] = ftplib.FTP_TLS,
) -> list[RemoteArchive]:
    """List completed encrypted archives without selecting one implicitly."""
    client = _connected_ftps(project, ftps_factory=ftps_factory)
    try:
        try:
            mlsd_entries = list(client.mlsd())
        except (AttributeError, ftplib.error_perm):
            mlsd_entries = None
        if mlsd_entries is None:
            listed_names = {
                name
                for name in client.nlst()
                if _safe_remote_name(name)
            }
            archives = [
                RemoteArchive(
                    name=name,
                    size_bytes=size,
                    record_present=(
                        f"{name}.transfer.json" in listed_names
                    ),
                )
                for name in listed_names
                if name.endswith(".age")
                and (size := _remote_size(client, name)) is not None
            ]
        else:
            archives = []
            listed_names = {
                name
                for name, facts in mlsd_entries
                if (
                    _safe_remote_name(name)
                    and isinstance(facts, dict)
                    and facts.get("type") == "file"
                )
            }
            for name, facts in mlsd_entries:
                if (
                    not _safe_remote_name(name)
                    or not name.endswith(".age")
                    or not isinstance(facts, dict)
                    or facts.get("type") != "file"
                ):
                    continue
                raw_size = facts.get("size")
                try:
                    size = int(raw_size)
                except (TypeError, ValueError):
                    continue
                if size < 0:
                    continue
                archives.append(
                    RemoteArchive(
                        name=name,
                        size_bytes=size,
                        record_present=(
                            f"{name}.transfer.json" in listed_names
                        ),
                    )
                )
        client.quit()
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        raise
    return sorted(archives, key=lambda archive: archive.name)


def download_remote_record(
    project: LoadedProject,
    remote_name: str,
    *,
    output: Path,
    ftps_factory: Callable[..., FtpsClient] = ftplib.FTP_TLS,
) -> RecordDownloadResult:
    """Download the recovery sidecar for one explicitly named ciphertext."""
    if not _safe_remote_name(remote_name) or not remote_name.endswith(".age"):
        raise BackupTransferError("remote archive must be a safe .age filename")
    remote_record = f"{remote_name}.transfer.json"
    destination = output.expanduser().resolve()
    if destination.exists():
        raise BackupTransferError(
            f"record destination already exists: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".downloading",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    client: FtpsClient | None = None
    try:
        client = _connected_ftps(project, ftps_factory=ftps_factory)
        remote_size = _remote_size(client, remote_record)
        if remote_size is None:
            raise BackupTransferError(
                f"remote transfer record does not exist: {remote_record}"
            )
        if remote_size > 1024 * 1024:
            raise BackupTransferError(
                "remote transfer record exceeds the metadata size limit"
            )
        with temporary.open("wb") as stream:
            client.retrbinary(f"RETR {remote_record}", stream.write, 64 * 1024)
        client.quit()
        client = None
        if temporary.stat().st_size != remote_size:
            raise BackupTransferError(
                "downloaded transfer record size does not match the remote"
            )
        record = _validated_record(temporary)
        if record["remote_name"] != remote_name:
            raise BackupTransferError(
                "downloaded transfer record names a different archive"
            )
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    except Exception:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        raise
    finally:
        if temporary.exists():
            temporary.unlink()
    return RecordDownloadResult(
        status="record-downloaded-verified",
        remote_name=remote_name,
        remote_record_name=remote_record,
        record_path=destination,
    )


def download_remote_archive(
    project: LoadedProject,
    remote_name: str,
    *,
    record_path: Path,
    output: Path,
    ftps_factory: Callable[..., FtpsClient] = ftplib.FTP_TLS,
) -> DownloadResult:
    """Download one explicitly named ciphertext and verify its transfer record."""
    if not _safe_remote_name(remote_name) or not remote_name.endswith(".age"):
        raise BackupTransferError("remote archive must be a safe .age filename")
    resolved_record = record_path.expanduser().resolve()
    record = _validated_record(resolved_record)
    if record["remote_name"] != remote_name:
        raise BackupTransferError(
            "requested remote archive does not match the transfer record"
        )
    expected_sha256 = _record_sha256(
        record, "encrypted_sha256", resolved_record
    )
    expected_size = _record_size(record, resolved_record)

    destination = output.expanduser().resolve()
    if destination.exists():
        raise BackupTransferError(f"download destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".downloading",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)

    client: FtpsClient | None = None
    try:
        client = _connected_ftps(project, ftps_factory=ftps_factory)
        if _remote_size(client, remote_name) != expected_size:
            raise BackupTransferError(
                "remote size does not match the transfer record"
            )
        with temporary.open("wb") as stream:
            client.retrbinary(
                f"RETR {remote_name}",
                stream.write,
                1024 * 1024,
            )
        client.quit()
        client = None
        if temporary.stat().st_size != expected_size:
            raise BackupTransferError(
                "downloaded size does not match the transfer record"
            )
        if _sha256(temporary) != expected_sha256:
            raise BackupTransferError(
                "downloaded SHA-256 does not match the transfer record"
            )
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    except Exception:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        raise
    finally:
        if temporary.exists():
            temporary.unlink()

    return DownloadResult(
        status="downloaded-verified",
        remote_name=remote_name,
        encrypted_path=destination,
        encrypted_sha256=expected_sha256,
        encrypted_size_bytes=expected_size,
        record_path=resolved_record,
    )


def decrypt_downloaded_archive(
    encrypted: Path,
    *,
    record_path: Path,
    identity: Path,
    output: Path,
    decrypt: Callable[[Path, Path, Path], None] = _decrypt_with_age,
) -> DecryptResult:
    """Decrypt a verified ciphertext and verify the original archive SHA-256."""
    encrypted_path = encrypted.expanduser().resolve()
    identity_path = identity.expanduser().resolve()
    destination = output.expanduser().resolve()
    resolved_record = record_path.expanduser().resolve()
    record = _validated_record(resolved_record)
    if not encrypted_path.is_file():
        raise BackupTransferError(
            f"encrypted archive does not exist: {encrypted_path}"
        )
    if not identity_path.is_file():
        raise BackupTransferError(f"age identity does not exist: {identity_path}")
    if destination.exists():
        raise BackupTransferError(
            f"decryption destination already exists: {destination}"
        )
    if encrypted_path.stat().st_size != _record_size(record, resolved_record):
        raise BackupTransferError(
            "encrypted archive size does not match the transfer record"
        )
    if _sha256(encrypted_path) != _record_sha256(
        record, "encrypted_sha256", resolved_record
    ):
        raise BackupTransferError(
            "encrypted archive SHA-256 does not match the transfer record"
        )

    expected_archive_sha256 = _record_sha256(
        record, "source_sha256", resolved_record
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".decrypting",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    try:
        decrypt(encrypted_path, temporary, identity_path)
        if not temporary.is_file():
            raise BackupTransferError("decryption did not create an output file")
        if _sha256(temporary) != expected_archive_sha256:
            raise BackupTransferError(
                "decrypted archive SHA-256 does not match the transfer record"
            )
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()

    return DecryptResult(
        status="decrypted-verified",
        encrypted_path=encrypted_path,
        archive_path=destination,
        archive_sha256=expected_archive_sha256,
        record_path=resolved_record,
    )


def transfer_archive(
    project: LoadedProject,
    archive: Path,
    *,
    verify_download: bool = False,
    encrypt: Callable[[Path, Path, str], None] = _encrypt_with_age,
    ftps_factory: Callable[..., FtpsClient] = ftplib.FTP_TLS,
    progress: Callable[[str], None] | None = None,
) -> TransferResult:
    transport = _transport(project)

    archive = archive.expanduser().resolve()
    if not archive.is_file():
        raise BackupTransferError(f"archive does not exist: {archive}")
    if archive.name.endswith(".age"):
        raise BackupTransferError("source archive must be plaintext input, not an .age file")
    if not _safe_remote_name(archive.name):
        raise BackupTransferError(
            "source archive filename is not safe for remote transfer"
        )

    encryption = transport["encryption"]
    recipient = encryption["recipient"]
    source_sha256 = _sha256(archive)
    encrypted_path = archive.with_name(f"{archive.name}.age")
    record_path = encrypted_path.with_name(f"{encrypted_path.name}.transfer.json")
    if encrypted_path.exists() or record_path.exists():
        if progress is not None:
            progress("reusing-ciphertext")
        if not encrypted_path.is_file() or not record_path.is_file():
            raise BackupTransferError("encrypted archive and transfer record must either both exist or both be absent")
        record = _load_record(record_path)
        encrypted_sha256 = _sha256(encrypted_path)
        encrypted_size = encrypted_path.stat().st_size
        expected_remote_name = f"{archive.name}.{encrypted_sha256}.age"
        expected = {
            "schema_version": 1,
            "source_name": archive.name,
            "source_sha256": source_sha256,
            "recipient": recipient,
            "encrypted_name": encrypted_path.name,
            "encrypted_sha256": encrypted_sha256,
            "encrypted_size_bytes": encrypted_size,
            "remote_name": expected_remote_name,
        }
        if any(record.get(key) != value for key, value in expected.items()):
            raise BackupTransferError("existing encrypted archive does not match its transfer record")
        remote_name = expected_remote_name
    else:
        if progress is not None:
            progress("encrypting")
        temporary_encrypted = encrypted_path.with_name(f".{encrypted_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            encrypt(archive, temporary_encrypted, recipient)
            if not temporary_encrypted.is_file():
                raise BackupTransferError("encryption did not create an output file")
            os.chmod(temporary_encrypted, 0o600)
            os.replace(temporary_encrypted, encrypted_path)
        finally:
            if temporary_encrypted.exists():
                temporary_encrypted.unlink()

        encrypted_sha256 = _sha256(encrypted_path)
        encrypted_size = encrypted_path.stat().st_size
        remote_name = f"{archive.name}.{encrypted_sha256}.age"
        record = {
            "schema_version": 1,
            "status": "encrypted",
            "source_name": archive.name,
            "source_sha256": source_sha256,
            "recipient": recipient,
            "encrypted_name": encrypted_path.name,
            "encrypted_sha256": encrypted_sha256,
            "encrypted_size_bytes": encrypted_size,
            "remote_name": remote_name,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        _write_record(record_path, record)

    if progress is not None:
        progress("connecting")
    client = _connected_ftps(project, ftps_factory=ftps_factory)
    temporary_remote = f".{remote_name}.{uuid.uuid4().hex}.uploading"
    uploaded_temporary = False
    try:
        existing_size = _remote_size(client, remote_name)
        if existing_size is not None and existing_size != encrypted_size:
            raise BackupTransferError(f"remote file already exists with different size: {remote_name}")
        if existing_size is None:
            if progress is not None:
                progress("uploading")
            with encrypted_path.open("rb") as source:
                client.storbinary(f"STOR {temporary_remote}", source, 1024 * 1024)
            uploaded_temporary = True
            if _remote_size(client, temporary_remote) != encrypted_size:
                raise BackupTransferError("temporary remote size does not match encrypted archive")
            client.rename(temporary_remote, remote_name)
            uploaded_temporary = False
        if _remote_size(client, remote_name) != encrypted_size:
            raise BackupTransferError("final remote size does not match encrypted archive")

        status = "remote-size-verified"
        if verify_download:
            if progress is not None:
                progress("verifying-download")
            if _download_sha256(client, remote_name) != encrypted_sha256:
                raise BackupTransferError("downloaded remote SHA-256 does not match encrypted archive")
            status = "download-verified"
        record["status"] = status
        record["updated_at"] = datetime.now(UTC).isoformat()
        _write_record(record_path, record)
        if progress is not None:
            progress("publishing-record")
        _publish_record_sidecar(
            client,
            remote_name=remote_name,
            record_path=record_path,
            record=record,
        )
        client.quit()
    except Exception:
        if uploaded_temporary:
            try:
                client.delete(temporary_remote)
            except Exception:
                pass
        try:
            client.close()
        except Exception:
            pass
        raise

    return TransferResult(
        status=status,
        source_path=archive,
        source_sha256=source_sha256,
        encrypted_path=encrypted_path,
        encrypted_sha256=encrypted_sha256,
        encrypted_size_bytes=encrypted_size,
        remote_name=remote_name,
        record_path=record_path,
    )
