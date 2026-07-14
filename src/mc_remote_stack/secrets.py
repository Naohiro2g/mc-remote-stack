"""Local secret store that never places values in a deployment project."""

import os
import re
import tempfile
from pathlib import Path

SECRET_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def secret_home() -> Path:
    override = os.environ.get("MC_REMOTE_SECRET_HOME")
    if override:
        return Path(override).expanduser().resolve()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "mc-remote" / "secrets"


def validate_secret_name(name: str) -> None:
    if not SECRET_NAME.fullmatch(name):
        raise ValueError("secret name must match [a-z][a-z0-9_]{1,63}")


def project_secret_dir(deployment_name: str) -> Path:
    validate_secret_name(deployment_name.replace("-", "_"))
    return secret_home() / deployment_name


def set_secret(deployment_name: str, name: str, value: str) -> Path:
    validate_secret_name(name)
    if not value:
        raise ValueError("secret value must not be empty")
    directory = project_secret_dir(deployment_name)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.write("\n")
        destination = directory / name
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        return destination
    finally:
        if temporary.exists():
            temporary.unlink()


def list_secrets(deployment_name: str) -> list[str]:
    directory = project_secret_dir(deployment_name)
    if not directory.exists():
        return []
    return sorted(path.name for path in directory.iterdir() if path.is_file() and not path.name.startswith("."))


def read_secret(deployment_name: str, reference: str) -> str:
    prefix = "secret://"
    if not reference.startswith(prefix):
        raise ValueError("secret reference must start with secret://")
    name = reference.removeprefix(prefix)
    validate_secret_name(name)
    path = project_secret_dir(deployment_name) / name
    if not path.is_file():
        raise ValueError(f"secret is not configured: {name}")
    if path.stat().st_mode & 0o077:
        raise ValueError(f"secret has unsafe permissions: {name}")
    value = path.read_text(encoding="utf-8").rstrip("\r\n")
    if not value:
        raise ValueError(f"secret is empty: {name}")
    return value
