from pathlib import Path

import yaml

from mc_remote_stack.render import render_project
from mc_remote_stack.validation import load_project

from .helpers import enable_renderable_beta, make_renderable_project


def test_render_compose_preserves_security_and_volume_boundaries(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    output = tmp_path / "generated"

    render_project(load_project(project.root), output)

    compose = yaml.safe_load((output / "compose.yaml").read_text(encoding="utf-8"))
    minecraft = compose["services"]["minecraft-stable"]
    assert minecraft["environment"]["ENABLE_RCON"] == "false"
    assert minecraft["environment"]["CREATE_CONSOLE_IN_PIPE"] == "true"
    assert "/var/lib/mc-remote/minecraft:/data" in minecraft["volumes"]
    assert "/var/lib/mc-remote/backup:/backup" in minecraft["volumes"]
    assert f"/var/lib/mc-remote/artifacts/sha256/{10:064x}:/artifacts/paper-26.1.2-72.jar:ro" in minecraft["volumes"]
    assert any(volume.endswith(":/plugins/plugin-20.jar:ro") for volume in minecraft["volumes"])
    assert "25575:25575/tcp" in minecraft["ports"]
    assert minecraft["environment"]["PAPER_CUSTOM_JAR"] == "/artifacts/paper-26.1.2-72.jar"
    assert "PAPER_BUILD" not in minecraft["environment"]
    assert minecraft["environment"]["REMOVE_OLD_MODS"] == "true"
    assert minecraft["environment"]["REMOVE_OLD_MODS_DEPTH"] == "1"
    assert minecraft["environment"]["SKIP_DOWNLOAD_DEFAULTS"] == "true"


def test_render_bridge_uses_current_env_contract_and_internal_sandbox_alias(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    output = tmp_path / "generated"

    render_project(load_project(project.root), output)

    compose = yaml.safe_load((output / "compose.yaml").read_text(encoding="utf-8"))
    bridge = compose["services"]["bridge-stable"]
    assert bridge["environment"] == {
        "BRIDGE_WS_HOST": "0.0.0.0",
        "BRIDGE_WS_PORT": "8080",
        "BRIDGE_ORIGIN_ALLOWLIST": "https://scratch.mc-remote.com",
        "BRIDGE_SANDBOX_ALLOWLIST": "sb.mc-remote.com",
        "BRIDGE_DEFAULT_SANDBOX": "sb.mc-remote.com",
        "BRIDGE_SANDBOX_PORT": "25575",
    }
    assert "ports" not in bridge
    assert compose["services"]["minecraft-stable"]["networks"]["app"]["aliases"] == ["sb.mc-remote.com"]


def test_render_serverbackup_uses_fixed_schedule_and_external_outbox(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    output = tmp_path / "generated"

    render_project(load_project(project.root), output)

    backup_path = output / "minecraft-stable" / "plugins" / "ServerBackup" / "config.yml"
    backup = yaml.safe_load(backup_path.read_text(encoding="utf-8"))
    assert backup["BackupWorlds"] == ["@server"]
    assert backup["BackupDestination"] == "/backup/outbox"
    assert backup["BackupTimer"]["Times"] == ["04-45", "08-45", "12-45", "15-45", "19-45", "23-45"]
    assert backup["BackupLimiter"] == 0
    assert backup["AutomaticUpdates"] is False
    assert backup["Ftp"]["UploadBackup"] is False


def test_render_runtime_and_bridge_route_use_same_public_identity(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    output = tmp_path / "generated"

    render_project(load_project(project.root), output)

    runtime = (output / "runtime" / "stable.json").read_text(encoding="utf-8")
    routes = yaml.safe_load((output / "bridge" / "routes.yml").read_text(encoding="utf-8"))
    assert '"default_sandbox": "sb.mc-remote.com"' in runtime
    assert routes["routes"]["sb.mc-remote.com"] == {"host": "minecraft-stable", "port": 25575}


def test_render_caddyfile_has_one_site_block_for_each_bridge_channel(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    output = tmp_path / "generated"

    render_project(load_project(project.root), output)

    caddyfile = (output / "Caddyfile").read_text(encoding="utf-8")
    assert caddyfile.count("bridge.mc-remote.com {") == 1
    assert caddyfile.count("bridge-beta.mc-remote.com {") == 1


def test_render_caddy_serves_locked_homepage_without_another_public_port(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    output = tmp_path / "generated"

    render_project(load_project(project.root), output)

    compose = yaml.safe_load((output / "compose.yaml").read_text(encoding="utf-8"))
    caddy = compose["services"]["caddy"]
    assert f"/var/lib/mc-remote/homepage/sha256/{11:064x}:/srv/homepage:ro" in caddy["volumes"]
    assert caddy["ports"] == ["80:80/tcp", "443:443/tcp"]
    assert caddy["cap_drop"] == ["ALL"]
    assert caddy["cap_add"] == ["NET_BIND_SERVICE"]
    assert "homepage" not in compose["services"]

    caddyfile = (output / "Caddyfile").read_text(encoding="utf-8")
    assert "mc-remote.com, www.mc-remote.com {" in caddyfile
    assert "    root * /srv/homepage\n" in caddyfile
    assert "    encode zstd gzip\n" in caddyfile
    assert "    file_server\n" in caddyfile


def test_render_minecraft_native_config_preserves_classroom_policy(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    output = tmp_path / "generated"

    render_project(load_project(project.root), output)

    properties = (output / "minecraft-stable" / "server.properties").read_text(encoding="utf-8")
    assert "gamemode=creative\n" in properties
    assert "force-gamemode=true\n" in properties
    assert "hardcore=true\n" in properties
    assert "max-world-size=9984\n" in properties
    assert "spawn-protection=150\n" in properties
    assert "max-tick-time=-1\n" in properties
    assert "network-compression-threshold=-1\n" in properties
    assert "enable-rcon=false\n" in properties

    spigot = yaml.safe_load((output / "minecraft-stable" / "spigot.yml").read_text(encoding="utf-8"))
    bukkit = yaml.safe_load((output / "minecraft-stable" / "bukkit.yml").read_text(encoding="utf-8"))
    assert spigot["settings"]["restart-on-crash"] is False
    assert bukkit["settings"]["connection-throttle"] == 4000


def test_render_beta_is_dormant_and_has_separate_state_ports_and_artifacts(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    enable_renderable_beta(project)
    output = tmp_path / "generated"

    render_project(load_project(project.root), output)

    compose = yaml.safe_load((output / "compose.yaml").read_text(encoding="utf-8"))
    beta = compose["services"]["minecraft-beta"]
    assert beta["profiles"] == ["beta"]
    assert beta["ports"] == ["25565:25565/tcp", "25565:19132/udp", "25575:25575/tcp"]
    assert "/var/lib/mc-remote/minecraft-beta:/data" in beta["volumes"]
    assert "/var/lib/mc-remote/backup-beta:/backup" in beta["volumes"]
    assert beta["image"].endswith(f"@sha256:{40:064x}")
    assert beta["environment"]["PAPER_CUSTOM_JAR"] == "/artifacts/paper-1.21.11-132.jar"
    assert beta["networks"]["app"]["aliases"] == ["sb-beta.mc-remote.com"]


def test_render_beta_routes_scratch_beta_without_changing_stable_default(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    enable_renderable_beta(project)
    output = tmp_path / "generated"

    render_project(load_project(project.root), output)

    compose = yaml.safe_load((output / "compose.yaml").read_text(encoding="utf-8"))
    stable_bridge = compose["services"]["bridge-stable"]["environment"]
    beta_bridge = compose["services"]["bridge-beta"]["environment"]
    stable_runtime = (output / "runtime" / "stable.json").read_text(encoding="utf-8")
    dev_runtime = (output / "runtime" / "beta.json").read_text(encoding="utf-8")
    routes = yaml.safe_load((output / "bridge" / "routes.yml").read_text(encoding="utf-8"))

    assert stable_bridge["BRIDGE_SANDBOX_ALLOWLIST"] == "sb.mc-remote.com"
    assert stable_bridge["BRIDGE_DEFAULT_SANDBOX"] == "sb.mc-remote.com"
    assert beta_bridge["BRIDGE_SANDBOX_ALLOWLIST"] == "sb-beta.mc-remote.com"
    assert beta_bridge["BRIDGE_DEFAULT_SANDBOX"] == "sb-beta.mc-remote.com"
    assert '"default_sandbox": "sb.mc-remote.com"' in stable_runtime
    assert '"default_sandbox": "sb-beta.mc-remote.com"' in dev_runtime
    assert routes["routes"]["sb-beta.mc-remote.com"] == {"host": "minecraft-beta", "port": 25575}


def test_render_beta_uses_daily_0333_backup_schedule(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    enable_renderable_beta(project)
    output = tmp_path / "generated"

    render_project(load_project(project.root), output)

    path = output / "minecraft-beta" / "plugins" / "ServerBackup" / "config.yml"
    backup = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert backup["AutomaticBackups"] is True
    assert backup["BackupTimer"]["Times"] == ["03-33"]
    assert backup["BackupDestination"] == "/backup/outbox"


def test_render_beta_includes_exclusive_instance_switch_operations(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    enable_renderable_beta(project)
    output = tmp_path / "generated"

    render_project(load_project(project.root), output)

    use_beta = (output / "operations" / "use-beta.sh").read_text(encoding="utf-8")
    use_stable = (output / "operations" / "use-stable.sh").read_text(encoding="utf-8")
    assert "mc-send-to-console save-all flush" in use_beta
    assert "docker compose --profile stable stop --timeout 120 minecraft-stable" in use_beta
    assert "docker compose --profile beta up -d minecraft-beta" in use_beta
    assert "</dev/tcp/127.0.0.1/25565" in use_beta
    assert "</dev/tcp/127.0.0.1/25575" in use_beta
    assert "docker compose --profile beta stop --timeout 120 minecraft-beta" in use_stable
    assert "docker compose --profile stable up -d minecraft-stable" in use_stable
    assert "</dev/tcp/127.0.0.1/25565" in use_stable
    assert "</dev/tcp/127.0.0.1/25575" in use_stable
    assert "grep -Fq 'Done ('" not in use_beta + use_stable
