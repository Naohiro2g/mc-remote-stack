import json
from copy import deepcopy

import pytest

from mc_remote_stack.release_manifest import (
    ReleaseManifestError,
    collect_release_artifacts,
    parse_release_manifest,
    select_release_artifact,
    verify_preset_minecraft,
)

from .release_manifest_v2_fixture import contract_root, digest, minecraft_v2, preset, scratch_v2
from .test_release_manifest import SCRATCH_V2301_0_0B7_POST1


@pytest.fixture
def root(tmp_path):
    return contract_root(tmp_path)


def parse(document, root):
    return parse_release_manifest(json.dumps(document).encode(), data_root=root)


def reason(expected, call):
    with pytest.raises(ReleaseManifestError) as info:
        call()
    assert info.value.reason == expected
    return info.value


def test_accepts_three_variants_and_preserves_optional_fields(root):
    document = scratch_v2()
    document["bundled_wirescope_source_commit"] = "a" * 40
    document["artifacts"][0]["artifact_version"] = "2320.0.0b10"
    assert parse(document, root) == document


@pytest.mark.parametrize(
    "field,value",
    [
        ("os", None),
        ("os", ""),
        ("os", "Windows"),
        ("os", "freebsd"),
        ("arch", None),
        ("arch", ""),
        ("arch", "amd64"),
        ("bytes", None),
        ("bytes", True),
        ("bytes", "0"),
        ("bytes", -1),
        ("bytes", 0.5),
        ("extra", 1),
        ("sha256", "A" * 64),
    ],
)
def test_rejects_invalid_file_fields(root, field, value):
    document = scratch_v2()
    document["artifacts"][-1][field] = value
    reason("release_manifest_schema_invalid", lambda: parse(document, root))


@pytest.mark.parametrize("field", ["os", "arch", "bytes"])
def test_rejects_missing_pair_or_size(root, field):
    document = scratch_v2()
    document["artifacts"][-1].pop(field)
    reason("release_manifest_schema_invalid", lambda: parse(document, root))


@pytest.mark.parametrize("index", [0, -1])
def test_rejects_duplicate_keys_even_with_different_content(root, index):
    document = scratch_v2()
    duplicate = dict(document["artifacts"][index])
    duplicate.update(sha256="a" * 64) if duplicate["kind"] == "https-file" else duplicate.update(
        digest="sha256:" + "a" * 64
    )
    document["artifacts"].append(duplicate)
    reason("release_manifest_duplicate_key", lambda: parse(document, root))


def test_rejects_selector_mixing_in_unused_role(root):
    document = scratch_v2()
    artifact = dict(document["artifacts"][-1])
    artifact.pop("os")
    artifact.pop("arch")
    document["artifacts"].append(artifact)
    reason("release_manifest_mixed_selectors", lambda: parse(document, root))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("minecraft_compatibility"),
        lambda d: d["minecraft_compatibility"]["verifications"][0].update(result="FAIL"),
        lambda d: d["minecraft_compatibility"]["verifications"][0].update(paper_build=0),
        lambda d: d["minecraft_compatibility"]["declaration"].update(extra=1),
        lambda d: d["minecraft_compatibility"]["verifications"][0]["record"].update(file="../record.json"),
        lambda d: d["minecraft_compatibility"]["verifications"][0]["record"].update(file="https://other/record.json"),
        lambda d: d["minecraft_compatibility"]["declaration"].update(minecraft_versions=["26.2", "26.2"]),
    ],
)
def test_rejects_compatibility_shape_errors(root, mutate):
    document, _, _ = minecraft_v2()
    mutate(document)
    reason("release_manifest_schema_invalid", lambda: parse(document, root))


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda d: d["minecraft_compatibility"]["verifications"].pop(), "release_manifest_compatibility_set_mismatch"),
        (
            lambda d: d["minecraft_compatibility"]["verifications"][0].update(minecraft_version="26.3"),
            "release_manifest_compatibility_set_mismatch",
        ),
        (
            lambda d: d["minecraft_compatibility"]["verifications"].append(
                deepcopy(d["minecraft_compatibility"]["verifications"][0])
            ),
            "release_manifest_duplicate_verification",
        ),
        (
            lambda d: d["minecraft_compatibility"]["verifications"][0].update(jar_sha256="a" * 64),
            "release_manifest_jar_digest_mismatch",
        ),
        (
            lambda d: d["minecraft_compatibility"]["declaration"].update(path="../targets.json"),
            "release_manifest_schema_invalid",
        ),
        (
            lambda d: d["artifacts"].append(
                {"role": "record", "kind": "https-file", "file": "first.json", "sha256": "a" * 64, "bytes": 1}
            ),
            "release_manifest_record_artifact_forbidden",
        ),
    ],
)
def test_rejects_whole_document_semantic_errors(root, mutate, expected):
    document, _, _ = minecraft_v2()
    mutate(document)
    reason(expected, lambda: parse(document, root))


@pytest.mark.parametrize("version", [None, 0, 3, "2", True])
def test_rejects_unknown_version(root, version):
    document = scratch_v2()
    document["schema_version"] = version
    reason("release_manifest_schema_invalid", lambda: parse(document, root))


def test_v1_behavior_and_optional_fields_are_unchanged(root):
    document = json.loads(SCRATCH_V2301_0_0B7_POST1)
    document["bundled_wirescope_source_commit"] = "a" * 40
    document["artifacts"][0]["artifact_version"] = "old"
    assert parse(document, root) == document
    document["artifacts"][2]["bytes"] = 0
    reason("release_manifest_schema_invalid", lambda: parse(document, root))


def test_selects_explicit_variant_and_kind_without_filename_inference(root):
    document = parse(scratch_v2(), root)
    selected = select_release_artifact(document, "scratch-local", kind="https-file", os="macos", arch="arm64")
    assert selected["file"] == "macos-arm64.zip"
    assert select_release_artifact(document, "scratch", kind="oci")["kind"] == "oci"


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"kind": "https-file"}, "release_manifest_selector_required"),
        ({"kind": "https-file", "os": "linux"}, "release_manifest_selector_required"),
        ({"kind": "https-file", "os": "linux", "arch": "arm64"}, "release_manifest_role_missing"),
        ({"kind": "oci", "os": "linux", "arch": "x64"}, "release_manifest_kind_mismatch"),
    ],
)
def test_selection_fails_closed(root, kwargs, expected):
    document = parse(scratch_v2(), root)
    reason(expected, lambda: select_release_artifact(document, "scratch-local", **kwargs))


def test_even_one_variant_requires_selectors(root):
    document = scratch_v2()
    document["artifacts"] = document["artifacts"][-1:]
    reason(
        "release_manifest_selector_required",
        lambda: select_release_artifact(parse(document, root), "scratch-local", kind="https-file"),
    )


def test_collection_fetches_only_selected_files(root):
    document = scratch_v2()
    document["artifacts"][2].update(sha256=digest(b"wire"), bytes=4)
    calls = []

    def read_asset(name):
        calls.append(name)
        assert name == "wirescope-app.zip"
        return b"wire"

    result = collect_release_artifacts(
        parse(document, root),
        [("scratch", "oci", None, None), ("wirescope", "https-file", None, None)],
        read_asset=read_asset,
    )
    assert [a["role"] for a in result] == ["scratch", "wirescope"]
    assert calls == ["wirescope-app.zip"]


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (lambda d: d["artifacts"][0].update(bytes=4), "release_manifest_artifact_bytes_mismatch"),
        (lambda d: d["artifacts"][0].update(sha256="b" * 64), "release_manifest_jar_digest_mismatch"),
    ],
)
def test_collection_rejects_selected_artifact_identity_mismatch(root, mutation, expected):
    document, declaration, assets = minecraft_v2()
    mutation(document)
    reason(
        expected,
        lambda: collect_release_artifacts(
            parse(document, root),
            [("jar", "https-file", None, None)],
            read_asset=assets.__getitem__,
            read_declaration=lambda path, commit: declaration,
        ),
    )


def test_compatibility_verifies_every_record_and_pinned_declaration(root):
    document, declaration, assets = minecraft_v2()
    calls = []

    def read_declaration(path, commit):
        calls.append((path, commit))
        return declaration

    def read_asset(name):
        calls.append(name)
        return assets[name]

    collect_release_artifacts(
        parse(document, root),
        [("jar", "https-file", None, None)],
        read_asset=read_asset,
        read_declaration=read_declaration,
    )
    assert set(c for c in calls if isinstance(c, str)) == set(assets)
    assert ("release/minecraft-targets.json", document["source_commit"]) in calls


@pytest.mark.parametrize("target", ["declaration", "first.json", "second.json"])
def test_collection_rejects_digest_mismatch_in_each_external_reference(root, target):
    document, declaration, assets = minecraft_v2()
    if target == "declaration":
        declaration += b" "
    else:
        assets[target] += b" "
    expected = (
        "release_manifest_declaration_digest_mismatch"
        if target == "declaration"
        else "release_manifest_record_digest_mismatch"
    )
    reason(
        expected,
        lambda: collect_release_artifacts(
            parse(document, root), [], read_asset=assets.__getitem__, read_declaration=lambda path, commit: declaration
        ),
    )


def test_declaration_contents_must_match_manifest(root):
    document, _, assets = minecraft_v2()
    declaration = json.dumps(
        {
            "schema": "mc-remote.minecraft-targets",
            "schema_version": 1,
            "minecraft_versions": ["26.3"],
        }
    ).encode()
    document["minecraft_compatibility"]["declaration"]["sha256"] = digest(declaration)
    reason(
        "release_manifest_declaration_versions_mismatch",
        lambda: collect_release_artifacts(
            parse(document, root), [], read_asset=assets.__getitem__, read_declaration=lambda path, commit: declaration
        ),
    )


def test_absent_declaration_does_not_claim_verified(root):
    document, _, assets = minecraft_v2()
    reason(
        "release_manifest_declaration_required",
        lambda: collect_release_artifacts(parse(document, root), [], read_asset=assets.__getitem__),
    )


def test_explicit_preset_minecraft_selects_matching_observation(root):
    document, _, _ = minecraft_v2()
    selected = verify_preset_minecraft(
        parse(document, root), preset(), paper_build=10, java_version="21.0.12.1+1-1-24.04.4-Ubuntu"
    )
    assert selected["minecraft_version"] == "26.2"
    assert selected["paper_build"] == 10


@pytest.mark.parametrize(
    "change", ["minecraft", "server", "plugin", "paper_build", "java_version", "missing_minecraft"]
)
def test_preset_mismatch_returns_coordinator_diagnostic(root, change):
    document, _, _ = minecraft_v2()
    selected_preset = preset()
    build, java = 10, "21.0.12.1+1-1-24.04.4-Ubuntu"
    if change == "minecraft":
        selected_preset["components"][0]["minecraft_version"] = "26.3"
    elif change == "missing_minecraft":
        selected_preset["components"][0].pop("minecraft_version")
    elif change == "server":
        selected_preset["artifacts"][0]["sha256"] = "b" * 64
    elif change == "plugin":
        selected_preset["artifacts"][1]["sha256"] = "b" * 64
    elif change == "paper_build":
        build = 11
    else:
        java = "21"
    error = reason(
        "release_manifest_foundation_unverified",
        lambda: verify_preset_minecraft(parse(document, root), selected_preset, paper_build=build, java_version=java),
    )
    assert "coordinator" in str(error)


@pytest.mark.parametrize("field", ["bytes", "sha256", "source_commit"])
def test_schema_lock_fails_closed_on_invalid_pin(root, field):
    path = root / "release-manifest-lock.json"
    lock = json.loads(path.read_bytes())
    if field == "source_commit":
        lock[field] = "main"
        expected = "release_manifest_schema_lock_invalid"
    else:
        lock["schema"][field] = 0 if field == "bytes" else "a" * 64
        expected = "release_manifest_schema_digest_mismatch"
    path.write_text(json.dumps(lock))
    reason(expected, lambda: parse(scratch_v2(), root))


def test_missing_v2_pin_does_not_use_v1_or_network(root):
    (root / "release-manifest-lock.json").unlink()
    reason("release_manifest_v2_schema_not_pinned", lambda: parse(scratch_v2(), root))
    assert parse_release_manifest(SCRATCH_V2301_0_0B7_POST1, data_root=root)["schema_version"] == 1


def test_legacy_role_helper_requires_expected_kind_for_v2(root):
    from mc_remote_stack.release_manifest import artifact_by_role

    document = parse(scratch_v2(), root)
    reason("release_manifest_kind_required", lambda: artifact_by_role(document, "scratch"))
    assert artifact_by_role(document, "scratch", kind="oci")["role"] == "scratch"


def test_collection_rejects_actual_jar_body_digest(root):
    document, declaration, assets = minecraft_v2()
    assets["neutral-name.jar"] = b"bad"
    reason(
        "release_manifest_artifact_digest_mismatch",
        lambda: collect_release_artifacts(
            parse(document, root),
            [("jar", "https-file", None, None)],
            read_asset=assets.__getitem__,
            read_declaration=lambda path, commit: declaration,
        ),
    )


def test_schema_disallows_remote_references_even_with_recomputed_pin(root):
    path = root / "schemas/release-manifest-v2.schema.json"
    schema = json.loads(path.read_bytes())
    schema["properties"]["artifacts"]["items"] = {"$ref": "https://example.test/remote.json"}
    source = json.dumps(schema).encode()
    path.write_bytes(source)
    lock_path = root / "release-manifest-lock.json"
    lock = json.loads(lock_path.read_bytes())
    lock["schema"].update(bytes=len(source), sha256=digest(source))
    lock_path.write_text(json.dumps(lock))
    reason("release_manifest_schema_invalid", lambda: parse(scratch_v2(), root))


@pytest.mark.parametrize("field", ["components", "artifacts"])
def test_malformed_preset_returns_structured_coordinator_diagnostic(root, field):
    document, _, _ = minecraft_v2()
    invalid = preset()
    invalid[field] = "invalid"
    reason(
        "release_manifest_foundation_unverified",
        lambda: verify_preset_minecraft(
            parse(document, root),
            invalid,
            paper_build=10,
            java_version="21.0.12.1+1-1-24.04.4-Ubuntu",
        ),
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: ["1.21.11", "26.2"],
        lambda d: {**d, "extra": 1},
        lambda d: {**d, "schema": "different"},
        lambda d: {**d, "schema_version": 2},
        lambda d: {**d, "schema_version": True},
        lambda d: {**d, "schema_version": "1"},
        lambda d: {**d, "minecraft_versions": []},
        lambda d: {**d, "minecraft_versions": ["26.2", "26.2"]},
        lambda d: {**d, "minecraft_versions": [""]},
        lambda d: {**d, "minecraft_versions": [None]},
        lambda d: {key: value for key, value in d.items() if key != "schema"},
    ],
)
def test_declaration_object_rejects_unknown_shape_and_fields(root, mutate):
    document, _, assets = minecraft_v2()
    declaration = json.dumps(
        mutate(
            {
                "schema": "mc-remote.minecraft-targets",
                "schema_version": 1,
                "minecraft_versions": ["1.21.11", "26.2"],
            }
        )
    ).encode()
    document["minecraft_compatibility"]["declaration"]["sha256"] = digest(declaration)
    reason(
        "release_manifest_declaration_invalid",
        lambda: collect_release_artifacts(
            parse(document, root),
            [],
            read_asset=assets.__getitem__,
            read_declaration=lambda path, commit: declaration,
        ),
    )


def test_accepts_declaration_object_matching_embedded_raw_bytes(root):
    document, _, assets = minecraft_v2()
    declaration = (
        b'{"schema":"mc-remote.minecraft-targets","schema_version":1,"minecraft_versions":["26.2","1.21.11"]}\n'
    )
    document["minecraft_compatibility"]["declaration"]["sha256"] = digest(declaration)
    collect_release_artifacts(
        parse(document, root),
        [],
        read_asset=assets.__getitem__,
        read_declaration=lambda path, commit: declaration,
        jar_declaration=declaration,
    )
