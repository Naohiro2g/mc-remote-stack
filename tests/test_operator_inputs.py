from pathlib import Path

import pytest

from mc_remote_stack.cli import main
from mc_remote_stack.operator_inputs import (
    OperatorInputError,
    _parse_connection_targets,
    _parse_homepage_static,
    _parse_minecraft_backup,
    _parse_minecraft_plugins,
    _parse_minecraft_server,
    _parse_public_routes_v2,
)
from mc_remote_stack.preset_registry import semantic_sha256
from mc_remote_stack.resolver import (
    ResolutionError,
    inspect_lock,
    load_lock,
    resolve_project,
)

from .test_resolver import FIRST_RESOLVED_AT, _acknowledge, _fixture

MOTD_ROLE = "minecraft-motd"
MOTD_ADAPTER = "minecraft-motd@1"
MOTD_PATH = "operator/minecraft-motd/server.properties"


def _declare_motd_role(data_root: Path) -> None:
    profile_path = data_root / "profiles" / "home-server" / "1" / "profile.toml"
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8")
        + """
[[operator_input_roles]]
id = "minecraft-motd"
adapter = "minecraft-motd@1"
required = false
""",
        encoding="utf-8",
    )


def _add_motd_input(project: Path, content: bytes) -> Path:
    order_path = project / "mc-remote.toml"
    order_path.write_text(
        order_path.read_text(encoding="utf-8")
        + """
[[operator_inputs]]
role = "minecraft-motd"
adapter = "minecraft-motd@1"
path = "operator/minecraft-motd/server.properties"
""",
        encoding="utf-8",
    )
    source_path = project / MOTD_PATH
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(content)
    return source_path


def _add_public_routes_input(project: Path, data_root: Path, content: bytes) -> None:
    profile_path = data_root / "profiles" / "home-server" / "1" / "profile.toml"
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8")
        + """
[[operator_input_roles]]
id = "public-routes"
adapter = "public-routes@1"
required = true
""",
        encoding="utf-8",
    )
    order_path = project / "mc-remote.toml"
    order_path.write_text(
        order_path.read_text(encoding="utf-8")
        + """
[[operator_inputs]]
role = "public-routes"
adapter = "public-routes@1"
path = "operator/public-routes/routes.toml"
""",
        encoding="utf-8",
    )
    source_path = project / "operator" / "public-routes" / "routes.toml"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(content)


def test_operator_input_semantics_enter_lock_identity_without_lexical_churn(
    tmp_path: Path,
) -> None:
    project, data_root = _fixture(tmp_path)
    _declare_motd_role(data_root)
    source_path = _add_motd_input(
        project,
        b"# classroom-visible text\nmotd = McRemote home beta\n",
    )
    _acknowledge(project, "unverified")

    created = resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=FIRST_RESOLVED_AT,
    )
    lock = load_lock(project, data_root=data_root)
    expected = {
        "role": MOTD_ROLE,
        "adapter": MOTD_ADAPTER,
        "path": MOTD_PATH,
        "semantic_sha256": semantic_sha256({"motd": "McRemote home beta"}),
        "semantic": {"motd": "McRemote home beta"},
    }
    assert lock["operator_inputs"] == [expected]
    assert lock["render_plan"]["operator_inputs"] == [expected]
    assert lock["render_plan"]["operator_input_roles"] == [
        {
            "id": MOTD_ROLE,
            "adapter": MOTD_ADAPTER,
            "required": False,
        }
    ]

    lock_path = project / "mc-remote.lock.toml"
    before_bytes = lock_path.read_bytes()
    before_mtime = lock_path.stat().st_mtime_ns
    source_path.write_text(
        "# comment-only lexical change\n\nmotd=McRemote home beta\n",
        encoding="utf-8",
    )

    assert inspect_lock(project, data_root=data_root).status == "unchanged"
    unchanged = resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at="2026-07-24T01:00:00Z",
    )
    assert unchanged.status == "unchanged"
    assert unchanged.lock_identity == created.lock_identity
    assert lock_path.read_bytes() == before_bytes
    assert lock_path.stat().st_mtime_ns == before_mtime

    source_path.write_text("motd=Different public text\n", encoding="utf-8")
    assert inspect_lock(project, data_root=data_root).status == "stale"


def test_operator_input_must_be_declared_by_selected_profile(tmp_path: Path) -> None:
    project, data_root = _fixture(tmp_path)
    _add_motd_input(project, b"motd=McRemote home beta\n")
    _acknowledge(project, "unverified")

    with pytest.raises(ResolutionError) as exc_info:
        resolve_project(
            project,
            data_root=data_root,
            allow_unverified=True,
            resolved_at=FIRST_RESOLVED_AT,
        )

    assert exc_info.value.reason == "operator_input_profile_mismatch"
    assert not (project / "mc-remote.lock.toml").exists()


def test_public_routes_semantics_enter_the_lock(tmp_path: Path) -> None:
    project, data_root = _fixture(tmp_path)
    _add_public_routes_input(
        project,
        data_root,
        (
            b'homepage = "mc-remote.example"\n'
            b'homepage_aliases = ["www.mc-remote.example"]\n'
            b'scratch = "scratch.mc-remote.example"\n'
            b'bridge = "bridge.mc-remote.example"\n'
            b'minecraft = "sb.mc-remote.example"\n'
        ),
    )
    _acknowledge(project, "unverified")

    resolve_project(
        project,
        data_root=data_root,
        allow_unverified=True,
        resolved_at=FIRST_RESOLVED_AT,
    )
    lock = load_lock(project, data_root=data_root)

    assert lock["operator_inputs"][0]["semantic"] == {
        "bridge": "bridge.mc-remote.example",
        "homepage": "mc-remote.example",
        "homepage_aliases": ["www.mc-remote.example"],
        "minecraft": "sb.mc-remote.example",
        "scratch": "scratch.mc-remote.example",
    }


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (
            b'homepage = "192.0.2.10"\nhomepage_aliases = []\n'
            b'scratch = "scratch.example"\nbridge = "bridge.example"\n'
            b'minecraft = "sb.example"\n',
            "operator_input_parse_failed",
        ),
        (
            b'homepage = "mc.example"\nhomepage_aliases = []\n'
            b'scratch = "same.example"\nbridge = "same.example"\n'
            b'minecraft = "sb.example"\n',
            "operator_input_parse_failed",
        ),
        (
            b'homepage = "mc.example"\nhomepage_aliases = []\n'
            b'scratch = "secret://runtime"\nbridge = "bridge.example"\n'
            b'minecraft = "sb.example"\n',
            "operator_input_secret_forbidden",
        ),
    ],
)
def test_public_routes_adapter_fails_closed(
    tmp_path: Path,
    content: bytes,
    reason: str,
) -> None:
    project, data_root = _fixture(tmp_path)
    _add_public_routes_input(project, data_root, content)
    _acknowledge(project, "unverified")

    with pytest.raises(ResolutionError) as exc_info:
        resolve_project(
            project,
            data_root=data_root,
            allow_unverified=True,
            resolved_at=FIRST_RESOLVED_AT,
        )

    assert exc_info.value.reason == reason


def test_minecraft_server_adapter_rejects_management_listener(
    tmp_path: Path,
) -> None:
    source = b"""
allow_flight = false
difficulty = "hard"
enable_query = false
enable_status = true
force_gamemode = true
gamemode = "creative"
hardcore = true
log_ips = true
management_server_enabled = true
max_players = 18
max_tick_time = -1
max_world_size = 9984
motd = "McRemote Sandbox Server"
network_compression_threshold = -1
simulation_distance = 6
spawn_protection = 150
view_distance = 10
white_list = false
""".lstrip()

    with pytest.raises(OperatorInputError) as exc_info:
        _parse_minecraft_server(tmp_path / "server.toml", source)

    assert exc_info.value.reason == "operator_input_parse_failed"
    assert "management server must remain disabled" in str(exc_info.value)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"\xff", "operator_input_encoding_invalid"),
        (b"motd=one\nmotd=two\n", "operator_input_parse_failed"),
        (b"motd=one\\\n", "operator_input_parse_failed"),
        (b"unknown=value\n", "operator_input_parse_failed"),
        (b"motd=secret://classroom-token\n", "operator_input_secret_forbidden"),
    ],
)
def test_minecraft_motd_adapter_fails_closed(
    tmp_path: Path,
    content: bytes,
    reason: str,
) -> None:
    project, data_root = _fixture(tmp_path)
    _declare_motd_role(data_root)
    _add_motd_input(project, content)
    _acknowledge(project, "unverified")

    with pytest.raises(ResolutionError) as exc_info:
        resolve_project(
            project,
            data_root=data_root,
            allow_unverified=True,
            resolved_at=FIRST_RESOLVED_AT,
        )

    assert exc_info.value.reason == reason
    assert not (project / "mc-remote.lock.toml").exists()


def test_connection_targets_adapter_parses_ordered_semantic_list(tmp_path: Path) -> None:
    source = b"""
[[targets]]
id = "stable"
label = "Stable"
sandbox = "sb.mc-remote.example"

[[targets]]
id = "beta"
label = "Beta"
sandbox = "beta.sb.mc-remote.example"
""".lstrip()

    semantic = _parse_connection_targets(tmp_path / "targets.toml", source)

    assert semantic == {
        "targets": [
            {"id": "stable", "label": "Stable", "sandbox": "sb.mc-remote.example"},
            {"id": "beta", "label": "Beta", "sandbox": "beta.sb.mc-remote.example"},
        ]
    }


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"targets = []\n", "operator_input_parse_failed"),
        (
            b'[[targets]]\nid = "stable"\nlabel = "Stable"\n'
            b'sandbox = "sb.mc-remote.example"\nextra = "nope"\n',
            "operator_input_parse_failed",
        ),
        (
            b'[[targets]]\nid = "Stable"\nlabel = "Stable"\n'
            b'sandbox = "sb.mc-remote.example"\n',
            "operator_input_parse_failed",
        ),
        (
            b'[[targets]]\nid = "stable"\nlabel = ""\n'
            b'sandbox = "sb.mc-remote.example"\n',
            "operator_input_parse_failed",
        ),
        (
            b'[[targets]]\nid = "stable"\nlabel = "secret://token"\n'
            b'sandbox = "sb.mc-remote.example"\n',
            "operator_input_secret_forbidden",
        ),
        (
            b'[[targets]]\nid = "stable"\nlabel = "Stable"\n'
            b'sandbox = "192.0.2.10"\n',
            "operator_input_parse_failed",
        ),
        (
            b'[[targets]]\nid = "stable"\nlabel = "Stable"\n'
            b'sandbox = "sb.mc-remote.example"\n'
            b'[[targets]]\nid = "stable"\nlabel = "Stable again"\n'
            b'sandbox = "other.mc-remote.example"\n',
            "operator_input_parse_failed",
        ),
        (
            b'[[targets]]\nid = "stable"\nlabel = "Stable"\n'
            b'sandbox = "sb.mc-remote.example"\n'
            b'[[targets]]\nid = "beta"\nlabel = "Beta"\n'
            b'sandbox = "sb.mc-remote.example"\n',
            "operator_input_parse_failed",
        ),
    ],
)
def test_connection_targets_adapter_fails_closed(
    tmp_path: Path,
    content: bytes,
    reason: str,
) -> None:
    with pytest.raises(OperatorInputError) as exc_info:
        _parse_connection_targets(tmp_path / "targets.toml", content)

    assert exc_info.value.reason == reason


def test_public_routes_v2_requires_distinct_wirescope_hostname(tmp_path: Path) -> None:
    source = b'''homepage = "mc-remote.example"
homepage_aliases = ["www.mc-remote.example"]
scratch = "scratch-beta.mc-remote.example"
bridge = "bridge-beta.mc-remote.example"
minecraft = "sb-beta.mc-remote.example"
wirescope = "wirescope-beta.mc-remote.example"
'''

    assert _parse_public_routes_v2(tmp_path / "routes.toml", source) == {
        "homepage": "mc-remote.example",
        "homepage_aliases": ["www.mc-remote.example"],
        "scratch": "scratch-beta.mc-remote.example",
        "bridge": "bridge-beta.mc-remote.example",
        "minecraft": "sb-beta.mc-remote.example",
        "wirescope": "wirescope-beta.mc-remote.example",
    }

    with pytest.raises(OperatorInputError) as exc_info:
        _parse_public_routes_v2(
            tmp_path / "routes.toml",
            source.replace(
                b'wirescope = "wirescope-beta.mc-remote.example"',
                b'wirescope = "scratch-beta.mc-remote.example"',
            ),
        )

    assert exc_info.value.reason == "operator_input_parse_failed"


def test_minecraft_plugins_adapter_normalizes_exact_artifact_set(tmp_path: Path) -> None:
    source = b'''[[plugins]]
filename = "worldedit-bukkit-7.4.2.jar"
sha256 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

[[plugins]]
filename = "Geyser-Spigot.jar"
sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
'''

    assert _parse_minecraft_plugins(tmp_path / "plugins.toml", source) == {
        "plugins": [
            {
                "filename": "Geyser-Spigot.jar",
                "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            },
            {
                "filename": "worldedit-bukkit-7.4.2.jar",
                "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            },
        ]
    }


@pytest.mark.parametrize(
    "source",
    [
        b"plugins = []\n",
        b'[[plugins]]\nfilename = "../bad.jar"\nsha256 = "' + b"a" * 64 + b'"\n',
        b'[[plugins]]\nfilename = "McRemote.jar"\nsha256 = "' + b"a" * 64 + b'"\n',
        b'[[plugins]]\nfilename = "old-mcremote-copy.jar"\nsha256 = "' + b"a" * 64 + b'"\n',
        b'[[plugins]]\nfilename = "plugin.zip"\nsha256 = "' + b"a" * 64 + b'"\n',
        b'[[plugins]]\nfilename = "plugin.jar"\nsha256 = "nope"\n',
        (
            b'[[plugins]]\nfilename = "same.jar"\nsha256 = "'
            + b"a" * 64
            + b'"\n[[plugins]]\nfilename = "same.jar"\nsha256 = "'
            + b"b" * 64
            + b'"\n'
        ),
    ],
)
def test_minecraft_plugins_adapter_rejects_unsafe_or_ambiguous_sets(
    tmp_path: Path,
    source: bytes,
) -> None:
    with pytest.raises(OperatorInputError) as exc_info:
        _parse_minecraft_plugins(tmp_path / "plugins.toml", source)

    assert exc_info.value.reason == "operator_input_parse_failed"


def test_homepage_static_adapter_records_one_exact_tree(tmp_path: Path) -> None:
    source = b'''tree_sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
file_count = 4
total_bytes = 8192
'''

    assert _parse_homepage_static(tmp_path / "homepage.toml", source) == {
        "tree_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "file_count": 4,
        "total_bytes": 8192,
    }


@pytest.mark.parametrize(
    "source",
    [
        b'tree_sha256 = "nope"\nfile_count = 1\ntotal_bytes = 1\n',
        b'tree_sha256 = "' + b"a" * 64 + b'"\nfile_count = 0\ntotal_bytes = 1\n',
        b'tree_sha256 = "' + b"a" * 64 + b'"\nfile_count = 1\ntotal_bytes = 0\n',
        b'tree_sha256 = "' + b"a" * 64 + b'"\nfile_count = 1\ntotal_bytes = 1\nextra = true\n',
    ],
)
def test_homepage_static_adapter_fails_closed(tmp_path: Path, source: bytes) -> None:
    with pytest.raises(OperatorInputError) as exc_info:
        _parse_homepage_static(tmp_path / "homepage.toml", source)

    assert exc_info.value.reason == "operator_input_parse_failed"


def test_minecraft_backup_adapter_accepts_one_explicit_host_path(tmp_path: Path) -> None:
    assert _parse_minecraft_backup(
        tmp_path / "backup.toml",
        b'host_path = "/var/lib/mc-remote/backup-beta"\n',
    ) == {"host_path": "/var/lib/mc-remote/backup-beta"}


@pytest.mark.parametrize(
    "source",
    [
        b'host_path = "relative"\n',
        b'host_path = "/"\n',
        b'host_path = "/var/../root"\n',
        b'host_path = "secret://backup"\n',
        b'host_path = "/var/backup"\nextra = true\n',
    ],
)
def test_minecraft_backup_adapter_fails_closed(tmp_path: Path, source: bytes) -> None:
    with pytest.raises(OperatorInputError) as exc_info:
        _parse_minecraft_backup(tmp_path / "backup.toml", source)

    assert exc_info.value.reason == "operator_input_parse_failed"


def test_cli_validate_runs_operator_adapter_before_lock_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, data_root = _fixture(tmp_path)
    _declare_motd_role(data_root)
    _add_motd_input(project, b"unknown=value\n")
    monkeypatch.setattr("mc_remote_stack.cli._preset_data_root", lambda: data_root)

    assert main(["validate", "--project", str(project)]) == 2

    output = capsys.readouterr().out
    assert "FAIL validate reason=operator_input_parse_failed" in output
    assert not (project / "mc-remote.lock.toml").exists()
