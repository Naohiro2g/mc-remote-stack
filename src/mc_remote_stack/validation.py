"""Schema and cross-field validation for deployment projects."""

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .project import ProjectPaths
from .yamlio import YamlError, load_mapping

IMAGE_DIGEST = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TIME_OF_DAY = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DOMAIN_NAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
SECRET_REFERENCE = re.compile(r"^secret://[a-z][a-z0-9_]{1,63}$")
AGE_RECIPIENT = re.compile(r"^age1[0-9a-z]{20,}$")


@dataclass(frozen=True)
class Issue:
    severity: str
    path: str
    message: str


@dataclass(frozen=True)
class LoadedProject:
    paths: ProjectPaths
    config: dict[str, Any]
    lock: dict[str, Any]


def _mapping(parent: dict[str, Any], key: str, issues: list[Issue], path: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        issues.append(Issue("FAIL", path, "must be a mapping"))
        return {}
    return value


def _validate_origin(origin: object, issues: list[Issue], path: str) -> None:
    if not isinstance(origin, dict):
        issues.append(Issue("FAIL", path, "must be a mapping"))
        return
    kind = origin.get("kind")
    if kind == "https":
        url = origin.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            issues.append(Issue("FAIL", f"{path}.url", "HTTPS artifact URL is required"))
        return
    if kind == "recovery_archive":
        archive_sha256 = origin.get("archive_sha256")
        if not isinstance(archive_sha256, str) or not SHA256.fullmatch(archive_sha256):
            issues.append(Issue("FAIL", f"{path}.archive_sha256", "exact archive SHA-256 is required"))
        member = origin.get("member")
        if not isinstance(member, str) or not member or "\\" in member:
            issues.append(Issue("FAIL", f"{path}.member", "safe archive member path is required"))
            return
        member_path = PurePosixPath(member)
        if member_path.is_absolute() or ".." in member_path.parts or member_path.suffix.lower() != ".jar":
            issues.append(Issue("FAIL", f"{path}.member", "safe JAR archive member path is required"))
        return
    issues.append(Issue("FAIL", f"{path}.kind", "must be https or recovery_archive"))


def _validate_artifact(artifact: dict[str, Any], issues: list[Issue], path: str) -> None:
    filename = artifact.get("filename")
    if (
        not isinstance(filename, str)
        or not filename
        or filename.startswith("REPLACE_")
        or Path(filename).name != filename
        or not filename.lower().endswith(".jar")
    ):
        issues.append(Issue("FAIL", f"{path}.filename", "safe JAR filename is required"))
    sha256 = artifact.get("sha256")
    if not isinstance(sha256, str) or not SHA256.fullmatch(sha256):
        issues.append(Issue("FAIL", f"{path}.sha256", "exact SHA-256 is required"))
    _validate_origin(artifact.get("origin"), issues, f"{path}.origin")


def _validate_homepage_origin(origin: object, issues: list[Issue], path: str) -> None:
    if not isinstance(origin, dict):
        issues.append(Issue("FAIL", path, "must be a mapping"))
        return
    if origin.get("kind") != "source_archive":
        _validate_origin(origin, issues, path)
        return

    archive_sha256 = origin.get("archive_sha256")
    if not isinstance(archive_sha256, str) or not SHA256.fullmatch(archive_sha256):
        issues.append(Issue("FAIL", f"{path}.archive_sha256", "exact source archive SHA-256 is required"))
    archive_filename = origin.get("archive_filename")
    if (
        not isinstance(archive_filename, str)
        or not archive_filename.lower().endswith(".zip")
        or Path(archive_filename).name != archive_filename
    ):
        issues.append(Issue("FAIL", f"{path}.archive_filename", "safe source ZIP filename is required"))

    source_root = origin.get("source_root")
    if not isinstance(source_root, str) or not source_root or "\\" in source_root:
        issues.append(Issue("FAIL", f"{path}.source_root", "safe relative source root is required"))
    else:
        source_path = PurePosixPath(source_root)
        if source_path.is_absolute() or ".." in source_path.parts:
            issues.append(Issue("FAIL", f"{path}.source_root", "safe relative source root is required"))

    excluded = origin.get("excluded")
    if not isinstance(excluded, list):
        issues.append(Issue("FAIL", f"{path}.excluded", "must contain relative excluded paths"))
        return
    for index, value in enumerate(excluded):
        if not isinstance(value, str) or not value or "\\" in value:
            issues.append(Issue("FAIL", f"{path}.excluded[{index}]", "safe relative excluded path is required"))
            continue
        excluded_path = PurePosixPath(value)
        if excluded_path.is_absolute() or ".." in excluded_path.parts:
            issues.append(Issue("FAIL", f"{path}.excluded[{index}]", "safe relative excluded path is required"))


def _validate_static_archive(artifact: dict[str, Any], issues: list[Issue], path: str) -> None:
    version = artifact.get("version")
    if not isinstance(version, str) or not version or version.startswith("REPLACE_"):
        issues.append(Issue("FAIL", f"{path}.version", "exact homepage version is required"))
    filename = artifact.get("filename")
    if (
        not isinstance(filename, str)
        or not filename
        or filename.startswith("REPLACE_")
        or Path(filename).name != filename
        or not filename.lower().endswith(".tar.gz")
    ):
        issues.append(Issue("FAIL", f"{path}.filename", "safe .tar.gz homepage archive filename is required"))
    sha256 = artifact.get("sha256")
    if not isinstance(sha256, str) or not SHA256.fullmatch(sha256):
        issues.append(Issue("FAIL", f"{path}.sha256", "exact SHA-256 is required"))
    _validate_homepage_origin(artifact.get("origin"), issues, f"{path}.origin")


def _validate_backup_transport(value: object, issues: list[Issue], path: str) -> None:
    if value == "disabled":
        issues.append(Issue("WARN", path, "off-host transfer is not configured"))
        return
    if not isinstance(value, dict):
        issues.append(Issue("FAIL", path, "must be disabled or an adapter mapping"))
        return
    if value.get("type") != "ftps-explicit":
        issues.append(Issue("FAIL", f"{path}.type", "initial adapter must be ftps-explicit"))
    host = value.get("host")
    if not isinstance(host, str) or not DOMAIN_NAME.fullmatch(host):
        issues.append(Issue("FAIL", f"{path}.host", "valid lowercase FTPS hostname is required"))
    if value.get("port") != 21:
        issues.append(Issue("FAIL", f"{path}.port", "explicit FTPS uses port 21"))
    if value.get("passive") is not True:
        issues.append(Issue("FAIL", f"{path}.passive", "XServer FTPS requires passive mode"))
    if value.get("tls_verify") is not True:
        issues.append(Issue("FAIL", f"{path}.tls_verify", "TLS certificate verification must be enabled"))
    username = value.get("username")
    if not isinstance(username, str) or not username:
        issues.append(Issue("FAIL", f"{path}.username", "FTPS username is required"))
    credential = value.get("credential")
    if not isinstance(credential, str) or not SECRET_REFERENCE.fullmatch(credential):
        issues.append(Issue("FAIL", f"{path}.credential", "secret:// credential reference is required"))
    remote_directory = value.get("remote_directory")
    if (
        not isinstance(remote_directory, str)
        or not remote_directory.startswith("/")
        or ".." in PurePosixPath(remote_directory).parts
    ):
        issues.append(Issue("FAIL", f"{path}.remote_directory", "safe absolute FTP account path is required"))
    encryption = value.get("encryption")
    if not isinstance(encryption, dict):
        issues.append(Issue("FAIL", f"{path}.encryption", "age encryption mapping is required"))
        return
    if encryption.get("type") != "age":
        issues.append(Issue("FAIL", f"{path}.encryption.type", "backup payload encryption must use age"))
    recipient = encryption.get("recipient")
    if not isinstance(recipient, str) or not AGE_RECIPIENT.fullmatch(recipient):
        issues.append(Issue("FAIL", f"{path}.encryption.recipient", "valid public age recipient is required"))


def load_project(root: Path) -> LoadedProject:
    paths = ProjectPaths(root.resolve())
    return LoadedProject(paths, load_mapping(paths.config), load_mapping(paths.lock))


def validate_project(project: LoadedProject) -> list[Issue]:
    issues: list[Issue] = []
    config = project.config
    lock = project.lock

    if config.get("schema_version") != 1:
        issues.append(Issue("FAIL", "mc-remote.yml:schema_version", "must be 1"))
    if lock.get("schema_version") != 1:
        issues.append(Issue("FAIL", "mc-remote.lock.yml:schema_version", "must be 1"))

    deployment = _mapping(config, "deployment", issues, "mc-remote.yml:deployment")
    if deployment.get("profile") != "official-vps":
        issues.append(Issue("FAIL", "mc-remote.yml:deployment.profile", "unsupported profile"))
    if deployment.get("timezone") != "Asia/Tokyo":
        issues.append(Issue("WARN", "mc-remote.yml:deployment.timezone", "official preset is Asia/Tokyo"))
    if deployment.get("eula_accepted") is not True:
        issues.append(Issue("FAIL", "mc-remote.yml:deployment.eula_accepted", "explicit EULA acceptance is required"))

    host = _mapping(config, "host", issues, "mc-remote.yml:host")
    for key in ("ssh_host", "ssh_user"):
        value = host.get(key)
        if not isinstance(value, str) or not value or value.startswith("REPLACE_"):
            issues.append(Issue("FAIL", f"mc-remote.yml:host.{key}", "must be configured"))

    domains = _mapping(config, "domains", issues, "mc-remote.yml:domains")
    for key in ("homepage", "scratch", "scratch_beta", "bridge", "bridge_beta", "minecraft"):
        value = domains.get(key)
        if not isinstance(value, str) or not DOMAIN_NAME.fullmatch(value):
            issues.append(Issue("FAIL", f"mc-remote.yml:domains.{key}", "valid lowercase DNS hostname is required"))
    homepage_aliases = domains.get("homepage_aliases")
    if not isinstance(homepage_aliases, list) or any(
        not isinstance(value, str) or not DOMAIN_NAME.fullmatch(value) for value in homepage_aliases
    ):
        issues.append(
            Issue("FAIL", "mc-remote.yml:domains.homepage_aliases", "must contain valid lowercase DNS hostnames")
        )

    minecraft = _mapping(config, "minecraft", issues, "mc-remote.yml:minecraft")
    if minecraft.get("rcon_enabled") is not False:
        issues.append(Issue("FAIL", "mc-remote.yml:minecraft.rcon_enabled", "RCON must be disabled"))
    if minecraft.get("console_in_pipe") is not True:
        issues.append(Issue("FAIL", "mc-remote.yml:minecraft.console_in_pipe", "console pipe must be enabled"))
    if minecraft.get("mcremote_port") != 25575:
        issues.append(Issue("FAIL", "mc-remote.yml:minecraft.mcremote_port", "official McRemote port is 25575"))
    announce = minecraft.get("stop_announce_seconds")
    grace = minecraft.get("stop_grace_seconds")
    if not isinstance(announce, int) or not isinstance(grace, int) or grace <= announce:
        issues.append(Issue("FAIL", "mc-remote.yml:minecraft.stop_grace_seconds", "must exceed announce delay"))

    gameplay = _mapping(config, "gameplay", issues, "mc-remote.yml:gameplay")
    expected_gameplay = {"gamemode": "creative", "force_gamemode": True, "hardcore": True}
    if gameplay != expected_gameplay:
        issues.append(Issue("FAIL", "mc-remote.yml:gameplay", "official sandbox classroom policy does not match"))

    world = _mapping(config, "world", issues, "mc-remote.yml:world")
    if world.get("radius_blocks") != 9984:
        issues.append(Issue("FAIL", "mc-remote.yml:world.radius_blocks", "official sandbox radius is 9984"))
    if world.get("border_center") != [0, 0]:
        issues.append(Issue("FAIL", "mc-remote.yml:world.border_center", "official sandbox border center is [0, 0]"))
    if world.get("spawn_protection_radius") != 150:
        issues.append(Issue("FAIL", "mc-remote.yml:world.spawn_protection_radius", "official spawn protection is 150"))

    mcremote = _mapping(config, "mcremote", issues, "mc-remote.yml:mcremote")
    if mcremote.get("default_origin") != [200, 0, 200]:
        issues.append(Issue("FAIL", "mc-remote.yml:mcremote.default_origin", "official origin is [200, 0, 200]"))
    if mcremote.get("default_build_range") != 50:
        issues.append(Issue("FAIL", "mc-remote.yml:mcremote.default_build_range", "official build range is 50"))

    performance = _mapping(config, "performance", issues, "mc-remote.yml:performance")
    if performance.get("max_tick_time") != -1:
        issues.append(Issue("FAIL", "mc-remote.yml:performance.max_tick_time", "official recovery policy is -1"))
    if performance.get("network_compression_threshold") != -1:
        issues.append(
            Issue("FAIL", "mc-remote.yml:performance.network_compression_threshold", "official CPU policy is -1")
        )

    backup = _mapping(config, "backup", issues, "mc-remote.yml:backup")
    if backup.get("source") != "@server":
        issues.append(Issue("FAIL", "mc-remote.yml:backup.source", "initial profile requires @server"))
    output = backup.get("output")
    if not isinstance(output, str) or not output.startswith("/backup/"):
        issues.append(Issue("FAIL", "mc-remote.yml:backup.output", "must be below separate /backup mount"))
    if isinstance(output, str) and output.startswith("/data/"):
        issues.append(Issue("FAIL", "mc-remote.yml:backup.output", "must not be inside Minecraft /data"))
    times = backup.get("times")
    invalid_times = not isinstance(times, list) or not times
    if isinstance(times, list):
        invalid_times = invalid_times or any(not isinstance(v, str) or not TIME_OF_DAY.fullmatch(v) for v in times)
    if invalid_times:
        issues.append(Issue("FAIL", "mc-remote.yml:backup.times", "must contain HH:MM values"))
    _validate_backup_transport(backup.get("transport"), issues, "mc-remote.yml:backup.transport")

    images = _mapping(lock, "images", issues, "mc-remote.lock.yml:images")
    for name in ("caddy", "scratch_stable", "scratch_beta", "bridge", "minecraft"):
        value = images.get(name)
        if not isinstance(value, str) or not IMAGE_DIGEST.fullmatch(value):
            issues.append(Issue("FAIL", f"mc-remote.lock.yml:images.{name}", "exact image@sha256 digest is required"))
    if lock.get("resolved") is not True:
        issues.append(Issue("FAIL", "mc-remote.lock.yml:resolved", "lock is unresolved"))

    homepage = _mapping(lock, "homepage", issues, "mc-remote.lock.yml:homepage")
    _validate_static_archive(homepage, issues, "mc-remote.lock.yml:homepage")

    locked_minecraft = _mapping(lock, "minecraft", issues, "mc-remote.lock.yml:minecraft")
    minecraft_version = locked_minecraft.get("version")
    if not isinstance(minecraft_version, str) or not minecraft_version or minecraft_version.startswith("REPLACE_"):
        issues.append(Issue("FAIL", "mc-remote.lock.yml:minecraft.version", "exact Minecraft version is required"))
    paper = _mapping(locked_minecraft, "paper", issues, "mc-remote.lock.yml:minecraft.paper")
    paper_build = paper.get("build")
    if not isinstance(paper_build, int) or isinstance(paper_build, bool) or paper_build <= 0:
        issues.append(Issue("FAIL", "mc-remote.lock.yml:minecraft.paper.build", "exact Paper build is required"))
    _validate_artifact(paper, issues, "mc-remote.lock.yml:minecraft.paper")

    plugin_config = _mapping(config, "plugins", issues, "mc-remote.yml:plugins")
    enabled_plugins = plugin_config.get("enabled")
    locked_plugins = _mapping(lock, "plugins", issues, "mc-remote.lock.yml:plugins")
    if not isinstance(enabled_plugins, list) or not enabled_plugins:
        issues.append(Issue("FAIL", "mc-remote.yml:plugins.enabled", "must contain enabled plugin names"))
    else:
        for name in enabled_plugins:
            if not isinstance(name, str):
                issues.append(Issue("FAIL", "mc-remote.yml:plugins.enabled", "plugin names must be strings"))
                continue
            artifact = locked_plugins.get(name)
            if not isinstance(artifact, dict):
                issues.append(Issue("FAIL", f"mc-remote.lock.yml:plugins.{name}", "locked plugin artifact is required"))
                continue
            version = artifact.get("version")
            if not isinstance(version, str) or not version or version.startswith("REPLACE_"):
                issues.append(
                    Issue(
                        "FAIL",
                        f"mc-remote.lock.yml:plugins.{name}.version",
                        "exact plugin version is required",
                    )
                )
            _validate_artifact(artifact, issues, f"mc-remote.lock.yml:plugins.{name}")

    beta = _mapping(config, "beta", issues, "mc-remote.yml:beta")
    beta_domain = beta.get("domain")
    if not isinstance(beta_domain, str) or not DOMAIN_NAME.fullmatch(beta_domain):
        issues.append(Issue("FAIL", "mc-remote.yml:beta.domain", "valid lowercase DNS hostname is required"))
    elif beta_domain == domains.get("minecraft"):
        issues.append(Issue("FAIL", "mc-remote.yml:beta.domain", "must differ from stable Minecraft"))

    if beta.get("enabled") is True:
        beta_paths = _mapping(beta, "paths", issues, "mc-remote.yml:beta.paths")
        for key in ("minecraft", "backup"):
            value = beta_paths.get(key)
            if not isinstance(value, str) or not value.startswith("/var/lib/mc-remote/"):
                issues.append(Issue("FAIL", f"mc-remote.yml:beta.paths.{key}", "absolute runtime path is required"))
        if beta_paths.get("minecraft") == host.get("paths", {}).get("minecraft"):
            issues.append(Issue("FAIL", "mc-remote.yml:beta.paths.minecraft", "must differ from stable data"))
        if beta_paths.get("backup") == host.get("paths", {}).get("backup"):
            issues.append(Issue("FAIL", "mc-remote.yml:beta.paths.backup", "must differ from stable backup"))

        beta_minecraft = _mapping(beta, "minecraft", issues, "mc-remote.yml:beta.minecraft")
        if beta_minecraft.get("rcon_enabled") is not False:
            issues.append(Issue("FAIL", "mc-remote.yml:beta.minecraft.rcon_enabled", "RCON must be disabled"))
        if beta_minecraft.get("console_in_pipe") is not True:
            issues.append(Issue("FAIL", "mc-remote.yml:beta.minecraft.console_in_pipe", "console pipe must be enabled"))
        expected_ports = {"java_port": 25565, "bedrock_port": 25565, "mcremote_port": 25575}
        for key, expected in expected_ports.items():
            if beta_minecraft.get(key) != expected:
                issues.append(
                    Issue(
                        "FAIL",
                        f"mc-remote.yml:beta.minecraft.{key}",
                        f"official beta port is {expected}",
                    )
                )
        beta_announce = beta_minecraft.get("stop_announce_seconds")
        beta_grace = beta_minecraft.get("stop_grace_seconds")
        if not isinstance(beta_announce, int) or not isinstance(beta_grace, int) or beta_grace <= beta_announce:
            issues.append(
                Issue(
                    "FAIL",
                    "mc-remote.yml:beta.minecraft.stop_grace_seconds",
                    "must exceed announce delay",
                )
            )

        beta_backup = _mapping(beta, "backup", issues, "mc-remote.yml:beta.backup")
        if beta_backup.get("source") != "@server":
            issues.append(Issue("FAIL", "mc-remote.yml:beta.backup.source", "initial profile requires @server"))
        if beta_backup.get("output") != "/backup/outbox":
            issues.append(
                Issue("FAIL", "mc-remote.yml:beta.backup.output", "must use the separate /backup/outbox mount")
            )
        beta_times = beta_backup.get("times")
        if beta_times != ["03:33"]:
            issues.append(Issue("FAIL", "mc-remote.yml:beta.backup.times", "official beta schedule is 03:33"))

        beta_lock = _mapping(lock, "beta", issues, "mc-remote.lock.yml:beta")
        beta_image = beta_lock.get("image")
        if not isinstance(beta_image, str) or not IMAGE_DIGEST.fullmatch(beta_image):
            issues.append(Issue("FAIL", "mc-remote.lock.yml:beta.image", "exact image@sha256 digest is required"))
        beta_locked_minecraft = _mapping(
            beta_lock,
            "minecraft",
            issues,
            "mc-remote.lock.yml:beta.minecraft",
        )
        beta_version = beta_locked_minecraft.get("version")
        if not isinstance(beta_version, str) or not beta_version or beta_version.startswith("REPLACE_"):
            issues.append(
                Issue("FAIL", "mc-remote.lock.yml:beta.minecraft.version", "exact Minecraft version is required")
            )
        beta_paper = _mapping(
            beta_locked_minecraft,
            "paper",
            issues,
            "mc-remote.lock.yml:beta.minecraft.paper",
        )
        beta_build = beta_paper.get("build")
        if not isinstance(beta_build, int) or isinstance(beta_build, bool) or beta_build <= 0:
            issues.append(
                Issue("FAIL", "mc-remote.lock.yml:beta.minecraft.paper.build", "exact Paper build is required")
            )
        _validate_artifact(beta_paper, issues, "mc-remote.lock.yml:beta.minecraft.paper")

        beta_plugin_config = _mapping(beta, "plugins", issues, "mc-remote.yml:beta.plugins")
        beta_enabled_plugins = beta_plugin_config.get("enabled")
        beta_locked_plugins = _mapping(beta_lock, "plugins", issues, "mc-remote.lock.yml:beta.plugins")
        if not isinstance(beta_enabled_plugins, list) or not beta_enabled_plugins:
            issues.append(Issue("FAIL", "mc-remote.yml:beta.plugins.enabled", "must contain enabled plugin names"))
        else:
            for name in beta_enabled_plugins:
                artifact = beta_locked_plugins.get(name) if isinstance(name, str) else None
                if not isinstance(artifact, dict):
                    issues.append(
                        Issue(
                            "FAIL",
                            f"mc-remote.lock.yml:beta.plugins.{name}",
                            "locked plugin artifact is required",
                        )
                    )
                    continue
                version = artifact.get("version")
                if not isinstance(version, str) or not version or version.startswith("REPLACE_"):
                    issues.append(
                        Issue(
                            "FAIL",
                            f"mc-remote.lock.yml:beta.plugins.{name}.version",
                            "exact plugin version is required",
                        )
                    )
                _validate_artifact(artifact, issues, f"mc-remote.lock.yml:beta.plugins.{name}")
    return issues


def try_load_project(root: Path) -> tuple[LoadedProject | None, list[Issue]]:
    try:
        project = load_project(root)
    except (OSError, YamlError) as exc:
        return None, [Issue("FAIL", str(root), str(exc))]
    return project, validate_project(project)
