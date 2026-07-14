import ssl
from pathlib import Path

from mc_remote_stack.backup import TransferResult, transfer_archive
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

    def fake_transfer(project, source: Path, *, verify_download: bool):
        assert project.paths.root == paths.root
        assert source == archive
        assert verify_download is True
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
    assert "status=download-verified" in output
    assert "remote=backup.zip.2222222222222222222222222222222222222222222222222222222222222222.age" in output
    assert "hidden-password" not in output


def test_plan_shows_backup_transport_without_secret_value(tmp_path: Path, capsys) -> None:
    paths = make_renderable_project(tmp_path)
    configure_ftps(paths.root)

    assert main(["plan", "--project", str(paths.root)]) == 0

    output = capsys.readouterr().out
    assert "backup-transport=ftps-explicit" in output
    assert "backup-encryption=age" in output
    assert "backup-remote=sv12345.xserver.jp:/" in output
    assert "secret://backup_ftps_password" not in output
