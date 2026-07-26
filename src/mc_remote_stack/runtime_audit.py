"""Sanitized diagnostics for explicit Minecraft runtime network activity."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit


class RuntimeEvent(TypedDict):
    category: str
    component: str
    host: str | None
    count: int


class RuntimeAudit(TypedDict):
    log_name: str
    event_count: int
    events: list[RuntimeEvent]
    limitations: list[str]


PAPER_LIBRARY_START = re.compile(
    r"\[SpigotLibraryLoader\] \[(?P<component>[^\]\r\n]+)\] "
    r"Loading \d+ librar(?:y|ies)"
)
PAPER_LIBRARY_DOWNLOAD = re.compile(
    r"\[SpigotLibraryLoader\] Downloading (?P<url>https?://\S+)"
)
RUNTIME_CONTENT_DOWNLOAD = re.compile(
    r"\[(?P<component>[^\]\r\n]+)\] Downloading Minecraft JAR\b"
)
UPDATE_CHECK = re.compile(
    r".*\[(?P<component>[^\]\r\n]+)\][^\r\n]*"
    r"(?:Searching for updates|up-to-date|newer plugin version available)"
)


def audit_minecraft_log(path: Path) -> RuntimeAudit:
    """Report only recognized event classes, never raw log lines or URL paths."""
    resolved = path.expanduser().resolve()
    events: dict[tuple[str, str, str | None], int] = {}
    active_library_component = "unknown"
    with resolved.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if match := PAPER_LIBRARY_START.search(line):
                active_library_component = match.group("component")
                continue
            if match := PAPER_LIBRARY_DOWNLOAD.search(line):
                host = urlsplit(match.group("url")).hostname
                key = (
                    "paper-library-download",
                    active_library_component,
                    host.lower() if host else None,
                )
                events[key] = events.get(key, 0) + 1
                continue
            if match := RUNTIME_CONTENT_DOWNLOAD.search(line):
                key = (
                    "runtime-content-download",
                    match.group("component"),
                    None,
                )
                events[key] = events.get(key, 0) + 1
                continue
            if match := UPDATE_CHECK.search(line):
                key = ("update-check", match.group("component"), None)
                events[key] = events.get(key, 0) + 1

    rendered = [
        RuntimeEvent(
            category=category,
            component=component,
            host=host,
            count=count,
        )
        for (category, component, host), count in events.items()
    ]
    return {
        "log_name": resolved.name,
        "event_count": sum(event["count"] for event in rendered),
        "events": rendered,
        "limitations": [
            "only explicit matching log events are reported",
            "absence of events does not prove absence of runtime network access",
        ],
    }
