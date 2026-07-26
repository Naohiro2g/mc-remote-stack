import json
from pathlib import Path

from mc_remote_stack.cli import main
from mc_remote_stack.runtime_audit import audit_minecraft_log


def test_runtime_audit_classifies_downloads_without_copying_url_paths(
    tmp_path: Path,
) -> None:
    log = tmp_path / "minecraft.log"
    log.write_text(
        "\n".join(
            [
                "[INFO]: [SpigotLibraryLoader] [DirectionHUD] Loading 1 libraries",
                "[INFO]: [SpigotLibraryLoader] Downloading "
                "https://repo.example.test/private/path?token=secret",
                "[INFO]: [Geyser-Spigot] Downloading Minecraft JAR to extract required files",
                "[INFO]: [ServerBackup] ServerBackup: Searching for updates...",
                "[WARN]: [ViaVersion] There is a newer plugin version available",
                "[INFO]: Done",
            ]
        ),
        encoding="utf-8",
    )

    result = audit_minecraft_log(log)

    assert result["event_count"] == 4
    assert result["events"] == [
        {
            "category": "paper-library-download",
            "component": "DirectionHUD",
            "host": "repo.example.test",
            "count": 1,
        },
        {
            "category": "runtime-content-download",
            "component": "Geyser-Spigot",
            "host": None,
            "count": 1,
        },
        {
            "category": "update-check",
            "component": "ServerBackup",
            "host": None,
            "count": 1,
        },
        {
            "category": "update-check",
            "component": "ViaVersion",
            "host": None,
            "count": 1,
        },
    ]
    assert "secret" not in json.dumps(result)
    assert "private/path" not in json.dumps(result)
    assert result["limitations"] == [
        "only explicit matching log events are reported",
        "absence of events does not prove absence of runtime network access",
    ]


def test_runtime_audit_cli_emits_json(tmp_path: Path, capsys) -> None:
    log = tmp_path / "minecraft.log"
    log.write_text(
        "[INFO]: [Geyser-Spigot] Downloading Minecraft JAR to extract required files\n",
        encoding="utf-8",
    )

    assert main(["runtime", "audit-log", str(log), "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["event_count"] == 1
    assert output["events"][0]["component"] == "Geyser-Spigot"
