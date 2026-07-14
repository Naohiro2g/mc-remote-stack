"""Read-only inventory for secret-bearing Minecraft recovery archives."""

import hashlib
import io
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, TypedDict

import yaml

MAX_PLUGIN_JAR_METADATA_BYTES = 128 * 1024 * 1024
MAX_PLUGIN_DESCRIPTOR_BYTES = 1024 * 1024


class JarArtifact(TypedDict):
    filename: str
    sha256: str
    size_bytes: int


class PluginDescriptor(TypedDict, total=False):
    status: str
    path: str
    name: str
    version: str
    api_version: str
    main: str


class PluginArtifact(JarArtifact):
    descriptor: PluginDescriptor


class ArchiveInventory(TypedDict):
    archive_name: str
    archive_sha256: str
    compressed_size_bytes: int
    uncompressed_size_bytes: int
    entry_count: int
    region_files: int
    crc_ok: bool
    ignored_nested_plugin_jars: int
    server_jars: list[JarArtifact]
    plugin_jars: list[PluginArtifact]


def _sha256_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _descriptor_scalar(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return None


def _plugin_descriptor(jar_bytes: bytes) -> PluginDescriptor:
    try:
        with zipfile.ZipFile(io.BytesIO(jar_bytes)) as plugin:
            names = {name.lower(): name for name in plugin.namelist()}
            descriptor_path = next(
                (names[candidate] for candidate in ("paper-plugin.yml", "plugin.yml") if candidate in names),
                None,
            )
            if descriptor_path is None:
                return {"status": "descriptor-missing"}
            descriptor_info = plugin.getinfo(descriptor_path)
            if descriptor_info.file_size > MAX_PLUGIN_DESCRIPTOR_BYTES:
                return {"status": "descriptor-too-large"}
            with plugin.open(descriptor_info) as descriptor_stream:
                descriptor = yaml.safe_load(descriptor_stream.read())
    except (OSError, UnicodeError, yaml.YAMLError, zipfile.BadZipFile):
        return {"status": "invalid-jar"}

    if not isinstance(descriptor, dict):
        return {"status": "descriptor-invalid"}
    name = _descriptor_scalar(descriptor.get("name"))
    version = _descriptor_scalar(descriptor.get("version"))
    if name is None or version is None:
        return {"status": "descriptor-invalid"}

    result: PluginDescriptor = {
        "status": "ok",
        "path": descriptor_path,
        "name": name,
        "version": version,
    }
    optional_keys = {"api-version": "api_version", "main": "main"}
    for source_key, result_key in optional_keys.items():
        value = _descriptor_scalar(descriptor.get(source_key))
        if value is not None:
            result[result_key] = value
    return result


def inspect_archive(path: Path) -> ArchiveInventory:
    """Inspect a ZIP without extracting or printing contained configuration."""
    path = path.resolve()
    with path.open("rb") as stream:
        archive_sha256 = _sha256_stream(stream)

    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        crc_ok = archive.testzip() is None
        plugin_jars: list[PluginArtifact] = []
        server_jars: list[JarArtifact] = []
        ignored_nested_plugin_jars = 0
        if crc_ok:
            for entry in entries:
                archive_path = PurePosixPath(entry.filename.replace("\\", "/"))
                parts = tuple(part.lower() for part in archive_path.parts)
                if entry.is_dir() or archive_path.suffix.lower() != ".jar":
                    continue
                is_active_plugin = len(parts) >= 2 and parts[-2] == "plugins"
                is_root_server_jar = len(parts) == 1
                if "plugins" in parts and not is_active_plugin:
                    ignored_nested_plugin_jars += 1
                    continue
                if not is_active_plugin and not is_root_server_jar:
                    continue
                descriptor: PluginDescriptor | None = None
                if is_active_plugin and entry.file_size <= MAX_PLUGIN_JAR_METADATA_BYTES:
                    with archive.open(entry) as plugin_stream:
                        jar_bytes = plugin_stream.read()
                    sha256 = hashlib.sha256(jar_bytes).hexdigest()
                    descriptor = _plugin_descriptor(jar_bytes)
                else:
                    with archive.open(entry) as plugin_stream:
                        sha256 = _sha256_stream(plugin_stream)
                artifact: JarArtifact = {
                    "filename": archive_path.name,
                    "sha256": sha256,
                    "size_bytes": entry.file_size,
                }
                if is_active_plugin:
                    plugin_jars.append(
                        PluginArtifact(
                            **artifact,
                            descriptor=descriptor or {"status": "jar-too-large"},
                        )
                    )
                else:
                    server_jars.append(artifact)

    return {
        "archive_name": path.name,
        "archive_sha256": archive_sha256,
        "compressed_size_bytes": path.stat().st_size,
        "uncompressed_size_bytes": sum(entry.file_size for entry in entries),
        "entry_count": len(entries),
        "region_files": sum(
            1
            for entry in entries
            if PurePosixPath(entry.filename.replace("\\", "/")).suffix.lower() == ".mca"
            and "region" in (part.lower() for part in PurePosixPath(entry.filename.replace("\\", "/")).parts)
        ),
        "crc_ok": crc_ok,
        "ignored_nested_plugin_jars": ignored_nested_plugin_jars,
        "server_jars": sorted(server_jars, key=lambda artifact: artifact["filename"].lower()),
        "plugin_jars": sorted(plugin_jars, key=lambda artifact: artifact["filename"].lower()),
    }
