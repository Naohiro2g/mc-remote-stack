"""Tests for vps-server@22 / compose@24 / unity-sandboxes@3 (.NET箱庭 & Unity Webアプリ naming)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import ssl
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest
import yaml

from mc_remote_stack.deployment_update import (
    DeploymentUpdateContractError,
    apply_deployment_update,
    plan_deployment_update,
)
from mc_remote_stack.doctor import (
    DoctorContractError,
    ProtocolHelloResult,
    doctor_toml_project,
    probe_unity_sandbox_redirect,
    probe_unity_sandbox_web,
    probe_unity_sandbox_ws_sync,
)
from mc_remote_stack.operator_inputs import (
    UNITY_SANDBOXES_PATH,
    UNITY_SANDBOXES_V2_ADAPTER,
    UNITY_SANDBOXES_V3_ADAPTER,
    OperatorInputError,
    _parse_adapter,
)
from mc_remote_stack.preset_registry import (
    load_profile,
    validate_bundled_schema,
)
from mc_remote_stack.render import RenderContractError, render_toml_project
from mc_remote_stack.resolver import ResolutionError, load_lock, resolve_project

from .test_doctor import _vps_doctor_responses
from .test_toml_apply import FakeDocker
from .test_vps_dual_line_and_external_targets import (
    FIRST_RESOLVED_AT,
    PRODUCTION_LTS_WEB,
    PRODUCTION_ROUTES,
    UNITY_LATEST_DIGEST,
    UNITY_LTS_DIGEST,
    _copy_bundled_profile,
    _setup_dual_line_data_root,
)
from .vps_fixture import PLUGIN_BYTES, _sha256, rendered_containers


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

SANDBOXES_V3_SOURCE = f"""[[sandboxes]]
id = "lts"
hostname = "unity-app-lts.mc-remote.example"
aliases = ["unity-sb-lts.mc-remote.example"]
redirect_aliases = ["dotnet-sb-lts.mc-remote.example"]
tcp_port = 25585
image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox:v2321.2.0@{UNITY_LTS_DIGEST}"

[[sandboxes]]
id = "latest"
hostname = "unity-app.mc-remote.example"
aliases = ["unity-sb.mc-remote.example"]
redirect_aliases = ["dotnet-sb.mc-remote.example"]
tcp_port = 25586
image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox:2400.0.0rc1@{UNITY_LATEST_DIGEST}"
"""

DEC1_PRODUCTION_TARGETS = """[[targets]]
id = "stable"
label = "マイクラ箱庭サーバー"
host = "sb.mc-remote.example"
port = 25575
external = false

[[targets]]
id = "dotnet-sb"
label = ".NET箱庭サーバー"
host = "dotnet-sb.mc-remote.example"
port = 25575
external = false

[[targets]]
id = "lts"
label = "マイクラ箱庭サーバー（LTS）"
host = "sb-lts.mc-remote.example"
port = 25575
external = true

[[targets]]
id = "dotnet-sb-lts"
label = ".NET箱庭サーバー（LTS）"
host = "dotnet-sb-lts.mc-remote.example"
port = 25575
external = false

[[notices]]
heading = "お知らせ"
body = "production notice"

[[selections]]
scratch = "scratch.mc-remote.example"
target_ids = ["stable", "dotnet-sb"]
default_target = "stable"

[[selections]]
scratch = "scratch-beta.mc-remote.example"
target_ids = ["stable", "dotnet-sb"]
default_target = "stable"
"""

DEC1_PRODUCTION_LTS_WEB = """preset = "fixture-web-paper@1"
scratch = "scratch-lts.mc-remote.example"
bridge = "bridge-lts.mc-remote.example"
wirescope = "wirescope-lts.mc-remote.example"
target_ids = ["lts", "dotnet-sb-lts"]
default_target = "lts"
"""


def _setup_v22_data_root(tmp_path: Path) -> tuple[Path, Path, Path]:
    data_root, store, backup_dir = _setup_dual_line_data_root(tmp_path)
    _copy_bundled_profile(data_root, "22")
    return data_root, store, backup_dir


def _vps_doctor_probes(output: Path) -> dict[str, object]:
    def fake_scratch_probe(url: str, timeout: int):
        del timeout
        host = url.split("/")[2]
        if host == "scratch-lts.mc-remote.example":
            return json.loads((output / "runtime/scratch-lts.json").read_text(encoding="utf-8"))
        pair_file = output / "runtime/web-pairs" / f"{host}.json"
        if pair_file.exists():
            return json.loads(pair_file.read_text(encoding="utf-8"))
        return json.loads((output / "runtime/scratch.json").read_text(encoding="utf-8"))

    return {
        "homepage_probe": lambda *_a, **_k: None,
        "scratch_runtime_probe": fake_scratch_probe,
        "wirescope_probe": lambda *_a, **_k: None,
    }


def _build_vps22_fixture(
    tmp_path: Path,
    *,
    profile: str = "vps-server@22",
    sandboxes_adapter: str = "unity-sandboxes@3",
    sandboxes_source: str | None = SANDBOXES_V3_SOURCE,
    routes_source: str = PRODUCTION_ROUTES,
    targets_source: str = DEC1_PRODUCTION_TARGETS,
    lts_web_source: str | None = DEC1_PRODUCTION_LTS_WEB,
    resolve_now: bool = True,
) -> SimpleNamespace:
    data_root, store, backup_dir = _setup_v22_data_root(tmp_path)
    project = tmp_path / "project"
    output = project / "generated"
    _write_file(project / "operator/public-routes/routes.toml", routes_source)
    _write_file(
        project / "operator/minecraft-server/server.toml",
        (
            "allow_flight = true\n"
            'difficulty = "normal"\n'
            "enable_query = false\n"
            "enable_status = true\n"
            "force_gamemode = false\n"
            'gamemode = "creative"\n'
            "hardcore = false\n"
            "log_ips = false\n"
            "management_server_enabled = false\n"
            "max_players = 20\n"
            "max_tick_time = 60000\n"
            "max_world_size = 29999984\n"
            'motd = "McRemote public"\n'
            "network_compression_threshold = 256\n"
            "simulation_distance = 10\n"
            "spawn_protection = 0\n"
            "view_distance = 10\n"
            "white_list = false\n"
        ),
    )
    _write_file(project / "operator/connection-targets/targets.toml", targets_source)
    _write_file(
        project / "operator/minecraft-plugins/plugins.toml",
        f'[[plugins]]\nfilename = "LuckPerms-Bukkit.jar"\nsha256 = "{_sha256(PLUGIN_BYTES)}"\n',
    )
    homepage_meta = json.loads((store.parent / "homepage-meta.json").read_text(encoding="utf-8"))
    _write_file(
        project / "operator/homepage-static/homepage.toml",
        (
            f'tree_sha256 = "{homepage_meta["tree_sha256"]}"\n'
            f'file_count = {homepage_meta["file_count"]}\n'
            f'total_bytes = {homepage_meta["total_bytes"]}\n'
        ),
    )
    _write_file(
        project / "operator/minecraft-backup/backup.toml",
        f'host_path = "{backup_dir}"\n',
    )
    operator_inputs = [
        {
            "role": "public-routes",
            "adapter": "public-routes@3",
            "path": "operator/public-routes/routes.toml",
        },
        {
            "role": "minecraft-server",
            "adapter": "minecraft-server@1",
            "path": "operator/minecraft-server/server.toml",
        },
        {
            "role": "connection-targets",
            "adapter": "connection-targets@5",
            "path": "operator/connection-targets/targets.toml",
        },
        {
            "role": "minecraft-plugins",
            "adapter": "minecraft-plugins@1",
            "path": "operator/minecraft-plugins/plugins.toml",
        },
        {
            "role": "homepage-static",
            "adapter": "homepage-static@1",
            "path": "operator/homepage-static/homepage.toml",
        },
        {
            "role": "minecraft-backup",
            "adapter": "minecraft-backup@1",
            "path": "operator/minecraft-backup/backup.toml",
        },
    ]
    if sandboxes_source is not None:
        _write_file(project / UNITY_SANDBOXES_PATH, sandboxes_source)
        operator_inputs.append(
            {
                "role": "unity-sandboxes",
                "adapter": sandboxes_adapter,
                "path": UNITY_SANDBOXES_PATH,
            }
        )
    if lts_web_source is not None:
        _write_file(project / "operator/lts-web/lts.toml", lts_web_source)
        operator_inputs.append(
            {
                "role": "lts-web",
                "adapter": "lts-web@1",
                "path": "operator/lts-web/lts.toml",
            }
        )

    order_lines = [
        "schema_version = 1",
        "[deployment]",
        'name = "public-vps"',
        f'profile = "{profile}"',
        "[environment]",
        'identity = "public-main"',
        'channel = "stable"',
        'exposure = "public"',
        'purpose = "integration"',
        'preset = "fixture-web-paper@2"',
        "[runtime]",
        f'artifact_store = "{store}"',
        '[[runtime.volumes]]\nrole = "minecraft-data"\nidentity = "mc-data-vol"',
        '[[runtime.volumes]]\nrole = "caddy-data"\nidentity = "caddy-data-vol"',
        '[[runtime.volumes]]\nrole = "caddy-config"\nidentity = "caddy-config-vol"',
        "[world]",
        'identity = "world"',
        "[network]",
        'bind_address = "0.0.0.0"',
        "java_port = 25565",
        "mcremote_port = 25575",
        "[minecraft]",
        'heap = "3G"',
        "[agreements]",
        "minecraft_eula = true",
        "[acknowledgements]",
        "allow_unverified = true",
        "allow_eol = false",
        'unverified_reason = "dec1 naming test"',
        'eol_reason = ""',
    ]
    for item in operator_inputs:
        order_lines.extend(
            [
                "[[operator_inputs]]",
                f'role = "{item["role"]}"',
                f'adapter = "{item["adapter"]}"',
                f'path = "{item["path"]}"',
            ]
        )
    _write_file(project / "mc-remote.toml", "\n".join(order_lines) + "\n")
    if resolve_now:
        resolve_project(
            project,
            data_root=data_root,
            allow_unverified=True,
            resolved_at=FIRST_RESOLVED_AT,
        )
    return SimpleNamespace(project=project, output=output, data_root=data_root, store=store)


# ---------------------------------------------------------------------------
# 1. Profile & unity-sandboxes@3 parser tests
# ---------------------------------------------------------------------------


def test_bundled_vps_server_22_profile_matches_schema() -> None:
    profile = load_profile("vps-server@22")
    assert profile.data["profile"]["revision"] == "22"
    assert profile.data["renderer"] == {"name": "compose", "revision": "24"}
    roles = {item["id"]: item for item in profile.data["operator_input_roles"]}
    assert roles["unity-sandboxes"] == {
        "id": "unity-sandboxes",
        "adapter": "unity-sandboxes@3",
        "required": False,
    }


def test_unity_sandboxes_v3_parser_supports_redirect_aliases_and_rejects_duplicates(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "sandboxes.toml"
    source_path.write_text(
        SANDBOXES_V3_SOURCE
        + '\n[[sandboxes]]\nid = "extra"\nhostname = "extra-app.mc-remote.example"\n'
        f'redirect_aliases = ["z-dotnet.mc-remote.example", "a-dotnet.mc-remote.example"]\n'
        f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{UNITY_LATEST_DIGEST}"\n',
        encoding="utf-8",
    )
    parsed = _parse_adapter(UNITY_SANDBOXES_V3_ADAPTER, source_path, UNITY_SANDBOXES_PATH)
    by_id = {entry["id"]: entry for entry in parsed["sandboxes"]}
    assert by_id["latest"]["redirect_aliases"] == ["dotnet-sb.mc-remote.example"]
    assert by_id["lts"]["redirect_aliases"] == ["dotnet-sb-lts.mc-remote.example"]
    assert by_id["extra"]["redirect_aliases"] == [
        "a-dotnet.mc-remote.example",
        "z-dotnet.mc-remote.example",
    ]

    # Omitting redirect_aliases defaults to [] so existing sandboxes.toml files upgrade smoothly.
    omitted_path = tmp_path / "omitted.toml"
    omitted_path.write_text(
        f'[[sandboxes]]\nid = "latest"\nhostname = "unity-app.mc-remote.example"\n'
        f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{UNITY_LATEST_DIGEST}"\n',
        encoding="utf-8",
    )
    omitted_parsed = _parse_adapter(
        UNITY_SANDBOXES_V3_ADAPTER, omitted_path, UNITY_SANDBOXES_PATH
    )
    assert omitted_parsed["sandboxes"][0]["redirect_aliases"] == []

    # unity-sandboxes@2 must still reject redirect_aliases.
    with pytest.raises(OperatorInputError) as v2_exc:
        _parse_adapter(UNITY_SANDBOXES_V2_ADAPTER, source_path, UNITY_SANDBOXES_PATH)
    assert v2_exc.value.reason == "operator_input_parse_failed"

    # Duplicate hostname across hostname/aliases/redirect_aliases or across sandboxes fails closed.
    bad_cases = [
        (
            '[[sandboxes]]\nid = "latest"\nhostname = "unity-app.mc-remote.example"\n'
            'redirect_aliases = ["unity-app.mc-remote.example"]\n'
            f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{UNITY_LATEST_DIGEST}"\n'
        ),
        (
            '[[sandboxes]]\nid = "latest"\nhostname = "unity-app.mc-remote.example"\n'
            'aliases = ["dotnet-sb.mc-remote.example"]\n'
            'redirect_aliases = ["dotnet-sb.mc-remote.example"]\n'
            f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{UNITY_LATEST_DIGEST}"\n'
        ),
        (
            '[[sandboxes]]\nid = "lts"\nhostname = "unity-app-lts.mc-remote.example"\n'
            'redirect_aliases = ["dotnet-sb.mc-remote.example"]\n'
            f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{UNITY_LTS_DIGEST}"\n'
            '[[sandboxes]]\nid = "latest"\nhostname = "unity-app.mc-remote.example"\n'
            'redirect_aliases = ["dotnet-sb.mc-remote.example"]\n'
            f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{UNITY_LATEST_DIGEST}"\n'
        ),
        (
            '[[sandboxes]]\nid = "latest"\nhostname = "unity-app.mc-remote.example"\n'
            'redirect_aliases = ["127.0.0.1"]\n'
            f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{UNITY_LATEST_DIGEST}"\n'
        ),
        (
            '[[sandboxes]]\nid = "latest"\nhostname = "unity-app.mc-remote.example"\n'
            'redirect_aliases = ["secret://vault/host"]\n'
            f'image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox@{UNITY_LATEST_DIGEST}"\n'
        ),
    ]
    for idx, content in enumerate(bad_cases):
        bad_path = tmp_path / f"bad-{idx}.toml"
        bad_path.write_text(content, encoding="utf-8")
        with pytest.raises(OperatorInputError):
            _parse_adapter(UNITY_SANDBOXES_V3_ADAPTER, bad_path, UNITY_SANDBOXES_PATH)


# ---------------------------------------------------------------------------
# 2. Resolver, lock schema, and render (compose@24) tests
# ---------------------------------------------------------------------------


def test_vps22_resolver_validates_lock_schema_and_rejects_redirect_collisions(
    tmp_path: Path,
) -> None:
    fixture = _build_vps22_fixture(tmp_path / "valid")
    lock = load_lock(fixture.project, data_root=fixture.data_root)
    validate_bundled_schema(
        lock,
        "lock.schema.json",
        source=fixture.project / "mc-remote.lock.toml",
    )
    assert lock["render_plan"]["adapter_revision"] == "24"

    # redirect_aliases colliding with lts-web hostnames must fail closed.
    colliding_lts = SANDBOXES_V3_SOURCE.replace(
        'redirect_aliases = ["dotnet-sb-lts.mc-remote.example"]',
        'redirect_aliases = ["scratch-lts.mc-remote.example"]',
    )
    with pytest.raises(ResolutionError) as lts_exc:
        _build_vps22_fixture(tmp_path / "lts-collision", sandboxes_source=colliding_lts)
    assert lts_exc.value.reason == "operator_input_parse_failed"

    # redirect_aliases colliding with public-routes must fail closed on render.
    colliding_public = SANDBOXES_V3_SOURCE.replace(
        'redirect_aliases = ["dotnet-sb.mc-remote.example"]',
        'redirect_aliases = ["sb.mc-remote.example"]',
    )
    fix_pub = _build_vps22_fixture(
        tmp_path / "public-collision",
        sandboxes_source=colliding_public,
    )
    with pytest.raises(RenderContractError) as pub_exc:
        render_toml_project(fix_pub.project, fix_pub.output, data_root=fix_pub.data_root)
    assert pub_exc.value.reason == "render_plan_invalid"


def test_compose24_renders_dec1_naming_caddy_302_redirects_and_preserves_containers(
    tmp_path: Path,
) -> None:
    fixture = _build_vps22_fixture(tmp_path)
    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)

    compose = yaml.safe_load((fixture.output / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]

    # Container names, ports, and isolation limits remain unchanged for Dec 1.
    assert set(services) == {
        "caddy",
        "scratch",
        "bridge",
        "minecraft",
        "scratch-lts",
        "bridge-lts",
        "unity-sandbox-lts",
        "unity-sandbox-latest",
    }
    latest_svc = services["unity-sandbox-latest"]
    assert latest_svc["ports"] == ["0.0.0.0:25586:25575/tcp"]
    assert latest_svc["networks"] == {
        "app": {
            "aliases": [
                "unity-app.mc-remote.example",
                "unity-sb.mc-remote.example",
                "dotnet-sb.mc-remote.example",
            ]
        },
        "edge": {},
    }
    lts_svc = services["unity-sandbox-lts"]
    assert lts_svc["ports"] == ["0.0.0.0:25585:25575/tcp"]
    assert lts_svc["networks"] == {
        "app": {
            "aliases": [
                "unity-app-lts.mc-remote.example",
                "unity-sb-lts.mc-remote.example",
                "dotnet-sb-lts.mc-remote.example",
            ]
        },
        "edge": {},
    }

    # Minecraft app aliases must NOT absorb dotnet-sb* or unity-app* or unity-sb*.
    assert services["minecraft"]["networks"]["app"]["aliases"] == ["sb.mc-remote.example"]

    # Caddyfile serves unity-app* + unity-sb* via reverse_proxy and 302-redirects dotnet-sb*.
    caddyfile = (fixture.output / "Caddyfile").read_text(encoding="utf-8")
    assert (
        "unity-app-lts.mc-remote.example, unity-sb-lts.mc-remote.example {\n"
        "    reverse_proxy unity-sandbox-lts:8080\n"
        "}" in caddyfile
    )
    assert (
        "dotnet-sb-lts.mc-remote.example {\n"
        "    redir https://unity-app-lts.mc-remote.example{uri} 302\n"
        "}" in caddyfile
    )
    assert (
        "unity-app.mc-remote.example, unity-sb.mc-remote.example {\n"
        "    reverse_proxy unity-sandbox-latest:8080\n"
        "}" in caddyfile
    )
    assert (
        "dotnet-sb.mc-remote.example {\n"
        "    redir https://unity-app.mc-remote.example{uri} 302\n"
        "}" in caddyfile
    )

    # Scratch runtime configs and bridge targets use the new Dec 1 names/IDs/labels.
    scratch_json = json.loads((fixture.output / "runtime/scratch.json").read_text(encoding="utf-8"))
    assert scratch_json["schema_version"] == 2
    assert scratch_json["default_target_id"] == "stable"
    assert scratch_json["connection_targets"] == [
        {
            "id": "stable",
            "label": "マイクラ箱庭サーバー",
            "host": "sb.mc-remote.example",
            "port": 25575,
        },
        {
            "id": "dotnet-sb",
            "label": ".NET箱庭サーバー",
            "host": "dotnet-sb.mc-remote.example",
            "port": 25575,
        },
    ]
    beta_json = json.loads(
        (fixture.output / "runtime/web-pairs/scratch-beta.mc-remote.example.json").read_text(
            encoding="utf-8"
        )
    )
    assert beta_json["connection_targets"] == scratch_json["connection_targets"]
    assert beta_json["wirescope_url"] == "https://wirescope-beta.mc-remote.example/"

    bridge_targets = json.loads(
        (fixture.output / "runtime/bridge-targets.json").read_text(encoding="utf-8")
    )
    assert bridge_targets["targets"] == scratch_json["connection_targets"]

    lts_json = json.loads(
        (fixture.output / "runtime/scratch-lts.json").read_text(encoding="utf-8")
    )
    assert lts_json["schema_version"] == 1
    assert lts_json["default_sandbox"] == "sb-lts.mc-remote.example"
    assert lts_json["connection_targets"] == [
        {
            "id": "lts",
            "label": "マイクラ箱庭サーバー（LTS）",
            "sandbox": "sb-lts.mc-remote.example",
        },
        {
            "id": "dotnet-sb-lts",
            "label": ".NET箱庭サーバー（LTS）",
            "sandbox": "dotnet-sb-lts.mc-remote.example",
        },
    ]


# ---------------------------------------------------------------------------
# 3. Doctor probe unit tests (web, /ws/sync, 302 redirect, full doctor)
# ---------------------------------------------------------------------------


class _FakeHttpResponse:
    def __init__(self, *, status: int = 200, headers: dict[str, str] | None = None, body: bytes = b"") -> None:
        self.status = status
        self.headers = headers or {}
        self._body = body

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]


def test_probe_unity_sandbox_web_accepts_200_html_and_fails_closed() -> None:
    probe_unity_sandbox_web(
        "https://unity-app.mc-remote.example/",
        timeout=5,
        opener=lambda _req, *, timeout: _FakeHttpResponse(
            status=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            body=b"<!doctype html><html><body>Unity</body></html>",
        ),
    )

    # Redirect on a web-serving hostname is unhealthy.
    def redirect_opener(req, *, timeout):
        del timeout
        raise HTTPError(req.full_url, 302, "Found", {"Location": "https://other/"}, None)

    with pytest.raises(DoctorContractError) as redir_exc:
        probe_unity_sandbox_web("https://unity-app.mc-remote.example/", timeout=5, opener=redirect_opener)
    assert redir_exc.value.reason == "doctor_unity_sandbox_unhealthy"

    # Non-HTML or empty response fails closed as unhealthy.
    with pytest.raises(DoctorContractError) as json_exc:
        probe_unity_sandbox_web(
            "https://unity-app.mc-remote.example/",
            timeout=5,
            opener=lambda _req, *, timeout: _FakeHttpResponse(
                status=200,
                headers={"Content-Type": "application/json"},
                body=b'{"ok":true}',
            ),
        )
    assert json_exc.value.reason == "doctor_unity_sandbox_unhealthy"

    # HTTP 500 or transport error fails closed as unavailable.
    def error_opener(req, *, timeout):
        del timeout
        raise HTTPError(req.full_url, 502, "Bad Gateway", {}, None)

    with pytest.raises(DoctorContractError) as err_exc:
        probe_unity_sandbox_web("https://unity-app.mc-remote.example/", timeout=5, opener=error_opener)
    assert err_exc.value.reason == "doctor_unity_sandbox_unavailable"

    def timeout_opener(_req, *, timeout):
        del timeout
        raise URLError(TimeoutError("timed out"))

    with pytest.raises(DoctorContractError) as timeout_exc:
        probe_unity_sandbox_web("https://unity-app.mc-remote.example/", timeout=5, opener=timeout_opener)
    assert timeout_exc.value.reason == "doctor_unity_sandbox_unavailable"


class _FakeWsSocket:
    def __init__(
        self,
        *,
        status_line: str = "HTTP/1.1 101 Switching Protocols",
        bad_accept: bool = False,
        missing_upgrade: bool = False,
    ) -> None:
        self.sent = bytearray()
        self._status_line = status_line
        self._bad_accept = bad_accept
        self._missing_upgrade = missing_upgrade
        self._response_chunks: list[bytes] = []
        self.closed = False

    def __enter__(self) -> _FakeWsSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)
        if not self._response_chunks and b"\r\n\r\n" in self.sent:
            header_text = bytes(self.sent).split(b"\r\n\r\n", 1)[0].decode("ascii")
            ws_key = ""
            for line in header_text.split("\r\n")[1:]:
                if line.lower().startswith("sec-websocket-key:"):
                    ws_key = line.split(":", 1)[1].strip()
            accept = base64.b64encode(
                hashlib.sha1((ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
            ).decode("ascii")
            if self._bad_accept:
                accept = "wrong-accept-value=="
            headers = [self._status_line]
            if not self._missing_upgrade:
                headers.extend(["Upgrade: websocket", "Connection: Upgrade"])
            headers.append(f"Sec-WebSocket-Accept: {accept}")
            self._response_chunks.append(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))

    def recv(self, _size: int) -> bytes:
        if self._response_chunks:
            return self._response_chunks.pop(0)
        return b""


def test_probe_unity_sandbox_ws_sync_verifies_rfc6455_handshake() -> None:
    sock = _FakeWsSocket()
    probe_unity_sandbox_ws_sync(
        "unity-app.mc-remote.example",
        timeout=5,
        tls_connector=lambda host, port, timeout: sock,
    )
    assert sock.closed
    assert b"GET /ws/sync HTTP/1.1\r\n" in sock.sent
    assert b"Host: unity-app.mc-remote.example\r\n" in sock.sent
    assert b"Origin: https://unity-app.mc-remote.example\r\n" in sock.sent
    # Verify masked RFC 6455 close frame was sent cleanly after 101.
    assert sock.sent.endswith(b"\x88\x80\x00\x00\x00\x00")

    # Non-101, bad Sec-WebSocket-Accept, or missing Upgrade fail closed.
    for bad_sock in (
        _FakeWsSocket(status_line="HTTP/1.1 403 Forbidden"),
        _FakeWsSocket(bad_accept=True),
        _FakeWsSocket(missing_upgrade=True),
    ):
        with pytest.raises(DoctorContractError) as exc_info:
            probe_unity_sandbox_ws_sync(
                "unity-app.mc-remote.example",
                timeout=5,
                tls_connector=lambda *_a, _sock=bad_sock: _sock,
            )
        assert exc_info.value.reason == "doctor_unity_sandbox_ws_unhealthy"

    # TLS/socket failure reports unavailable.
    def fail_tls(*_args):
        raise ssl.SSLError("cert mismatch")

    with pytest.raises(DoctorContractError) as tls_exc:
        probe_unity_sandbox_ws_sync(
            "unity-app.mc-remote.example",
            timeout=5,
            tls_connector=fail_tls,
        )
    assert tls_exc.value.reason == "doctor_unity_sandbox_ws_unavailable"


def test_probe_unity_sandbox_redirect_verifies_exact_302_location() -> None:
    def ok_302(req, *, timeout):
        del timeout
        raise HTTPError(
            req.full_url,
            302,
            "Found",
            {"Location": "https://unity-app.mc-remote.example/"},
            io.BytesIO(b""),
        )

    probe_unity_sandbox_redirect(
        "https://dotnet-sb.mc-remote.example/",
        expected_location="https://unity-app.mc-remote.example/",
        timeout=5,
        opener=ok_302,
    )

    # 200 OK instead of 302 fails closed.
    with pytest.raises(DoctorContractError) as ok_exc:
        probe_unity_sandbox_redirect(
            "https://dotnet-sb.mc-remote.example/",
            expected_location="https://unity-app.mc-remote.example/",
            timeout=5,
            opener=lambda _req, *, timeout: _FakeHttpResponse(status=200),
        )
    assert ok_exc.value.reason == "doctor_unity_sandbox_unhealthy"

    # Wrong status (301) or wrong Location fails closed.
    for code, loc in (
        (301, "https://unity-app.mc-remote.example/"),
        (302, "https://wrong.mc-remote.example/"),
    ):
        def bad_redirect(req, *, timeout, _code=code, _loc=loc):
            del timeout
            raise HTTPError(req.full_url, _code, "Redirect", {"Location": _loc}, None)

        with pytest.raises(DoctorContractError) as bad_exc:
            probe_unity_sandbox_redirect(
                "https://dotnet-sb.mc-remote.example/",
                expected_location="https://unity-app.mc-remote.example/",
                timeout=5,
                opener=bad_redirect,
            )
        assert bad_exc.value.reason == "doctor_unity_sandbox_unhealthy"


def test_doctor_vps22_checks_web_ws_sync_redirects_and_tcp_auth(tmp_path: Path) -> None:
    fixture = _build_vps22_fixture(tmp_path)
    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)
    lock = load_lock(fixture.project, data_root=fixture.data_root)
    containers = rendered_containers(fixture.output)
    assert len(containers) == 8

    health_calls: list[str] = []
    web_calls: list[str] = []
    ws_calls: list[str] = []
    redirect_calls: list[tuple[str, str]] = []
    tcp_calls: list[int] = []
    ext_calls: list[tuple[str, int]] = []

    runner = FakeDocker(_vps_doctor_responses(fixture.output, lock, containers=containers))

    result = doctor_toml_project(
        fixture.project,
        fixture.output,
        docker_context="default",
        data_root=fixture.data_root,
        runner=runner,
        hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
        unity_health_probe=lambda url, timeout: health_calls.append(url),
        unity_web_probe=lambda url, timeout: web_calls.append(url),
        unity_ws_probe=lambda host, timeout: ws_calls.append(host),
        unity_redirect_probe=lambda url, *, expected_location, timeout: redirect_calls.append(
            (url, expected_location)
        ),
        unity_tcp_probe=lambda _addr, port, _timeout: tcp_calls.append(port),
        external_target_probe=lambda host, port, _timeout: ext_calls.append((host, port)),
        **_vps_doctor_probes(fixture.output),
    )

    assert result.unity_sandbox_status == "healthy"
    assert result.unity_sandbox_count == 2
    assert health_calls == [
        "https://unity-app.mc-remote.example/healthz",
        "https://unity-sb.mc-remote.example/healthz",
        "https://unity-app-lts.mc-remote.example/healthz",
        "https://unity-sb-lts.mc-remote.example/healthz",
    ]
    assert web_calls == [
        "https://unity-app.mc-remote.example/",
        "https://unity-sb.mc-remote.example/",
        "https://unity-app-lts.mc-remote.example/",
        "https://unity-sb-lts.mc-remote.example/",
    ]
    assert ws_calls == [
        "unity-app.mc-remote.example",
        "unity-sb.mc-remote.example",
        "unity-app-lts.mc-remote.example",
        "unity-sb-lts.mc-remote.example",
    ]
    assert redirect_calls == [
        ("https://dotnet-sb.mc-remote.example/", "https://unity-app.mc-remote.example/"),
        ("https://dotnet-sb-lts.mc-remote.example/", "https://unity-app-lts.mc-remote.example/"),
    ]
    assert sorted(tcp_calls) == [25585, 25586]
    assert ext_calls == [("sb-lts.mc-remote.example", 25575)]


# ---------------------------------------------------------------------------
# 4. In-place deployment update (vps-server@21 -> vps-server@22) + rollback
# ---------------------------------------------------------------------------


def _dynamic_v22_runner(output: Path, source_lock: dict, source_containers: list[dict]):
    state: dict[str, object] = {"containers": source_containers}

    def runner(command: list[str], timeout: int):
        del timeout
        cmd = tuple(command)
        if cmd == ("docker", "context", "inspect", "default"):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}]),
                stderr="",
            )
        if cmd == ("docker", "--context", "default", "version", "--format", "{{.Server.Version}}"):
            return SimpleNamespace(returncode=0, stdout="29.1.3\n", stderr="")
        if cmd == ("docker", "--context", "default", "compose", "version", "--short"):
            return SimpleNamespace(returncode=0, stdout="2.40.3\n", stderr="")
        if cmd[:4] == ("docker", "--context", "default", "ps"):
            return SimpleNamespace(
                returncode=0,
                stdout="".join(f"{c['Id']}\n" for c in state["containers"]),
                stderr="",
            )
        if cmd[:4] == ("docker", "--context", "default", "inspect"):
            cid = cmd[4]
            for c in state["containers"]:
                if c["Id"] == cid:
                    return SimpleNamespace(returncode=0, stdout=json.dumps([c]), stderr="")
            raise AssertionError(f"unknown container {cid}")
        if cmd[:5] == ("docker", "--context", "default", "volume", "inspect"):
            vol = cmd[5]
            return SimpleNamespace(
                returncode=0,
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
                ),
                stderr="",
            )
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
                return SimpleNamespace(returncode=0, stdout=json.dumps(compose_obj), stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "compose" in cmd and ("pull" in cmd or "down" in cmd):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "compose" in cmd and "up" in cmd:
            state["containers"] = rendered_containers(output)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {command!r}")

    return runner


def _patch_v22_doctor_probes(monkeypatch: pytest.MonkeyPatch, output: Path) -> None:
    def fake_scratch_probe(url: str, timeout: int):
        del timeout
        host = url.split("/")[2]
        if host == "scratch-lts.mc-remote.example":
            return json.loads((output / "runtime/scratch-lts.json").read_text(encoding="utf-8"))
        pair_file = output / "runtime/web-pairs" / f"{host}.json"
        if pair_file.exists():
            return json.loads(pair_file.read_text(encoding="utf-8"))
        return json.loads((output / "runtime/scratch.json").read_text(encoding="utf-8"))

    monkeypatch.setattr("mc_remote_stack.doctor.probe_homepage_public", lambda *_a, **_k: None)
    monkeypatch.setattr("mc_remote_stack.doctor.probe_scratch_runtime_config", fake_scratch_probe)
    monkeypatch.setattr("mc_remote_stack.doctor.probe_wirescope_public_handoff", lambda *_a, **_k: None)
    monkeypatch.setattr("mc_remote_stack.doctor.probe_unity_sandbox_health", lambda *_a, **_k: None)
    monkeypatch.setattr("mc_remote_stack.doctor.probe_unity_sandbox_web", lambda *_a, **_k: None)
    monkeypatch.setattr("mc_remote_stack.doctor.probe_unity_sandbox_ws_sync", lambda *_a, **_k: None)
    monkeypatch.setattr("mc_remote_stack.doctor.probe_unity_sandbox_redirect", lambda *_a, **_k: None)
    monkeypatch.setattr("mc_remote_stack.doctor.probe_unity_sandbox_tcp_auth", lambda *_a, **_k: None)
    monkeypatch.setattr("mc_remote_stack.doctor.probe_external_mcremote_auth", lambda *_a, **_k: None)


def test_deployment_update_from_vps21_to_vps22_untouched_and_full_dec1_with_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v2_sandboxes = f"""[[sandboxes]]
id = "lts"
hostname = "unity-sb-lts.mc-remote.example"
tcp_port = 25585
image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox:v2321.2.0@{UNITY_LTS_DIGEST}"

[[sandboxes]]
id = "latest"
hostname = "unity-sb.mc-remote.example"
tcp_port = 25586
image = "ghcr.io/naohiro2g/mc-remote-unity-sandbox:2400.0.0rc1@{UNITY_LATEST_DIGEST}"
"""
    # Start on vps-server@21 with unity-sandboxes@2 and untouched sandboxes.toml upgrade to @22.
    v2_targets = (
        DEC1_PRODUCTION_TARGETS.replace('host = "dotnet-sb.mc-remote.example"', 'host = "unity-sb.mc-remote.example"')
        .replace('host = "dotnet-sb-lts.mc-remote.example"', 'host = "unity-sb-lts.mc-remote.example"')
        .replace("dotnet-sb-lts", "unity-lts")
        .replace("dotnet-sb", "unity-latest")
    )
    fix = _build_vps22_fixture(
        tmp_path / "untouched",
        profile="vps-server@21",
        sandboxes_adapter="unity-sandboxes@2",
        sandboxes_source=v2_sandboxes,
        lts_web_source=PRODUCTION_LTS_WEB.replace("dotnet-sb-lts", "unity-lts"),
        targets_source=v2_targets,
    )
    render_toml_project(fix.project, fix.output, data_root=fix.data_root)
    src_lock = load_lock(fix.project, data_root=fix.data_root)
    src_containers = rendered_containers(fix.output)
    _patch_v22_doctor_probes(monkeypatch, fix.output)
    runner = _dynamic_v22_runner(fix.output, src_lock, src_containers)

    plan_untouched = plan_deployment_update(
        fix.project,
        fix.output,
        target_profile="vps-server@22",
        target_preset="fixture-web-paper@2",
        input_overrides={},
        docker_context="default",
        data_root=fix.data_root,
        allow_unverified=True,
        runner=runner,
        hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
    )
    res_untouched = apply_deployment_update(
        fix.project,
        plan_id=plan_untouched.plan_id,
        confirmed=True,
        data_root=fix.data_root,
        runner=runner,
        hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
    )
    assert res_untouched.phase == "complete"
    upgraded_lock = load_lock(fix.project, data_root=fix.data_root)
    assert upgraded_lock["render_plan"]["adapter_revision"] == "24"

    # Now update with full Dec 1 sandboxes (redirect_aliases) + targets.toml + lts.toml,
    # verifying rollback when the new redirect doctor probe fails, then clean success.
    sandboxes_v3_file = tmp_path / "sandboxes-v3.toml"
    sandboxes_v3_file.write_text(SANDBOXES_V3_SOURCE, encoding="utf-8")
    targets_file = tmp_path / "targets-dec1.toml"
    targets_file.write_text(DEC1_PRODUCTION_TARGETS, encoding="utf-8")
    lts_file = tmp_path / "lts-dec1.toml"
    lts_file.write_text(DEC1_PRODUCTION_LTS_WEB, encoding="utf-8")

    plan_dec1 = plan_deployment_update(
        fix.project,
        fix.output,
        target_profile="vps-server@22",
        target_preset="fixture-web-paper@2",
        input_overrides={},
        input_files={
            "unity-sandboxes": sandboxes_v3_file,
            "connection-targets": targets_file,
            "lts-web": lts_file,
        },
        docker_context="default",
        data_root=fix.data_root,
        allow_unverified=True,
        runner=runner,
        hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
    )

    def fail_redirect(url: str, *, expected_location: str, timeout: int) -> None:
        del expected_location, timeout
        raise DoctorContractError("doctor_unity_sandbox_unhealthy", url, "redirect failed")

    monkeypatch.setattr("mc_remote_stack.doctor.probe_unity_sandbox_redirect", fail_redirect)
    with pytest.raises(DeploymentUpdateContractError) as rb_exc:
        apply_deployment_update(
            fix.project,
            plan_id=plan_dec1.plan_id,
            confirmed=True,
            data_root=fix.data_root,
            runner=runner,
            hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
        )
    assert rb_exc.value.reason == "doctor_unity_sandbox_unhealthy"
    assert load_lock(fix.project, data_root=fix.data_root)["lock_identity"] == upgraded_lock["lock_identity"]

    monkeypatch.setattr("mc_remote_stack.doctor.probe_unity_sandbox_redirect", lambda *_a, **_k: None)
    res_dec1 = apply_deployment_update(
        fix.project,
        plan_id=plan_dec1.plan_id,
        confirmed=True,
        data_root=fix.data_root,
        runner=runner,
        hello_probe=lambda *_a, **_k: ProtocolHelloResult("auth-required", None, None),
    )
    assert res_dec1.phase == "complete"
    assert "dotnet-sb.mc-remote.example" in (fix.output / "Caddyfile").read_text(encoding="utf-8")
