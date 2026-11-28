"""Tests for Unity sandbox standalone template and public VPS co-location (vps-server@19)."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from mc_remote_stack.cli import main
from mc_remote_stack.deployment_update import (
    DeploymentUpdateContractError,
    apply_deployment_update,
    plan_deployment_update,
)
from mc_remote_stack.doctor import (
    DoctorContractError,
    ProtocolHelloResult,
    doctor_toml_project,
    probe_unity_sandbox_health,
    probe_unity_sandbox_tcp_auth,
)
from mc_remote_stack.operator_inputs import (
    OperatorInputError,
    _parse_unity_sandboxes,
)
from mc_remote_stack.render import RenderContractError, render_toml_project
from mc_remote_stack.resolver import ResolutionError, load_lock, resolve_project
from mc_remote_stack.scratch_contract import load_runtime_config_schema, validate_runtime_config

from .test_doctor import _vps_doctor_responses, _vps_public_probes
from .test_resolver import SECOND_RESOLVED_AT
from .test_toml_apply import _result
from .vps_fixture import PRESET, VpsFixture, build_vps_fixture, rendered_containers

REPO_ROOT = Path(__file__).resolve().parents[1]
LATEST_DIGEST = f"sha256:{'a' * 64}"
LTS_DIGEST = f"sha256:{'b' * 64}"

SANDBOXES_TWO_SOURCE = f"""[[sandboxes]]
id = "latest"
hostname = "unity-sb.mc-remote.example"
image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox:v2321.3.0@{LATEST_DIGEST}"

[[sandboxes]]
id = "lts"
hostname = "unity-sb-lts.mc-remote.example"
image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox:v2321.3.0@{LTS_DIGEST}"
"""

FOUR_TARGETS_SOURCE = """[[targets]]
id = "stable"
label = "標準（Minecraft）"
sandbox = "sb.mc-remote.example"

[[targets]]
id = "lts"
label = "長期固定（Minecraft）"
sandbox = "sb-lts.mc-remote.example"

[[targets]]
id = "unity-latest"
label = "Unity箱庭"
sandbox = "unity-sb.mc-remote.example"

[[targets]]
id = "unity-lts"
label = "Unity箱庭（LTS）"
sandbox = "unity-sb-lts.mc-remote.example"

[[notices]]
heading = "お知らせ"
body = "Unity箱庭とMinecraftの両方を選べます。"

[[selections]]
scratch = "scratch-beta.mc-remote.example"
target_ids = ["lts", "unity-lts"]
default_target = "lts"

[[selections]]
scratch = "scratch.mc-remote.example"
target_ids = ["stable", "unity-latest", "lts", "unity-lts"]
default_target = "stable"
"""


def _copy_profile(fixture: VpsFixture, revision: str) -> None:
    target = fixture.data_root / f"profiles/vps-server/{revision}/profile.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        files("mc_remote_stack")
        .joinpath(f"data/profiles/vps-server/{revision}/profile.toml")
        .read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def _copy_profile_19(fixture: VpsFixture) -> None:
    _copy_profile(fixture, "19")


def _attach_unity_sandboxes(
    fixture: VpsFixture,
    sandboxes_source: str = SANDBOXES_TWO_SOURCE,
    targets_source: str = FOUR_TARGETS_SOURCE,
    *,
    adapter: str = "unity-sandboxes@1",
) -> None:
    targets_path = fixture.project / "operator/connection-targets/targets.toml"
    targets_path.write_text(targets_source, encoding="utf-8")

    sandboxes_rel = "operator/unity-sandboxes/sandboxes.toml"
    sandboxes_path = fixture.project / sandboxes_rel
    sandboxes_path.parent.mkdir(parents=True, exist_ok=True)
    sandboxes_path.write_text(sandboxes_source, encoding="utf-8")

    order_path = fixture.project / "mc-remote.toml"
    order_path.write_text(
        order_path.read_text(encoding="utf-8")
        + (
            '\n[[operator_inputs]]\n'
            'role = "unity-sandboxes"\n'
            f'adapter = "{adapter}"\n'
            f'path = "{sandboxes_rel}"\n'
        ),
        encoding="utf-8",
    )
    resolve_project(
        fixture.project,
        data_root=fixture.data_root,
        allow_unverified=True,
        resolved_at=SECOND_RESOLVED_AT,
    )


def test_unity_sandboxes_parser_accepts_zero_one_two_and_normalizes_order(
    tmp_path: Path,
) -> None:
    empty = _parse_unity_sandboxes(tmp_path / "sandboxes.toml", b"sandboxes = []\n")
    assert empty == {"sandboxes": []}

    one_source = (
        '[[sandboxes]]\n'
        'id = "only"\n'
        'hostname = "unity.mc-remote.example"\n'
        f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LATEST_DIGEST}"\n'
    ).encode()
    one = _parse_unity_sandboxes(tmp_path / "sandboxes.toml", one_source)
    assert one == {
        "sandboxes": [
            {
                "id": "only",
                "hostname": "unity.mc-remote.example",
                "aliases": [],
                "image": f"ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LATEST_DIGEST}",
            }
        ]
    }

    aliased_source = (
        '[[sandboxes]]\n'
        'id = "unity"\n'
        'hostname = "unity-sb.mc-remote.example"\n'
        'aliases = ["z-alias.mc-remote.example", "unity-sb-lts.mc-remote.example"]\n'
        f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LATEST_DIGEST}"\n'
    ).encode()
    aliased = _parse_unity_sandboxes(tmp_path / "sandboxes.toml", aliased_source)
    assert aliased["sandboxes"][0]["aliases"] == [
        "unity-sb-lts.mc-remote.example",
        "z-alias.mc-remote.example",
    ]

    reversed_source = f"""[[sandboxes]]
id = "z-lts"
hostname = "unity-sb-lts.mc-remote.example"
image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox:v2321.3.0@{LTS_DIGEST}"

[[sandboxes]]
id = "a-latest"
hostname = "unity-sb.mc-remote.example"
image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox:v2321.3.0@{LATEST_DIGEST}"
""".encode()
    two = _parse_unity_sandboxes(tmp_path / "sandboxes.toml", reversed_source)
    assert [item["id"] for item in two["sandboxes"]] == ["a-latest", "z-lts"]


@pytest.mark.parametrize(
    ("source", "expected_reason"),
    [
        (
            '[[sandboxes]]\nid = "latest"\nhostname = "unity-sb.mc-remote.example"\n'
            'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox:v2321.3.0"\n',
            "operator_input_parse_failed",
        ),
        (
            '[[sandboxes]]\nid = "latest"\nhostname = "unity-sb.mc-remote.example"\n'
            f'image = "ghcr.io/Naohiro2g/mc-remote-unity-sandbox@{LATEST_DIGEST}"\n',
            "operator_input_parse_failed",
        ),
        (
            '[[sandboxes]]\nid = "latest"\nhostname = "192.0.2.10"\n'
            f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LATEST_DIGEST}"\n',
            "operator_input_parse_failed",
        ),
        (
            SANDBOXES_TWO_SOURCE.replace('id = "lts"', 'id = "latest"'),
            "operator_input_parse_failed",
        ),
        (
            SANDBOXES_TWO_SOURCE.replace(
                'hostname = "unity-sb-lts.mc-remote.example"',
                'hostname = "unity-sb.mc-remote.example"',
            ),
            "operator_input_parse_failed",
        ),
        (
            '[[sandboxes]]\nid = "latest"\nhostname = "unity-sb.mc-remote.example"\n'
            f'image = "secret://ghcr.io/naohiro2g/unity@{LATEST_DIGEST}"\n',
            "operator_input_secret_forbidden",
        ),
        (
            '[[sandboxes]]\nid = "latest"\nhostname = "unity-sb.mc-remote.example"\n'
            f'image = "${{UNITY_IMAGE}}@{LATEST_DIGEST}"\n',
            "operator_input_secret_forbidden",
        ),
        (
            SANDBOXES_TWO_SOURCE + "extra = true\n",
            "operator_input_parse_failed",
        ),
        (
            "".join(
                f'[[sandboxes]]\nid = "sb-{index}"\n'
                f'hostname = "unity-{index}.mc-remote.example"\n'
                f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LATEST_DIGEST}"\n\n'
                for index in range(9)
            ),
            "operator_input_parse_failed",
        ),
        (
            '[[sandboxes]]\nid = "unity"\nhostname = "unity-sb.mc-remote.example"\n'
            'aliases = ["unity-sb.mc-remote.example"]\n'
            f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LATEST_DIGEST}"\n',
            "operator_input_parse_failed",
        ),
        (
            f"{SANDBOXES_TWO_SOURCE}"
            'aliases = ["unity-sb.mc-remote.example"]\n',
            "operator_input_parse_failed",
        ),
        (
            '[[sandboxes]]\nid = "unity"\nhostname = "unity-sb.mc-remote.example"\n'
            'aliases = "unity-sb-lts.mc-remote.example"\n'
            f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LATEST_DIGEST}"\n',
            "operator_input_parse_failed",
        ),
        (
            '[[sandboxes]]\nid = "unity"\nhostname = "unity-sb.mc-remote.example"\n'
            'aliases = ['
            + ", ".join(f'"a{index}.mc-remote.example"' for index in range(9))
            + "]\n"
            f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LATEST_DIGEST}"\n',
            "operator_input_parse_failed",
        ),
    ],
    ids=[
        "missing_digest",
        "uppercase_image",
        "ip_hostname",
        "duplicate_id",
        "duplicate_hostname",
        "secret_scheme",
        "env_interpolation",
        "unknown_key",
        "too_many_sandboxes",
        "alias_duplicates_own_hostname",
        "alias_duplicates_other_sandbox",
        "aliases_not_list",
        "too_many_aliases",
    ],
)
def test_unity_sandboxes_parser_rejects_invalid_inputs(
    tmp_path: Path, source: str, expected_reason: str
) -> None:
    with pytest.raises(OperatorInputError) as exc_info:
        _parse_unity_sandboxes(tmp_path / "sandboxes.toml", source.encode("utf-8"))

    assert exc_info.value.reason == expected_reason


def test_vps_server_19_without_unity_sandboxes_renders_four_services(tmp_path: Path) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@19", minecraft_heap="3G")

    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)

    lock = load_lock(fixture.project, data_root=fixture.data_root)
    assert [service["id"] for service in lock["render_plan"]["services"]] == [
        "caddy",
        "scratch",
        "bridge",
        "minecraft",
    ]
    compose = yaml.safe_load((fixture.output / "compose.yaml").read_text(encoding="utf-8"))
    assert list(compose["services"]) == ["caddy", "scratch", "bridge", "minecraft"]


def test_vps_server_19_renders_two_independent_unity_sandboxes(tmp_path: Path) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@19", minecraft_heap="3G")
    _attach_unity_sandboxes(fixture)

    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)

    lock = load_lock(fixture.project, data_root=fixture.data_root)
    assert lock["render_plan"]["services"] == [
        {"id": "caddy", "role": "caddy-edge"},
        {"id": "scratch", "role": "scratch-runtime"},
        {"id": "bridge", "role": "websocket-bridge"},
        {"id": "minecraft", "role": "minecraft"},
        {"id": "unity-sandbox-latest", "role": "unity-sandbox"},
        {"id": "unity-sandbox-lts", "role": "unity-sandbox"},
    ]

    compose = yaml.safe_load((fixture.output / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert list(services) == [
        "caddy",
        "scratch",
        "bridge",
        "minecraft",
        "unity-sandbox-latest",
        "unity-sandbox-lts",
    ]

    latest = services["unity-sandbox-latest"]
    lts = services["unity-sandbox-lts"]
    assert "ports" not in latest
    assert "ports" not in lts
    assert "mem_limit" not in latest
    assert "mem_limit" not in lts
    assert latest["command"] == ["--tcp-port", "25575"]
    assert lts["command"] == ["--tcp-port", "25575"]
    assert latest["environment"]["TZ"] == "Asia/Tokyo"
    assert lts["environment"]["TZ"] == "Asia/Tokyo"
    assert latest["networks"] == {"app": {"aliases": ["unity-sb.mc-remote.example"]}}
    assert lts["networks"] == {"app": {"aliases": ["unity-sb-lts.mc-remote.example"]}}
    assert latest["image"].endswith(LATEST_DIGEST)
    assert lts["image"].endswith(LTS_DIGEST)

    assert services["minecraft"]["networks"]["app"]["aliases"] == [
        "sb-lts.mc-remote.example",
        "sb.mc-remote.example",
    ]
    assert services["bridge"]["environment"]["BRIDGE_SANDBOX_ALLOWLIST"] == (
        "sb-lts.mc-remote.example,sb.mc-remote.example,"
        "unity-sb-lts.mc-remote.example,unity-sb.mc-remote.example"
    )

    caddy = (fixture.output / "Caddyfile").read_text(encoding="utf-8")
    assert "unity-sb.mc-remote.example {\n    reverse_proxy unity-sandbox-latest:8080\n}" in caddy
    assert "unity-sb-lts.mc-remote.example {\n    reverse_proxy unity-sandbox-lts:8080\n}" in caddy


def test_vps_server_19_renders_shared_unity_sandbox_with_aliases(tmp_path: Path) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@19", minecraft_heap="3G")
    shared_source = (
        '[[sandboxes]]\n'
        'id = "unity"\n'
        'hostname = "unity-sb.mc-remote.example"\n'
        'aliases = ["unity-sb-lts.mc-remote.example"]\n'
        f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox:v2321.2.0@{LATEST_DIGEST}"\n'
    )
    _attach_unity_sandboxes(fixture, sandboxes_source=shared_source)

    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)

    lock = load_lock(fixture.project, data_root=fixture.data_root)
    assert [service["id"] for service in lock["render_plan"]["services"]] == [
        "caddy",
        "scratch",
        "bridge",
        "minecraft",
        "unity-sandbox-unity",
    ]
    compose = yaml.safe_load((fixture.output / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert list(services) == [
        "caddy",
        "scratch",
        "bridge",
        "minecraft",
        "unity-sandbox-unity",
    ]
    assert services["unity-sandbox-unity"]["networks"] == {
        "app": {"aliases": ["unity-sb.mc-remote.example", "unity-sb-lts.mc-remote.example"]}
    }
    assert services["minecraft"]["networks"]["app"]["aliases"] == [
        "sb-lts.mc-remote.example",
        "sb.mc-remote.example",
    ]
    assert services["bridge"]["environment"]["BRIDGE_SANDBOX_ALLOWLIST"] == (
        "sb-lts.mc-remote.example,sb.mc-remote.example,"
        "unity-sb-lts.mc-remote.example,unity-sb.mc-remote.example"
    )
    caddy = (fixture.output / "Caddyfile").read_text(encoding="utf-8")
    assert (
        "unity-sb.mc-remote.example, unity-sb-lts.mc-remote.example {\n"
        "    reverse_proxy unity-sandbox-unity:8080\n}"
    ) in caddy


def test_vps_server_19_rejects_unity_alias_colliding_with_public_routes(tmp_path: Path) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@19", minecraft_heap="3G")
    bad_sandboxes = (
        '[[sandboxes]]\n'
        'id = "unity"\n'
        'hostname = "unity-sb.mc-remote.example"\n'
        'aliases = ["scratch.mc-remote.example"]\n'
        f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LATEST_DIGEST}"\n'
    )
    _attach_unity_sandboxes(fixture, sandboxes_source=bad_sandboxes)

    with pytest.raises(RenderContractError) as exc_info:
        render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)

    assert exc_info.value.reason == "render_plan_invalid"


@pytest.mark.parametrize(
    "colliding_hostname",
    [
        "mc-remote.example",
        "scratch-beta.mc-remote.example",
        "bridge-beta.mc-remote.example",
        "sb-beta.mc-remote.example",
        "wirescope-beta.mc-remote.example",
        "scratch.mc-remote.example",
        "wirescope.mc-remote.example",
    ],
)
def test_vps_server_19_rejects_unity_hostname_colliding_with_public_routes(
    tmp_path: Path, colliding_hostname: str
) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@19", minecraft_heap="3G")
    bad_sandboxes = (
        '[[sandboxes]]\n'
        'id = "latest"\n'
        f'hostname = "{colliding_hostname}"\n'
        f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LATEST_DIGEST}"\n'
    )
    _attach_unity_sandboxes(fixture, sandboxes_source=bad_sandboxes)

    with pytest.raises(RenderContractError) as exc_info:
        render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)

    assert exc_info.value.reason == "render_plan_invalid"


def test_doctor_validates_six_containers_and_unity_healthz_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@19", minecraft_heap="3G")
    _attach_unity_sandboxes(fixture)
    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)
    lock = load_lock(fixture.project, data_root=fixture.data_root)

    containers = rendered_containers(fixture.output)
    assert len(containers) == 6
    from .test_toml_apply import FakeDocker

    runner = FakeDocker(_vps_doctor_responses(fixture.output, lock, containers=containers))
    probes = _vps_public_probes(fixture.output)
    probed_urls: list[str] = []

    def fake_unity_probe(url: str, timeout: int) -> None:
        assert timeout == 5
        probed_urls.append(url)

    result = doctor_toml_project(
        fixture.project,
        fixture.output,
        docker_context="default",
        data_root=fixture.data_root,
        runner=runner,
        hello_probe=lambda *_args, **_kwargs: ProtocolHelloResult(
            status="auth-required", protocol=None, minecraft_version=None
        ),
        unity_health_probe=fake_unity_probe,
        **probes,
    )

    assert result.unity_sandbox_status == "healthy"
    assert result.unity_sandbox_count == 2
    assert probed_urls == [
        "https://unity-sb.mc-remote.example/healthz",
        "https://unity-sb-lts.mc-remote.example/healthz",
    ]

    monkeypatch.setattr("mc_remote_stack.cli.doctor_toml_project", lambda *_a, **_k: result)
    assert main(["doctor", "--project", str(fixture.project)]) == 0
    assert "OK doctor unity-sandbox=healthy count=2" in capsys.readouterr().out


def test_doctor_validates_unity_aliases_and_fails_on_unhealthy_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@19", minecraft_heap="3G")
    shared_source = (
        '[[sandboxes]]\n'
        'id = "unity"\n'
        'hostname = "unity-sb.mc-remote.example"\n'
        'aliases = ["unity-sb-lts.mc-remote.example"]\n'
        f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox:v2321.2.0@{LATEST_DIGEST}"\n'
    )
    _attach_unity_sandboxes(fixture, sandboxes_source=shared_source)
    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)
    lock = load_lock(fixture.project, data_root=fixture.data_root)

    containers = rendered_containers(fixture.output)
    assert len(containers) == 5
    from .test_toml_apply import FakeDocker

    runner = FakeDocker(_vps_doctor_responses(fixture.output, lock, containers=containers))
    probes = _vps_public_probes(fixture.output)
    probed_urls: list[str] = []

    def fake_unity_probe(url: str, timeout: int) -> None:
        assert timeout == 5
        probed_urls.append(url)

    result = doctor_toml_project(
        fixture.project,
        fixture.output,
        docker_context="default",
        data_root=fixture.data_root,
        runner=runner,
        hello_probe=lambda *_args, **_kwargs: ProtocolHelloResult(
            status="auth-required", protocol=None, minecraft_version=None
        ),
        unity_health_probe=fake_unity_probe,
        **probes,
    )

    assert result.unity_sandbox_status == "healthy"
    assert result.unity_sandbox_count == 1
    assert probed_urls == [
        "https://unity-sb.mc-remote.example/healthz",
        "https://unity-sb-lts.mc-remote.example/healthz",
    ]
    monkeypatch.setattr("mc_remote_stack.cli.doctor_toml_project", lambda *_a, **_k: result)
    assert main(["doctor", "--project", str(fixture.project)]) == 0
    assert "OK doctor unity-sandbox=healthy count=1" in capsys.readouterr().out

    def fail_alias_probe(url: str, timeout: int) -> None:
        del timeout
        if "unity-sb-lts.mc-remote.example" in url:
            raise DoctorContractError("doctor_unity_sandbox_unhealthy", url, "injected")

    with pytest.raises(DoctorContractError) as exc_info:
        doctor_toml_project(
            fixture.project,
            fixture.output,
            docker_context="default",
            data_root=fixture.data_root,
            runner=FakeDocker(_vps_doctor_responses(fixture.output, lock, containers=containers)),
            hello_probe=lambda *_args, **_kwargs: ProtocolHelloResult(
                status="auth-required", protocol=None, minecraft_version=None
            ),
            unity_health_probe=fail_alias_probe,
            **probes,
        )
    assert exc_info.value.reason == "doctor_unity_sandbox_unhealthy"


@pytest.mark.parametrize(
    ("payload", "expected_reason"),
    [
        ({"status": "error", "tcpPort": 25575}, "doctor_unity_sandbox_unhealthy"),
        ({"status": "ok", "tcpPort": 25585}, "doctor_unity_sandbox_unhealthy"),
        (None, "doctor_unity_sandbox_unavailable"),
    ],
)
def test_probe_unity_sandbox_health_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict | None,
    expected_reason: str,
) -> None:
    class _FakeResponse:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return self.body

    def fake_urlopen(_request: object, *, timeout: int) -> _FakeResponse:
        assert timeout == 5
        if payload is None:
            raise TimeoutError("timed out")
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr("mc_remote_stack.doctor.urlopen", fake_urlopen)

    with pytest.raises(DoctorContractError) as exc_info:
        probe_unity_sandbox_health("https://unity-sb.mc-remote.example/healthz", timeout=5)

    assert exc_info.value.reason == expected_reason


def test_deployment_update_adds_unity_sandboxes_and_rolls_back_to_four_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@18", minecraft_heap="3G")
    _copy_profile_19(fixture)
    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)
    source_lock = load_lock(fixture.project, data_root=fixture.data_root)
    source_containers = rendered_containers(fixture.output)
    # Add provenance for _discover_runtime_compose_files: it loops over containers until minecraft
    # Make sure rendered_containers includes the order of services in compose.yaml.

    sandboxes_input = tmp_path / "reviewed-sandboxes.toml"
    sandboxes_input.write_text(SANDBOXES_TWO_SOURCE, encoding="utf-8")
    targets_input = tmp_path / "reviewed-targets.toml"
    targets_input.write_text(FOUR_TARGETS_SOURCE, encoding="utf-8")

    # Dynamic runner that serves source/target inspect and records compose up service lists
    up_services: list[list[str]] = []
    candidate_state: dict[str, object] = {}

    def runner(command: list[str], timeout: int):
        del timeout
        cmd = tuple(command)
        if cmd == ("docker", "context", "inspect", "default"):
            return _result(
                cmd,
                stdout=json.dumps(
                    [{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}]
                )
                + "\n",
            )
        if cmd == ("docker", "--context", "default", "version", "--format", "{{.Server.Version}}"):
            return _result(cmd, stdout="29.1.3\n")
        if cmd == ("docker", "--context", "default", "compose", "version", "--short"):
            return _result(cmd, stdout="2.40.3\n")
        if cmd[:4] == ("docker", "--context", "default", "ps"):
            current_containers = candidate_state.get("active_containers", source_containers)
            return _result(cmd, stdout="".join(f"{c['Id']}\n" for c in current_containers))
        if cmd[:4] == ("docker", "--context", "default", "inspect"):
            cid = cmd[4]
            current_containers = candidate_state.get("active_containers", source_containers)
            for c in current_containers:
                if c["Id"] == cid:
                    return _result(cmd, stdout=json.dumps([c]) + "\n")
            raise AssertionError(f"unknown container id {cid}")
        if cmd[:5] == ("docker", "--context", "default", "volume", "inspect"):
            vol = cmd[5]
            return _result(cmd, stdout=json.dumps([{
                "Name": vol,
                "Driver": "local",
                "Labels": {
                    "io.mc-remote.owner": "mcrctl",
                    "io.mc-remote.deployment": source_lock["deployment"]["name"],
                    "io.mc-remote.environment": source_lock["environment"]["identity"],
                    "io.mc-remote.world": source_lock["world"]["identity"],
                    "io.mc-remote.created-by-lock": source_lock["lock_identity"],
                },
            }]) + "\n")
        if "compose" in cmd and "config" in cmd:
            if "--format" in cmd and "json" in cmd:
                file_idx = cmd.index("--file") + 1
                compose_path = Path(cmd[file_idx])
                compose_obj = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
                for service in compose_obj.get("services", {}).values():
                    for volume in service.get("volumes", []):
                        if isinstance(volume, dict) and volume.get("type") == "bind":
                            src = Path(volume["source"])
                            if not src.is_absolute():
                                volume["source"] = str((compose_path.parent / src).resolve())
                return _result(cmd, stdout=json.dumps(compose_obj))
            return _result(cmd)
        if "compose" in cmd and "pull" in cmd:
            return _result(cmd)
        if "compose" in cmd and "down" in cmd:
            return _result(cmd)
        if "compose" in cmd and "up" in cmd:
            never_idx = cmd.index("never", cmd.index("up"))
            services = list(cmd[never_idx + 1 :])
            up_services.append(services)
            candidate_state["active_containers"] = rendered_containers(fixture.output)
            return _result(cmd)
        raise AssertionError(f"unexpected command: {command!r}")

    def fake_scratch_probe(url: str, timeout: int):
        del timeout
        host = url.split("/")[2]
        pair_file = fixture.output / "runtime/web-pairs" / f"{host}.json"
        if pair_file.exists():
            return json.loads(pair_file.read_text(encoding="utf-8"))
        return json.loads((fixture.output / "runtime/scratch.json").read_text(encoding="utf-8"))

    monkeypatch.setattr("mc_remote_stack.doctor.probe_homepage_public", lambda *_a, **_k: None)
    monkeypatch.setattr("mc_remote_stack.doctor.probe_scratch_runtime_config", fake_scratch_probe)
    monkeypatch.setattr("mc_remote_stack.doctor.probe_wirescope_public_handoff", lambda *_a, **_k: None)
    fail_unity_once = {"fail": True}

    def fake_unity_probe(url: str, timeout: int) -> None:
        del url, timeout
        if fail_unity_once["fail"]:
            raise DoctorContractError("doctor_unity_sandbox_unhealthy", "unity", "injected failure")

    monkeypatch.setattr("mc_remote_stack.doctor.probe_unity_sandbox_health", fake_unity_probe)

    plan = plan_deployment_update(
        fixture.project,
        fixture.output,
        target_profile="vps-server@19",
        target_preset=PRESET,
        input_overrides={},
        input_files={
            "connection-targets": targets_input,
            "unity-sandboxes": sandboxes_input,
        },
        docker_context="default",
        data_root=fixture.data_root,
        allow_unverified=True,
        runner=runner,
        hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
    )

    assert plan.services == (
        "caddy",
        "scratch",
        "bridge",
        "minecraft",
        "unity-sandbox-latest",
        "unity-sandbox-lts",
    )

    # First apply fails target unity healthz -> rolls back starting ONLY the 4 source services.
    with pytest.raises(DeploymentUpdateContractError) as exc_info:
        apply_deployment_update(
            fixture.project,
            plan_id=plan.plan_id,
            confirmed=True,
            data_root=fixture.data_root,
            runner=runner,
            hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
        )
    assert exc_info.value.reason == "doctor_unity_sandbox_unhealthy"
    assert up_services == [
        ["caddy", "scratch", "bridge", "minecraft", "unity-sandbox-latest", "unity-sandbox-lts"],
        ["caddy", "scratch", "bridge", "minecraft"],
    ]

    # Second retry succeeds cleanly.
    fail_unity_once["fail"] = False
    result = apply_deployment_update(
        fixture.project,
        plan_id=plan.plan_id,
        confirmed=True,
        data_root=fixture.data_root,
        runner=runner,
        hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
    )
    assert result.phase == "complete"
    assert len(load_lock(fixture.project, data_root=fixture.data_root)["render_plan"]["services"]) == 6


def _assert_unity_isolation_limits(service: dict) -> None:
    assert service["mem_limit"] == "512m"
    assert service["memswap_limit"] == "512m"
    assert service["cpus"] == 0.5
    assert service["pids_limit"] == 256
    assert service["ulimits"] == {"nofile": {"soft": 1024, "hard": 1024}}
    assert service["logging"] == {
        "driver": "json-file",
        "options": {"max-size": "10m", "max-file": "3"},
    }


def test_standalone_unity_sandbox_example_is_valid_yaml_and_matches_scratch_schema() -> None:
    example_dir = REPO_ROOT / "examples/unity-sandbox"
    compose = yaml.safe_load((example_dir / "compose.yaml").read_text(encoding="utf-8"))
    dotnet_service = compose["services"]["dotnet-sandbox"]
    assert "ports" not in dotnet_service
    assert dotnet_service["networks"] == {
        "app": {"aliases": ["dotnet-sb.example.org"]}
    }
    _assert_unity_isolation_limits(dotnet_service)
    caddyfile = (example_dir / "Caddyfile").read_text(encoding="utf-8")
    assert "unity-app.example.org {\n    reverse_proxy dotnet-sandbox:8080\n}" in caddyfile
    assert "dotnet-sb.example.org {\n    redir https://unity-app.example.org{uri} 302\n}" in caddyfile

    scratch_config = json.loads(
        (example_dir / "runtime/scratch.json").read_text(encoding="utf-8")
    )
    assert scratch_config["default_sandbox"] == "dotnet-sb.example.org"
    assert scratch_config["connection_targets"] == [
        {
            "id": "dotnet-sb",
            "label": ".NET箱庭サーバー",
            "sandbox": "dotnet-sb.example.org",
        }
    ]
    schema = load_runtime_config_schema(
        {
            "source_commit": "57fb7691bb5d9a5895785b200ae78734240ebfa6",
            "source_directory": "packages/scratch-gui/contracts/runtime-config",
            "directory_tree_sha": "ecb669a02ac6c8e502b44850e6dd28260c5adad4",
            "schema_sha256": "4e1f8489dc6ea03800f5cf0fefd2f078fd6d71c8efda581f1711f68e384f99e4",
            "container_mount_path": "/usr/share/nginx/html/mc-remote-runtime-config.json",
            "image_digest": "sha256:f3bcf6a6e9ae939e8a8781628ad1e9b40507286da34224f91dfba711a4920c67",
            "accepted_fixtures": ["fixtures/disabled.json", "fixtures/valid.json"],
            "rejected_fixtures": [
                "fixtures/invalid/enabled-missing-targets.json",
                "fixtures/invalid/nested-unknown-field.json",
                "fixtures/invalid/schema-version.json",
                "fixtures/invalid/unknown-field.json",
            ],
            "fixture_sha256": {
                "fixtures/disabled.json": (
                    "bec0cf2c31fbca7d3bd603e29c1d145d82b6ecf7b4661b9adafde31dfa2eec2d"
                ),
                "fixtures/valid.json": (
                    "cc2282144e1e87b42d2e31229461f9aeead26eeb446ae817571eb96935360e20"
                ),
                "fixtures/invalid/enabled-missing-targets.json": (
                    "f5392059e42431b45fb8f7f03aa09e734fb8b9e79f8fb92913082ff05842736c"
                ),
                "fixtures/invalid/nested-unknown-field.json": (
                    "ed0923793b8713ec67615b6f14bc9f8fb342041b0cdeed5d7b9b10110a3c62b7"
                ),
                "fixtures/invalid/schema-version.json": (
                    "9e0d32fc83501a2b73095ae59675e27ace5d11fb31e19443b78e49341ba3e766"
                ),
                "fixtures/invalid/unknown-field.json": (
                    "35f1f21562e237cce722f5a1f93723f00d927d07769f8b12174d3ff9f73d5e3d"
                ),
            },
        },
        data_root=REPO_ROOT / "src/mc_remote_stack/data",
    )
    validate_runtime_config(scratch_config, schema)


SANDBOXES_V2_PORTS_SOURCE = f"""[[sandboxes]]
id = "lts"
hostname = "unity-sb-lts.mc-remote.example"
tcp_port = 25585
image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox:v2321.2.0@{LTS_DIGEST}"

[[sandboxes]]
id = "latest"
hostname = "unity-sb.mc-remote.example"
tcp_port = 25586
image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox:2400.0.0rc1@{LATEST_DIGEST}"
"""


def test_unity_sandboxes_v2_parser_accepts_optional_tcp_port(tmp_path: Path) -> None:
    source = (
        f"{SANDBOXES_V2_PORTS_SOURCE}"
        '\n[[sandboxes]]\n'
        'id = "internal-only"\n'
        'hostname = "unity-internal.mc-remote.example"\n'
        f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LATEST_DIGEST}"\n'
    ).encode()

    parsed = _parse_unity_sandboxes(
        tmp_path / "sandboxes.toml", source, allow_tcp_port=True
    )

    assert parsed == {
        "sandboxes": [
            {
                "id": "internal-only",
                "hostname": "unity-internal.mc-remote.example",
                "aliases": [],
                "image": f"ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LATEST_DIGEST}",
            },
            {
                "id": "latest",
                "hostname": "unity-sb.mc-remote.example",
                "aliases": [],
                "tcp_port": 25586,
                "image": f"ghcr.io/naohiro2g/mc-remote-unity-sandbox:2400.0.0rc1@{LATEST_DIGEST}",
            },
            {
                "id": "lts",
                "hostname": "unity-sb-lts.mc-remote.example",
                "aliases": [],
                "tcp_port": 25585,
                "image": f"ghcr.io/naohiro2g/mc-remote-unity-sandbox:v2321.2.0@{LTS_DIGEST}",
            },
        ]
    }


@pytest.mark.parametrize(
    ("source", "allow_tcp_port"),
    [
        (
            '[[sandboxes]]\nid = "lts"\nhostname = "unity-sb-lts.mc-remote.example"\n'
            f'tcp_port = 25585\nimage = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LTS_DIGEST}"\n',
            False,
        ),
        (
            '[[sandboxes]]\nid = "lts"\nhostname = "unity-sb-lts.mc-remote.example"\n'
            f'tcp_port = true\nimage = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LTS_DIGEST}"\n',
            True,
        ),
        (
            '[[sandboxes]]\nid = "lts"\nhostname = "unity-sb-lts.mc-remote.example"\n'
            f'tcp_port = 0\nimage = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LTS_DIGEST}"\n',
            True,
        ),
        (
            '[[sandboxes]]\nid = "lts"\nhostname = "unity-sb-lts.mc-remote.example"\n'
            f'tcp_port = 65536\nimage = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LTS_DIGEST}"\n',
            True,
        ),
        (
            SANDBOXES_V2_PORTS_SOURCE.replace("tcp_port = 25586", "tcp_port = 25585"),
            True,
        ),
        (
            '[[sandboxes]]\nid = "lts"\nhostname = "unity-sb-lts.mc-remote.example"\n'
            f'tcp_port = 80\nimage = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LTS_DIGEST}"\n',
            True,
        ),
        (
            '[[sandboxes]]\nid = "lts"\nhostname = "unity-sb-lts.mc-remote.example"\n'
            f'tcp_port = 443\nimage = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LTS_DIGEST}"\n',
            True,
        ),
        (
            '[[sandboxes]]\nid = "lts"\nhostname = "unity-sb-lts.mc-remote.example"\n'
            f'tcp_port = 19132\nimage = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LTS_DIGEST}"\n',
            True,
        ),
    ],
    ids=[
        "v1_rejects_tcp_port",
        "bool_tcp_port",
        "zero_tcp_port",
        "overflow_tcp_port",
        "duplicate_tcp_port",
        "reserved_80",
        "reserved_443",
        "reserved_19132",
    ],
)
def test_unity_sandboxes_tcp_port_rejects_invalid_values(
    tmp_path: Path, source: str, allow_tcp_port: bool
) -> None:
    with pytest.raises(OperatorInputError) as exc_info:
        _parse_unity_sandboxes(
            tmp_path / "sandboxes.toml",
            source.encode("utf-8"),
            allow_tcp_port=allow_tcp_port,
        )
    assert exc_info.value.reason == "operator_input_parse_failed"


@pytest.mark.parametrize("colliding_port", [25565, 25575])
def test_vps_server_20_resolver_rejects_minecraft_port_collision(
    tmp_path: Path, colliding_port: int
) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@20", minecraft_heap="3G")
    bad_source = (
        '[[sandboxes]]\n'
        'id = "lts"\n'
        'hostname = "unity-sb-lts.mc-remote.example"\n'
        f"tcp_port = {colliding_port}\n"
        f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LTS_DIGEST}"\n'
    )
    with pytest.raises(ResolutionError) as exc_info:
        _attach_unity_sandboxes(
            fixture,
            sandboxes_source=bad_source,
            adapter="unity-sandboxes@2",
        )
    assert exc_info.value.reason == "port_conflict"


def test_vps_server_20_renders_external_and_internal_unity_tcp_ports(tmp_path: Path) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@20", minecraft_heap="3G")
    source = (
        f"{SANDBOXES_V2_PORTS_SOURCE}"
        '\n[[sandboxes]]\n'
        'id = "internal"\n'
        'hostname = "unity-internal.mc-remote.example"\n'
        f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LATEST_DIGEST}"\n'
    )
    targets = FOUR_TARGETS_SOURCE.replace(
        "[[notices]]",
        (
            '[[targets]]\nid = "unity-internal"\nlabel = "内部"\n'
            'sandbox = "unity-internal.mc-remote.example"\n\n[[notices]]'
        ),
    )
    _attach_unity_sandboxes(
        fixture,
        sandboxes_source=source,
        targets_source=targets,
        adapter="unity-sandboxes@2",
    )

    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)
    lock = load_lock(fixture.project, data_root=fixture.data_root)
    assert lock["render_plan"]["adapter_revision"] == "22"

    compose = yaml.safe_load((fixture.output / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]

    lts = services["unity-sandbox-lts"]
    assert lts["command"] == ["--tcp-port", "25575"]
    assert lts["ports"] == ["0.0.0.0:25585:25575/tcp"]
    assert lts["networks"] == {"app": {"aliases": ["unity-sb-lts.mc-remote.example"]}, "edge": {}}
    assert lts["labels"]["io.mc-remote.unity-sandbox-tcp-port"] == "25585"
    _assert_unity_isolation_limits(lts)

    latest = services["unity-sandbox-latest"]
    assert latest["command"] == ["--tcp-port", "25575"]
    assert latest["ports"] == ["0.0.0.0:25586:25575/tcp"]
    assert latest["networks"] == {"app": {"aliases": ["unity-sb.mc-remote.example"]}, "edge": {}}
    assert latest["labels"]["io.mc-remote.unity-sandbox-tcp-port"] == "25586"
    _assert_unity_isolation_limits(latest)

    internal = services["unity-sandbox-internal"]
    assert internal["command"] == ["--tcp-port", "25575"]
    assert "ports" not in internal
    assert internal["networks"] == {"app": {"aliases": ["unity-internal.mc-remote.example"]}}
    assert "io.mc-remote.unity-sandbox-tcp-port" not in internal["labels"]
    _assert_unity_isolation_limits(internal)


class _SequentialSocket:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.sent = b""

    def __enter__(self) -> _SequentialSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def sendall(self, value: bytes) -> None:
        self.sent += value

    def recv(self, _size: int) -> bytes:
        value, self.response = self.response, b""
        return value


def test_probe_unity_sandbox_tcp_auth_handles_v23_v24_and_failures() -> None:
    sent_requests: list[dict] = []

    def v23_connector(address: tuple[str, int], timeout: int) -> _SequentialSocket:
        assert address == ("0.0.0.0", 25585)
        assert timeout == 5
        sock = _SequentialSocket(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "error": {
                            "code": -32000,
                            "message": "auth required",
                            "data": {"reason": "auth_required"},
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        orig_sendall = sock.sendall

        def capture(v: bytes) -> None:
            orig_sendall(v)
            sent_requests.append(json.loads(v))

        sock.sendall = capture  # type: ignore[method-assign]
        return sock

    probe_unity_sandbox_tcp_auth("0.0.0.0", 25585, 5, connector=v23_connector)
    assert sent_requests == [
        {"jsonrpc": "2.0", "id": 1, "method": "hello", "params": {"protocol": "23.3.0"}}
    ]

    v24_sent: list[dict] = []
    v24_responses = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32000,
                "message": "protocol mismatch",
                "data": {
                    "reason": "protocol_mismatch",
                    "server": "24.0.0",
                    "client_requires": "23.3.0",
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32000,
                "message": "auth required",
                "data": {"reason": "auth_required"},
            },
        },
    ]

    def v24_connector(_address: tuple[str, int], _timeout: int) -> _SequentialSocket:
        payload = v24_responses.pop(0)
        sock = _SequentialSocket((json.dumps(payload) + "\n").encode())
        orig_sendall = sock.sendall

        def capture(v: bytes) -> None:
            orig_sendall(v)
            v24_sent.append(json.loads(v))

        sock.sendall = capture  # type: ignore[method-assign]
        return sock

    probe_unity_sandbox_tcp_auth("0.0.0.0", 25586, 5, connector=v24_connector)
    assert v24_sent == [
        {"jsonrpc": "2.0", "id": 1, "method": "hello", "params": {"protocol": "23.3.0"}},
        {"jsonrpc": "2.0", "id": 1, "method": "hello", "params": {"protocol": "24.0.0"}},
    ]

    def unauth_connector(_address: tuple[str, int], _timeout: int) -> _SequentialSocket:
        return _SequentialSocket(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"protocol": "23.3.0"},
                    }
                )
                + "\n"
            ).encode()
        )

    with pytest.raises(DoctorContractError) as exc_info:
        probe_unity_sandbox_tcp_auth("0.0.0.0", 25585, 5, connector=unauth_connector)
    assert exc_info.value.reason == "doctor_auth_not_enforced"

    def unreachable_connector(_address: tuple[str, int], _timeout: int) -> _SequentialSocket:
        raise OSError("connection refused")

    with pytest.raises(DoctorContractError) as exc_info:
        probe_unity_sandbox_tcp_auth("0.0.0.0", 25585, 5, connector=unreachable_connector)
    assert exc_info.value.reason == "doctor_unity_sandbox_tcp_unavailable"


def test_doctor_validates_vps_server_20_unity_tcp_ports_and_probes(tmp_path: Path) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@20", minecraft_heap="3G")
    _attach_unity_sandboxes(
        fixture,
        sandboxes_source=SANDBOXES_V2_PORTS_SOURCE,
        adapter="unity-sandboxes@2",
    )
    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)
    lock = load_lock(fixture.project, data_root=fixture.data_root)
    containers = rendered_containers(fixture.output)
    from .test_toml_apply import FakeDocker

    probed_tcp: list[tuple[str, int, int]] = []
    probes = _vps_public_probes(fixture.output)

    result = doctor_toml_project(
        fixture.project,
        fixture.output,
        docker_context="default",
        data_root=fixture.data_root,
        runner=FakeDocker(_vps_doctor_responses(fixture.output, lock, containers=containers)),
        hello_probe=lambda *_args, **_kwargs: ProtocolHelloResult("auth-required", None, None),
        unity_health_probe=lambda *_args, **_kwargs: None,
        unity_tcp_probe=lambda address, port, timeout: probed_tcp.append((address, port, timeout)),
        **probes,
    )

    assert result.unity_sandbox_status == "healthy"
    assert result.unity_sandbox_count == 2
    assert probed_tcp == [("0.0.0.0", 25586, 5), ("0.0.0.0", 25585, 5)]

    bad_containers = rendered_containers(fixture.output)
    for container in bad_containers:
        if container["Config"]["Labels"]["com.docker.compose.service"] == "unity-sandbox-lts":
            container["NetworkSettings"]["Ports"] = {}

    with pytest.raises(DoctorContractError) as exc_info:
        doctor_toml_project(
            fixture.project,
            fixture.output,
            docker_context="default",
            data_root=fixture.data_root,
            runner=FakeDocker(_vps_doctor_responses(fixture.output, lock, containers=bad_containers)),
            hello_probe=lambda *_args, **_kwargs: ProtocolHelloResult("auth-required", None, None),
            unity_health_probe=lambda *_args, **_kwargs: None,
            unity_tcp_probe=lambda *_args, **_kwargs: None,
            **probes,
        )
    assert exc_info.value.reason == "doctor_network_mismatch"


def test_deployment_update_upgrades_vps_server_19_to_20_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@19", minecraft_heap="3G")
    _attach_unity_sandboxes(fixture)
    _copy_profile(fixture, "20")
    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)
    source_lock = load_lock(fixture.project, data_root=fixture.data_root)
    source_containers = rendered_containers(fixture.output)

    sandboxes_v2_input = tmp_path / "reviewed-sandboxes-v2.toml"
    sandboxes_v2_input.write_text(SANDBOXES_V2_PORTS_SOURCE, encoding="utf-8")

    up_services: list[list[str]] = []
    candidate_state: dict[str, object] = {}

    def runner(command: list[str], timeout: int):
        del timeout
        cmd = tuple(command)
        if cmd == ("docker", "context", "inspect", "default"):
            return _result(
                cmd,
                stdout=json.dumps([{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}]) + "\n",
            )
        if cmd == ("docker", "--context", "default", "version", "--format", "{{.Server.Version}}"):
            return _result(cmd, stdout="29.1.3\n")
        if cmd == ("docker", "--context", "default", "compose", "version", "--short"):
            return _result(cmd, stdout="2.40.3\n")
        if cmd[:4] == ("docker", "--context", "default", "ps"):
            current = candidate_state.get("active_containers", source_containers)
            return _result(cmd, stdout="".join(f"{c['Id']}\n" for c in current))
        if cmd[:4] == ("docker", "--context", "default", "inspect"):
            cid = cmd[4]
            current = candidate_state.get("active_containers", source_containers)
            for c in current:
                if c["Id"] == cid:
                    return _result(cmd, stdout=json.dumps([c]) + "\n")
            raise AssertionError(f"unknown container id {cid}")
        if cmd[:5] == ("docker", "--context", "default", "volume", "inspect"):
            vol = cmd[5]
            return _result(
                cmd,
                stdout=json.dumps(
                    [
                        {
                            "Name": vol,
                            "Driver": "local",
                            "Labels": {
                                "io.mc-remote.owner": "mcrctl",
                                "io.mc-remote.deployment": source_lock["deployment"]["name"],
                                "io.mc-remote.environment": source_lock["environment"]["identity"],
                                "io.mc-remote.world": source_lock["world"]["identity"],
                                "io.mc-remote.created-by-lock": source_lock["lock_identity"],
                            },
                        }
                    ]
                )
                + "\n",
            )
        if "compose" in cmd and "config" in cmd:
            if "--format" in cmd and "json" in cmd:
                compose_path = Path(cmd[cmd.index("--file") + 1])
                compose_obj = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
                for service in compose_obj.get("services", {}).values():
                    for volume in service.get("volumes", []):
                        if isinstance(volume, dict) and volume.get("type") == "bind":
                            src = Path(volume["source"])
                            if not src.is_absolute():
                                volume["source"] = str((compose_path.parent / src).resolve())
                return _result(cmd, stdout=json.dumps(compose_obj))
            return _result(cmd)
        if "compose" in cmd and ("pull" in cmd or "down" in cmd):
            return _result(cmd)
        if "compose" in cmd and "up" in cmd:
            never_idx = cmd.index("never", cmd.index("up"))
            up_services.append(list(cmd[never_idx + 1 :]))
            candidate_state["active_containers"] = rendered_containers(fixture.output)
            return _result(cmd)
        raise AssertionError(f"unexpected command: {command!r}")

    def fake_scratch_probe(url: str, timeout: int):
        del timeout
        host = url.split("/")[2]
        pair_file = fixture.output / "runtime/web-pairs" / f"{host}.json"
        if pair_file.exists():
            return json.loads(pair_file.read_text(encoding="utf-8"))
        return json.loads((fixture.output / "runtime/scratch.json").read_text(encoding="utf-8"))

    monkeypatch.setattr("mc_remote_stack.doctor.probe_homepage_public", lambda *_a, **_k: None)
    monkeypatch.setattr("mc_remote_stack.doctor.probe_scratch_runtime_config", fake_scratch_probe)
    monkeypatch.setattr("mc_remote_stack.doctor.probe_wirescope_public_handoff", lambda *_a, **_k: None)
    monkeypatch.setattr("mc_remote_stack.doctor.probe_unity_sandbox_health", lambda *_a, **_k: None)
    tcp_calls: list[int] = []
    fail_tcp_once = {"fail": True}

    def fake_tcp_probe(address: str, port: int, timeout: int) -> None:
        del address, timeout
        tcp_calls.append(port)
        if fail_tcp_once["fail"]:
            raise DoctorContractError("doctor_unity_sandbox_tcp_unavailable", f"0.0.0.0:{port}", "blocked")

    monkeypatch.setattr("mc_remote_stack.doctor.probe_unity_sandbox_tcp_auth", fake_tcp_probe)

    plan = plan_deployment_update(
        fixture.project,
        fixture.output,
        target_profile="vps-server@20",
        target_preset=PRESET,
        input_overrides={},
        input_files={"unity-sandboxes": sandboxes_v2_input},
        docker_context="default",
        data_root=fixture.data_root,
        allow_unverified=True,
        runner=runner,
        hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
    )

    with pytest.raises(DeploymentUpdateContractError) as exc_info:
        apply_deployment_update(
            fixture.project,
            plan_id=plan.plan_id,
            confirmed=True,
            data_root=fixture.data_root,
            runner=runner,
            hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
        )
    assert exc_info.value.reason == "doctor_unity_sandbox_tcp_unavailable"
    assert len(up_services) == 2

    fail_tcp_once["fail"] = False
    tcp_calls.clear()
    result = apply_deployment_update(
        fixture.project,
        plan_id=plan.plan_id,
        confirmed=True,
        data_root=fixture.data_root,
        runner=runner,
        hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
    )
    assert result.phase == "complete"
    assert sorted(tcp_calls) == [25585, 25586]
    assert load_lock(fixture.project, data_root=fixture.data_root)["render_plan"]["adapter_revision"] == "22"


def test_compose_v22_preserves_render_manifest_order_with_extra_web_pair(tmp_path: Path) -> None:
    fixture = build_vps_fixture(tmp_path, profile="vps-server@20", minecraft_heap="3G")
    _attach_unity_sandboxes(
        fixture,
        sandboxes_source=SANDBOXES_V2_PORTS_SOURCE,
        adapter="unity-sandboxes@2",
    )

    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)
    manifest = json.loads((fixture.output / "render-manifest.json").read_text(encoding="utf-8"))
    paths = [entry["path"] for entry in manifest["files"]]

    assert paths.index("runtime/scratch.json") < paths.index("minecraft/server.properties")
    assert paths.index("minecraft/server.properties") < paths.index(
        "minecraft/plugins/McRemote/config.yml"
    )
    assert paths.index("minecraft/plugins/McRemote/config.yml") < paths.index(
        "runtime/web-pairs/scratch.mc-remote.example.json"
    )
