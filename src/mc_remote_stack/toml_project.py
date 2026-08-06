"""One-environment TOML deployment project boundaries and lossless order editing."""

from __future__ import annotations

import copy
import os
import re
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time
from ipaddress import AddressValueError, IPv4Address, IPv4Network
from pathlib import Path, PurePosixPath
from typing import Any

import tomlkit
from tomlkit.exceptions import ParseError
from tomlkit.items import String

ORDER_NAME = "mc-remote.toml"
LOCK_NAME = "mc-remote.lock.toml"
LEGACY_NAMES = (
    "mc-remote.yml",
    "mc-remote.yaml",
    "mc-remote.lock.yml",
    "mc-remote.lock.yaml",
)
COMPOSITION_KEYS = frozenset({"include", "import", "extends", "glob"})
EXACT_REFERENCE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}@[1-9][0-9]*$")
EXPLICIT_IDENTITY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
ENVIRONMENT_INTERPOLATION = re.compile(r"\$\{[^}]*\}")
MAX_IDENTITY_INTEGER = 2**53 - 1

ROOT_KEYS = frozenset(
    {
        "schema_version",
        "deployment",
        "environment",
        "runtime",
        "world",
        "network",
        "agreements",
        "acknowledgements",
        "operator_inputs",
    }
)
DEPLOYMENT_KEYS = frozenset({"name", "profile"})
ENVIRONMENT_KEYS = frozenset({"identity", "channel", "exposure", "purpose", "preset"})
RUNTIME_KEYS = frozenset({"artifact_store", "volumes"})
RUNTIME_VOLUME_KEYS = frozenset({"role", "identity"})
WORLD_KEYS = frozenset({"identity"})
NETWORK_KEYS = frozenset({"bind_address", "java_port", "mcremote_port"})
AGREEMENT_KEYS = frozenset({"minecraft_eula"})
ACKNOWLEDGEMENT_KEYS = frozenset({"allow_unverified", "unverified_reason", "allow_eol", "eol_reason"})
OPERATOR_INPUT_KEYS = frozenset({"role", "adapter", "path"})
CHANNELS = frozenset({"stable", "beta", "alpha", "dev"})
EXPOSURES = frozenset({"public", "lan-only", "isolated"})
PRIVATE_IPV4_NETWORKS = (
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
)

PROJECT_GITIGNORE = """/generated/
/.mcrctl/
/secrets/
/backup/
/backups/
/world/
/.env
*.secret
*.zip
*.tar
*.tar.gz
"""

PROJECT_README = """# McRemote deployment project

This directory contains the human-owned order for exactly one environment.

- `mc-remote.toml` is the tracked human-owned order.
- `mc-remote.lock.toml` is created only by a successful resolve and remains tracked.
- `operator/` contains only explicitly referenced, non-secret operator inputs.
- Runtime volume and world identities are explicit references; their bytes stay outside this project.
- Generated output, secrets, artifacts, runtime volumes, worlds, and backups stay outside this project.
- The project is trusted operator input: do not grant write access to untrusted local users or agents.
"""


class ProjectOrderError(ValueError):
    """Stable, fail-closed diagnostic for a TOML deployment project."""

    def __init__(self, reason: str, path: Path, message: str) -> None:
        self.reason = reason
        self.path = path
        super().__init__(f"{reason}: {path}: {message}")


@dataclass(frozen=True)
class TomlProjectPaths:
    root: Path

    @property
    def order(self) -> Path:
        return self.root / ORDER_NAME

    @property
    def lock(self) -> Path:
        return self.root / LOCK_NAME

    @property
    def gitignore(self) -> Path:
        return self.root / ".gitignore"

    @property
    def readme(self) -> Path:
        return self.root / "README.md"


@dataclass(frozen=True)
class LoadedOrder:
    paths: TomlProjectPaths
    order: dict[str, Any]
    source_bytes: bytes


def _fail(reason: str, path: Path, message: str) -> None:
    raise ProjectOrderError(reason, path, message)


def _project_paths(root: Path) -> TomlProjectPaths:
    return TomlProjectPaths(root.resolve())


def _write_new_project_file(path: Path, content: str) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o640,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _detect_layout(paths: TomlProjectPaths) -> None:
    root = paths.root
    if not root.is_dir():
        _fail("order_missing", paths.order, "explicit project directory does not contain mc-remote.toml")

    legacy = [root / name for name in LEGACY_NAMES if (root / name).exists()]
    if paths.order.exists() and legacy:
        names = ", ".join(path.name for path in legacy)
        _fail("mixed_order_formats", paths.order, f"TOML order cannot coexist with legacy files: {names}")

    if not paths.order.exists():
        if legacy:
            names = ", ".join(path.name for path in legacy)
            _fail(
                "legacy_order_requires_explicit_conversion",
                root,
                f"legacy files require the explicit official-vps conversion path: {names}",
            )
        if paths.lock.exists():
            _fail("orphan_lock", paths.lock, "mc-remote.lock.toml exists without mc-remote.toml")
        _fail("order_missing", paths.order, "explicit project directory does not contain mc-remote.toml")

    additional_orders = sorted(
        path
        for path in root.iterdir()
        if path.name.startswith("mc-remote.")
        and path.suffix == ".toml"
        and path.name not in {ORDER_NAME, LOCK_NAME}
    )
    if additional_orders:
        names = ", ".join(path.name for path in additional_orders)
        _fail("additional_order_file", root, f"only {ORDER_NAME} may be an order file; found: {names}")


def _require_table(order: dict[str, Any], key: str, order_path: Path) -> dict[str, Any]:
    value = order.get(key)
    if not isinstance(value, dict):
        _fail("order_schema_invalid", order_path, f"{key} must be a table")
    return value


def _reject_unknown_keys(
    value: dict[str, Any],
    *,
    allowed: frozenset[str],
    logical_path: str,
    order_path: Path,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _fail("unknown_order_key", order_path, f"{logical_path} contains unknown keys: {', '.join(unknown)}")


def _require_nonempty_string(value: object, logical_path: str, order_path: Path) -> str:
    if not isinstance(value, str) or not value.strip() or (value.startswith("<") and value.endswith(">")):
        _fail("order_schema_invalid", order_path, f"{logical_path} must be an explicit non-empty string")
    return value


def _require_explicit_identity(value: object, logical_path: str, order_path: Path) -> str:
    identity = _require_nonempty_string(value, logical_path, order_path)
    if not EXPLICIT_IDENTITY.fullmatch(identity):
        _fail(
            "order_schema_invalid",
            order_path,
            f"{logical_path} must match {EXPLICIT_IDENTITY.pattern}",
        )
    return identity


def _require_port(value: object, logical_path: str, order_path: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        _fail("order_schema_invalid", order_path, f"{logical_path} must be an integer from 1 through 65535")
    return value


def _require_operator_input_path(
    value: object,
    *,
    adapter: str,
    logical_path: str,
    order_path: Path,
) -> str:
    path = _require_nonempty_string(value, logical_path, order_path)
    adapter_name = adapter.partition("@")[0]
    parts = path.split("/")
    if (
        "\\" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or len(parts) < 3
        or parts[:2] != ["operator", adapter_name]
        or PurePosixPath(path).as_posix() != path
    ):
        _fail(
            "operator_input_path_invalid",
            order_path,
            f"{logical_path} must be an exact operator/{adapter_name}/<native-path> reference",
        )
    return path


def _walk_identity_values(value: object, logical_path: str, order_path: Path) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{logical_path}.{key}" if logical_path else key
            if key in COMPOSITION_KEYS:
                _fail("composition_forbidden", order_path, f"generic composition is forbidden at {child_path}")
            _walk_identity_values(child, child_path, order_path)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _walk_identity_values(child, f"{logical_path}[{index}]", order_path)
        return
    if isinstance(value, str):
        if ENVIRONMENT_INTERPOLATION.search(value):
            _fail(
                "environment_interpolation_forbidden",
                order_path,
                f"{logical_path} must not contain environment interpolation",
            )
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int):
        if abs(value) > MAX_IDENTITY_INTEGER:
            _fail("order_schema_invalid", order_path, f"{logical_path} integer exceeds the identity-safe range")
        return
    if isinstance(value, float | date | datetime | time):
        _fail("order_schema_invalid", order_path, f"{logical_path} uses a forbidden identity value type")
    _fail("order_schema_invalid", order_path, f"{logical_path} uses an unsupported value type")


def _validate_order(order: object, order_path: Path) -> dict[str, Any]:
    if not isinstance(order, dict):
        _fail("order_schema_invalid", order_path, "top level must be a table")

    _walk_identity_values(order, "", order_path)
    if "environments" in order:
        _fail("multiple_environments", order_path, "use singular [environment] in one sibling project")
    _reject_unknown_keys(order, allowed=ROOT_KEYS, logical_path="root", order_path=order_path)

    if order.get("schema_version") != 1 or isinstance(order.get("schema_version"), bool):
        _fail("order_schema_invalid", order_path, "schema_version must be integer 1")

    deployment = _require_table(order, "deployment", order_path)
    environment = _require_table(order, "environment", order_path)
    runtime = _require_table(order, "runtime", order_path)
    world = _require_table(order, "world", order_path)
    network = _require_table(order, "network", order_path)
    agreements = _require_table(order, "agreements", order_path)
    acknowledgements = _require_table(order, "acknowledgements", order_path)
    _reject_unknown_keys(
        deployment,
        allowed=DEPLOYMENT_KEYS,
        logical_path="deployment",
        order_path=order_path,
    )
    _reject_unknown_keys(
        environment,
        allowed=ENVIRONMENT_KEYS,
        logical_path="environment",
        order_path=order_path,
    )
    _reject_unknown_keys(
        runtime,
        allowed=RUNTIME_KEYS,
        logical_path="runtime",
        order_path=order_path,
    )
    _reject_unknown_keys(
        world,
        allowed=WORLD_KEYS,
        logical_path="world",
        order_path=order_path,
    )
    _reject_unknown_keys(
        network,
        allowed=NETWORK_KEYS,
        logical_path="network",
        order_path=order_path,
    )
    _reject_unknown_keys(
        agreements,
        allowed=AGREEMENT_KEYS,
        logical_path="agreements",
        order_path=order_path,
    )
    _reject_unknown_keys(
        acknowledgements,
        allowed=ACKNOWLEDGEMENT_KEYS,
        logical_path="acknowledgements",
        order_path=order_path,
    )

    _require_nonempty_string(deployment.get("name"), "deployment.name", order_path)
    profile = _require_nonempty_string(deployment.get("profile"), "deployment.profile", order_path)
    if not EXACT_REFERENCE.fullmatch(profile):
        _fail("order_schema_invalid", order_path, "deployment.profile must be an exact name@revision reference")

    _require_nonempty_string(environment.get("identity"), "environment.identity", order_path)
    channel = _require_nonempty_string(environment.get("channel"), "environment.channel", order_path)
    if channel not in CHANNELS:
        _fail("order_schema_invalid", order_path, f"environment.channel must be one of: {', '.join(sorted(CHANNELS))}")
    exposure = _require_nonempty_string(environment.get("exposure"), "environment.exposure", order_path)
    if exposure not in EXPOSURES:
        _fail(
            "order_schema_invalid",
            order_path,
            f"environment.exposure must be one of: {', '.join(sorted(EXPOSURES))}",
        )
    _require_nonempty_string(environment.get("purpose"), "environment.purpose", order_path)
    preset = _require_nonempty_string(environment.get("preset"), "environment.preset", order_path)
    if not EXACT_REFERENCE.fullmatch(preset):
        _fail("order_schema_invalid", order_path, "environment.preset must be an exact name@revision reference")

    artifact_store = _require_nonempty_string(runtime.get("artifact_store"), "runtime.artifact_store", order_path)
    artifact_path = PurePosixPath(artifact_store)
    if (
        not artifact_path.is_absolute()
        or artifact_path == PurePosixPath("/")
        or ".." in artifact_path.parts
        or "\\" in artifact_store
    ):
        _fail(
            "order_schema_invalid",
            order_path,
            "runtime.artifact_store must be a non-root absolute POSIX path without parent traversal",
        )

    volumes = runtime.get("volumes")
    if not isinstance(volumes, list) or not volumes:
        _fail("order_schema_invalid", order_path, "runtime.volumes must contain at least one assignment")
    volume_roles: set[str] = set()
    volume_identities: set[str] = set()
    for index, volume in enumerate(volumes):
        if not isinstance(volume, dict):
            _fail("order_schema_invalid", order_path, f"runtime.volumes[{index}] must be a table")
        _reject_unknown_keys(
            volume,
            allowed=RUNTIME_VOLUME_KEYS,
            logical_path=f"runtime.volumes[{index}]",
            order_path=order_path,
        )
        role = _require_explicit_identity(volume.get("role"), f"runtime.volumes[{index}].role", order_path)
        identity = _require_explicit_identity(
            volume.get("identity"),
            f"runtime.volumes[{index}].identity",
            order_path,
        )
        if role in volume_roles:
            _fail("order_schema_invalid", order_path, f"runtime volume role is assigned more than once: {role}")
        if identity in volume_identities:
            _fail(
                "order_schema_invalid",
                order_path,
                f"runtime volume identity is assigned more than once: {identity}",
            )
        volume_roles.add(role)
        volume_identities.add(identity)

    _require_explicit_identity(world.get("identity"), "world.identity", order_path)

    bind_value = _require_nonempty_string(network.get("bind_address"), "network.bind_address", order_path)
    try:
        bind_address = IPv4Address(bind_value)
    except AddressValueError:
        _fail("order_schema_invalid", order_path, "network.bind_address must be an explicit IPv4 address")
    java_port = _require_port(network.get("java_port"), "network.java_port", order_path)
    mcremote_port = _require_port(network.get("mcremote_port"), "network.mcremote_port", order_path)
    if java_port == mcremote_port:
        _fail("order_schema_invalid", order_path, "network ports must be distinct")
    if exposure == "isolated" and not bind_address.is_loopback:
        _fail(
            "unsupported_environment_combination",
            order_path,
            "isolated exposure requires a loopback network.bind_address",
        )
    if exposure == "lan-only" and not any(
        bind_address in private_network for private_network in PRIVATE_IPV4_NETWORKS
    ):
        _fail(
            "unsupported_environment_combination",
            order_path,
            "lan-only exposure requires an RFC 1918 network.bind_address",
        )

    if not isinstance(agreements.get("minecraft_eula"), bool):
        _fail("order_schema_invalid", order_path, "agreements.minecraft_eula must be boolean")

    for flag, reason in (
        ("allow_unverified", "unverified_reason"),
        ("allow_eol", "eol_reason"),
    ):
        allowed = acknowledgements.get(flag)
        explanation = acknowledgements.get(reason)
        if not isinstance(allowed, bool):
            _fail("order_schema_invalid", order_path, f"acknowledgements.{flag} must be boolean")
        if not isinstance(explanation, str):
            _fail("order_schema_invalid", order_path, f"acknowledgements.{reason} must be a string")
        if allowed and not explanation.strip():
            _fail(
                "acknowledgement_reason_required",
                order_path,
                f"acknowledgements.{reason} is required when {flag} is true",
            )

    operator_inputs = order.get("operator_inputs", [])
    if not isinstance(operator_inputs, list):
        _fail("order_schema_invalid", order_path, "operator_inputs must be an array of tables")
    input_roles: set[str] = set()
    input_paths: set[str] = set()
    for index, operator_input in enumerate(operator_inputs):
        if not isinstance(operator_input, dict):
            _fail("order_schema_invalid", order_path, f"operator_inputs[{index}] must be a table")
        _reject_unknown_keys(
            operator_input,
            allowed=OPERATOR_INPUT_KEYS,
            logical_path=f"operator_inputs[{index}]",
            order_path=order_path,
        )
        role = _require_explicit_identity(
            operator_input.get("role"),
            f"operator_inputs[{index}].role",
            order_path,
        )
        adapter = _require_nonempty_string(
            operator_input.get("adapter"),
            f"operator_inputs[{index}].adapter",
            order_path,
        )
        if not EXACT_REFERENCE.fullmatch(adapter):
            _fail(
                "order_schema_invalid",
                order_path,
                f"operator_inputs[{index}].adapter must be an exact name@revision reference",
            )
        path = _require_operator_input_path(
            operator_input.get("path"),
            adapter=adapter,
            logical_path=f"operator_inputs[{index}].path",
            order_path=order_path,
        )
        if role in input_roles:
            _fail("order_schema_invalid", order_path, f"operator input role is assigned more than once: {role}")
        if path in input_paths:
            _fail("order_schema_invalid", order_path, f"operator input path is referenced more than once: {path}")
        input_roles.add(role)
        input_paths.add(path)

    return order


def _validate_operator_tree(order: dict[str, Any], paths: TomlProjectPaths) -> None:
    referenced = {item["path"] for item in order.get("operator_inputs", [])}
    operator_root = paths.root / "operator"
    if not operator_root.exists() and not operator_root.is_symlink():
        if referenced:
            first = min(referenced)
            _fail(
                "operator_input_missing",
                paths.root / PurePosixPath(first),
                "referenced operator input does not exist",
            )
        return
    if operator_root.is_symlink():
        _fail(
            "operator_input_symlink_forbidden",
            operator_root,
            "operator input directories and files must not be symlinks",
        )
    if not operator_root.is_dir():
        _fail("operator_input_not_regular", operator_root, "operator must be a real directory")

    actual_files: set[str] = set()
    for candidate in sorted(operator_root.rglob("*")):
        if candidate.is_symlink():
            _fail(
                "operator_input_symlink_forbidden",
                candidate,
                "operator input directories and files must not be symlinks",
            )
        mode = candidate.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            _fail("operator_input_not_regular", candidate, "operator input must be a regular file")
        actual_files.add(candidate.relative_to(paths.root).as_posix())

    missing = sorted(referenced - actual_files)
    if missing:
        _fail(
            "operator_input_missing",
            paths.root / PurePosixPath(missing[0]),
            "referenced operator input does not exist as a regular file",
        )
    unreferenced = sorted(actual_files - referenced)
    if unreferenced:
        _fail(
            "operator_input_unreferenced",
            paths.root / PurePosixPath(unreferenced[0]),
            "every operator-owned file must be explicitly referenced by mc-remote.toml",
        )


def load_order(root: Path) -> LoadedOrder:
    """Load only the exact TOML order directly below an explicit project root."""

    paths = _project_paths(root)
    _detect_layout(paths)
    try:
        source_bytes = paths.order.read_bytes()
    except OSError as exc:
        _fail("order_read_failed", paths.order, str(exc))
    if source_bytes.startswith(b"\xef\xbb\xbf"):
        _fail("order_encoding_invalid", paths.order, "UTF-8 BOM is forbidden")
    try:
        source = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail("order_encoding_invalid", paths.order, str(exc))
    try:
        parsed = tomllib.loads(source)
    except tomllib.TOMLDecodeError as exc:
        _fail("order_parse_failed", paths.order, str(exc))
    order = _validate_order(parsed, paths.order)
    _validate_operator_tree(order, paths)
    return LoadedOrder(paths, order, source_bytes)


def _new_order_document(
    *,
    deployment_name: str,
    profile: str,
    environment_identity: str,
    channel: str,
    exposure: str,
    purpose: str,
    preset: str,
    artifact_store: str,
    runtime_volumes: dict[str, str],
    world_identity: str,
    bind_address: str,
    java_port: int,
    mcremote_port: int,
    minecraft_eula: bool,
) -> str:
    document = tomlkit.document()
    document.add("schema_version", 1)

    deployment = tomlkit.table()
    deployment.add("name", deployment_name)
    deployment.add("profile", profile)
    document.add("deployment", deployment)

    environment = tomlkit.table()
    environment.add("identity", environment_identity)
    environment.add("channel", channel)
    environment.add("exposure", exposure)
    environment.add("purpose", purpose)
    environment.add("preset", preset)
    document.add("environment", environment)

    runtime = tomlkit.table()
    runtime.add("artifact_store", artifact_store)
    volumes = tomlkit.aot()
    for role, identity in sorted(runtime_volumes.items()):
        volume = tomlkit.table()
        volume.add("role", role)
        volume.add("identity", identity)
        volumes.append(volume)
    runtime.add("volumes", volumes)
    document.add("runtime", runtime)

    world = tomlkit.table()
    world.add("identity", world_identity)
    document.add("world", world)

    network = tomlkit.table()
    network.add("bind_address", bind_address)
    network.add("java_port", java_port)
    network.add("mcremote_port", mcremote_port)
    document.add("network", network)

    agreements = tomlkit.table()
    agreements.add("minecraft_eula", minecraft_eula)
    document.add("agreements", agreements)

    acknowledgements = tomlkit.table()
    acknowledgements.add("allow_unverified", False)
    acknowledgements.add("unverified_reason", "")
    acknowledgements.add("allow_eol", False)
    acknowledgements.add("eol_reason", "")
    document.add("acknowledgements", acknowledgements)
    return tomlkit.dumps(document)


def init_toml_project(
    root: Path,
    *,
    deployment_name: str,
    profile: str,
    environment_identity: str,
    channel: str,
    exposure: str,
    purpose: str,
    preset: str,
    artifact_store: str,
    runtime_volumes: dict[str, str],
    world_identity: str,
    bind_address: str,
    java_port: int,
    mcremote_port: int,
    minecraft_eula: bool = False,
) -> TomlProjectPaths:
    """Create one human-owned order and no placeholder lock."""

    paths = _project_paths(root)
    order_source = _new_order_document(
        deployment_name=deployment_name,
        profile=profile,
        environment_identity=environment_identity,
        channel=channel,
        exposure=exposure,
        purpose=purpose,
        preset=preset,
        artifact_store=artifact_store,
        runtime_volumes=runtime_volumes,
        world_identity=world_identity,
        bind_address=bind_address,
        java_port=java_port,
        mcremote_port=mcremote_port,
        minecraft_eula=minecraft_eula,
    )
    try:
        candidate = tomllib.loads(order_source)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - internal serializer invariant
        _fail("order_parse_failed", paths.order, str(exc))
    _validate_order(candidate, paths.order)

    if paths.root.exists() and (not paths.root.is_dir() or any(paths.root.iterdir())):
        raise ValueError(f"refusing to initialize non-empty directory: {paths.root}")

    paths.root.mkdir(parents=True, exist_ok=True, mode=0o750)
    os.chmod(paths.root, stat.S_IMODE(paths.root.stat().st_mode) & 0o750)
    _write_new_project_file(paths.gitignore, PROJECT_GITIGNORE)
    _write_new_project_file(paths.readme, PROJECT_README)
    _write_new_project_file(paths.order, order_source)
    return paths


def _same_scalar(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


def _replacement_item(existing: object, value: str | int | bool) -> object:
    if isinstance(existing, String) and isinstance(value, str):
        rendered = existing.as_string()
        multiline = rendered.startswith(('"""', "'''"))
        literal = rendered.startswith(("'", "'''"))
        return tomlkit.string(value, literal=literal, multiline=multiline)
    return tomlkit.item(value)


def _atomic_replace(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, path.stat().st_mode & 0o7777)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def update_order_scalar(root: Path, logical_path: tuple[str, ...], value: str | int | bool) -> bool:
    """Losslessly update one existing scalar after semantic-drift checks."""

    if not logical_path:
        raise ValueError("logical_path must not be empty")
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > MAX_IDENTITY_INTEGER:
        raise ValueError("integer exceeds the identity-safe range")
    if not isinstance(value, str | int | bool):
        raise TypeError("value must be a string, integer, or boolean")

    loaded = load_order(root)
    original = loaded.source_bytes.decode("utf-8")
    semantic_parent: dict[str, Any] = loaded.order
    for segment in logical_path[:-1]:
        child = semantic_parent.get(segment)
        if not isinstance(child, dict):
            raise ValueError(f"logical path is not an existing table: {'.'.join(logical_path)}")
        semantic_parent = child
    leaf = logical_path[-1]
    if leaf not in semantic_parent or isinstance(semantic_parent[leaf], dict | list):
        raise ValueError(f"logical path is not an existing scalar: {'.'.join(logical_path)}")
    if _same_scalar(semantic_parent[leaf], value):
        return False

    try:
        document = tomlkit.parse(original)
    except ParseError as exc:
        _fail("order_parse_failed", loaded.paths.order, str(exc))
    document_parent: Any = document
    for segment in logical_path[:-1]:
        if segment not in document_parent:
            raise ValueError(f"logical path is absent from edit document: {'.'.join(logical_path)}")
        document_parent = document_parent[segment]
    existing_item = document_parent[leaf]
    document_parent[leaf] = _replacement_item(existing_item, value)
    candidate_source = tomlkit.dumps(document)

    try:
        candidate = tomllib.loads(candidate_source)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - guarded by TOML Kit
        _fail("order_parse_failed", loaded.paths.order, str(exc))
    _validate_order(candidate, loaded.paths.order)

    expected = copy.deepcopy(loaded.order)
    expected_parent: dict[str, Any] = expected
    for segment in logical_path[:-1]:
        expected_parent = expected_parent[segment]
    expected_parent[leaf] = value
    if candidate != expected:
        _fail(
            "order_semantic_drift",
            loaded.paths.order,
            f"edit changed values outside {'.'.join(logical_path)}",
        )

    _atomic_replace(loaded.paths.order, candidate_source.encode("utf-8"))
    return True


def update_order_volume_identity(root: Path, role: str, identity: str) -> bool:
    """Losslessly replace one existing runtime volume identity by exact role."""

    if not EXPLICIT_IDENTITY.fullmatch(role):
        raise ValueError("runtime volume role must be an explicit identity")
    if not EXPLICIT_IDENTITY.fullmatch(identity):
        raise ValueError("runtime volume identity must be an explicit identity")

    loaded = load_order(root)
    matching_indexes = [
        index
        for index, volume in enumerate(loaded.order["runtime"]["volumes"])
        if volume["role"] == role
    ]
    if len(matching_indexes) != 1:
        raise ValueError(f"unknown runtime volume role: {role}")
    index = matching_indexes[0]
    if loaded.order["runtime"]["volumes"][index]["identity"] == identity:
        return False

    original = loaded.source_bytes.decode("utf-8")
    try:
        document = tomlkit.parse(original)
    except ParseError as exc:
        _fail("order_parse_failed", loaded.paths.order, str(exc))
    document_volumes = document["runtime"]["volumes"]
    document_indexes = [
        item_index
        for item_index, volume in enumerate(document_volumes)
        if volume.get("role") == role
    ]
    if document_indexes != [index]:
        raise ValueError(f"runtime volume role is ambiguous in edit document: {role}")
    existing_item = document_volumes[index]["identity"]
    document_volumes[index]["identity"] = _replacement_item(existing_item, identity)
    candidate_source = tomlkit.dumps(document)

    try:
        candidate = tomllib.loads(candidate_source)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - guarded by TOML Kit
        _fail("order_parse_failed", loaded.paths.order, str(exc))
    _validate_order(candidate, loaded.paths.order)

    expected = copy.deepcopy(loaded.order)
    expected["runtime"]["volumes"][index]["identity"] = identity
    if candidate != expected:
        _fail(
            "order_semantic_drift",
            loaded.paths.order,
            f"edit changed values outside runtime.volumes[{index}].identity",
        )

    _atomic_replace(loaded.paths.order, candidate_source.encode("utf-8"))
    return True
