"""Exact runtime artifact mount contracts shared by doctor and migrations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class RuntimeArtifactContractError(ValueError):
    """The effective runtime does not mount the exact locked artifact."""


@dataclass(frozen=True)
class RuntimeMount:
    kind: str
    source: str | None
    target: str
    read_only: bool


def expected_mcremote_mount(lock: dict[str, Any]) -> tuple[str, str]:
    components = [
        component
        for component in lock.get("components", [])
        if component.get("role") == "mcremote-plugin"
    ]
    if len(components) != 1:
        raise RuntimeArtifactContractError(
            "lock must contain exactly one McRemote plugin component"
        )
    artifact_id = components[0].get("artifact")
    artifacts = [
        artifact
        for artifact in lock.get("artifacts", [])
        if artifact.get("id") == artifact_id
    ]
    if len(artifacts) != 1:
        raise RuntimeArtifactContractError(
            "lock must contain exactly one McRemote plugin artifact"
        )
    artifact = artifacts[0]
    filename = artifact.get("filename")
    sha256 = artifact.get("sha256")
    runtime = lock.get("runtime")
    artifact_store = runtime.get("artifact_store") if isinstance(runtime, dict) else None
    if (
        not isinstance(filename, str)
        or not filename
        or "/" in filename
        or not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
        or not isinstance(artifact_store, str)
        or not artifact_store
    ):
        raise RuntimeArtifactContractError(
            "locked McRemote artifact filename, digest, or store is invalid"
        )
    source = str((Path(artifact_store) / "sha256" / sha256).resolve(strict=False))
    return source, f"/plugins/{filename}"


def validate_mcremote_mounts(
    mounts: list[RuntimeMount],
    lock: dict[str, Any],
) -> None:
    expected_source, expected_target = expected_mcremote_mount(lock)
    exact = [
        mount
        for mount in mounts
        if mount.kind == "bind"
        and mount.source is not None
        and str(Path(mount.source).resolve(strict=False)) == expected_source
        and mount.target == expected_target
        and mount.read_only
    ]
    if len(exact) != 1:
        raise RuntimeArtifactContractError(
            "runtime requires one read-only bind of the exact locked McRemote artifact"
        )

    expected_path = PurePosixPath(expected_target)
    for mount in mounts:
        target = PurePosixPath(mount.target)
        if target != expected_path and expected_path.is_relative_to(target):
            raise RuntimeArtifactContractError(
                f"mount at {target} masks the exact McRemote artifact mount"
            )
        normalized_name = target.name.lower().replace("_", "-")
        if (
            target != expected_path
            and target.parent == PurePosixPath("/plugins")
            and ("mc-remote" in normalized_name or "mcremote" in normalized_name)
        ):
            raise RuntimeArtifactContractError(
                f"additional McRemote artifact mount is not allowed at {target}"
            )
