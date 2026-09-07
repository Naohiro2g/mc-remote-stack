import json

import pytest

from mc_remote_stack.release_manifest import (
    ReleaseManifestError,
    artifact_by_role,
    parse_release_manifest,
)

# Real manifest.json fetched from scratch-editor's v2301.0.0b7-post1 GitHub
# Release (DEC 2026-09-06-02/-04), kept verbatim so this test exercises the
# schema against an actual, already-published document rather than a
# hand-written approximation of it.
SCRATCH_V2301_0_0B7_POST1 = json.dumps(
    {
        "schema": "mc-remote.release-manifest",
        "schema_version": 1,
        "release_tag": "v2301.0.0b7-post1",
        "source_commit": "19b9993c79ab712bd5e86234ca0b4445f0f867f1",
        "artifacts": [
            {
                "role": "scratch",
                "kind": "oci",
                "locator": "ghcr.io/naohiro2g/mc-remote-scratch",
                "digest": "sha256:09aa987ea413f4c4680afd6d2a1dbb3a5c2977cb0e7025171c590cf2d1e8a359",
            },
            {
                "role": "bridge",
                "kind": "oci",
                "locator": "ghcr.io/naohiro2g/mc-remote-bridge",
                "digest": "sha256:3c165947bb67053f927a099e47e049d742631761932c7f16c259630215731b4d",
            },
            {
                "role": "wirescope",
                "kind": "https-file",
                "file": "wirescope-app.zip",
                "sha256": "98d684dc15f369f6568d249357d8fd3af11893859d3c07c2554295df19a263b8",
            },
            {
                "role": "wirescope-manifest",
                "kind": "https-file",
                "file": "wirescope-app.manifest.json",
                "sha256": "664a20c941e2568c5b5185ce3d7cc07180a1473a8d4728c05e2c1bbd4f75518f",
            },
            {
                "role": "contracts",
                "kind": "https-file",
                "file": "contracts.tar.gz",
                "sha256": "fd7ceaee078fde087731d929a6edb1e21021c073d8c23334d1d5aa9828532758",
            },
        ],
    }
).encode()


def test_parses_the_real_scratch_release_manifest() -> None:
    manifest = parse_release_manifest(SCRATCH_V2301_0_0B7_POST1)

    assert manifest["release_tag"] == "v2301.0.0b7-post1"
    assert len(manifest["artifacts"]) == 5

    scratch = artifact_by_role(manifest, "scratch")
    assert scratch["kind"] == "oci"
    assert scratch["locator"] == "ghcr.io/naohiro2g/mc-remote-scratch"

    wirescope = artifact_by_role(manifest, "wirescope")
    assert wirescope["kind"] == "https-file"
    assert wirescope["file"] == "wirescope-app.zip"


def test_artifact_by_role_fails_closed_when_role_is_absent() -> None:
    manifest = parse_release_manifest(SCRATCH_V2301_0_0B7_POST1)

    with pytest.raises(ReleaseManifestError) as exc_info:
        artifact_by_role(manifest, "sdist")

    assert exc_info.value.reason == "release_manifest_role_missing"


def test_mcremote_style_manifest_parses() -> None:
    source = json.dumps(
        {
            "schema": "mc-remote.release-manifest",
            "schema_version": 1,
            "release_tag": "v1.21.11-2301.0.0b7",
            "source_commit": "3d5f710db97f4b14613f7e0abaafd535701d1906",
            "artifacts": [
                {
                    "role": "jar",
                    "kind": "https-file",
                    "file": "mc-remote-1.21.11-2301.0.0b7.jar",
                    "sha256": "f08388cf393e02db1eb605e707dfaec890792e7a475de5a51caacbc940028ee9",
                }
            ],
        }
    ).encode()

    manifest = parse_release_manifest(source)

    assert artifact_by_role(manifest, "jar")["file"] == "mc-remote-1.21.11-2301.0.0b7.jar"


def test_python_manifest_with_bundled_wirescope_source_commit_parses() -> None:
    source = json.dumps(
        {
            "schema": "mc-remote.release-manifest",
            "schema_version": 1,
            "release_tag": "v2301.0.0b7",
            "source_commit": "91a25d317c95570fd9d92b5e63a5f585a856eda3",
            "bundled_wirescope_source_commit": "0be46fcfaca409a5ede10f592520d93e7c59ba15",
            "artifacts": [
                {
                    "role": "wheel",
                    "kind": "https-file",
                    "file": "minecraft_remote_api-2301.0.0b7-py3-none-any.whl",
                    "sha256": "81540d22b1ee05d7b24bd2e6c9270a37a194c6c1ddc868148a8263624826d2ba",
                }
            ],
        }
    ).encode()

    manifest = parse_release_manifest(source)

    assert manifest["bundled_wirescope_source_commit"] == "0be46fcfaca409a5ede10f592520d93e7c59ba15"


def test_rejects_malformed_json() -> None:
    with pytest.raises(ReleaseManifestError) as exc_info:
        parse_release_manifest(b"not json")

    assert exc_info.value.reason == "release_manifest_parse_failed"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.pop("schema"),
        lambda doc: doc.update(schema="something-else"),
        lambda doc: doc.update(schema_version=2),
        lambda doc: doc["artifacts"][0].pop("kind"),
        lambda doc: doc.update(artifacts=[]),
    ],
)
def test_rejects_schema_violations(mutate) -> None:
    doc = json.loads(SCRATCH_V2301_0_0B7_POST1)
    mutate(doc)

    with pytest.raises(ReleaseManifestError) as exc_info:
        parse_release_manifest(json.dumps(doc).encode())

    assert exc_info.value.reason == "release_manifest_schema_invalid"


def test_kind_default_is_not_permitted() -> None:
    """DEC 2026-09-06-04 explicitly retracted the earlier 09-06-03 default of
    https-file when `kind` is omitted; every artifact must state its kind."""

    doc = json.loads(SCRATCH_V2301_0_0B7_POST1)
    del doc["artifacts"][2]["kind"]

    with pytest.raises(ReleaseManifestError) as exc_info:
        parse_release_manifest(json.dumps(doc).encode())

    assert exc_info.value.reason == "release_manifest_schema_invalid"


def test_rejects_duplicate_roles() -> None:
    doc = json.loads(SCRATCH_V2301_0_0B7_POST1)
    doc["artifacts"].append(dict(doc["artifacts"][0]))

    with pytest.raises(ReleaseManifestError) as exc_info:
        parse_release_manifest(json.dumps(doc).encode())

    assert exc_info.value.reason == "release_manifest_duplicate_role"


def test_oci_artifact_rejects_https_file_fields() -> None:
    doc = json.loads(SCRATCH_V2301_0_0B7_POST1)
    doc["artifacts"][0]["file"] = "scratch.tar.gz"

    with pytest.raises(ReleaseManifestError) as exc_info:
        parse_release_manifest(json.dumps(doc).encode())

    assert exc_info.value.reason == "release_manifest_schema_invalid"
