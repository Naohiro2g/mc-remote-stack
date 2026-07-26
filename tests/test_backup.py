import hashlib
import json
import os
import ssl
import zipfile
from pathlib import Path

import pytest

from mc_remote_stack.backup import (
    BackupEndpoint,
    BackupTransferError,
    DecryptResult,
    DownloadResult,
    RecordDownloadResult,
    RemoteArchive,
    TransferResult,
    decrypt_downloaded_archive,
    download_remote_archive,
    download_remote_record,
    list_remote_archives,
    load_backup_endpoint,
    ready_outbox_archives,
    transfer_archive,
)
from mc_remote_stack.cli import main
from mc_remote_stack.secrets import set_secret
from mc_remote_stack.validation import load_project
from mc_remote_stack.yamlio import dump_mapping, load_mapping

from .helpers import make_renderable_project


class FakeFtps:
    def __init__(self, *, context: ssl.SSLContext, timeout: int) -> None:
        self.context = context
        self.timeout = timeout
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple] = []

    def connect(self, host: str, port: int) -> None:
        self.calls.append(("connect", host, port))

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", username, password))

    def prot_p(self) -> None:
        self.calls.append(("prot_p",))

    def set_pasv(self, passive: bool) -> None:
        self.calls.append(("set_pasv", passive))

    def cwd(self, directory: str) -> None:
        self.calls.append(("cwd", directory))

    def storbinary(self, command: str, source, blocksize: int) -> None:
        self.files[command.removeprefix("STOR ")] = source.read()

    def voidcmd(self, command: str) -> None:
        self.calls.append(("voidcmd", command))

    def size(self, filename: str) -> int:
        value = self.files.get(filename)
        return None if value is None else len(value)

    def rename(self, source: str, destination: str) -> None:
        self.files[destination] = self.files.pop(source)

    def retrbinary(self, command: str, callback, blocksize: int) -> None:
        callback(self.files[command.removeprefix("RETR ")])

    def nlst(self) -> list[str]:
        return list(self.files)

    def mlsd(self):
        return [
            (
                name,
                {
                    "type": "file",
                    "size": str(len(value)),
                },
            )
            for name, value in self.files.items()
        ]

    def delete(self, filename: str) -> None:
        self.files.pop(filename, None)

    def quit(self) -> None:
        self.calls.append(("quit",))

    def close(self) -> None:
        self.calls.append(("close",))


def configure_ftps(project_root: Path) -> None:
    config_path = project_root / "mc-remote.yml"
    config = load_mapping(config_path)
    config["backup"]["transport"] = {
        "type": "ftps-explicit",
        "host": "sv12345.xserver.jp",
        "port": 21,
        "passive": True,
        "tls_verify": True,
        "username": "vps-backup@example.com",
        "credential": "secret://backup_ftps_password",
        "remote_directory": "/",
        "encryption": {
            "type": "age",
            "recipient": "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq",
        },
    }
    dump_mapping(config_path, config)


def test_private_transport_config_loads_without_password_value(
    tmp_path: Path,
) -> None:
    config = tmp_path / "backup-transport.toml"
    config.write_text(
        """
schema_version = 1

[transport]
type = "ftps-explicit"
host = "sv16181.xserver.jp"
port = 21
passive = true
tls_verify = true
username = "vps-backup@mc-remote.com"
credential = "secret://backup_ftps_password"
remote_directory = "/home/xs814772/VPS_BACKUP"

[transport.encryption]
type = "age"
recipient = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"
""".lstrip(),
        encoding="utf-8",
    )
    config.chmod(0o600)

    endpoint = load_backup_endpoint(
        config,
        deployment_name="official-public-beta",
    )

    assert isinstance(endpoint, BackupEndpoint)
    assert endpoint.config["deployment"]["name"] == "official-public-beta"
    assert endpoint.config["backup"]["transport"]["remote_directory"] == (
        "/home/xs814772/VPS_BACKUP"
    )
    assert "\npassword =" not in config.read_text(encoding="utf-8").lower()


def test_private_transport_config_rejects_embedded_password(
    tmp_path: Path,
) -> None:
    config = tmp_path / "backup-transport.toml"
    config.write_text(
        """
schema_version = 1
[transport]
type = "ftps-explicit"
password = "must-not-be-here"
""".lstrip(),
        encoding="utf-8",
    )
    config.chmod(0o600)

    with pytest.raises(BackupTransferError, match="unknown keys"):
        load_backup_endpoint(config, deployment_name="official-public-beta")


def test_transfer_encrypts_before_explicit_ftps_and_download_verifies(tmp_path: Path, monkeypatch) -> None:
    paths = make_renderable_project(tmp_path)
    configure_ftps(paths.root)
    monkeypatch.setenv("MC_REMOTE_SECRET_HOME", str(tmp_path / "secrets"))
    set_secret("official-vps", "backup_ftps_password", "hidden-password")
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"secret-bearing archive")
    ftps = FakeFtps(context=ssl.create_default_context(), timeout=30)

    def encrypt(source: Path, destination: Path, recipient: str) -> None:
        assert source == archive
        assert recipient.startswith("age1")
        destination.write_bytes(b"AGE-CIPHERTEXT:" + source.read_bytes())

    result = transfer_archive(
        load_project(paths.root),
        archive,
        verify_download=True,
        encrypt=encrypt,
        ftps_factory=lambda **kwargs: ftps,
    )

    assert result.status == "download-verified"
    assert result.remote_name.startswith("backup.zip.")
    assert result.remote_name.endswith(".age")
    assert result.encrypted_sha256 in result.remote_name
    assert ftps.files[result.remote_name] == b"AGE-CIPHERTEXT:secret-bearing archive"
    assert f"{result.remote_name}.transfer.json" in ftps.files
    assert ("connect", "sv12345.xserver.jp", 21) in ftps.calls
    assert ("login", "vps-backup@example.com", "hidden-password") in ftps.calls
    assert ("prot_p",) in ftps.calls
    assert ("set_pasv", True) in ftps.calls
    assert ftps.context.check_hostname is True
    assert ftps.context.verify_mode == ssl.CERT_REQUIRED
    assert archive.exists()
    assert result.encrypted_path.exists()
    assert result.record_path.exists()


def test_transfer_retry_reuses_recorded_ciphertext(tmp_path: Path, monkeypatch) -> None:
    paths = make_renderable_project(tmp_path)
    configure_ftps(paths.root)
    monkeypatch.setenv("MC_REMOTE_SECRET_HOME", str(tmp_path / "secrets"))
    set_secret("official-vps", "backup_ftps_password", "hidden-password")
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"archive")
    ftps = FakeFtps(context=ssl.create_default_context(), timeout=30)
    encryption_calls = 0

    def encrypt(source: Path, destination: Path, recipient: str) -> None:
        nonlocal encryption_calls
        encryption_calls += 1
        destination.write_bytes(b"stable encrypted payload")

    first = transfer_archive(
        load_project(paths.root),
        archive,
        encrypt=encrypt,
        ftps_factory=lambda **kwargs: ftps,
    )
    second = transfer_archive(
        load_project(paths.root),
        archive,
        verify_download=True,
        encrypt=encrypt,
        ftps_factory=lambda **kwargs: ftps,
    )

    assert encryption_calls == 1
    assert first.encrypted_sha256 == second.encrypted_sha256
    assert first.remote_name == second.remote_name
    assert second.status == "download-verified"


def test_ready_outbox_archives_selects_only_stable_valid_zips_after_marker(
    tmp_path: Path,
) -> None:
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    marker = tmp_path / "activated"
    marker.touch()
    os.utime(marker, ns=(1_000_000_000, 1_000_000_000))

    old_archive = outbox / "old.zip"
    ready_archive = outbox / "ready.zip"
    young_archive = outbox / "young.zip"
    for archive in (old_archive, ready_archive, young_archive):
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("world/level.dat", archive.name)
    os.utime(old_archive, ns=(500_000_000, 500_000_000))
    os.utime(ready_archive, ns=(2_000_000_000, 2_000_000_000))
    os.utime(young_archive, ns=(199_500_000_000, 199_500_000_000))

    result = ready_outbox_archives(
        outbox,
        activated_after=marker,
        now=200.0,
        minimum_age_seconds=120,
    )

    assert result == [ready_archive]


def test_ready_outbox_archives_skips_download_verified_transfer(
    tmp_path: Path,
) -> None:
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    marker = tmp_path / "activated"
    marker.touch()
    os.utime(marker, ns=(1_000_000_000, 1_000_000_000))
    archive = outbox / "backup.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("world/level.dat", "world")
    os.utime(archive, ns=(2_000_000_000, 2_000_000_000))
    (outbox / "backup.zip.age.transfer.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "download-verified",
                "source_name": "backup.zip",
                "source_sha256": "1" * 64,
                "encrypted_sha256": "2" * 64,
                "encrypted_size_bytes": 123,
                "remote_name": f"backup.zip.{'2' * 64}.age",
            }
        ),
        encoding="utf-8",
    )

    result = ready_outbox_archives(
        outbox,
        activated_after=marker,
        now=200.0,
    )

    assert result == []


def test_ready_outbox_archives_rejects_corrupt_stable_zip(
    tmp_path: Path,
) -> None:
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    marker = tmp_path / "activated"
    marker.touch()
    os.utime(marker, ns=(1_000_000_000, 1_000_000_000))
    archive = outbox / "backup.zip"
    archive.write_bytes(b"not a zip")
    os.utime(archive, ns=(2_000_000_000, 2_000_000_000))

    with pytest.raises(BackupTransferError, match="not a valid ZIP"):
        ready_outbox_archives(
            outbox,
            activated_after=marker,
            now=200.0,
        )


def test_remote_list_returns_only_completed_age_archives(
    tmp_path: Path, monkeypatch
) -> None:
    paths = make_renderable_project(tmp_path)
    configure_ftps(paths.root)
    monkeypatch.setenv("MC_REMOTE_SECRET_HOME", str(tmp_path / "secrets"))
    set_secret("official-vps", "backup_ftps_password", "hidden-password")
    ftps = FakeFtps(context=ssl.create_default_context(), timeout=30)
    ftps.files = {
        "beta-b.age": b"bb",
        "beta-a.age": b"a",
        ".beta-c.age.uploading": b"partial",
        "notes.txt": b"ignored",
    }

    result = list_remote_archives(
        load_project(paths.root),
        ftps_factory=lambda **kwargs: ftps,
    )

    assert [(item.name, item.size_bytes) for item in result] == [
        ("beta-a.age", 1),
        ("beta-b.age", 2),
    ]
    assert ("prot_p",) in ftps.calls


def test_remote_list_uses_mlsd_facts_when_size_command_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = make_renderable_project(tmp_path)
    configure_ftps(paths.root)
    monkeypatch.setenv("MC_REMOTE_SECRET_HOME", str(tmp_path / "secrets"))
    set_secret("official-vps", "backup_ftps_password", "hidden-password")

    class MlsdOnlySizeFtps(FakeFtps):
        def size(self, filename: str) -> None:
            return None

    ftps = MlsdOnlySizeFtps(
        context=ssl.create_default_context(),
        timeout=30,
    )
    ftps.files = {
        "beta.age": b"ciphertext",
        "beta.age.transfer.json": b"record",
        "legacy.zip": b"legacy",
    }

    result = list_remote_archives(
        load_project(paths.root),
        ftps_factory=lambda **kwargs: ftps,
    )

    assert [(item.name, item.size_bytes) for item in result] == [
        ("beta.age", len(b"ciphertext")),
    ]


def test_remote_download_requires_transfer_record_and_verifies_ciphertext(
    tmp_path: Path, monkeypatch
) -> None:
    paths = make_renderable_project(tmp_path)
    configure_ftps(paths.root)
    monkeypatch.setenv("MC_REMOTE_SECRET_HOME", str(tmp_path / "secrets"))
    set_secret("official-vps", "backup_ftps_password", "hidden-password")
    encrypted = b"AGE-CIPHERTEXT"
    encrypted_sha256 = hashlib.sha256(encrypted).hexdigest()
    remote_name = f"backup.zip.{encrypted_sha256}.age"
    record = tmp_path / "backup.zip.age.transfer.json"
    record.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "download-verified",
                "source_name": "backup.zip",
                "source_sha256": hashlib.sha256(b"archive").hexdigest(),
                "recipient": "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq",
                "encrypted_name": "backup.zip.age",
                "encrypted_sha256": encrypted_sha256,
                "encrypted_size_bytes": len(encrypted),
                "remote_name": remote_name,
            }
        ),
        encoding="utf-8",
    )
    ftps = FakeFtps(context=ssl.create_default_context(), timeout=30)
    ftps.files[remote_name] = encrypted
    output = tmp_path / "recovered" / "backup.zip.age"

    result = download_remote_archive(
        load_project(paths.root),
        remote_name,
        record_path=record,
        output=output,
        ftps_factory=lambda **kwargs: ftps,
    )

    assert result.status == "downloaded-verified"
    assert output.read_bytes() == encrypted
    assert output.stat().st_mode & 0o777 == 0o600
    assert not list(output.parent.glob(".*.downloading"))


def test_remote_download_rejects_record_whose_name_does_not_embed_ciphertext_hash(
    tmp_path: Path, monkeypatch
) -> None:
    paths = make_renderable_project(tmp_path)
    configure_ftps(paths.root)
    monkeypatch.setenv("MC_REMOTE_SECRET_HOME", str(tmp_path / "secrets"))
    set_secret("official-vps", "backup_ftps_password", "hidden-password")
    record = tmp_path / "record.json"
    record.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_sha256": hashlib.sha256(b"archive").hexdigest(),
                "encrypted_sha256": hashlib.sha256(b"ciphertext").hexdigest(),
                "encrypted_size_bytes": len(b"ciphertext"),
                "remote_name": "backup.zip.wrong.age",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        BackupTransferError,
        match="remote_name does not embed encrypted_sha256",
    ):
        download_remote_archive(
            load_project(paths.root),
            "backup.zip.wrong.age",
            record_path=record,
            output=tmp_path / "output.age",
        )


def test_remote_record_download_validates_named_archive(
    tmp_path: Path, monkeypatch
) -> None:
    paths = make_renderable_project(tmp_path)
    configure_ftps(paths.root)
    monkeypatch.setenv("MC_REMOTE_SECRET_HOME", str(tmp_path / "secrets"))
    set_secret("official-vps", "backup_ftps_password", "hidden-password")
    encrypted_sha256 = hashlib.sha256(b"ciphertext").hexdigest()
    remote_name = f"backup.zip.{encrypted_sha256}.age"
    remote_record = f"{remote_name}.transfer.json"
    ftps = FakeFtps(context=ssl.create_default_context(), timeout=30)
    ftps.files[remote_record] = json.dumps(
        {
            "schema_version": 1,
            "status": "download-verified",
            "source_name": "backup.zip",
            "source_sha256": hashlib.sha256(b"archive").hexdigest(),
            "recipient": "age1test",
            "encrypted_name": "backup.zip.age",
            "encrypted_sha256": encrypted_sha256,
            "encrypted_size_bytes": len(b"ciphertext"),
            "remote_name": remote_name,
        }
    ).encode()
    output = tmp_path / "records" / "backup.transfer.json"

    result = download_remote_record(
        load_project(paths.root),
        remote_name,
        output=output,
        ftps_factory=lambda **kwargs: ftps,
    )

    assert result.status == "record-downloaded-verified"
    assert result.remote_record_name == remote_record
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text())["remote_name"] == remote_name


def test_decrypt_downloaded_archive_verifies_plaintext_sha256(tmp_path: Path) -> None:
    encrypted = tmp_path / "backup.zip.age"
    encrypted.write_bytes(b"ciphertext")
    archive = b"archive"
    record = tmp_path / "backup.zip.age.transfer.json"
    record.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_name": "backup.zip",
                "source_sha256": hashlib.sha256(archive).hexdigest(),
                "encrypted_sha256": hashlib.sha256(encrypted.read_bytes()).hexdigest(),
                "encrypted_size_bytes": encrypted.stat().st_size,
                "remote_name": (
                    "backup.zip."
                    f"{hashlib.sha256(encrypted.read_bytes()).hexdigest()}.age"
                ),
            }
        ),
        encoding="utf-8",
    )
    identity = tmp_path / "identity.txt"
    identity.write_text("AGE-SECRET-KEY-TEST", encoding="utf-8")
    output = tmp_path / "backup.zip"

    def decrypt(source: Path, destination: Path, identity_path: Path) -> None:
        assert source == encrypted
        assert identity_path == identity
        destination.write_bytes(archive)

    result = decrypt_downloaded_archive(
        encrypted,
        record_path=record,
        identity=identity,
        output=output,
        decrypt=decrypt,
    )

    assert result.status == "decrypted-verified"
    assert result.archive_sha256 == hashlib.sha256(archive).hexdigest()
    assert output.read_bytes() == archive
    assert output.stat().st_mode & 0o777 == 0o600


def test_ftps_transport_validation_rejects_weakened_tls(tmp_path: Path) -> None:
    paths = make_renderable_project(tmp_path)
    configure_ftps(paths.root)
    config = load_mapping(paths.config)
    config["backup"]["transport"]["tls_verify"] = False
    dump_mapping(paths.config, config)

    from mc_remote_stack.validation import try_load_project

    _, issues = try_load_project(paths.root)

    assert any(issue.path.endswith("backup.transport.tls_verify") and issue.severity == "FAIL" for issue in issues)


def test_cli_backup_transfer_reports_identity_without_secret(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = make_renderable_project(tmp_path)
    configure_ftps(paths.root)
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"archive")
    encrypted = tmp_path / "backup.zip.age"
    record = tmp_path / "backup.zip.age.transfer.json"

    def fake_transfer(
        project,
        source: Path,
        *,
        verify_download: bool,
        progress,
    ):
        assert project.paths.root == paths.root
        assert source == archive
        assert verify_download is True
        progress("encrypting")
        progress("verifying-download")
        return TransferResult(
            status="download-verified",
            source_path=archive,
            source_sha256="1" * 64,
            encrypted_path=encrypted,
            encrypted_sha256="2" * 64,
            encrypted_size_bytes=123,
            remote_name="backup.zip.2222222222222222222222222222222222222222222222222222222222222222.age",
            record_path=record,
        )

    monkeypatch.setattr("mc_remote_stack.cli.transfer_archive", fake_transfer)

    assert (
        main(
            [
                "backup",
                "transfer",
                str(archive),
                "--project",
                str(paths.root),
                "--verify-download",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "STEP backup transfer archive=backup.zip phase=encrypting" in output
    assert (
        "STEP backup transfer archive=backup.zip "
        "phase=verifying-download"
    ) in output
    assert "status=download-verified" in output
    assert "remote=backup.zip.2222222222222222222222222222222222222222222222222222222222222222.age" in output
    assert "hidden-password" not in output


def test_cli_backup_drain_reports_progress_and_forces_download_verification(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    paths = make_renderable_project(tmp_path)
    configure_ftps(paths.root)
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    archive = outbox / "backup.zip"
    archive.write_bytes(b"archive")
    marker = tmp_path / "activated"
    marker.touch()

    def fake_ready(
        selected_outbox: Path,
        *,
        activated_after: Path,
    ) -> list[Path]:
        assert selected_outbox == outbox
        assert activated_after == marker
        return [archive]

    def fake_transfer(
        project,
        source: Path,
        *,
        verify_download: bool,
        progress,
    ) -> TransferResult:
        assert project.paths.root == paths.root
        assert source == archive
        assert verify_download is True
        progress("uploading")
        return TransferResult(
            status="download-verified",
            source_path=archive,
            source_sha256="1" * 64,
            encrypted_path=outbox / "backup.zip.age",
            encrypted_sha256="2" * 64,
            encrypted_size_bytes=123,
            remote_name=f"backup.zip.{'2' * 64}.age",
            record_path=outbox / "backup.zip.age.transfer.json",
        )

    monkeypatch.setattr(
        "mc_remote_stack.cli.ready_outbox_archives",
        fake_ready,
    )
    monkeypatch.setattr(
        "mc_remote_stack.cli.transfer_archive",
        fake_transfer,
    )

    assert (
        main(
            [
                "backup",
                "drain",
                str(outbox),
                "--after",
                str(marker),
                "--project",
                str(paths.root),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "STEP backup drain archive=backup.zip" in output
    assert "STEP backup drain archive=backup.zip phase=uploading" in output
    assert "status=download-verified" in output
    assert "OK backup drain status=complete archives=1" in output


def test_cli_backup_recovery_commands_report_verified_identities(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    paths = make_renderable_project(tmp_path)
    configure_ftps(paths.root)
    record = tmp_path / "backup.zip.age.transfer.json"
    encrypted = tmp_path / "backup.zip.age"
    archive = tmp_path / "backup.zip"
    identity = tmp_path / "identity.txt"

    monkeypatch.setattr(
        "mc_remote_stack.cli.list_remote_archives",
        lambda project: [
            RemoteArchive(name="backup.zip.test.age", size_bytes=123)
        ],
    )

    def fake_download(project, remote_name, *, record_path: Path, output: Path):
        assert project.paths.root == paths.root
        assert remote_name == "backup.zip.test.age"
        assert record_path == record
        assert output == encrypted
        return DownloadResult(
            status="downloaded-verified",
            remote_name=remote_name,
            encrypted_path=encrypted,
            encrypted_sha256="2" * 64,
            encrypted_size_bytes=123,
            record_path=record,
        )

    def fake_decrypt(
        encrypted_path: Path,
        *,
        record_path: Path,
        identity: Path,
        output: Path,
    ):
        assert encrypted_path == encrypted
        assert record_path == record
        assert identity == tmp_path / "identity.txt"
        assert output == archive
        return DecryptResult(
            status="decrypted-verified",
            encrypted_path=encrypted,
            archive_path=archive,
            archive_sha256="1" * 64,
            record_path=record,
        )

    monkeypatch.setattr(
        "mc_remote_stack.cli.download_remote_archive", fake_download
    )
    monkeypatch.setattr(
        "mc_remote_stack.cli.download_remote_record",
        lambda project, remote_name, *, output: RecordDownloadResult(
            status="record-downloaded-verified",
            remote_name=remote_name,
            remote_record_name=f"{remote_name}.transfer.json",
            record_path=output,
        ),
    )
    monkeypatch.setattr(
        "mc_remote_stack.cli.decrypt_downloaded_archive", fake_decrypt
    )

    assert main(["backup", "list", "--project", str(paths.root)]) == 0
    assert (
        main(
            [
                "backup",
                "download-record",
                "backup.zip.test.age",
                "--project",
                str(paths.root),
                "--output",
                str(record),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "backup",
                "download",
                "backup.zip.test.age",
                "--project",
                str(paths.root),
                "--record",
                str(record),
                "--output",
                str(encrypted),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "backup",
                "decrypt",
                str(encrypted),
                "--record",
                str(record),
                "--identity",
                str(identity),
                "--output",
                str(archive),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "REMOTE name=backup.zip.test.age size-bytes=123" in output
    assert "OK backup download-record status=record-downloaded-verified" in output
    assert "OK backup download status=downloaded-verified" in output
    assert "sha256=" + "2" * 64 in output
    assert "OK backup decrypt status=decrypted-verified" in output
    assert "sha256=" + "1" * 64 in output


def test_plan_shows_backup_transport_without_secret_value(tmp_path: Path, capsys) -> None:
    paths = make_renderable_project(tmp_path)
    configure_ftps(paths.root)

    assert main(["plan", "--project", str(paths.root)]) == 0

    output = capsys.readouterr().out
    assert "backup-transport=ftps-explicit" in output
    assert "backup-encryption=age" in output
    assert "backup-remote=sv12345.xserver.jp:/" in output
    assert "secret://backup_ftps_password" not in output
