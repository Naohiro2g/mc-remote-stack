import json
from pathlib import Path

import yaml

from mc_remote_stack.render import render_project
from mc_remote_stack.validation import load_project

from .helpers import enable_renderable_beta, make_renderable_project


def test_render_uses_stable_without_suffix_and_beta_public_names(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    enable_renderable_beta(project)
    output = tmp_path / "generated"

    render_project(load_project(project.root), output)

    compose = yaml.safe_load((output / "compose.yaml").read_text(encoding="utf-8"))
    caddyfile = (output / "Caddyfile").read_text(encoding="utf-8")
    rendered = "\n".join(
        [
            (output / "compose.yaml").read_text(encoding="utf-8"),
            caddyfile,
            *(path.read_text(encoding="utf-8") for path in (output / "runtime").glob("*.json")),
        ]
    )

    assert "scratch-stable" in compose["services"]
    assert "scratch-beta" in compose["services"]
    assert "bridge-stable" in compose["services"]
    assert "bridge-beta" in compose["services"]
    assert "minecraft-stable" in compose["services"]
    assert "minecraft-beta" in compose["services"]
    assert "sb.mc-remote.com" in rendered
    assert "sb-stable.mc-remote.com" not in rendered
    assert "scratch-beta.mc-remote.com" in caddyfile
    assert "bridge-beta.mc-remote.com" in caddyfile
    assert "-dev.mc-remote.com" not in rendered


def test_render_separates_stable_and_beta_bridge_trust_domains(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    enable_renderable_beta(project)
    output = tmp_path / "generated"

    render_project(load_project(project.root), output)

    compose = yaml.safe_load((output / "compose.yaml").read_text(encoding="utf-8"))
    stable = compose["services"]["bridge-stable"]["environment"]
    beta = compose["services"]["bridge-beta"]["environment"]

    assert stable["BRIDGE_ORIGIN_ALLOWLIST"] == "https://scratch.mc-remote.com"
    assert stable["BRIDGE_SANDBOX_ALLOWLIST"] == "sb.mc-remote.com"
    assert stable["BRIDGE_DEFAULT_SANDBOX"] == "sb.mc-remote.com"
    assert beta["BRIDGE_ORIGIN_ALLOWLIST"] == "https://scratch-beta.mc-remote.com"
    assert beta["BRIDGE_SANDBOX_ALLOWLIST"] == "sb-beta.mc-remote.com"
    assert beta["BRIDGE_DEFAULT_SANDBOX"] == "sb-beta.mc-remote.com"


def test_render_sets_jst_and_uses_exclusive_standard_ports_for_both_channels(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    enable_renderable_beta(project)
    output = tmp_path / "generated"

    render_project(load_project(project.root), output)

    compose = yaml.safe_load((output / "compose.yaml").read_text(encoding="utf-8"))
    stable = compose["services"]["minecraft-stable"]
    beta = compose["services"]["minecraft-beta"]

    assert stable["environment"]["TZ"] == "Asia/Tokyo"
    assert beta["environment"]["TZ"] == "Asia/Tokyo"
    assert stable["ports"] == ["25565:25565/tcp", "25565:19132/udp", "25575:25575/tcp"]
    assert beta["ports"] == ["25565:25565/tcp", "25565:19132/udp", "25575:25575/tcp"]
    assert stable["profiles"] == ["stable"]
    assert beta["profiles"] == ["beta"]


def test_disabled_beta_runtime_fails_closed(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    output = tmp_path / "generated"

    render_project(load_project(project.root), output)

    runtime = json.loads((output / "runtime" / "beta.json").read_text(encoding="utf-8"))
    compose = yaml.safe_load((output / "compose.yaml").read_text(encoding="utf-8"))
    assert runtime["connection_enabled"] is False
    assert runtime["bridge_url"] == "wss://bridge-beta.mc-remote.com"
    assert runtime["default_sandbox"] == "sb-beta.mc-remote.com"
    assert "minecraft-beta" not in compose["services"]
