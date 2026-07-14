"""Encrypted off-host backup transfer with explicit FTPS."""

import ftplib
import hashlib
import json
import os
import ssl
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
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


def transfer_archive(
    project: LoadedProject,
    archive: Path,
    *,
    verify_download: bool = False,
    encrypt: Callable[[Path, Path, str], None] = _encrypt_with_age,
    ftps_factory: Callable[..., FtpsClient] = ftplib.FTP_TLS,
) -> TransferResult:
    backup = project.config["backup"]
    transport = backup["transport"]
    if not isinstance(transport, dict) or transport.get("type") != "ftps-explicit":
        raise BackupTransferError("backup transport must be configured as ftps-explicit")

    archive = archive.expanduser().resolve()
    if not archive.is_file():
        raise BackupTransferError(f"archive does not exist: {archive}")
    if archive.name.endswith(".age"):
        raise BackupTransferError("source archive must be plaintext input, not an .age file")

    encryption = transport["encryption"]
    recipient = encryption["recipient"]
    source_sha256 = _sha256(archive)
    encrypted_path = archive.with_name(f"{archive.name}.age")
    record_path = encrypted_path.with_name(f"{encrypted_path.name}.transfer.json")
    if encrypted_path.exists() or record_path.exists():
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

    deployment_name = project.config["deployment"]["name"]
    password = read_secret(deployment_name, transport["credential"])
    context = ssl.create_default_context()
    client = ftps_factory(context=context, timeout=30)
    temporary_remote = f".{remote_name}.{uuid.uuid4().hex}.uploading"
    uploaded_temporary = False
    try:
        client.connect(transport["host"], transport["port"])
        client.login(transport["username"], password)
        client.prot_p()
        client.set_pasv(transport["passive"])
        client.cwd(transport["remote_directory"])
        client.voidcmd("TYPE I")

        existing_size = _remote_size(client, remote_name)
        if existing_size is not None and existing_size != encrypted_size:
            raise BackupTransferError(f"remote file already exists with different size: {remote_name}")
        if existing_size is None:
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
            if _download_sha256(client, remote_name) != encrypted_sha256:
                raise BackupTransferError("downloaded remote SHA-256 does not match encrypted archive")
            status = "download-verified"
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

    record["status"] = status
    record["updated_at"] = datetime.now(UTC).isoformat()
    _write_record(record_path, record)
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
