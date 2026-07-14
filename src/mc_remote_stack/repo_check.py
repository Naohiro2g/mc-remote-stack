"""Secret and generated-file checks for deployment projects."""

import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path

from .validation import Issue
from .yamlio import YamlError, load_mapping

SENSITIVE_KEY = re.compile(r"(?:password|passwd|token|secret|credential|private[_-]?key)$", re.IGNORECASE)
SECRET_TEXT = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bmcr[sp]_[A-Za-z0-9_-]{12,}\b"),
)
FORBIDDEN_PARTS = {"generated", "secrets", "backup", "backups"}
REQUIRED_IGNORES = {"/generated/", "/secrets/", "/.env", "*.zip"}


def _ignored_without_git(relative: Path, ignores: set[str]) -> bool:
    value = relative.as_posix()
    for raw_pattern in ignores:
        pattern = raw_pattern.strip()
        if not pattern or pattern.startswith("#") or pattern.startswith("!"):
            continue
        if pattern.startswith("/"):
            pattern = pattern.removeprefix("/")
            if pattern.endswith("/"):
                prefix = pattern.removesuffix("/")
                if value == prefix or value.startswith(f"{prefix}/"):
                    return True
            elif value == pattern:
                return True
        elif fnmatch(value, pattern) or fnmatch(relative.name, pattern):
            return True
    return False


def _project_files(root: Path, ignores: set[str]) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return [
            path
            for path in root.rglob("*")
            if path.is_file()
            and ".git" not in path.parts
            and not _ignored_without_git(path.relative_to(root), ignores)
        ]
    return [root / line for line in result.stdout.splitlines() if line]


def _check_values(value: object, path: str, issues: list[Issue]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if SENSITIVE_KEY.search(str(key)) and isinstance(child, str) and child and not child.startswith("secret://"):
                issues.append(Issue("FAIL", child_path, "secret-like key must use a secret:// reference"))
            _check_values(child, child_path, issues)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _check_values(child, f"{path}[{index}]", issues)


def check_repository(root: Path) -> list[Issue]:
    root = root.resolve()
    issues: list[Issue] = []
    ignore_path = root / ".gitignore"
    try:
        ignores = set(ignore_path.read_text(encoding="utf-8").splitlines())
    except OSError as exc:
        issues.append(Issue("FAIL", ".gitignore", f"cannot read: {exc}"))
        ignores = set()
    for required in sorted(REQUIRED_IGNORES - ignores):
        issues.append(Issue("FAIL", ".gitignore", f"missing required pattern {required}"))

    for path in _project_files(root, ignores):
        relative = path.relative_to(root)
        if any(part.lower() in FORBIDDEN_PARTS for part in relative.parts):
            issues.append(Issue("FAIL", str(relative), "generated, secret, or backup path must not be tracked"))
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in SECRET_TEXT:
            if pattern.search(content):
                issues.append(Issue("FAIL", str(relative), "secret-like literal detected"))
                break
        if path.suffix in {".yml", ".yaml"} and path.name != "secrets.example.yml":
            try:
                _check_values(load_mapping(path), str(relative), issues)
            except YamlError as exc:
                issues.append(Issue("FAIL", str(relative), str(exc)))
    return issues
