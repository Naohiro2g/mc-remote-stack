import hashlib
import io
import json
import zipfile
from pathlib import Path

from mc_remote_stack.archive import inspect_archive
from mc_remote_stack.cli import main


def _make_archive(path: Path) -> bytes:
    plugin = b"test plugin jar"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("paper-1.21.11-132.jar", b"paper server")
        archive.writestr("plugins/McRemote.jar", plugin)
        archive.writestr("plugins/LuckPerms/libs/caffeine.jar", b"nested library")
        archive.writestr("plugins/.paper-remapped/McRemote.jar", b"remapped plugin")
        archive.writestr("world/region/r.0.0.mca", b"region data")
        archive.writestr("plugins/DiscordSRV/config.yml", "BotToken: secret-value\n")
    return plugin


def _plugin_jar(descriptor_path: str, descriptor: str) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as plugin:
        plugin.writestr(descriptor_path, descriptor)
        plugin.writestr("com/example/Plugin.class", b"bytecode")
    return stream.getvalue()


def test_inspect_archive_hashes_without_exposing_config_content(tmp_path: Path) -> None:
    path = tmp_path / "backup.zip"
    plugin = _make_archive(path)

    result = inspect_archive(path)

    assert result["archive_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["crc_ok"] is True
    assert result["region_files"] == 1
    assert result["ignored_nested_plugin_jars"] == 2
    assert result["plugin_jars"] == [
        {
            "filename": "McRemote.jar",
            "sha256": hashlib.sha256(plugin).hexdigest(),
            "size_bytes": len(plugin),
            "descriptor": {"status": "invalid-jar"},
        }
    ]
    assert result["server_jars"] == [
        {
            "filename": "paper-1.21.11-132.jar",
            "sha256": hashlib.sha256(b"paper server").hexdigest(),
            "size_bytes": len(b"paper server"),
        }
    ]
    assert "secret-value" not in json.dumps(result)


def test_inspect_archive_reads_only_lock_safe_plugin_identity(tmp_path: Path) -> None:
    path = tmp_path / "backup.zip"
    plugin = _plugin_jar(
        "plugin.yml",
        "name: McRemote\nversion: 1.21.8-1.4.0\napi-version: '1.21'\nmain: com.example.McRemote\n"
        "libraries:\n  - org.example:runtime-lib:1.2.3\n"
        "commands:\n  mcremote:\n    description: do not copy arbitrary descriptor fields\n",
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugins/McRemote.jar", plugin)

    result = inspect_archive(path)

    assert result["plugin_jars"][0]["descriptor"] == {
        "status": "ok",
        "path": "plugin.yml",
        "name": "McRemote",
        "version": "1.21.8-1.4.0",
        "api_version": "1.21",
        "main": "com.example.McRemote",
        "runtime_libraries": ["org.example:runtime-lib:1.2.3"],
    }
    assert "commands" not in json.dumps(result)


def test_archive_cli_emits_machine_readable_inventory(tmp_path: Path, capsys) -> None:
    path = tmp_path / "backup.zip"
    _make_archive(path)

    assert main(["archive", "inspect", str(path), "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["crc_ok"] is True
    assert output["plugin_jars"][0]["filename"] == "McRemote.jar"
