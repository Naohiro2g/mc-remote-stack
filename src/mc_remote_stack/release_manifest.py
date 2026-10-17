"""固定したv1/v2契約によるrelease metadataの検査と選択・収集照合。

正本はKnowledgeのDEC 2026-09-06-04および2026-10-07-09。
remote取得は呼び出し側が行い、このmoduleは渡された文書と生bytesを検査する。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


@dataclass(frozen=True)
class ReleaseManifestError(ValueError):
    reason: str
    path: str
    detail: str

    def __str__(self) -> str:
        return f"{self.reason}: {self.path}: {self.detail}"


def _fail(reason: str, path: object, detail: str) -> None:
    raise ReleaseManifestError(reason, str(path), detail)


def _load_schema(data_root: Traversable | None) -> dict[str, Any]:
    root = data_root or files("mc_remote_stack").joinpath("data")
    resource = root.joinpath("schemas", "release-manifest.schema.json")
    try:
        source = resource.read_bytes()
    except OSError as exc:
        _fail("release_manifest_schema_missing", resource, str(exc))
    try:
        schema = json.loads(source)
        Draft202012Validator.check_schema(schema)
    except (UnicodeDecodeError, json.JSONDecodeError, SchemaError) as exc:
        _fail("release_manifest_schema_invalid", resource, str(exc))
    return schema


def parse_release_manifest(
    source: bytes,
    *,
    path: object = "manifest.json",
    data_root: Traversable | None = None,
) -> dict[str, Any]:
    """Parse and schema-validate one release manifest, returning it as-is.

    Callers that already know the expected `release_tag` and per-artifact
    digests (from a review step) compare those values themselves; this
    function only enforces the shape, not any particular release identity.
    """

    try:
        document = json.loads(source)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("release_manifest_parse_failed", path, str(exc))
    version = document.get("schema_version") if isinstance(document, dict) else None
    if isinstance(version, bool) or version not in (1, 2):
        _fail("release_manifest_schema_invalid", path, f"unsupported schema_version: {version!r}")
    schema = _load_schema(data_root) if version == 1 else _load_v2_schema(data_root)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda error: error.json_path,
    )
    if errors:
        first = errors[0]
        _fail("release_manifest_schema_invalid", f"{path}{first.json_path}", first.message)
    if version == 2:
        _validate_v2_semantics(document, path=path)
        return document
    roles = [artifact["role"] for artifact in document["artifacts"]]
    if len(roles) != len(set(roles)):
        _fail("release_manifest_duplicate_role", path, "artifacts[].role must be unique")
    return document


def artifact_by_role(
    manifest: dict[str, Any],
    role: str,
    *,
    kind: str | None = None,
    os: str | None = None,
    arch: str | None = None,
) -> dict[str, Any]:
    """v1の既存呼び出しを保ち、v2では期待kindを明示した選択を要求する。"""
    if manifest.get("schema_version") == 2 or kind is not None:
        if kind is None:
            _fail("release_manifest_kind_required", role, "v2 selection requires expected kind")
        return select_release_artifact(manifest, role, kind=kind, os=os, arch=arch)
    matches = [artifact for artifact in manifest["artifacts"] if artifact["role"] == role]
    if len(matches) != 1:
        _fail(
            "release_manifest_role_missing",
            f"artifacts[role={role}]",
            f"expected exactly one artifact with role {role!r}, found {len(matches)}",
        )
    return matches[0]


def _load_v2_schema(data_root: Traversable | None) -> dict[str, Any]:
    """toolingから取得済みの固定schemaのみ読む。runtimeでremoteは参照しない。"""
    root = data_root or files("mc_remote_stack").joinpath("data")
    lock_resource = root.joinpath("release-manifest-lock.json")
    try:
        lock = json.loads(lock_resource.read_bytes())
    except OSError as exc:
        _fail("release_manifest_v2_schema_not_pinned", lock_resource, str(exc))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("release_manifest_schema_lock_invalid", lock_resource, str(exc))
    if (
        not isinstance(lock, dict)
        or set(lock)
        not in ({"repository", "source_commit", "schema"}, {"repository", "source_commit", "schema", "fixtures"})
        or lock.get("repository") != "https://github.com/Naohiro2g/minecraft-remote-tooling"
        or not isinstance(lock.get("source_commit"), str)
        or re.fullmatch(r"[0-9a-f]{40}", lock["source_commit"]) is None
    ):
        _fail("release_manifest_schema_lock_invalid", lock_resource, "expected immutable tooling commit")
    pin = lock["schema"]
    if (
        not isinstance(pin, dict)
        or set(pin) != {"path", "bytes", "sha256"}
        or pin.get("path") != "schemas/release-manifest-v2.schema.json"
        or type(pin.get("bytes")) is not int
        or pin["bytes"] < 0
        or not isinstance(pin.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", pin["sha256"]) is None
    ):
        _fail("release_manifest_schema_lock_invalid", lock_resource, "expected path, raw bytes and SHA-256")
    resource = root.joinpath("schemas", "release-manifest-v2.schema.json")
    try:
        source = resource.read_bytes()
    except OSError as exc:
        _fail("release_manifest_schema_missing", resource, str(exc))
    if len(source) != pin["bytes"] or hashlib.sha256(source).hexdigest() != pin["sha256"]:
        _fail("release_manifest_schema_digest_mismatch", resource, "schema differs from its separate tooling pin")
    try:
        schema = json.loads(source)
        Draft202012Validator.check_schema(schema)
    except (UnicodeDecodeError, json.JSONDecodeError, SchemaError) as exc:
        _fail("release_manifest_schema_invalid", resource, str(exc))
    if not isinstance(schema, dict) or schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        _fail("release_manifest_schema_invalid", resource, "expected Draft 2020-12 object schema")
    _reject_remote_references(schema, resource)
    return schema


def _reject_remote_references(value: object, path: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("$ref", "$dynamicRef") and (not isinstance(child, str) or not child.startswith("#")):
                _fail("release_manifest_schema_invalid", path, "schema references must be local fragments")
            _reject_remote_references(child, path)
    elif isinstance(value, list):
        for child in value:
            _reject_remote_references(child, path)


def _relative_path(value: str, *, basename: bool, path: object) -> None:
    parts = PurePosixPath(value).parts
    if (
        not value
        or value in (".", "..")
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or ".." in parts
        or "\x00" in value
        or (basename and "/" in value)
    ):
        reason = "release_manifest_asset_name_invalid" if basename else "release_manifest_declaration_path_invalid"
        _fail(reason, path, "expected a Release asset basename" if basename else "expected a producer-relative path")


def _validate_v2_semantics(document: dict[str, Any], *, path: object = "manifest.json") -> None:
    keys: set[tuple[str, str | None, str | None]] = set()
    selectors: dict[str, bool] = {}
    for artifact in document["artifacts"]:
        role = artifact["role"]
        key = (role, artifact.get("os"), artifact.get("arch"))
        if key in keys:
            _fail("release_manifest_duplicate_key", path, f"duplicate artifact key {key!r}")
        keys.add(key)
        has_selector = "os" in artifact
        if role in selectors and selectors[role] != has_selector:
            _fail("release_manifest_mixed_selectors", path, f"role {role!r} mixes selected and unselected artifacts")
        selectors[role] = has_selector
    compatibility = document.get("minecraft_compatibility")
    if compatibility is None:
        return
    jar = select_release_artifact(document, "jar", kind="https-file")
    declaration = compatibility["declaration"]
    _relative_path(declaration["path"], basename=False, path=f"{path}.minecraft_compatibility.declaration.path")
    versions = [item["minecraft_version"] for item in compatibility["verifications"]]
    if len(versions) != len(set(versions)):
        _fail(
            "release_manifest_duplicate_verification", path, "expected exactly one verification per Minecraft version"
        )
    if set(versions) != set(declaration["minecraft_versions"]):
        _fail(
            "release_manifest_compatibility_set_mismatch", path, "declared and verified Minecraft version sets differ"
        )
    artifact_files = {item.get("file") for item in document["artifacts"]}
    for verification in compatibility["verifications"]:
        if verification["jar_sha256"] != jar["sha256"]:
            _fail(
                "release_manifest_jar_digest_mismatch", path, "every verification must identify the same published JAR"
            )
        record = verification["record"]
        _relative_path(record["file"], basename=True, path=f"{path}.minecraft_compatibility.record.file")
        if record["file"] in artifact_files:
            _fail(
                "release_manifest_record_artifact_forbidden", path, "verification records are referenced only by record"
            )


def select_release_artifact(
    manifest: dict[str, Any],
    role: str,
    *,
    kind: str,
    os: str | None = None,
    arch: str | None = None,
) -> dict[str, Any]:
    """検査済みmanifestからrole・期待kind・明示selectorで1件を選ぶ。fallbackしない。"""
    matches = [item for item in manifest["artifacts"] if item["role"] == role]
    variant = any("os" in item or "arch" in item for item in matches)
    if (os is None) != (arch is None) or (variant and os is None):
        _fail("release_manifest_selector_required", role, "variant selection requires both os and arch")
    if os is not None:
        if os not in ("windows", "macos", "linux") or arch not in ("x64", "arm64"):
            _fail("release_manifest_selector_invalid", role, "unknown os or arch; fallback is forbidden")
        matches = [item for item in matches if item.get("os") == os and item.get("arch") == arch]
    if len(matches) != 1:
        _fail("release_manifest_role_missing", role, f"expected exactly one selected artifact, found {len(matches)}")
    selected = matches[0]
    if selected["kind"] != kind:
        _fail("release_manifest_kind_mismatch", role, f"expected {kind!r}, found {selected['kind']!r}")
    return selected


def _read_release_file(reader: Callable[[str], bytes], name: str) -> bytes:
    _relative_path(name, basename=True, path=name)
    try:
        return reader(name)
    except (OSError, KeyError) as exc:
        _fail("release_manifest_asset_read_failed", name, str(exc))


def verify_minecraft_compatibility(
    manifest: dict[str, Any],
    *,
    read_asset: Callable[[str], bytes],
    read_declaration: Callable[[str, str], bytes] | None,
    jar_declaration: bytes | None = None,
) -> None:
    """全verificationのrecordと、source_commitの宣言の生bytes・内容を照合する。"""
    compatibility = manifest.get("minecraft_compatibility")
    if compatibility is None:
        return
    if read_declaration is None:
        _fail("release_manifest_declaration_required", "minecraft_compatibility", "read declaration at source_commit")
    declaration = compatibility["declaration"]
    try:
        source = read_declaration(declaration["path"], manifest["source_commit"])
    except (OSError, KeyError) as exc:
        _fail("release_manifest_declaration_read_failed", declaration["path"], str(exc))
    if hashlib.sha256(source).hexdigest() != declaration["sha256"]:
        _fail("release_manifest_declaration_digest_mismatch", declaration["path"], "raw declaration bytes differ")
    try:
        contents = json.loads(source)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("release_manifest_declaration_invalid", declaration["path"], str(exc))
    if (
        not isinstance(contents, dict)
        or set(contents) != {"schema", "schema_version", "minecraft_versions"}
        or contents["schema"] != "mc-remote.minecraft-targets"
        or isinstance(contents["schema_version"], bool)
        or contents["schema_version"] != 1
    ):
        _fail("release_manifest_declaration_invalid", declaration["path"], "expected minecraft-targets v1 object")
    versions = contents["minecraft_versions"]
    if (
        not isinstance(versions, list)
        or not versions
        or any(not isinstance(version, str) or not version for version in versions)
        or len(versions) != len(set(versions))
    ):
        _fail(
            "release_manifest_declaration_invalid", declaration["path"], "expected nonempty, unique Minecraft versions"
        )
    if set(versions) != set(declaration["minecraft_versions"]):
        _fail("release_manifest_declaration_versions_mismatch", declaration["path"], "declaration versions differ")
    if jar_declaration is not None and jar_declaration != source:
        _fail(
            "release_manifest_jar_declaration_bytes_mismatch",
            declaration["path"],
            "embedded declaration bytes differ",
        )
    for verification in compatibility["verifications"]:
        record = verification["record"]
        source = _read_release_file(read_asset, record["file"])
        if hashlib.sha256(source).hexdigest() != record["sha256"]:
            _fail("release_manifest_record_digest_mismatch", record["file"], "raw verification record bytes differ")


def collect_release_artifacts(
    manifest: dict[str, Any],
    selections: list[tuple[str, str, str | None, str | None]],
    *,
    read_asset: Callable[[str], bytes],
    read_declaration: Callable[[str, str], bytes] | None = None,
    jar_declaration: bytes | None = None,
) -> list[dict[str, Any]]:
    """検査済み文書から必要なartifactだけを取得・照合し、compatibilityの全参照も照合する。"""
    if manifest["schema_version"] == 2:
        _validate_v2_semantics(manifest)
    selected = [
        select_release_artifact(manifest, role, kind=kind, os=os, arch=arch) for role, kind, os, arch in selections
    ]
    # 取得前に全selectionを確定する。選択不足やkind違いで一部だけ取得しない。
    verify_minecraft_compatibility(
        manifest,
        read_asset=read_asset,
        read_declaration=read_declaration,
        jar_declaration=jar_declaration,
    )
    for artifact in selected:
        if artifact["kind"] != "https-file":
            continue
        source = _read_release_file(read_asset, artifact["file"])
        if "bytes" in artifact and len(source) != artifact["bytes"]:
            _fail(
                "release_manifest_artifact_bytes_mismatch", artifact["file"], "selected artifact raw byte count differs"
            )
        if hashlib.sha256(source).hexdigest() != artifact["sha256"]:
            _fail("release_manifest_artifact_digest_mismatch", artifact["file"], "selected artifact SHA-256 differs")
    return selected


def verify_preset_minecraft(
    manifest: dict[str, Any],
    preset: dict[str, Any],
    *,
    paper_build: int,
    java_version: str,
) -> dict[str, Any]:
    """presetの明示MC版・JARのidentityを実測build/runtimeと合わせて検証構成へ照合する。"""

    def unverified(detail: str) -> None:
        _fail("release_manifest_foundation_unverified", "preset", detail + "; return to gate coordinator")

    compatibility = manifest.get("minecraft_compatibility")
    if compatibility is None:
        unverified("manifest has no Minecraft verification")
    _validate_v2_semantics(manifest)
    components = preset.get("components", [])
    artifacts = preset.get("artifacts", [])
    if (
        not isinstance(components, list)
        or not all(isinstance(item, dict) for item in components)
        or not isinstance(artifacts, list)
        or not all(isinstance(item, dict) for item in artifacts)
    ):
        unverified("preset components and artifacts must be lists of records")
    paper = [item for item in components if item.get("role") == "paper-server"]
    plugin = [item for item in components if item.get("role") == "mcremote-plugin"]
    if len(paper) != 1 or len(plugin) != 1:
        unverified("preset must explicitly identify one Paper server and one McRemote plugin")
    version = paper[0].get("minecraft_version")
    matches = [item for item in compatibility["verifications"] if item["minecraft_version"] == version]
    if len(matches) != 1:
        unverified(f"explicit Minecraft version {version!r} is not in the compatibility set")
    verification = matches[0]
    if type(paper_build) is not int or paper_build < 1 or not isinstance(java_version, str) or not java_version:
        unverified("measured Paper build and exact Java runtime string are required")
    identities = []
    for component in (paper[0], plugin[0]):
        artifacts = [item for item in preset.get("artifacts", []) if item.get("id") == component.get("artifact")]
        if len(artifacts) != 1 or artifacts[0].get("kind") != "https-file":
            unverified("preset must identify the exact server and plugin JAR SHA-256")
        identities.append(artifacts[0].get("sha256"))
    actual = {
        "server_sha256": identities[0],
        "jar_sha256": identities[1],
        "paper_build": paper_build,
        "java_version": java_version,
    }
    differences = [key for key, value in actual.items() if verification[key] != value]
    if differences:
        unverified("verification configuration differs: " + ", ".join(differences))
    return verification
