import json
import shutil
from importlib.resources import files
from pathlib import Path

import pytest

from mc_remote_stack.preset_registry import (
    PresetDataError,
    build_preset_catalog,
    component_set_sha256,
    evaluate_lifecycle,
    load_catalog_policy,
    load_preset,
    load_preset_catalog,
    load_profile,
    semantic_sha256,
    verify_append_only,
    verify_preset_catalog,
)

SCHEMA_SOURCE = Path(__file__).parents[1] / "src" / "mc_remote_stack" / "data" / "schemas"
OCI_DIGEST = f"sha256:{11:064x}"


def _data_root(tmp_path: Path, name: str = "data") -> Path:
    root = tmp_path / name
    root.mkdir()
    shutil.copytree(SCHEMA_SOURCE, root / "schemas")
    return root


def _profile_source(*, name: str = "home-server", revision: str = "1") -> str:
    return f"""schema_version = 1

[profile]
name = "{name}"
revision = "{revision}"
description = "Home server topology fixture"

[capabilities]
provided = ["compose", "paper", "persistent-world"]
required_component_roles = ["minecraft", "mcremote-plugin"]

[environment]
allowed_channels = ["beta", "alpha"]
allowed_exposures = ["isolated", "lan-only"]
allowed_purposes = ["integration"]

[policy]
required_security_controls = ["online-mode"]
instance_fields = [
  "runtime.artifact_store",
  "runtime.volumes",
  "world.identity",
  "network.bind_address",
  "agreements.minecraft_eula",
]
override_allowlist = ["capacity.memory"]

[renderer]
name = "compose"
revision = "1"

[[services]]
id = "minecraft"
role = "minecraft"

[[volume_roles]]
id = "minecraft-data"
kind = "world"
"""


def _preset_source(
    *,
    name: str = "classroom-paper",
    revision: str = "3",
    minecraft_artifact: str = "minecraft-image",
) -> str:
    return f"""schema_version = 1

[preset]
name = "{name}"
revision = "{revision}"
description = "Deterministic test fixture"

[requirements]
profile_capabilities = ["compose", "paper", "persistent-world"]
allowed_channels = ["beta"]
required_claims = ["profile-render"]

[[components]]
id = "minecraft-server"
role = "minecraft"
artifact = "{minecraft_artifact}"

[[components]]
id = "mcremote-paper"
role = "mcremote-plugin"
artifact = "mcremote-jar"
protocol = "21.0.0"

[[artifacts]]
id = "minecraft-image"
kind = "oci"
version = "1.21.8"
locator = "registry.example/minecraft"
digest = "{OCI_DIGEST}"

[[artifacts]]
id = "mcremote-jar"
kind = "https-file"
version = "2100.0.0b2"
filename = "mc-remote-example.jar"
sha256 = "{22:064x}"
origin = "https://example.invalid/mc-remote-example.jar"
"""


def _write_profile(
    root: Path,
    *,
    path_name: str = "home-server",
    path_revision: str = "1",
    record_name: str | None = None,
    record_revision: str | None = None,
) -> Path:
    path = root / "profiles" / path_name / path_revision / "profile.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _profile_source(
            name=record_name or path_name,
            revision=record_revision or path_revision,
        ),
        encoding="utf-8",
    )
    return path


def _write_preset(
    root: Path,
    *,
    path_name: str = "classroom-paper",
    path_revision: str = "3",
    record_name: str | None = None,
    record_revision: str | None = None,
    minecraft_artifact: str = "minecraft-image",
) -> Path:
    path = root / "preset_registry" / path_name / path_revision / "preset.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _preset_source(
            name=record_name or path_name,
            revision=record_revision or path_revision,
            minecraft_artifact=minecraft_artifact,
        ),
        encoding="utf-8",
    )
    return path


def test_new_preset_does_not_need_retired_compatibility_claims(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    path = _write_preset(root)
    path.write_text(path.read_text().replace('required_claims = ["profile-render"]\n', ''))

    preset = load_preset("classroom-paper@3", data_root=root)

    assert "required_claims" not in preset.data["requirements"]


def _write_policy(root: Path, entries: list[dict[str, str]]) -> Path:
    lines = ["schema_version = 1", ""]
    for entry in entries:
        lines.append("[[presets]]")
        for key, value in entry.items():
            lines.append(f'{key} = "{value}"')
        lines.append("")
    path = root / "preset_catalog_policy.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_compatibility_record(
    root: Path,
    *,
    record_id: str,
    preset_sha256: str,
    profile_sha256: str,
    component_set_digest: str | None = None,
    body_id: str | None = None,
    result: str = "pass",
) -> Path:
    path = root / "compatibility" / "records" / f"{record_id}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    component_set_value = component_set_digest or f"{33:064x}"
    path.write_text(
        f"""schema_version = 1

[record]
id = "{body_id or record_id}"
test_class = "unit/deterministic"
result = "{result}"
verified_at = "2026-07-23T00:00:00Z"

[subject]
preset_ref = "classroom-paper@3"
preset_sha256 = "{preset_sha256}"
profile_ref = "home-server@1"
profile_sha256 = "{profile_sha256}"
component_set_sha256 = "{component_set_value}"

[[claims]]
id = "profile-render"
constraint = "all"

[[evidence]]
repository = "Naohiro2g/mc-remote-stack"
commit = "{44:040x}"
path = "tests/test_preset_registry.py"
""",
        encoding="utf-8",
    )
    return path


def test_exact_profile_and_preset_refs_load_with_semantic_digest(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    _write_profile(root)
    _write_preset(root)

    profile = load_profile("home-server@1", data_root=root)
    preset = load_preset("classroom-paper@3", data_root=root)

    assert profile.ref == "home-server@1"
    assert profile.data["profile"]["name"] == "home-server"
    assert len(profile.content_sha256) == 64
    assert preset.ref == "classroom-paper@3"
    assert preset.data["components"][0]["artifact"] == "minecraft-image"
    assert len(preset.content_sha256) == 64


def test_semantic_digest_ignores_toml_key_order_quotes_and_comments() -> None:
    import tomllib

    first = tomllib.loads('schema_version = 1\n[preset]\nname = "fixture"\nrevision = "1"\n')
    second = tomllib.loads("# comment\nschema_version=1\n[preset]\nrevision='1'\nname='fixture'\n")

    assert semantic_sha256(first) == semantic_sha256(second)


@pytest.mark.parametrize(
    "value",
    [
        {"float": 1.5},
        {"large_integer": 2**53},
    ],
)
def test_semantic_digest_rejects_non_interoperable_values(value: object) -> None:
    with pytest.raises(PresetDataError) as exc_info:
        semantic_sha256(value)

    assert exc_info.value.reason == "canonicalization_value_invalid"


@pytest.mark.parametrize(
    "selector",
    [
        "classroom-paper",
        "classroom-paper@latest",
        "classroom-paper@main",
        "classroom-paper@0",
        "classroom-paper@01",
        "classroom-paper@^3",
        "classroom-paper@3..4",
    ],
)
def test_moving_or_noncanonical_preset_selector_is_rejected(tmp_path: Path, selector: str) -> None:
    root = _data_root(tmp_path)

    with pytest.raises(PresetDataError) as exc_info:
        load_preset(selector, data_root=root)

    assert exc_info.value.reason == "mutable_selector"


@pytest.mark.parametrize(
    ("loader", "writer", "ref"),
    [
        (load_profile, _write_profile, "home-server@1"),
        (load_preset, _write_preset, "classroom-paper@3"),
    ],
)
def test_record_identity_must_match_registry_path(
    tmp_path: Path,
    loader,
    writer,
    ref: str,
) -> None:
    root = _data_root(tmp_path)
    writer(root, record_revision="9")

    with pytest.raises(PresetDataError) as exc_info:
        loader(ref, data_root=root)

    assert exc_info.value.reason == "registry_record_tampered"


def test_unknown_schema_key_is_rejected(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    path = _write_preset(root)
    path.write_text(path.read_text(encoding="utf-8") + '\nlatest = "forbidden"\n', encoding="utf-8")

    with pytest.raises(PresetDataError) as exc_info:
        load_preset("classroom-paper@3", data_root=root)

    assert exc_info.value.reason == "registry_schema_invalid"


def test_oci_artifact_requires_manifest_digest(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    path = _write_preset(root)
    source = path.read_text(encoding="utf-8").replace(f'digest = "{OCI_DIGEST}"\n', "")
    path.write_text(source, encoding="utf-8")

    with pytest.raises(PresetDataError) as exc_info:
        load_preset("classroom-paper@3", data_root=root)

    assert exc_info.value.reason == "registry_schema_invalid"


def test_component_must_reference_a_declared_artifact(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    _write_preset(root, minecraft_artifact="moving-latest")

    with pytest.raises(PresetDataError) as exc_info:
        load_preset("classroom-paper@3", data_root=root)

    assert exc_info.value.reason == "component_artifact_unknown"


def test_generated_preset_catalog_is_byte_stable_and_qualified(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    _write_preset(root)
    _write_policy(
        root,
        [{"ref": "classroom-paper@3", "status": "active", "available_since": "2026-07-23"}],
    )

    first = build_preset_catalog(data_root=root)
    second = build_preset_catalog(data_root=root)
    (root / "preset_catalog.toml").write_bytes(first)
    loaded = load_preset_catalog(data_root=root)

    assert first == second
    assert loaded["preset_catalog"]["presets"][0]["ref"] == "classroom-paper@3"
    assert b"[preset_catalog]" in first
    assert b"[[preset_catalog.presets]]" in first
    assert b'preset_registry = ' not in first
    assert b"\n[catalog]" not in first
    assert b"\n[registry]" not in first


def test_generated_preset_catalog_projects_exact_compatibility_coverage(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    _write_profile(root)
    _write_preset(root)
    _write_policy(
        root,
        [{"ref": "classroom-paper@3", "status": "active", "available_since": "2026-07-23"}],
    )
    profile = load_profile("home-server@1", data_root=root)
    preset = load_preset("classroom-paper@3", data_root=root)
    without_evidence = build_preset_catalog(data_root=root)
    _write_compatibility_record(
        root,
        record_id="home-server-classroom-paper-3",
        preset_sha256=preset.content_sha256,
        profile_sha256=profile.content_sha256,
        component_set_digest=component_set_sha256(preset.data),
    )

    with_evidence = build_preset_catalog(data_root=root)
    (root / "preset_catalog.toml").write_bytes(with_evidence)
    entry = load_preset_catalog(data_root=root)["preset_catalog"]["presets"][0]

    assert with_evidence == without_evidence
    assert "compatibility_status" not in entry
    assert "compatibility_records" not in entry


def test_catalog_policy_rejects_duplicate_ref(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    entry = {"ref": "fixture@1", "status": "active", "available_since": "2026-07-01"}
    _write_policy(root, [entry, entry])

    with pytest.raises(PresetDataError) as exc_info:
        load_catalog_policy(data_root=root)

    assert exc_info.value.reason == "preset_catalog_policy_invalid"


def test_catalog_generation_rejects_unknown_replacement(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    _write_preset(root)
    _write_policy(
        root,
        [
            {
                "ref": "classroom-paper@3",
                "status": "deprecated",
                "available_since": "2026-07-01",
                "deprecated_since": "2026-07-23",
                "reason": "superseded",
                "replacement": "classroom-paper@4",
            }
        ],
    )

    with pytest.raises(PresetDataError) as exc_info:
        build_preset_catalog(data_root=root)

    assert exc_info.value.reason == "unknown_preset_revision"


def test_catalog_policy_distinguishes_active_deprecated_and_eol(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    _write_policy(
        root,
        [
            {"ref": "fixture@1", "status": "active", "available_since": "2026-07-01"},
            {
                "ref": "fixture@2",
                "status": "deprecated",
                "available_since": "2026-07-02",
                "deprecated_since": "2026-07-20",
                "reason": "superseded",
                "replacement": "fixture@3",
            },
            {
                "ref": "fixture@3",
                "status": "eol",
                "available_since": "2026-07-03",
                "deprecated_since": "2026-07-20",
                "eol_since": "2026-07-23",
                "reason": "unsupported",
            },
        ],
    )
    policy = load_catalog_policy(data_root=root)

    active = evaluate_lifecycle(policy, "fixture@1")
    deprecated = evaluate_lifecycle(policy, "fixture@2")
    eol = evaluate_lifecycle(policy, "fixture@3")

    assert (active.status, active.new_resolve_allowed, active.requires_eol_ack) == ("active", True, False)
    assert (deprecated.status, deprecated.new_resolve_allowed, deprecated.warning) == (
        "deprecated",
        True,
        "superseded",
    )
    assert (eol.status, eol.new_resolve_allowed, eol.requires_eol_ack) == ("eol", False, True)


def test_stale_generated_preset_catalog_is_rejected(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    _write_preset(root)
    _write_policy(
        root,
        [{"ref": "classroom-paper@3", "status": "active", "available_since": "2026-07-23"}],
    )
    (root / "preset_catalog.toml").write_text("schema_version = 1\n", encoding="utf-8")

    with pytest.raises(PresetDataError) as exc_info:
        verify_preset_catalog(data_root=root)

    assert exc_info.value.reason == "stale_preset_catalog"


@pytest.mark.parametrize("mutation", ["edit", "delete"])
def test_published_revision_is_append_only(tmp_path: Path, mutation: str) -> None:
    baseline = _data_root(tmp_path, "baseline")
    current = _data_root(tmp_path, "current")
    _write_profile(baseline)
    _write_preset(baseline)
    _write_profile(current)
    current_preset = _write_preset(current)

    if mutation == "edit":
        current_preset.write_text(
            current_preset.read_text(encoding="utf-8").replace(
                'description = "Deterministic test fixture"',
                'description = "Mutated after publication"',
            ),
            encoding="utf-8",
        )
    else:
        current_preset.unlink()

    with pytest.raises(PresetDataError) as exc_info:
        verify_append_only(current_root=current, baseline_root=baseline)

    expected_reason = {
        "edit": "immutable_revision_edited",
        "delete": "immutable_revision_deleted",
    }
    assert exc_info.value.reason == expected_reason[mutation]


def test_bundled_registry_offers_only_current_profiles_and_presets() -> None:
    catalog = load_preset_catalog()
    public = load_profile("vps-server@12")
    home = load_profile("home-server@6")

    assert sorted(entry["ref"] for entry in catalog["preset_catalog"]["presets"]) == [
        "classroom@1",
        "home-alpha-full@1",
        "home-alpha-full@2",
        "home-alpha-full@3",
        "home-alpha-full@4",
        "public-web-paper@11",
    ]
    assert public.data["renderer"] == {"name": "compose", "revision": "13"}
    assert home.data["renderer"] == {"name": "compose", "revision": "14"}
    assert "canonical-runtime-composition" in public.data["capabilities"]["provided"]
    for profile in (public, home):
        assert "mcremote-auth-enforced" in profile.data["capabilities"]["provided"]
        assert "mcremote-auth-enforced" in profile.data["policy"][
            "required_security_controls"
        ]


def test_public_web_paper_11_pins_manifest_published_post2_artifacts() -> None:
    preset = load_preset("public-web-paper@11")

    assert "presentation" not in preset.data
    artifacts = {item["id"]: item for item in preset.data["artifacts"]}
    assert artifacts["scratch-image"]["digest"] == (
        "sha256:738dae72706df0e6d781c7413a50054586ec824db0a7c73b938c47d990d2fc56"
    )
    assert artifacts["bridge-image"]["digest"] == (
        "sha256:099d24d5887d92729eec4e47e2dd971100ff5a7769d776fb0914b2a365dfc88e"
    )
    assert artifacts["wirescope-zip"] == {
        "id": "wirescope-zip",
        "kind": "https-file",
        "version": "2301.0.0b7.post2",
        "filename": "wirescope-app.zip",
        "sha256": "98d684dc15f369f6568d249357d8fd3af11893859d3c07c2554295df19a263b8",
        "origin": (
            "https://github.com/Naohiro2g/scratch-editor/releases/download/"
            "v2301.0.0b7.post2/wirescope-app.zip"
        ),
    }
    assert artifacts["wirescope-manifest"]["sha256"] == (
        "8aec62fe73bceaa01ff097567a3b308012b3df2ef31d010478d4862fd34d7198"
    )
    assert artifacts["mcremote-jar"]["sha256"] == (
        "e3c20fad663cd8854e9e89dfccd4b4b80937f7120d7260f686e1902dc6d990b9"
    )

    contract = preset.data["scratch_runtime_contract"]
    assert contract["source_commit"] == "f133fc95ed7b23109cc1908dc4f0dae066510258"
    assert contract["directory_tree_sha"] == (
        "ecb669a02ac6c8e502b44850e6dd28260c5adad4"
    )
    assert contract["image_digest"] == artifacts["scratch-image"]["digest"]

    policy = load_catalog_policy()
    policy_entry = next(
        item for item in policy["presets"] if item["ref"] == "public-web-paper@11"
    )
    assert policy_entry == {
        "ref": "public-web-paper@11",
        "status": "active",
        "available_since": "2026-09-07",
    }

    verify_preset_catalog()


def test_lock_schema_internal_references_are_defined() -> None:
    schema = json.loads(
        files("mc_remote_stack")
        .joinpath("data", "schemas", "lock.schema.json")
        .read_text(encoding="utf-8")
    )
    references: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                references.append(reference.removeprefix("#/$defs/"))
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(schema)

    assert set(references) <= set(schema["$defs"])
