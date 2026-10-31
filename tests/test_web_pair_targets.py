"""Per-Scratch connection lists and defaults, without changing Bridge routing."""

import json
from pathlib import Path

import pytest
import yaml

from mc_remote_stack.operator_inputs import (
    OperatorInputError,
    _parse_connection_targets_v3,
    _parse_connection_targets_v4,
)
from mc_remote_stack.render import RenderContractError, render_toml_project
from mc_remote_stack.resolver import inspect_lock, resolve_project

from .test_resolver import SECOND_RESOLVED_AT
from .vps_fixture import CONNECTION_TARGETS_V4_SOURCE, build_vps_fixture


def test_selection_keeps_catalog_and_per_origin_defaults_separate(tmp_path: Path) -> None:
    parsed = _parse_connection_targets_v4(tmp_path / "targets.toml", CONNECTION_TARGETS_V4_SOURCE.encode())

    assert [target["id"] for target in parsed["targets"]] == ["beta", "stable", "unused"]
    assert parsed["selections"] == [
        {
            "scratch": "scratch-beta.mc-remote.example",
            "target_ids": ["beta"],
            "default_target": "beta",
        },
        {
            "scratch": "scratch.mc-remote.example",
            "target_ids": ["stable", "beta"],
            "default_target": "stable",
        },
    ]
    assert parsed["notices"] == [{"heading": "お知らせ", "body": "fixture notice"}]


@pytest.mark.parametrize(
    "source",
    [
        CONNECTION_TARGETS_V4_SOURCE.replace('target_ids = ["beta"]', "target_ids = []"),
        CONNECTION_TARGETS_V4_SOURCE.replace('target_ids = ["beta"]', 'target_ids = ["missing"]'),
        CONNECTION_TARGETS_V4_SOURCE.replace('target_ids = ["beta"]', 'target_ids = ["beta", "beta"]'),
        # The stable target exists globally but is absent from this origin's list.
        CONNECTION_TARGETS_V4_SOURCE.replace('default_target = "beta"', 'default_target = "stable"'),
        CONNECTION_TARGETS_V4_SOURCE.replace(
            'scratch = "scratch.mc-remote.example"',
            'scratch = "scratch-beta.mc-remote.example"',
        ),
        CONNECTION_TARGETS_V4_SOURCE.replace('default_target = "beta"\n', ""),
        CONNECTION_TARGETS_V4_SOURCE + "port = 25575\n",
        "selections = []\n" + CONNECTION_TARGETS_V4_SOURCE.split("[[selections]]")[0],
    ],
    ids=[
        "empty_ids",
        "unknown_id",
        "duplicate_id",
        "default_outside_origin",
        "duplicate_origin",
        "missing_default",
        "unknown_field",
        "empty_selections",
    ],
)
def test_invalid_selection_is_rejected_before_render(tmp_path: Path, source: str) -> None:
    with pytest.raises(OperatorInputError) as error:
        _parse_connection_targets_v4(tmp_path / "targets.toml", source.encode())

    assert error.value.reason == "operator_input_parse_failed"


def test_v3_input_contract_does_not_silently_accept_selections(tmp_path: Path) -> None:
    with pytest.raises(OperatorInputError):
        _parse_connection_targets_v3(tmp_path / "targets.toml", CONNECTION_TARGETS_V4_SOURCE.encode())


def test_each_origin_gets_its_list_default_and_wirescope(tmp_path: Path) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@17")

    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)

    primary = json.loads((fixture.output / "runtime/scratch.json").read_text())
    stable = json.loads((fixture.output / "runtime/web-pairs/scratch.mc-remote.example.json").read_text())
    assert primary["default_sandbox"] == "sb-beta.mc-remote.example"
    assert primary["connection_targets"] == [
        {"id": "beta", "label": "公開ベータ", "sandbox": "sb-beta.mc-remote.example"}
    ]
    assert stable["default_sandbox"] == "sb.mc-remote.example"
    assert stable["connection_targets"] == [
        {"id": "stable", "label": "安定版", "sandbox": "sb.mc-remote.example"},
        {"id": "beta", "label": "公開ベータ", "sandbox": "sb-beta.mc-remote.example"},
    ]
    assert primary["wirescope_url"] == "https://wirescope-beta.mc-remote.example/"
    assert stable["wirescope_url"] == "https://wirescope.mc-remote.example/"
    assert stable["notices"] == primary["notices"]
    assert "selections" not in stable  # Scratch's existing runtime schema is unchanged.
    compose = yaml.safe_load((fixture.output / "compose.yaml").read_text())
    bridge = compose["services"]["bridge"]
    assert bridge["environment"]["BRIDGE_SANDBOX_ALLOWLIST"] == ("sb-beta.mc-remote.example,sb.mc-remote.example")
    assert bridge["environment"]["BRIDGE_DEFAULT_SANDBOX"] == primary["default_sandbox"]
    assert bridge["networks"] == ["app"]
    assert compose["services"]["minecraft"]["networks"]["app"]["aliases"] == [
        "sb-beta.mc-remote.example",
        "sb.mc-remote.example",
    ]
    assert "0.0.0.0:19132:19132/tcp" in compose["services"]["minecraft"]["ports"]
    caddy = (fixture.output / "Caddyfile").read_text()
    assert "rewrite * /scratch.mc-remote.example.json" in caddy


def test_both_origins_can_offer_both_targets_with_opposite_defaults(tmp_path: Path) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@17")
    source = fixture.project / "operator/connection-targets/targets.toml"
    source.write_text(source.read_text().replace('target_ids = ["beta"]', 'target_ids = ["beta", "stable"]'))
    resolve_project(
        fixture.project,
        data_root=fixture.data_root,
        allow_unverified=True,
        resolved_at=SECOND_RESOLVED_AT,
    )

    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)

    beta = json.loads((fixture.output / "runtime/scratch.json").read_text())
    stable = json.loads((fixture.output / "runtime/web-pairs/scratch.mc-remote.example.json").read_text())
    assert {t["sandbox"] for t in beta["connection_targets"]} == {t["sandbox"] for t in stable["connection_targets"]}
    assert beta["default_sandbox"] != stable["default_sandbox"]


def test_primary_origin_selection_also_works_without_extra_web_pairs(tmp_path: Path) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@17")
    routes = fixture.project / "operator/public-routes/routes.toml"
    routes.write_text(routes.read_text().split("[[web_pairs]]")[0] + "web_pairs = []\n")
    source = fixture.project / "operator/connection-targets/targets.toml"
    source.write_text(
        source.read_text()
        .rsplit("[[selections]]", 1)[0]
        .replace('target_ids = ["beta"]', 'target_ids = ["stable"]')
        .replace('default_target = "beta"', 'default_target = "stable"')
    )
    resolve_project(
        fixture.project,
        data_root=fixture.data_root,
        allow_unverified=True,
        resolved_at=SECOND_RESOLVED_AT,
    )

    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)

    runtime = json.loads((fixture.output / "runtime/scratch.json").read_text())
    # This overrides public-routes.minecraft even for the primary origin.
    assert runtime["default_sandbox"] == "sb.mc-remote.example"
    assert [target["id"] for target in runtime["connection_targets"]] == ["stable"]
    compose = yaml.safe_load((fixture.output / "compose.yaml").read_text())
    assert compose["services"]["bridge"]["environment"]["BRIDGE_DEFAULT_SANDBOX"] == runtime["default_sandbox"]
    assert compose["services"]["bridge"]["environment"]["BRIDGE_SANDBOX_ALLOWLIST"] == "sb.mc-remote.example"
    assert not (fixture.output / "runtime/web-pairs").exists()


@pytest.mark.parametrize(
    "source",
    [
        # A public origin lacks a selection.
        CONNECTION_TARGETS_V4_SOURCE.rsplit("[[selections]]", 1)[0],
        # A selection is attached to an unserved origin.
        CONNECTION_TARGETS_V4_SOURCE.replace(
            'scratch = "scratch.mc-remote.example"', 'scratch = "typo.mc-remote.example"'
        ),
    ],
    ids=["missing_origin", "unknown_origin"],
)
def test_selections_must_cover_exactly_the_served_origins(tmp_path: Path, source: str) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@17")
    (fixture.project / "operator/connection-targets/targets.toml").write_text(source)
    resolve_project(
        fixture.project,
        data_root=fixture.data_root,
        allow_unverified=True,
        resolved_at=SECOND_RESOLVED_AT,
    )

    with pytest.raises(RenderContractError) as error:
        render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)

    assert error.value.reason == "scratch_runtime_config_invalid"
    assert not fixture.output.exists()


def test_changing_a_selection_invalidates_the_lock(tmp_path: Path) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@17")
    source = fixture.project / "operator/connection-targets/targets.toml"
    source.write_text(source.read_text().replace('default_target = "stable"', 'default_target = "beta"'))

    assert inspect_lock(fixture.project, data_root=fixture.data_root).status == "stale"
