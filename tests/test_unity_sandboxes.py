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
)
from mc_remote_stack.operator_inputs import (
    OperatorInputError,
    _parse_unity_sandboxes,
)
from mc_remote_stack.render import RenderContractError, render_toml_project
from mc_remote_stack.resolver import load_lock, resolve_project
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


def _copy_profile_19(fixture: VpsFixture) -> None:
    target = fixture.data_root / "profiles/vps-server/19/profile.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        files("mc_remote_stack")
        .joinpath("data/profiles/vps-server/19/profile.toml")
        .read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def _attach_unity_sandboxes(
    fixture: VpsFixture,
    sandboxes_source: str = SANDBOXES_TWO_SOURCE,
    targets_source: str = FOUR_TARGETS_SOURCE,
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
            'adapter = "unity-sandboxes@1"\n'
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
                "image": f"ghcr.io/naohiro2g/mc-remote-unity-sandbox@{LATEST_DIGEST}",
            }
        ]
    }

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


def test_standalone_unity_sandbox_example_is_valid_yaml_and_matches_scratch_schema() -> None:
    example_dir = REPO_ROOT / "examples/unity-sandbox"
    compose = yaml.safe_load((example_dir / "compose.yaml").read_text(encoding="utf-8"))
    assert "ports" not in compose["services"]["unity-sandbox"]
    assert compose["services"]["unity-sandbox"]["networks"] == {
        "app": {"aliases": ["unity-sb.example.org"]}
    }

    scratch_config = json.loads(
        (example_dir / "runtime/scratch.json").read_text(encoding="utf-8")
    )
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
