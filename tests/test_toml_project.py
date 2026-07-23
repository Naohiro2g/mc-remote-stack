import os
import tomllib
from pathlib import Path

import pytest

from mc_remote_stack.toml_project import (
    ProjectOrderError,
    init_toml_project,
    load_order,
    update_order_scalar,
)


def _valid_order() -> str:
    return """schema_version = 1

[deployment]
name = "home"
profile = "home-server@1"

[environment]
identity = "home-beta"
channel = "beta"
exposure = "isolated"
purpose = "integration"
preset = "classroom-paper@3"

[runtime]
artifact_store = "/var/lib/mc-remote/artifacts"

[[runtime.volumes]]
role = "minecraft-data"
identity = "home-beta-minecraft-data"

[world]
identity = "home-beta-world"

[network]
bind_address = "127.0.0.1"
java_port = 25565
mcremote_port = 25575

[agreements]
minecraft_eula = true

[acknowledgements]
allow_unverified = false
unverified_reason = ""
allow_eol = false
eol_reason = ""
"""


def _instance_kwargs(identity: str = "home-beta") -> dict[str, object]:
    return {
        "artifact_store": "/var/lib/mc-remote/artifacts",
        "runtime_volumes": {"minecraft-data": f"{identity}-minecraft-data"},
        "world_identity": f"{identity}-world",
        "bind_address": "127.0.0.1",
        "java_port": 25565,
        "mcremote_port": 25575,
    }


def _write_order(root: Path, content: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    order = root / "mc-remote.toml"
    order.write_text(content if content is not None else _valid_order(), encoding="utf-8")
    return order


def _with_operator_input(path: str) -> str:
    toml_path = path.replace("\\", "\\\\")
    return (
        _valid_order()
        + f"""
[[operator_inputs]]
role = "minecraft-motd"
adapter = "minecraft-motd@1"
path = "{toml_path}"
"""
    )


def test_init_creates_one_environment_order_without_placeholder_lock(tmp_path: Path) -> None:
    project = init_toml_project(
        tmp_path / "home-beta",
        deployment_name="home",
        profile="home-server@1",
        environment_identity="beta-classroom",
        channel="beta",
        exposure="isolated",
        purpose="integration",
        preset="classroom-paper@3",
        **_instance_kwargs("beta-classroom"),
    )

    assert project.order.exists()
    assert not project.lock.exists()
    assert project.readme.exists()
    assert project.gitignore.exists()
    assert "/generated/" in project.gitignore.read_text(encoding="utf-8")
    assert "mc-remote.lock.toml" not in project.gitignore.read_text(encoding="utf-8")

    order = tomllib.loads(project.order.read_text(encoding="utf-8"))
    assert order["deployment"] == {"name": "home", "profile": "home-server@1"}
    assert order["environment"] == {
        "identity": "beta-classroom",
        "channel": "beta",
        "exposure": "isolated",
        "purpose": "integration",
        "preset": "classroom-paper@3",
    }
    assert order["runtime"] == {
        "artifact_store": "/var/lib/mc-remote/artifacts",
        "volumes": [{"role": "minecraft-data", "identity": "beta-classroom-minecraft-data"}],
    }
    assert order["world"] == {"identity": "beta-classroom-world"}
    assert order["network"] == {
        "bind_address": "127.0.0.1",
        "java_port": 25565,
        "mcremote_port": 25575,
    }
    assert order["agreements"] == {"minecraft_eula": False}
    assert "environments" not in order


def test_init_refuses_non_empty_directory_without_changing_it(tmp_path: Path) -> None:
    root = tmp_path / "home-beta"
    root.mkdir()
    sentinel = root / "keep.txt"
    sentinel.write_text("operator data\n", encoding="utf-8")

    with pytest.raises(ValueError, match="non-empty"):
        init_toml_project(
            root,
            deployment_name="home",
            profile="home-server@1",
            environment_identity="home-beta",
            channel="beta",
            exposure="isolated",
            purpose="integration",
            preset="classroom-paper@3",
            **_instance_kwargs(),
        )

    assert sentinel.read_text(encoding="utf-8") == "operator data\n"
    assert not (root / "mc-remote.toml").exists()


@pytest.mark.parametrize(
    "path",
    [
        "../operator/minecraft-motd/server.properties",
        "/operator/minecraft-motd/server.properties",
        "operator/other-adapter/server.properties",
        "operator/minecraft-motd/../server.properties",
        "operator\\minecraft-motd\\server.properties",
    ],
)
def test_operator_input_path_must_match_exact_adapter_namespace(
    tmp_path: Path,
    path: str,
) -> None:
    root = tmp_path / "home-beta"
    _write_order(root, _with_operator_input(path))

    with pytest.raises(ProjectOrderError) as exc_info:
        load_order(root)

    assert exc_info.value.reason == "operator_input_path_invalid"


def test_referenced_operator_input_must_exist_as_a_regular_file(tmp_path: Path) -> None:
    root = tmp_path / "home-beta"
    _write_order(root, _with_operator_input("operator/minecraft-motd/server.properties"))

    with pytest.raises(ProjectOrderError) as exc_info:
        load_order(root)

    assert exc_info.value.reason == "operator_input_missing"


def test_unreferenced_operator_file_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "home-beta"
    _write_order(root)
    unreferenced = root / "operator" / "minecraft-motd" / "unreferenced.properties"
    unreferenced.parent.mkdir(parents=True)
    unreferenced.write_text("motd=unreferenced\n", encoding="utf-8")

    with pytest.raises(ProjectOrderError) as exc_info:
        load_order(root)

    assert exc_info.value.reason == "operator_input_unreferenced"


def test_operator_input_symlink_is_rejected_without_reading_target(tmp_path: Path) -> None:
    root = tmp_path / "home-beta"
    _write_order(root, _with_operator_input("operator/minecraft-motd/server.properties"))
    outside = tmp_path / "outside.properties"
    outside.write_text("motd=outside\n", encoding="utf-8")
    source = root / "operator" / "minecraft-motd" / "server.properties"
    source.parent.mkdir(parents=True)
    source.symlink_to(outside)

    with pytest.raises(ProjectOrderError) as exc_info:
        load_order(root)

    assert exc_info.value.reason == "operator_input_symlink_forbidden"


@pytest.mark.parametrize(
    "block",
    [
        """[runtime]
artifact_store = "/var/lib/mc-remote/artifacts"

[[runtime.volumes]]
role = "minecraft-data"
identity = "home-beta-minecraft-data"

""",
        """[world]
identity = "home-beta-world"

""",
        """[network]
bind_address = "127.0.0.1"
java_port = 25565
mcremote_port = 25575

""",
        """[agreements]
minecraft_eula = true

""",
    ],
)
def test_instance_contract_tables_are_required(tmp_path: Path, block: str) -> None:
    root = tmp_path / "home-beta"
    _write_order(root, _valid_order().replace(block, ""))

    with pytest.raises(ProjectOrderError) as exc_info:
        load_order(root)

    assert exc_info.value.reason == "order_schema_invalid"


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        (
            'artifact_store = "/var/lib/mc-remote/artifacts"',
            'artifact_store = "../artifacts"',
            "order_schema_invalid",
        ),
        (
            'bind_address = "127.0.0.1"',
            'bind_address = "192.168.1.10"',
            "unsupported_environment_combination",
        ),
        (
            "mcremote_port = 25575",
            "mcremote_port = 25565",
            "order_schema_invalid",
        ),
    ],
)
def test_invalid_instance_contract_fails_closed(
    tmp_path: Path,
    old: str,
    new: str,
    reason: str,
) -> None:
    root = tmp_path / "home-beta"
    _write_order(root, _valid_order().replace(old, new))

    with pytest.raises(ProjectOrderError) as exc_info:
        load_order(root)

    assert exc_info.value.reason == reason


def test_lan_only_requires_private_non_loopback_binding(tmp_path: Path) -> None:
    root = tmp_path / "home-beta"
    valid = _valid_order().replace('exposure = "isolated"', 'exposure = "lan-only"')

    _write_order(root, valid)
    with pytest.raises(ProjectOrderError) as loopback:
        load_order(root)
    assert loopback.value.reason == "unsupported_environment_combination"

    _write_order(
        root,
        valid.replace('bind_address = "127.0.0.1"', 'bind_address = "192.168.1.10"'),
    )
    assert load_order(root).order["network"]["bind_address"] == "192.168.1.10"

    _write_order(
        root,
        valid.replace('bind_address = "127.0.0.1"', 'bind_address = "192.0.2.10"'),
    )
    with pytest.raises(ProjectOrderError) as documentation_address:
        load_order(root)
    assert documentation_address.value.reason == "unsupported_environment_combination"


def test_runtime_volume_roles_and_identities_must_be_unique(tmp_path: Path) -> None:
    root = tmp_path / "home-beta"
    duplicate = _valid_order().replace(
        """identity = "home-beta-minecraft-data"
""",
        """identity = "home-beta-minecraft-data"

[[runtime.volumes]]
role = "minecraft-data"
identity = "home-beta-other-data"
""",
    )
    _write_order(root, duplicate)

    with pytest.raises(ProjectOrderError) as exc_info:
        load_order(root)

    assert exc_info.value.reason == "order_schema_invalid"


@pytest.mark.parametrize(
    "legacy_name",
    [
        "mc-remote.yml",
        "mc-remote.yaml",
        "mc-remote.lock.yml",
        "mc-remote.lock.yaml",
    ],
)
def test_toml_and_exact_legacy_name_fail_closed(tmp_path: Path, legacy_name: str) -> None:
    root = tmp_path / "home-beta"
    _write_order(root)
    (root / legacy_name).write_text("{}\n", encoding="utf-8")

    with pytest.raises(ProjectOrderError) as exc_info:
        load_order(root)

    assert exc_info.value.reason == "mixed_order_formats"


def test_legacy_yaml_only_requires_explicit_conversion(tmp_path: Path) -> None:
    root = tmp_path / "official-vps"
    root.mkdir()
    (root / "mc-remote.yml").write_text("schema_version: 1\n", encoding="utf-8")

    with pytest.raises(ProjectOrderError) as exc_info:
        load_order(root)

    assert exc_info.value.reason == "legacy_order_requires_explicit_conversion"


def test_toml_lock_without_order_is_orphaned(tmp_path: Path) -> None:
    root = tmp_path / "home-beta"
    root.mkdir()
    (root / "mc-remote.lock.toml").write_text("schema_version = 1\n", encoding="utf-8")

    with pytest.raises(ProjectOrderError) as exc_info:
        load_order(root)

    assert exc_info.value.reason == "orphan_lock"


def test_order_loading_never_searches_parent_or_children(tmp_path: Path) -> None:
    parent = tmp_path / "deployments"
    child = parent / "home-beta"
    _write_order(child)

    with pytest.raises(ProjectOrderError) as parent_exc:
        load_order(parent)
    assert parent_exc.value.reason == "order_missing"

    unrelated_child = child / "generated"
    unrelated_child.mkdir()
    with pytest.raises(ProjectOrderError) as child_exc:
        load_order(unrelated_child)
    assert child_exc.value.reason == "order_missing"


def test_singular_environment_is_required(tmp_path: Path) -> None:
    root = tmp_path / "home-beta"
    content = _valid_order().replace(
        """[environment]
identity = "home-beta"
channel = "beta"
exposure = "isolated"
purpose = "integration"
preset = "classroom-paper@3"
""",
        """[[environments]]
identity = "home-beta"
channel = "beta"
exposure = "isolated"
purpose = "integration"
preset = "classroom-paper@3"

[[environments]]
identity = "home-alpha"
channel = "alpha"
exposure = "isolated"
purpose = "integration"
preset = "classroom-paper@3"
""",
    )
    _write_order(root, content)

    with pytest.raises(ProjectOrderError) as exc_info:
        load_order(root)

    assert exc_info.value.reason == "multiple_environments"


def test_additional_root_order_file_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "home-beta"
    _write_order(root)
    (root / "mc-remote.extra.toml").write_text(_valid_order(), encoding="utf-8")

    with pytest.raises(ProjectOrderError) as exc_info:
        load_order(root)

    assert exc_info.value.reason == "additional_order_file"


@pytest.mark.parametrize("key", ["include", "import", "extends", "glob"])
def test_generic_composition_keys_are_rejected(tmp_path: Path, key: str) -> None:
    root = tmp_path / "home-beta"
    _write_order(root, f'{key} = "shared.toml"\n' + _valid_order())

    with pytest.raises(ProjectOrderError) as exc_info:
        load_order(root)

    assert exc_info.value.reason == "composition_forbidden"


def test_nested_composition_key_is_rejected_with_the_same_reason(tmp_path: Path) -> None:
    root = tmp_path / "home-beta"
    _write_order(root, _valid_order().replace('purpose = "integration"', 'include = "shared.toml"'))

    with pytest.raises(ProjectOrderError) as exc_info:
        load_order(root)

    assert exc_info.value.reason == "composition_forbidden"


def test_environment_interpolation_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "home-beta"
    _write_order(root, _valid_order().replace('purpose = "integration"', 'purpose = "${MC_REMOTE_PURPOSE}"'))

    with pytest.raises(ProjectOrderError) as exc_info:
        load_order(root)

    assert exc_info.value.reason == "environment_interpolation_forbidden"


def test_generated_yaml_is_not_a_mixed_order_format(tmp_path: Path) -> None:
    root = tmp_path / "home-beta"
    _write_order(root)
    generated = root / "generated"
    generated.mkdir()
    (generated / "compose.yaml").write_text("services: {}\n", encoding="utf-8")

    loaded = load_order(root)

    assert loaded.order["environment"]["identity"] == "home-beta"


def test_operator_yaml_is_classified_as_unreferenced_not_mixed_order(tmp_path: Path) -> None:
    root = tmp_path / "home-beta"
    _write_order(root)
    operator = root / "operator" / "example-adapter"
    operator.mkdir(parents=True)
    (operator / "config.yml").write_text("enabled: true\n", encoding="utf-8")

    with pytest.raises(ProjectOrderError) as exc_info:
        load_order(root)

    assert exc_info.value.reason == "operator_input_unreferenced"


def test_initializing_sibling_alpha_does_not_change_beta_order(tmp_path: Path) -> None:
    beta = init_toml_project(
        tmp_path / "home-beta",
        deployment_name="home",
        profile="home-server@1",
        environment_identity="home-beta",
        channel="beta",
        exposure="isolated",
        purpose="integration",
        preset="classroom-paper@3",
        **_instance_kwargs(),
    )
    beta_before = beta.order.read_bytes()

    alpha = init_toml_project(
        tmp_path / "home-alpha",
        deployment_name="home-alpha-experiment",
        profile="home-server@1",
        environment_identity="home-alpha",
        channel="alpha",
        exposure="isolated",
        purpose="integration",
        preset="classroom-paper@4",
        **_instance_kwargs("home-alpha"),
    )

    assert beta.order.read_bytes() == beta_before
    assert beta.root != alpha.root
    assert not beta.lock.exists()
    assert not alpha.lock.exists()
    alpha_order = load_order(alpha.root).order
    assert alpha_order["environment"]["identity"] == "home-alpha"
    assert alpha_order["runtime"]["volumes"][0]["identity"] == "home-alpha-minecraft-data"
    assert alpha_order["world"]["identity"] == "home-alpha-world"


def test_targeted_scalar_edit_preserves_unrelated_layout(tmp_path: Path) -> None:
    root = tmp_path / "home-beta"
    order = _write_order(
        root,
        """# operator note
schema_version = 1

[deployment] # deployment stays first
name = 'home'
profile = "home-server@1"

[environment]
identity = "home-beta" # do not derive the channel from this
channel = "beta"
exposure = "isolated"
purpose = "integration"
preset = "classroom-paper@3"

[runtime]
artifact_store = "/var/lib/mc-remote/artifacts"

[[runtime.volumes]]
role = "minecraft-data"
identity = "home-beta-minecraft-data"

[world]
identity = "home-beta-world"

[network]
bind_address = "127.0.0.1"
java_port = 25565
mcremote_port = 25575

[agreements]
minecraft_eula = true

[acknowledgements]
allow_unverified = false # reviewed separately
unverified_reason = ""
allow_eol = false
eol_reason = ""
""",
    )

    reason_changed = update_order_scalar(
        root,
        ("acknowledgements", "unverified_reason"),
        "reviewed with local compatibility evidence",
    )
    flag_changed = update_order_scalar(root, ("acknowledgements", "allow_unverified"), True)
    name_changed = update_order_scalar(root, ("deployment", "name"), "renamed-home")

    assert reason_changed is True
    assert flag_changed is True
    assert name_changed is True
    text = order.read_text(encoding="utf-8")
    assert text.startswith("# operator note\n")
    assert "[deployment] # deployment stays first" in text
    assert "name = 'renamed-home'" in text
    assert 'identity = "home-beta" # do not derive the channel from this' in text
    assert 'unverified_reason = "reviewed with local compatibility evidence"' in text
    assert "allow_unverified = true # reviewed separately" in text


def test_noop_scalar_edit_does_not_change_bytes_or_mtime(tmp_path: Path) -> None:
    root = tmp_path / "home-beta"
    order = _write_order(root)
    before = order.read_bytes()
    before_mtime = order.stat().st_mtime_ns

    changed = update_order_scalar(root, ("acknowledgements", "allow_unverified"), False)

    assert changed is False
    assert order.read_bytes() == before
    assert order.stat().st_mtime_ns == before_mtime


def test_invalid_scalar_edit_preserves_original_bytes(tmp_path: Path) -> None:
    root = tmp_path / "home-beta"
    order = _write_order(root)
    before = order.read_bytes()

    with pytest.raises(ProjectOrderError) as exc_info:
        update_order_scalar(root, ("acknowledgements", "allow_unverified"), True)

    assert exc_info.value.reason == "acknowledgement_reason_required"
    assert order.read_bytes() == before


def test_atomic_replace_failure_preserves_original_and_cleans_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "home-beta"
    order = _write_order(root)
    before = order.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError(f"simulated replace failure: {source} -> {destination}")

    monkeypatch.setattr("mc_remote_stack.toml_project.os.replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        update_order_scalar(
            root,
            ("acknowledgements", "unverified_reason"),
            "reviewed with local compatibility evidence",
        )

    assert order.read_bytes() == before
    assert not list(root.glob(".mc-remote.toml.*.tmp"))


def test_targeted_edit_preserves_order_file_mode(tmp_path: Path) -> None:
    root = tmp_path / "home-beta"
    order = _write_order(root)
    order.chmod(0o640)

    update_order_scalar(
        root,
        ("acknowledgements", "unverified_reason"),
        "reviewed with local compatibility evidence",
    )

    assert os.stat(order).st_mode & 0o777 == 0o640
