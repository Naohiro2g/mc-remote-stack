import shutil
from pathlib import Path

import pytest

from mc_remote_stack.preset_registry import (
    PresetDataError,
    build_preset_catalog,
    component_set_sha256,
    evaluate_lifecycle,
    load_catalog_policy,
    load_compatibility_record,
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

    assert with_evidence != without_evidence
    assert entry["compatibility_status"] == "verified"
    assert entry["compatibility_records"] == ["home-server-classroom-paper-3"]


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


def test_exact_compatibility_record_loads_with_evidence_identity(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    _write_profile(root)
    _write_preset(root)
    profile = load_profile("home-server@1", data_root=root)
    preset = load_preset("classroom-paper@3", data_root=root)
    _write_compatibility_record(
        root,
        record_id="home-server-classroom-paper-3",
        preset_sha256=preset.content_sha256,
        profile_sha256=profile.content_sha256,
        component_set_digest=component_set_sha256(preset.data),
    )

    record = load_compatibility_record("home-server-classroom-paper-3", data_root=root)

    assert record.ref == "home-server-classroom-paper-3"
    assert record.data["record"]["result"] == "pass"
    assert record.data["evidence"][0]["commit"] == f"{44:040x}"
    assert len(record.content_sha256) == 64


def test_compatibility_record_component_set_must_match_exact_preset(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    _write_profile(root)
    _write_preset(root)
    profile = load_profile("home-server@1", data_root=root)
    preset = load_preset("classroom-paper@3", data_root=root)
    _write_compatibility_record(
        root,
        record_id="home-server-classroom-paper-3",
        preset_sha256=preset.content_sha256,
        profile_sha256=profile.content_sha256,
        component_set_digest=f"{99:064x}",
    )

    with pytest.raises(PresetDataError) as exc_info:
        load_compatibility_record("home-server-classroom-paper-3", data_root=root)

    assert exc_info.value.reason == "compatibility_subject_mismatch"


@pytest.mark.parametrize(
    ("body_id", "result", "reason"),
    [
        ("different-record-id", "pass", "registry_record_tampered"),
        (None, "fail", "registry_schema_invalid"),
    ],
)
def test_compatibility_record_fails_closed(
    tmp_path: Path,
    body_id: str | None,
    result: str,
    reason: str,
) -> None:
    root = _data_root(tmp_path)
    _write_compatibility_record(
        root,
        record_id="home-server-classroom-paper-3",
        body_id=body_id,
        result=result,
        preset_sha256=f"{55:064x}",
        profile_sha256=f"{66:064x}",
    )

    with pytest.raises(PresetDataError) as exc_info:
        load_compatibility_record("home-server-classroom-paper-3", data_root=root)

    assert exc_info.value.reason == reason


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


def test_bundled_home_profile_and_preset_are_exact_and_catalogued() -> None:
    policy = load_catalog_policy()
    catalog = load_preset_catalog()
    original_profile = load_profile("home-server@1")
    profile = load_profile("home-server@2")
    preset = load_preset("mcremote-paper@1")
    compatibility = load_compatibility_record(
        "home-server-2-mcremote-paper-1-live-auto"
    )

    assert "operator_input_roles" not in original_profile.data
    assert profile.data["capabilities"]["required_component_roles"] == [
        "minecraft-runtime",
        "paper-server",
        "mcremote-plugin",
    ]
    assert profile.data["environment"] == {
        "allowed_channels": ["beta", "alpha"],
        "allowed_exposures": ["isolated", "lan-only"],
        "allowed_purposes": ["integration"],
    }
    assert profile.data["operator_input_roles"] == [
        {
            "id": "minecraft-motd",
            "adapter": "minecraft-motd@1",
            "required": False,
        }
    ]
    assert [component["id"] for component in preset.data["components"]] == [
        "minecraft-runtime",
        "paper-server",
        "mcremote-paper",
    ]
    assert preset.data["components"][1]["minecraft_version"] == "1.21.11"
    assert preset.data["components"][2]["protocol"] == "21.0.0"
    assert preset.data["artifacts"] == [
        {
            "id": "minecraft-image",
            "kind": "oci",
            "version": "2026.7.2-java21",
            "locator": "docker.io/itzg/minecraft-server",
            "digest": "sha256:7f69fd6688e03495c8a8f5a46e8a8e82001b4465f4b55bdcd024c02c3624d8c8",
        },
        {
            "id": "paper-jar",
            "kind": "https-file",
            "version": "1.21.11-132",
            "filename": "paper-1.21.11-132.jar",
            "sha256": "5ffef465eeeb5f2a3c23a24419d97c51afd7dbb4923ff42df9a3f58bba1ccfba",
            "origin": (
                "https://fill-data.papermc.io/v1/objects/"
                "5ffef465eeeb5f2a3c23a24419d97c51afd7dbb4923ff42df9a3f58bba1ccfba/"
                "paper-1.21.11-132.jar"
            ),
        },
        {
            "id": "mcremote-jar",
            "kind": "https-file",
            "version": "2100.0.0b2",
            "filename": "mc-remote-1.21.11-2100.0.0b2.jar",
            "sha256": "ad2674fa93645cc3c4c0d2b6aa5b37f11a8f9519162f61ac00b8be7122b023c7",
            "origin": (
                "https://github.com/Naohiro2g/McRemote/releases/download/"
                "v1.21.11-2100.0.0b2/mc-remote-1.21.11-2100.0.0b2.jar"
            ),
        },
    ]
    assert policy["presets"][0] == {
        "ref": "mcremote-paper@1",
        "status": "active",
        "available_since": "2026-07-24",
    }
    assert catalog["preset_catalog"]["presets"][0]["ref"] == "mcremote-paper@1"
    assert catalog["preset_catalog"]["presets"][0]["compatibility_status"] == "verified"
    assert catalog["preset_catalog"]["presets"][0]["compatibility_records"] == [
        "home-server-2-mcremote-paper-1-live-auto"
    ]
    assert compatibility.data["record"]["test_class"] == "live-auto"
    assert compatibility.data["subject"] == {
        "preset_ref": "mcremote-paper@1",
        "preset_sha256": preset.content_sha256,
        "profile_ref": "home-server@2",
        "profile_sha256": profile.content_sha256,
        "component_set_sha256": component_set_sha256(preset.data),
    }
    assert compatibility.data["claims"] == [
        {"id": "profile-render", "constraint": "all"},
        {"id": "protocol-hello", "constraint": "all"},
    ]
    verify_preset_catalog()


def test_bundled_credential_profile_declares_separate_backend_roles() -> None:
    profile = load_profile("home-server@3")

    assert profile.data["renderer"] == {"name": "compose", "revision": "5"}
    assert profile.data["volume_roles"] == [
        {"id": "minecraft-data", "kind": "world"},
        {"id": "credential-store", "kind": "runtime-data"},
        {"id": "credential-revocations", "kind": "security-state"},
    ]
    assert "credential-rollback-separated" in profile.data["capabilities"][
        "provided"
    ]
    assert "credential-authority-write-set-separated" in profile.data["policy"][
        "required_security_controls"
    ]


def test_bundled_current_profiles_require_mcremote_auth_enforcement() -> None:
    home = load_profile("home-server@4")
    public = load_profile("vps-server@5")

    assert home.data["renderer"] == {"name": "compose", "revision": "6"}
    assert public.data["renderer"] == {"name": "compose", "revision": "7"}
    for profile in (home, public):
        assert "mcremote-auth-enforced" in profile.data["capabilities"]["provided"]
        assert "mcremote-auth-enforced" in profile.data["policy"][
            "required_security_controls"
        ]


def test_bundled_alpha_preset_is_immutable_unverified_and_catalogued() -> None:
    policy = load_catalog_policy()
    catalog = load_preset_catalog()
    beta = load_preset("mcremote-paper@1")
    alpha = load_preset("mcremote-paper@2")

    assert policy["presets"][1] == {
        "ref": "mcremote-paper@2",
        "status": "active",
        "available_since": "2026-07-25",
    }
    assert alpha.data["requirements"] == {
        "profile_capabilities": ["compose", "paper", "persistent-world"],
        "allowed_channels": ["alpha"],
        "required_claims": ["profile-render", "protocol-hello"],
    }
    assert alpha.data["components"] == beta.data["components"]
    assert alpha.data["artifacts"] == beta.data["artifacts"]
    assert catalog["preset_catalog"]["presets"][1]["ref"] == "mcremote-paper@2"
    assert catalog["preset_catalog"]["presets"][1]["compatibility_status"] == "unverified"
    assert catalog["preset_catalog"]["presets"][1]["compatibility_records"] == []
    verify_preset_catalog()


def test_bundled_b3_preset_is_exact_unverified_and_credential_profile_only() -> None:
    policy = load_catalog_policy()
    catalog = load_preset_catalog()
    b2 = load_preset("mcremote-paper@2")
    b3 = load_preset("mcremote-paper@3")

    assert b3.data["requirements"] == {
        "profile_capabilities": [
            "compose",
            "paper",
            "persistent-world",
            "credential-rollback-separated",
        ],
        "allowed_channels": ["alpha"],
        "required_claims": ["profile-render", "protocol-hello"],
    }
    assert b3.data["components"] == b2.data["components"]
    assert b3.data["artifacts"][:2] == b2.data["artifacts"][:2]
    assert b3.data["artifacts"][2] == {
        "id": "mcremote-jar",
        "kind": "https-file",
        "version": "2100.0.0b3",
        "filename": "mc-remote-1.21.11-2100.0.0b3.jar",
        "sha256": "aeb190705bd9957ce73557dc1be0fe15efe7250ba9bc688945e6f537e00ef78e",
        "origin": (
            "https://github.com/Naohiro2g/McRemote/releases/download/"
            "v1.21.11-2100.0.0b3/mc-remote-1.21.11-2100.0.0b3.jar"
        ),
    }
    assert next(
        entry for entry in policy["presets"] if entry["ref"] == "mcremote-paper@3"
    ) == {
        "ref": "mcremote-paper@3",
        "status": "active",
        "available_since": "2026-08-07",
    }
    catalog_entry = next(
        entry
        for entry in catalog["preset_catalog"]["presets"]
        if entry["ref"] == "mcremote-paper@3"
    )
    assert catalog_entry["compatibility_status"] == "unverified"
    assert catalog_entry["compatibility_records"] == []
    verify_preset_catalog()


def test_bundled_b4_home_alpha_preset_is_exact_unverified_candidate() -> None:
    policy = load_catalog_policy()
    catalog = load_preset_catalog()
    b2 = load_preset("mcremote-paper@2")
    b4 = load_preset("mcremote-paper@4")

    assert b4.data["requirements"] == {
        "profile_capabilities": ["compose", "paper", "persistent-world"],
        "allowed_channels": ["alpha"],
        "required_claims": ["profile-render", "protocol-hello"],
    }
    assert b4.data["components"] == b2.data["components"]
    assert b4.data["artifacts"][:2] == b2.data["artifacts"][:2]
    assert b4.data["artifacts"][2] == {
        "id": "mcremote-jar",
        "kind": "https-file",
        "version": "2100.0.0b4",
        "filename": "mc-remote-1.21.11-2100.0.0b4.jar",
        "sha256": "ab3b87c38b6876ec4ba26112eff35d7cb016395a1dae1661578fd3690e1dbc46",
        "origin": (
            "https://github.com/Naohiro2g/McRemote/releases/download/"
            "v1.21.11-2100.0.0b4/mc-remote-1.21.11-2100.0.0b4.jar"
        ),
    }
    assert next(
        entry for entry in policy["presets"] if entry["ref"] == "mcremote-paper@4"
    ) == {
        "ref": "mcremote-paper@4",
        "status": "active",
        "available_since": "2026-08-17",
    }
    catalog_entry = next(
        entry
        for entry in catalog["preset_catalog"]["presets"]
        if entry["ref"] == "mcremote-paper@4"
    )
    assert catalog_entry["compatibility_status"] == "unverified"
    assert catalog_entry["compatibility_records"] == []
    verify_preset_catalog()


def test_bundled_b4_auth_close_fix_is_a_new_exact_unverified_candidate() -> None:
    policy = load_catalog_policy()
    catalog = load_preset_catalog()
    old_b4 = load_preset("mcremote-paper@4")
    fixed_b4 = load_preset("mcremote-paper@5")

    assert fixed_b4.data["requirements"] == old_b4.data["requirements"]
    assert fixed_b4.data["components"] == old_b4.data["components"]
    assert fixed_b4.data["artifacts"][:2] == old_b4.data["artifacts"][:2]
    assert old_b4.data["artifacts"][2]["sha256"] == (
        "ab3b87c38b6876ec4ba26112eff35d7cb016395a1dae1661578fd3690e1dbc46"
    )
    assert fixed_b4.data["artifacts"][2] == {
        "id": "mcremote-jar",
        "kind": "https-file",
        "version": "2100.0.0b4",
        "filename": "mc-remote-1.21.11-2100.0.0b4.jar",
        "sha256": "f902ed360ac1674143d8e79a49c8e109968f2c38dc36656c91a50dec89082aa8",
        "origin": (
            "https://github.com/Naohiro2g/McRemote/releases/download/"
            "v1.21.11-2100.0.0b4/mc-remote-1.21.11-2100.0.0b4.jar"
        ),
    }
    assert next(
        entry for entry in policy["presets"] if entry["ref"] == "mcremote-paper@5"
    ) == {
        "ref": "mcremote-paper@5",
        "status": "active",
        "available_since": "2026-08-17",
    }
    catalog_entry = next(
        entry
        for entry in catalog["preset_catalog"]["presets"]
        if entry["ref"] == "mcremote-paper@5"
    )
    assert catalog_entry["compatibility_status"] == "unverified"
    assert catalog_entry["compatibility_records"] == []
    verify_preset_catalog()


def test_bundled_b4_session_persistence_fix_requires_credential_profile() -> None:
    policy = load_catalog_policy()
    catalog = load_preset_catalog()
    old_b4 = load_preset("mcremote-paper@5")
    fixed_b4 = load_preset("mcremote-paper@6")

    assert fixed_b4.data["requirements"] == {
        "profile_capabilities": [
            "compose",
            "paper",
            "persistent-world",
            "credential-rollback-separated",
        ],
        "allowed_channels": ["alpha"],
        "required_claims": ["profile-render", "protocol-hello"],
    }
    assert fixed_b4.data["components"] == old_b4.data["components"]
    assert fixed_b4.data["artifacts"][:2] == old_b4.data["artifacts"][:2]
    assert fixed_b4.data["artifacts"][2] == {
        "id": "mcremote-jar",
        "kind": "https-file",
        "version": "2100.0.0b4",
        "filename": "mc-remote-1.21.11-2100.0.0b4.jar",
        "sha256": "331633ef15a729658496e89fe49cb8a5eb5ebcb2ec86937b7e5313528d7ec997",
        "origin": (
            "https://github.com/Naohiro2g/McRemote/releases/download/"
            "v1.21.11-2100.0.0b4/mc-remote-1.21.11-2100.0.0b4.jar"
        ),
    }
    assert next(
        entry for entry in policy["presets"] if entry["ref"] == "mcremote-paper@6"
    ) == {
        "ref": "mcremote-paper@6",
        "status": "active",
        "available_since": "2026-08-18",
    }
    catalog_entry = next(
        entry
        for entry in catalog["preset_catalog"]["presets"]
        if entry["ref"] == "mcremote-paper@6"
    )
    assert catalog_entry["compatibility_status"] == "verified"
    assert catalog_entry["compatibility_records"] == [
        "2026-08-18-b4-code-preservation-recovery-live-human",
        "2026-08-18-b4-session-persistence-home-alpha",
    ]
    verify_preset_catalog()


def test_bundled_home_auth_b3_preset_is_exact_jar_only_rollback_target() -> None:
    policy = load_catalog_policy()
    catalog = load_preset_catalog()
    b4 = load_preset("mcremote-paper@4")
    rollback = load_preset("mcremote-paper-auth-b3@1")

    assert rollback.data["requirements"] == b4.data["requirements"]
    assert rollback.data["components"] == b4.data["components"]
    assert rollback.data["artifacts"][:2] == b4.data["artifacts"][:2]
    assert rollback.data["artifacts"][2] == {
        "id": "mcremote-jar",
        "kind": "https-file",
        "version": "2100.0.0b3",
        "filename": "mc-remote-1.21.11-2100.0.0b3.jar",
        "sha256": "aeb190705bd9957ce73557dc1be0fe15efe7250ba9bc688945e6f537e00ef78e",
        "origin": (
            "https://github.com/Naohiro2g/McRemote/releases/download/"
            "v1.21.11-2100.0.0b3/mc-remote-1.21.11-2100.0.0b3.jar"
        ),
    }
    assert next(
        entry
        for entry in policy["presets"]
        if entry["ref"] == "mcremote-paper-auth-b3@1"
    ) == {
        "ref": "mcremote-paper-auth-b3@1",
        "status": "active",
        "available_since": "2026-08-17",
    }
    catalog_entry = next(
        entry
        for entry in catalog["preset_catalog"]["presets"]
        if entry["ref"] == "mcremote-paper-auth-b3@1"
    )
    assert catalog_entry["compatibility_status"] == "unverified"
    assert catalog_entry["compatibility_records"] == []
    verify_preset_catalog()


def test_bundled_public_b3_profile_and_preset_are_session_only() -> None:
    profile = load_profile("vps-server@6")
    preset = load_preset("public-web-paper@2")

    assert profile.data["renderer"] == {"name": "compose", "revision": "8"}
    assert "mcremote-session-only" in profile.data["capabilities"]["provided"]
    assert "credential-rollback-separated" not in profile.data["capabilities"][
        "provided"
    ]
    assert profile.data["volume_roles"] == [
        {"id": "minecraft-data", "kind": "world"},
        {"id": "caddy-data", "kind": "runtime-data"},
        {"id": "caddy-config", "kind": "runtime-data"},
    ]
    assert "mcremote-session-only" in preset.data["requirements"][
        "profile_capabilities"
    ]
    artifacts = {item["id"]: item for item in preset.data["artifacts"]}
    assert artifacts["scratch-image"]["version"] == (
        "sha-3f1a10a366bfbe76e32b5a31c54da19eddd56e56"
    )
    assert artifacts["bridge-image"]["version"] == (
        "sha-3f1a10a366bfbe76e32b5a31c54da19eddd56e56"
    )
    assert artifacts["mcremote-jar"] == {
        "id": "mcremote-jar",
        "kind": "https-file",
        "version": "2100.0.0b3",
        "filename": "mc-remote-1.21.11-2100.0.0b3.jar",
        "sha256": "aeb190705bd9957ce73557dc1be0fe15efe7250ba9bc688945e6f537e00ef78e",
        "origin": (
            "https://github.com/Naohiro2g/McRemote/releases/download/"
            "v1.21.11-2100.0.0b3/mc-remote-1.21.11-2100.0.0b3.jar"
        ),
    }
    catalog = load_preset_catalog()["preset_catalog"]["presets"]
    catalog_entry = next(
        item for item in catalog if item["ref"] == "public-web-paper@2"
    )
    assert catalog_entry["compatibility_status"] == "unverified"
    assert catalog_entry["compatibility_records"] == []
    verify_preset_catalog()


def test_bundled_public_b3_runtime_config_profile_is_fail_closed() -> None:
    profile = load_profile("vps-server@7")

    assert profile.data["renderer"] == {"name": "compose", "revision": "9"}
    roles = {item["id"]: item for item in profile.data["operator_input_roles"]}
    assert roles["connection-targets"] == {
        "id": "connection-targets",
        "adapter": "connection-targets@1",
        "required": True,
    }


def test_bundled_public_b4_profile_and_preset_are_exact_session_only() -> None:
    profile = load_profile("vps-server@8")
    preset = load_preset("public-web-paper@3")

    assert profile.data["renderer"] == {"name": "compose", "revision": "10"}
    assert "mcremote-session-only" in profile.data["capabilities"]["provided"]
    assert "credential-rollback-separated" not in profile.data["capabilities"][
        "provided"
    ]
    roles = {item["id"]: item for item in profile.data["operator_input_roles"]}
    assert roles["connection-targets"]["required"] is True

    artifacts = {item["id"]: item for item in preset.data["artifacts"]}
    assert artifacts["scratch-image"] == {
        "id": "scratch-image",
        "kind": "oci",
        "version": "sha-1d2f18785d260564ad4bc30a26a45ef33fc813d6",
        "locator": "ghcr.io/naohiro2g/mc-remote-scratch",
        "digest": "sha256:6425f9ac2549c26440fb418868f2e0fdcc7ad817c1a7ae684142d9e0d879f09f",
    }
    assert artifacts["bridge-image"] == {
        "id": "bridge-image",
        "kind": "oci",
        "version": "sha-1d2f18785d260564ad4bc30a26a45ef33fc813d6",
        "locator": "ghcr.io/naohiro2g/mc-remote-bridge",
        "digest": "sha256:4225408cf4e40eda8877b0e3cee08649dd53374144edb2910e9365c1544fa146",
    }
    assert artifacts["mcremote-jar"]["sha256"] == (
        "331633ef15a729658496e89fe49cb8a5eb5ebcb2ec86937b7e5313528d7ec997"
    )


def test_bundled_public_b4_wirescope_profile_pins_two_file_contract() -> None:
    profile = load_profile("vps-server@9")
    preset = load_preset("public-web-paper@4")

    assert profile.data["renderer"] == {"name": "compose", "revision": "11"}
    assert "wirescope-browser" in profile.data["capabilities"]["provided"]
    assert {
        item["id"]: item for item in profile.data["operator_input_roles"]
    }["public-routes"] == {
        "id": "public-routes",
        "adapter": "public-routes@2",
        "required": True,
    }
    artifacts = {item["id"]: item for item in preset.data["artifacts"]}
    assert artifacts["wirescope-zip"]["sha256"] == (
        "1a56617c78c283332f1afe3bdd3797ab37f0cdc3455c86c73c926c751721657f"
    )
    assert artifacts["wirescope-manifest"]["sha256"] == (
        "f3ec11496b595bbca4ba27a6e938a1149336eb5a2da55e742d60e1681cf4d154"
    )
    assert artifacts["wirescope-zip"]["origin"].endswith(
        "/wirescope-app-v2100.0.0b4/wirescope-app.zip"
    )
    assert artifacts["wirescope-manifest"]["origin"].endswith(
        "/wirescope-app-v2100.0.0b4/wirescope-app.manifest.json"
    )
    verify_preset_catalog()
