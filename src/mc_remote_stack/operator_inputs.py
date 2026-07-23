"""Typed, non-secret operator-owned input adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .preset_registry import semantic_sha256
from .toml_project import LoadedOrder

MINECRAFT_MOTD_ADAPTER = "minecraft-motd@1"
MINECRAFT_MOTD_PATH = "operator/minecraft-motd/server.properties"
MAX_MOTD_SOURCE_BYTES = 4096
MAX_MOTD_CHARACTERS = 256
SUPPORTED_ADAPTERS = frozenset({MINECRAFT_MOTD_ADAPTER})


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


def _parse_adapter(adapter: str, path: Path, relative_path: str) -> dict[str, str]:
    if adapter == MINECRAFT_MOTD_ADAPTER:
        if relative_path != MINECRAFT_MOTD_PATH:
            _fail(
                "operator_input_path_invalid",
                path,
                f"{adapter} requires exact path {MINECRAFT_MOTD_PATH}",
            )
        return _parse_minecraft_motd(path, _read_source(path))
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
