"""Deployment project defaults owned by mc-remote-stack."""

from copy import deepcopy

OFFICIAL_VPS_CONFIG = {
    "schema_version": 1,
    "deployment": {
        "name": "official-vps",
        "profile": "official-vps",
        "timezone": "Asia/Tokyo",
        "eula_accepted": False,
    },
    "host": {
        "ssh_host": "REPLACE_WITH_HOST",
        "ssh_user": "REPLACE_WITH_ADMIN_USER",
        "paths": {
            "generated": "/etc/mc-remote/generated",
            "secrets": "/etc/mc-remote/secrets",
            "minecraft": "/var/lib/mc-remote/minecraft",
            "backup": "/var/lib/mc-remote/backup",
            "caddy": "/var/lib/mc-remote/caddy",
            "homepage": "/var/lib/mc-remote/homepage",
            "artifacts": "/var/lib/mc-remote/artifacts",
        },
    },
    "domains": {
        "homepage": "mc-remote.com",
        "homepage_aliases": ["www.mc-remote.com"],
        "scratch": "scratch.mc-remote.com",
        "scratch_dev": "scratch-dev.mc-remote.com",
        "bridge": "bridge.mc-remote.com",
        "minecraft": "sb.mc-remote.com",
    },
    "identity": {
        "motd": [
            "§c ==x== §emc-remote §9Sandbox Server §c==x==§r",
            "§6  fueling creativity.§7  |  §dCode2Create.Club",
        ],
    },
    "gameplay": {
        "gamemode": "creative",
        "force_gamemode": True,
        "hardcore": True,
    },
    "world": {
        "radius_blocks": 9984,
        "border_center": [0, 0],
        "spawn_protection_radius": 150,
    },
    "mcremote": {
        "default_origin": [200, 0, 200],
        "default_build_range": 50,
    },
    "performance": {
        "max_tick_time": -1,
        "network_compression_threshold": -1,
    },
    "minecraft": {
        "uid": 10001,
        "gid": 10001,
        "memory": "4G",
        "java_port": 25565,
        "bedrock_port": 25565,
        "mcremote_port": 25575,
        "rcon_enabled": False,
        "console_in_pipe": True,
        "stop_announce_seconds": 60,
        "stop_grace_seconds": 120,
    },
    "plugins": {
        "enabled": [
            "AdvancedPortals",
            "DirectionHUD",
            "DiscordSRV",
            "Geyser-Spigot",
            "LuckPerms",
            "McRemote",
            "ServerBackup",
            "ViaBackwards",
            "ViaVersion",
            "WorldEdit",
            "floodgate",
        ],
    },
    "backup": {
        "source": "@server",
        "output": "/backup/outbox",
        "timezone": "Asia/Tokyo",
        "times": ["04:45", "08:45", "12:45", "15:45", "19:45", "23:45"],
        "transport": "disabled",
    },
}


UNRESOLVED_LOCK = {
    "schema_version": 1,
    "resolved": False,
    "images": {
        "caddy": "REPLACE_WITH_IMAGE_DIGEST",
        "scratch_stable": "REPLACE_WITH_IMAGE_DIGEST",
        "scratch_dev": "REPLACE_WITH_IMAGE_DIGEST",
        "bridge": "REPLACE_WITH_IMAGE_DIGEST",
        "minecraft": "REPLACE_WITH_IMAGE_DIGEST",
    },
    "homepage": {
        "version": "REPLACE_WITH_HOMEPAGE_VERSION",
        "filename": "REPLACE_WITH_HOMEPAGE_ARCHIVE",
        "sha256": "REPLACE",
        "origin": {"kind": "unresolved"},
    },
    "minecraft": {
        "version": "REPLACE_WITH_MINECRAFT_VERSION",
        "paper": {
            "build": "REPLACE_WITH_PAPER_BUILD",
            "filename": "REPLACE_WITH_PAPER_JAR",
            "sha256": "REPLACE",
            "origin": {"kind": "unresolved"},
        },
    },
    "plugins": {
        name: {
            "filename": "REPLACE_WITH_PLUGIN_JAR",
            "version": "REPLACE_WITH_PLUGIN_VERSION",
            "sha256": "REPLACE",
            "origin": {"kind": "unresolved"},
        }
        for name in OFFICIAL_VPS_CONFIG["plugins"]["enabled"]
    },
}


SECRETS_EXAMPLE = {
    "schema_version": 1,
    "required": {
        "backup_ftps_password": "Password used only by the explicit FTPS backup adapter.",
        "discord_bot_token": "Optional DiscordSRV bot token.",
    },
}


def config_for_profile(profile: str) -> dict:
    """Return an independent config mapping for a supported profile."""
    if profile != "official-vps":
        raise ValueError(f"unsupported profile: {profile}")
    return deepcopy(OFFICIAL_VPS_CONFIG)


def unresolved_lock() -> dict:
    """Return an independent unresolved lock mapping."""
    return deepcopy(UNRESOLVED_LOCK)
