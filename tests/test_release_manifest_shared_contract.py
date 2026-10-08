"""同一commitの66共有caseについて受入・拒否理由・拒否段階を照合する。"""

import hashlib
import json
from pathlib import Path

import pytest

from mc_remote_stack.release_manifest import ReleaseManifestError, collect_release_artifacts, parse_release_manifest

DATA = Path(__file__).parents[1] / "src/mc_remote_stack/data"
LOCK = json.loads((DATA / "release-manifest-lock.json").read_bytes())
FIXTURE_SOURCE = (DATA / LOCK["fixtures"]["path"]).read_bytes()
FIXTURES = json.loads(FIXTURE_SOURCE)

REASONS = {
    "release_manifest_schema_invalid": ("schema_invalid", "schema"),
    "release_manifest_duplicate_role": ("duplicate_role", "semantic"),
    "release_manifest_duplicate_key": ("duplicate_artifact_key", "semantic"),
    "release_manifest_mixed_selectors": ("mixed_artifact_variants", "semantic"),
    "release_manifest_duplicate_verification": ("duplicate_verification_version", "semantic"),
    "release_manifest_compatibility_set_mismatch": ("minecraft_version_set_mismatch", "semantic"),
    "release_manifest_jar_digest_mismatch": ("jar_sha256_mismatch", "semantic"),
    "release_manifest_record_artifact_forbidden": ("record_listed_as_artifact", "semantic"),
    "release_manifest_declaration_digest_mismatch": ("declaration_sha256_mismatch", "content"),
    "release_manifest_record_digest_mismatch": ("record_sha256_mismatch", "content"),
    "release_manifest_declaration_invalid": ("declaration_content_invalid", "content"),
    "release_manifest_declaration_versions_mismatch": ("declaration_versions_mismatch", "content"),
    "release_manifest_jar_declaration_bytes_mismatch": ("jar_declaration_bytes_mismatch", "content"),
    "release_manifest_asset_read_failed": ("reference_content_missing", "content"),
    "release_manifest_declaration_read_failed": ("reference_content_missing", "content"),
    "release_manifest_artifact_bytes_mismatch": ("artifact_bytes_mismatch", "content"),
    "release_manifest_artifact_digest_mismatch": ("artifact_sha256_mismatch", "content"),
}


def test_fixed_schema_fixture_identity_and_legacy_v1_regression_source():
    assert LOCK["source_commit"] == "fb6880b192a0063241f95c44a5fa6b836f5e7394"
    for pin in (LOCK["schema"], LOCK["fixtures"]):
        source = (DATA / pin["path"]).read_bytes()
        assert len(source) == pin["bytes"]
        assert hashlib.sha256(source).hexdigest() == pin["sha256"]
    legacy = FIXTURES["legacy_v1_schema_source"]
    assert hashlib.sha256((DATA / "schemas/release-manifest.schema.json").read_bytes()).hexdigest() == legacy["sha256"]
    assert len(FIXTURES["cases"]) == 66


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda case: case["id"])
def test_shared_manifest_contract(case):
    outcome = {"valid": True, "reason": None, "stage": None}
    try:
        manifest = parse_release_manifest(json.dumps(case["manifest"]).encode())
        if "contents" in case:
            contents = {name: value.encode("utf-8") for name, value in case["contents"].items()}
            selections = [
                (item["role"], item["kind"], item.get("os"), item.get("arch"))
                for item in manifest["artifacts"]
                if item["kind"] == "https-file" and item["file"] in contents
            ]
            collect_release_artifacts(
                manifest, selections, read_asset=contents.__getitem__,
                read_declaration=lambda path, commit: contents[path],
                jar_declaration=case["jar_declaration"].encode("utf-8") if "jar_declaration" in case else None,
            )
    except ReleaseManifestError as exc:
        if case["manifest"].get("schema_version") not in (1, 2):
            reason, stage = "unsupported_schema_version", "version"
        else:
            reason, stage = REASONS[exc.reason]
        outcome = {"valid": False, "reason": reason, "stage": stage}
    assert outcome == case["expected"]
