"""Typed, non-secret operator-owned input adapters."""

from __future__ import annotations

import re
import tomllib
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from .preset_registry import semantic_sha256
from .toml_project import LoadedOrder

MINECRAFT_MOTD_ADAPTER = "minecraft-motd@1"
MINECRAFT_MOTD_PATH = "operator/minecraft-motd/server.properties"
PUBLIC_ROUTES_ADAPTER = "public-routes@1"
PUBLIC_ROUTES_PATH = "operator/public-routes/routes.toml"
MINECRAFT_SERVER_ADAPTER = "minecraft-server@1"
MINECRAFT_SERVER_PATH = "operator/minecraft-server/server.toml"
MAX_MOTD_SOURCE_BYTES = 4096
MAX_MOTD_CHARACTERS = 256
SUPPORTED_ADAPTERS = frozenset(
    {
        MINECRAFT_MOTD_ADAPTER,
        MINECRAFT_SERVER_ADAPTER,
        PUBLIC_ROUTES_ADAPTER,
    }
)
PUBLIC_ROUTE_KEYS = frozenset(
    {"homepage", "homepage_aliases", "scratch", "bridge", "minecraft"}
)
DNS_NAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
MINECRAFT_SERVER_KEYS = frozenset(
    {
        "allow_flight",
        "difficulty",
        "enable_query",
        "enable_status",
        "force_gamemode",
        "gamemode",
        "hardcore",
        "log_ips",
        "management_server_enabled",
        "max_players",
        "max_tick_time",
        "max_world_size",
        "motd",
        "network_compression_threshold",
        "simulation_distance",
        "spawn_protection",
        "view_distance",
        "white_list",
    }
)


class OperatorInputError(ValueError):
    """Stable, fail-closed diagnostic for typed operator-owned inputs."""

    def __init__(self, reason: str, path: Path | str, message: str) -> None:
        self.reason = reason
        self.path = path
        super().__init__(f"{reason}: {path}: {message}")


def _fail(reason: str, path: Path | str, message: str) -> None:
    raise OperatorInputError(reason, path, message)


def _read_source(path: Path) -> bytes:
    try:
        source = path.read_bytes()
    except OSError as exc:
        _fail("operator_input_read_failed", path, str(exc))
    if len(source) > MAX_MOTD_SOURCE_BYTES:
        _fail(
            "operator_input_too_large",
            path,
            f"operator input exceeds the {MAX_MOTD_SOURCE_BYTES}-byte adapter limit",
        )
    return source


def _parse_minecraft_motd(path: Path, source: bytes) -> dict[str, str]:
    if source.startswith(b"\xef\xbb\xbf"):
        _fail("operator_input_encoding_invalid", path, "UTF-8 BOM is forbidden")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail("operator_input_encoding_invalid", path, str(exc))

    motd: str | None = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        if "\\" in line:
            _fail(
                "operator_input_parse_failed",
                path,
                f"line {line_number}: escapes and continuations are outside minecraft-motd@1",
            )
        if "=" not in line:
            _fail(
                "operator_input_parse_failed",
                path,
                f"line {line_number}: expected exact motd=<public-text> syntax",
            )
        key, value = (part.strip() for part in line.split("=", 1))
        if key != "motd":
            _fail(
                "operator_input_parse_failed",
                path,
                f"line {line_number}: minecraft-motd@1 accepts only the motd key",
            )
        if motd is not None:
            _fail("operator_input_parse_failed", path, "motd must be assigned exactly once")
        if not value or len(value) > MAX_MOTD_CHARACTERS:
            _fail(
                "operator_input_parse_failed",
                path,
                f"motd must contain 1 through {MAX_MOTD_CHARACTERS} characters",
            )
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            _fail("operator_input_parse_failed", path, "motd must not contain control characters")
        lowered = value.casefold()
        if "secret://" in lowered or "${" in value:
            _fail(
                "operator_input_secret_forbidden",
                path,
                "minecraft-motd@1 is a public display field and has no secret injection point",
            )
        motd = value
    if motd is None:
        _fail("operator_input_parse_failed", path, "motd must be assigned exactly once")
    return {"motd": motd}


def _public_dns_name(path: Path, key: str, value: object) -> str:
    if not isinstance(value, str):
        _fail("operator_input_parse_failed", path, f"{key} must be one DNS hostname")
    if "secret://" in value.casefold() or "${" in value:
        _fail(
            "operator_input_secret_forbidden",
            path,
            f"{key} is a public route and has no secret injection point",
        )
    try:
        ip_address(value)
    except ValueError:
        pass
    else:
        _fail("operator_input_parse_failed", path, f"{key} must not be an IP literal")
    if value != value.casefold() or not DNS_NAME.fullmatch(value):
        _fail(
            "operator_input_parse_failed",
            path,
            f"{key} must be a lowercase absolute DNS hostname without a trailing dot",
        )
    return value


def _parse_public_routes(path: Path, source: bytes) -> dict[str, Any]:
    if source.startswith(b"\xef\xbb\xbf"):
        _fail("operator_input_encoding_invalid", path, "UTF-8 BOM is forbidden")
    try:
        value = tomllib.loads(source.decode("utf-8"))
    except UnicodeDecodeError as exc:
        _fail("operator_input_encoding_invalid", path, str(exc))
    except tomllib.TOMLDecodeError as exc:
        _fail("operator_input_parse_failed", path, str(exc))
    if set(value) != PUBLIC_ROUTE_KEYS:
        missing = sorted(PUBLIC_ROUTE_KEYS - set(value))
        extra = sorted(set(value) - PUBLIC_ROUTE_KEYS)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if extra:
            details.append(f"unknown: {', '.join(extra)}")
        _fail(
            "operator_input_parse_failed",
            path,
            f"public-routes@1 requires the exact public route keys ({'; '.join(details)})",
        )

    aliases = value["homepage_aliases"]
    if not isinstance(aliases, list) or len(aliases) > 8:
        _fail(
            "operator_input_parse_failed",
            path,
            "homepage_aliases must be an array containing at most 8 DNS hostnames",
        )
    semantic: dict[str, Any] = {
        key: _public_dns_name(path, key, value[key])
        for key in ("homepage", "scratch", "bridge", "minecraft")
    }
    semantic["homepage_aliases"] = sorted(
        _public_dns_name(path, f"homepage_aliases[{index}]", alias)
        for index, alias in enumerate(aliases)
    )
    all_names = [
        semantic["homepage"],
        *semantic["homepage_aliases"],
        semantic["scratch"],
        semantic["bridge"],
        semantic["minecraft"],
    ]
    if len(set(all_names)) != len(all_names):
        _fail(
            "operator_input_parse_failed",
            path,
            "public route hostnames must be unique across all roles",
        )
    return semantic


def _parse_minecraft_server(path: Path, source: bytes) -> dict[str, Any]:
    if source.startswith(b"\xef\xbb\xbf"):
        _fail("operator_input_encoding_invalid", path, "UTF-8 BOM is forbidden")
    try:
        value = tomllib.loads(source.decode("utf-8"))
    except UnicodeDecodeError as exc:
        _fail("operator_input_encoding_invalid", path, str(exc))
    except tomllib.TOMLDecodeError as exc:
        _fail("operator_input_parse_failed", path, str(exc))
    if set(value) != MINECRAFT_SERVER_KEYS:
        missing = sorted(MINECRAFT_SERVER_KEYS - set(value))
        extra = sorted(set(value) - MINECRAFT_SERVER_KEYS)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if extra:
            details.append(f"unknown: {', '.join(extra)}")
        _fail(
            "operator_input_parse_failed",
            path,
            f"minecraft-server@1 requires the exact keys ({'; '.join(details)})",
        )

    boolean_keys = {
        "allow_flight",
        "enable_query",
        "enable_status",
        "force_gamemode",
        "hardcore",
        "log_ips",
        "management_server_enabled",
        "white_list",
    }
    if any(not isinstance(value[key], bool) for key in boolean_keys):
        _fail(
            "operator_input_parse_failed",
            path,
            "Minecraft boolean fields must use TOML true or false",
        )
    if value["enable_query"] or value["management_server_enabled"]:
        _fail(
            "operator_input_parse_failed",
            path,
            "query and management server must remain disabled in minecraft-server@1",
        )
    if value["difficulty"] not in {"peaceful", "easy", "normal", "hard"}:
        _fail("operator_input_parse_failed", path, "difficulty is invalid")
    if value["gamemode"] not in {"survival", "creative", "adventure", "spectator"}:
        _fail("operator_input_parse_failed", path, "gamemode is invalid")

    integer_ranges = {
        "max_players": (1, 1000),
        "max_world_size": (1, 29_999_984),
        "network_compression_threshold": (-1, 1024),
        "simulation_distance": (3, 32),
        "spawn_protection": (0, 4096),
        "view_distance": (3, 32),
    }
    for key, (minimum, maximum) in integer_ranges.items():
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, int):
            _fail("operator_input_parse_failed", path, f"{key} must be an integer")
        if number < minimum or number > maximum:
            _fail(
                "operator_input_parse_failed",
                path,
                f"{key} must be between {minimum} and {maximum}",
            )
    max_tick_time = value["max_tick_time"]
    if (
        isinstance(max_tick_time, bool)
        or not isinstance(max_tick_time, int)
        or (max_tick_time != -1 and max_tick_time < 1)
    ):
        _fail(
            "operator_input_parse_failed",
            path,
            "max_tick_time must be -1 or a positive integer",
        )
    motd = value["motd"]
    if (
        not isinstance(motd, str)
        or not motd
        or len(motd) > MAX_MOTD_CHARACTERS
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in motd)
    ):
        _fail(
            "operator_input_parse_failed",
            path,
            f"motd must contain 1 through {MAX_MOTD_CHARACTERS} safe characters",
        )
    if "secret://" in motd.casefold() or "${" in motd:
        _fail(
            "operator_input_secret_forbidden",
            path,
            "minecraft-server@1 contains only public non-secret settings",
        )
    return {key: value[key] for key in sorted(MINECRAFT_SERVER_KEYS)}


def _parse_adapter(adapter: str, path: Path, relative_path: str) -> dict[str, Any]:
    if adapter == MINECRAFT_MOTD_ADAPTER:
        if relative_path != MINECRAFT_MOTD_PATH:
            _fail(
                "operator_input_path_invalid",
                path,
                f"{adapter} requires exact path {MINECRAFT_MOTD_PATH}",
            )
        return _parse_minecraft_motd(path, _read_source(path))
    if adapter == PUBLIC_ROUTES_ADAPTER:
        if relative_path != PUBLIC_ROUTES_PATH:
            _fail(
                "operator_input_path_invalid",
                path,
                f"{adapter} requires exact path {PUBLIC_ROUTES_PATH}",
            )
        return _parse_public_routes(path, _read_source(path))
    if adapter == MINECRAFT_SERVER_ADAPTER:
        if relative_path != MINECRAFT_SERVER_PATH:
            _fail(
                "operator_input_path_invalid",
                path,
                f"{adapter} requires exact path {MINECRAFT_SERVER_PATH}",
            )
        return _parse_minecraft_server(path, _read_source(path))
    _fail(
        "unsupported_operator_input_adapter",
        adapter,
        "selected profile references an adapter that this mcrctl version does not implement",
    )


def resolve_operator_inputs(
    loaded_order: LoadedOrder,
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate profile ownership and return stable semantic projections for the lock."""

    role_records = profile.get("operator_input_roles", [])
    declared_roles = {record["id"]: record for record in role_records}
    for record in role_records:
        if record["adapter"] not in SUPPORTED_ADAPTERS:
            _fail(
                "unsupported_operator_input_adapter",
                record["adapter"],
                f"profile operator input role {record['id']} uses an unsupported adapter",
            )

    selected = {record["role"]: record for record in loaded_order.order.get("operator_inputs", [])}
    missing = sorted(
        record["id"]
        for record in role_records
        if record["required"] and record["id"] not in selected
    )
    if missing:
        _fail(
            "operator_input_required",
            loaded_order.paths.order,
            f"selected profile requires operator input roles: {', '.join(missing)}",
        )

    resolved: list[dict[str, Any]] = []
    for role in sorted(selected):
        selection = selected[role]
        declaration = declared_roles.get(role)
        if declaration is None or declaration["adapter"] != selection["adapter"]:
            _fail(
                "operator_input_profile_mismatch",
                f"operator_inputs.{role}",
                "operator input role and adapter must be declared by the selected profile",
            )
        relative_path = selection["path"]
        source_path = loaded_order.paths.root / relative_path
        semantic = _parse_adapter(selection["adapter"], source_path, relative_path)
        resolved.append(
            {
                "role": role,
                "adapter": selection["adapter"],
                "path": relative_path,
                "semantic_sha256": semantic_sha256(semantic),
                "semantic": semantic,
            }
        )
    return resolved
