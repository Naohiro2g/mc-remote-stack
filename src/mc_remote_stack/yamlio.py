"""Small YAML boundary with consistent error reporting."""

from pathlib import Path
from typing import Any

import yaml


class YamlError(ValueError):
    """Raised when an operator-owned YAML file cannot be loaded."""


def load_mapping(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise YamlError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise YamlError(f"{path} must contain a YAML mapping")
    return value


def dump_mapping(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(value, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
