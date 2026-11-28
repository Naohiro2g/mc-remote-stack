"""Read-only desired-state, Docker runtime, and protocol checks."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import ssl
import subprocess
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from ipaddress import ip_address
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from .apply import DOCKER_CONTEXT, compose_render_status
from .render import (
    CANDIDATE_ROUTE_REVISIONS,
    DUAL_LINE_REVISIONS,
    GEYSER_PORT,
    GEYSER_PORT_REVISIONS,
    PUBLIC_RENDERER_REVISIONS,
    SUPPORTED_RENDERER_REVISIONS,
    UNITY_SANDBOX_REVISIONS,
    UNITY_SANDBOX_V3_REVISIONS,
    RenderContractError,
    _locked_connection_input,
    _locked_public_routes,
    connection_target_external,
    connection_target_host,
    connection_target_port,
    locked_lts_web,
    locked_unity_sandboxes,
    unity_sandbox_redirect_hostnames,
    unity_sandbox_web_hostnames,
    verify_toml_render_output,
    web_pair_runtime_path,
    web_pairs,
)
from .runtime_artifacts import (
    RuntimeArtifactContractError,
    RuntimeMount,
    expected_mcremote_mount,
    validate_mcremote_mounts,
)
from .scratch_contract import (
    ScratchContractError,
    load_runtime_config_schema,
    validate_runtime_config,
)

MAX_HELLO_BYTES = 64 * 1024
MAX_SCRATCH_RUNTIME_BYTES = 64 * 1024
MAX_UNITY_HEALTH_BYTES = 16 * 1024
MAX_HOMEPAGE_INDEX_BYTES = 1024 * 1024
MAX_WIRESCOPE_INDEX_BYTES = 1024 * 1024
MCREMOTE_B2_SHA256 = "ad2674fa93645cc3c4c0d2b6aa5b37f11a8f9519162f61ac00b8be7122b023c7"


class CommandRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]: ...


class SocketLike(Protocol):
    def __enter__(self) -> SocketLike: ...

    def __exit__(self, *_args: object) -> object: ...

    def sendall(self, value: bytes) -> None: ...

    def recv(self, size: int) -> bytes: ...


class Connector(Protocol):
    def __call__(
        self,
        address: tuple[str, int],
        timeout: int,
    ) -> SocketLike: ...


@dataclass(frozen=True)
class ProtocolHelloResult:
    status: str
    protocol: str | None
    minecraft_version: str | None


@dataclass(frozen=True)
class TomlDoctorResult:
    deployment: str
    environment: str
    lock_identity: str
    docker_context: str
    runtime_status: str
    render_status: str
    network_scope: str
    bind_address: str
    java_port: int
    mcremote_port: int
    protocol_status: str
    protocol: str | None
    minecraft_version: str | None
    homepage_status: str = "not-applicable"
    scratch_runtime_status: str = "not-applicable"
    wirescope_status: str = "not-applicable"
    web_pair_count: int = 0
    unity_sandbox_status: str = "not-applicable"
    unity_sandbox_count: int = 0
    lts_web_status: str = "not-applicable"
    external_target_status: str = "not-applicable"
    external_target_count: int = 0


class DoctorContractError(ValueError):
    """Stable, fail-closed diagnostic for read-only live checks."""

    def __init__(self, reason: str, path: object, message: str) -> None:
        self.reason = reason
        self.path = str(path)
        super().__init__(f"{reason}: {path}: {message}")


def _fail(reason: str, path: object, message: str) -> None:
    raise DoctorContractError(reason, path, message)


def _default_runner(
    command: list[str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run(
    runner: CommandRunner,
    command: list[str],
    *,
    timeout: int,
    reason: str,
    path: object,
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(command, timeout)
    except FileNotFoundError:
        _fail("docker_unavailable", command[0], "Docker CLI is required for doctor")
    except subprocess.TimeoutExpired:
        _fail(reason, path, f"Docker command timed out after {timeout} seconds")
    except OSError as exc:
        _fail(reason, path, f"cannot execute Docker command: {exc}")
    if result.returncode != 0:
        _fail(reason, path, f"Docker command failed with exit status {result.returncode}")
    return result


UNITY_TCP_PROBE_PROTOCOL = "23.3.0"
PROTOCOL_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def _exchange_jsonrpc_line(
    address: str,
    port: int,
    request: dict[str, Any],
    timeout: int,
    *,
    connector: Connector,
    unavailable_reason: str,
    timeout_reason: str,
    invalid_reason: str,
    unavailable_message: str,
    path: str,
) -> dict[str, Any]:
    payload = json.dumps(request, separators=(",", ":")).encode() + b"\n"
    received = bytearray()
    try:
        with connector((address, port), timeout) as connection:
            connection.sendall(payload)
            while b"\n" not in received:
                chunk = connection.recv(min(4096, MAX_HELLO_BYTES + 1 - len(received)))
                if not chunk:
                    break
                received.extend(chunk)
                if len(received) > MAX_HELLO_BYTES:
                    _fail(invalid_reason, path, "response exceeded the 64 KiB limit")
    except DoctorContractError:
        raise
    except TimeoutError:
        _fail(timeout_reason, f"{address}:{port}", f"server did not answer hello within {timeout} seconds")
    except OSError:
        _fail(unavailable_reason, f"{address}:{port}", unavailable_message)

    line, separator, _remainder = bytes(received).partition(b"\n")
    if not separator or not line or len(line) > MAX_HELLO_BYTES:
        _fail(invalid_reason, path, "server did not return one bounded LF-terminated response")
    try:
        response = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(invalid_reason, path, "server response is not valid UTF-8 JSON")
    if not isinstance(response, dict) or response.get("jsonrpc") != "2.0" or response.get("id") != 1:
        _fail(invalid_reason, path, "response does not contain the expected JSON-RPC envelope")
    return response


def _probe_tcp_hello_auth(
    address: str,
    port: int,
    timeout: int,
    *,
    connector: Connector,
    unavailable_reason: str,
    invalid_reason: str,
    unhealthy_reason: str,
    unavailable_message: str,
    unhealthy_message: str,
    accepted_message: str,
) -> None:
    if timeout < 1 or timeout > 30:
        _fail("doctor_timeout_invalid", "doctor.timeout", "protocol timeout must be between 1 and 30 seconds")

    target = f"{address}:{port}"

    def _send_hello(protocol_version: str) -> dict[str, Any]:
        return _exchange_jsonrpc_line(
            address,
            port,
            {"jsonrpc": "2.0", "id": 1, "method": "hello", "params": {"protocol": protocol_version}},
            timeout,
            connector=connector,
            unavailable_reason=unavailable_reason,
            timeout_reason=unavailable_reason,
            invalid_reason=invalid_reason,
            unavailable_message=unavailable_message,
            path=target,
        )

    response = _send_hello(UNITY_TCP_PROBE_PROTOCOL)
    for attempt in range(2):
        if "result" in response:
            _fail("doctor_auth_not_enforced", target, accepted_message)
        error = response.get("error")
        data = error.get("data") if isinstance(error, dict) else None
        reason = data.get("reason") if isinstance(data, dict) else None
        if reason == "auth_required":
            return
        server_protocol = data.get("server") if isinstance(data, dict) else None
        if (
            attempt == 0
            and reason == "protocol_mismatch"
            and isinstance(server_protocol, str)
            and PROTOCOL_VERSION.fullmatch(server_protocol)
        ):
            response = _send_hello(server_protocol)
            continue
        _fail(unhealthy_reason, target, unhealthy_message)


def probe_unity_sandbox_tcp_auth(
    address: str,
    port: int,
    timeout: int,
    *,
    connector: Connector = socket.create_connection,
) -> None:
    """Verify one exposed Unity sandbox TCP endpoint rejects token-free hello."""

    _probe_tcp_hello_auth(
        address,
        port,
        timeout,
        connector=connector,
        unavailable_reason="doctor_unity_sandbox_tcp_unavailable",
        invalid_reason="doctor_unity_sandbox_invalid",
        unhealthy_reason="doctor_unity_sandbox_unhealthy",
        unavailable_message="cannot connect to the exposed Unity sandbox TCP endpoint",
        unhealthy_message="Unity sandbox TCP endpoint did not report auth_required on token-free hello",
        accepted_message="Unity sandbox TCP endpoint accepted token-free hello",
    )


def probe_external_mcremote_auth(
    address: str,
    port: int,
    timeout: int,
    *,
    connector: Connector = socket.create_connection,
) -> None:
    """Verify one external McRemote TCP endpoint enforces authentication."""

    _probe_tcp_hello_auth(
        address,
        port,
        timeout,
        connector=connector,
        unavailable_reason="doctor_external_target_unavailable",
        invalid_reason="doctor_external_target_invalid",
        unhealthy_reason="doctor_external_target_unhealthy",
        unavailable_message="cannot connect to the external McRemote endpoint",
        unhealthy_message="external McRemote endpoint did not report auth_required on token-free hello",
        accepted_message="external McRemote endpoint accepted token-free hello",
    )


def probe_unity_sandbox_health(url: str, timeout: int) -> None:
    """Fetch one public Unity sandbox /healthz document and verify tcpPort 25575."""

    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "mcrctl-doctor/1"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            source = response.read(MAX_UNITY_HEALTH_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        _fail(
            "doctor_unity_sandbox_unavailable",
            url,
            f"cannot fetch the public Unity sandbox health endpoint: {exc}",
        )
    if len(source) > MAX_UNITY_HEALTH_BYTES:
        _fail(
            "doctor_unity_sandbox_invalid",
            url,
            "Unity sandbox health response exceeded the 16 KiB limit",
        )
    try:
        payload = json.loads(source)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(
            "doctor_unity_sandbox_invalid",
            url,
            f"Unity sandbox health response is not valid UTF-8 JSON: {exc}",
        )
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "ok"
        or isinstance(payload.get("tcpPort"), bool)
        or payload.get("tcpPort") != 25575
    ):
        _fail(
            "doctor_unity_sandbox_unhealthy",
            url,
            "Unity sandbox health endpoint did not report status=ok and tcpPort=25575",
        )


_WS_MAGIC_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _NoRedirectHandler(HTTPRedirectHandler):
    """Prevent urllib from automatically following HTTP 3xx redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _default_no_redirect_open(request: Request, *, timeout: int):
    return build_opener(_NoRedirectHandler()).open(request, timeout=timeout)


def _default_tls_connector(host: str, port: int, timeout: int) -> SocketLike:
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        return ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise


def probe_unity_sandbox_web(
    url: str,
    timeout: int,
    *,
    opener=None,
) -> None:
    """Verify one public Unity Web app root serves HTTP 200 HTML without redirecting."""

    open_fn = _default_no_redirect_open if opener is None else opener
    request = Request(
        url,
        headers={"Accept": "text/html", "User-Agent": "mcrctl-doctor/1"},
    )
    try:
        with open_fn(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            headers = getattr(response, "headers", {})
            body = response.read(MAX_HOMEPAGE_INDEX_BYTES + 1)
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            _fail(
                "doctor_unity_sandbox_unhealthy",
                url,
                f"Unity Web app root unexpectedly redirected with HTTP {exc.code}",
            )
        _fail(
            "doctor_unity_sandbox_unavailable",
            url,
            f"cannot fetch the public Unity Web app root: HTTP {exc.code}",
        )
    except (URLError, TimeoutError, OSError) as exc:
        _fail(
            "doctor_unity_sandbox_unavailable",
            url,
            f"cannot fetch the public Unity Web app root: {exc}",
        )
    if status != 200:
        _fail(
            "doctor_unity_sandbox_unhealthy",
            url,
            f"Unity Web app root returned HTTP {status} instead of 200",
        )
    if not body or len(body) > MAX_HOMEPAGE_INDEX_BYTES:
        _fail(
            "doctor_unity_sandbox_unhealthy",
            url,
            "Unity Web app root returned an empty or oversized body",
        )
    content_type = str(headers.get("Content-Type", "")).casefold()
    lowered_head = body[:4096].lower()
    if "text/html" not in content_type and b"<html" not in lowered_head and b"<!doctype html" not in lowered_head:
        _fail(
            "doctor_unity_sandbox_unhealthy",
            url,
            "Unity Web app root did not return an HTML document",
        )


def probe_unity_sandbox_ws_sync(
    hostname: str,
    timeout: int,
    *,
    tls_connector=None,
) -> None:
    """Verify wss://<hostname>/ws/sync completes an RFC 6455 WebSocket handshake."""

    ws_url = f"wss://{hostname}/ws/sync"
    connect = _default_tls_connector if tls_connector is None else tls_connector
    ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
    expected_accept = base64.b64encode(
        hashlib.sha1((ws_key + _WS_MAGIC_GUID).encode("ascii")).digest()
    ).decode("ascii")
    handshake = (
        f"GET /ws/sync HTTP/1.1\r\n"
        f"Host: {hostname}\r\n"
        f"Origin: https://{hostname}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "User-Agent: mcrctl-doctor/1\r\n"
        "\r\n"
    ).encode("ascii")

    received = bytearray()
    try:
        with connect(hostname, 443, timeout) as conn:
            conn.sendall(handshake)
            while b"\r\n\r\n" not in received:
                chunk = conn.recv(min(4096, MAX_UNITY_HEALTH_BYTES + 1 - len(received)))
                if not chunk:
                    break
                received.extend(chunk)
                if len(received) > MAX_UNITY_HEALTH_BYTES:
                    _fail(
                        "doctor_unity_sandbox_ws_unhealthy",
                        ws_url,
                        "WebSocket handshake response exceeded the 16 KiB limit",
                    )
            header_block, separator, _ = bytes(received).partition(b"\r\n\r\n")
            if not separator:
                _fail(
                    "doctor_unity_sandbox_ws_unhealthy",
                    ws_url,
                    "WebSocket endpoint did not return a complete HTTP upgrade response",
                )
            lines = header_block.decode("iso-8859-1", errors="replace").split("\r\n")
            status_line = lines[0] if lines else ""
            if not (status_line.startswith("HTTP/1.1 101") or status_line.startswith("HTTP/1.0 101")):
                _fail(
                    "doctor_unity_sandbox_ws_unhealthy",
                    ws_url,
                    f"WebSocket upgrade rejected with status: {status_line or 'empty'}",
                )
            resp_headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    resp_headers[k.strip().casefold()] = v.strip()
            connection_tokens = {
                token.strip().casefold()
                for token in resp_headers.get("connection", "").split(",")
                if token.strip()
            }
            if (
                resp_headers.get("upgrade", "").casefold() != "websocket"
                or "upgrade" not in connection_tokens
                or resp_headers.get("sec-websocket-accept") != expected_accept
            ):
                _fail(
                    "doctor_unity_sandbox_ws_unhealthy",
                    ws_url,
                    "WebSocket upgrade response missed required RFC 6455 accept headers",
                )
            try:
                conn.sendall(b"\x88\x80\x00\x00\x00\x00")
            except OSError:
                pass
    except DoctorContractError:
        raise
    except (ssl.SSLError, TimeoutError, OSError) as exc:
        _fail(
            "doctor_unity_sandbox_ws_unavailable",
            ws_url,
            f"cannot establish WebSocket /ws/sync connection: {exc}",
        )


def probe_unity_sandbox_redirect(
    url: str,
    *,
    expected_location: str,
    timeout: int,
    opener=None,
) -> None:
    """Verify a .NET sandbox hostname redirects browsers with HTTP 302 to its Unity Web app."""

    open_fn = _default_no_redirect_open if opener is None else opener
    request = Request(url, headers={"User-Agent": "mcrctl-doctor/1"})
    try:
        with open_fn(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            _fail(
                "doctor_unity_sandbox_unhealthy",
                url,
                f"expected HTTP 302 redirect to {expected_location}, got HTTP {status}",
            )
    except HTTPError as exc:
        location = exc.headers.get("Location") if exc.headers is not None else None
        if exc.code != 302 or location != expected_location:
            _fail(
                "doctor_unity_sandbox_unhealthy",
                url,
                f"expected HTTP 302 redirect to {expected_location}, got HTTP {exc.code} Location={location!r}",
            )
    except (URLError, TimeoutError, OSError) as exc:
        _fail(
            "doctor_unity_sandbox_unavailable",
            url,
            f"cannot reach .NET sandbox redirect endpoint: {exc}",
        )


def probe_scratch_runtime_config(url: str, timeout: int) -> object:
    """Fetch one public Scratch runtime document without browser credentials."""

    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "mcrctl-doctor/1",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            source = response.read(MAX_SCRATCH_RUNTIME_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        _fail(
            "doctor_scratch_runtime_unavailable",
            url,
            f"cannot fetch the public Scratch runtime config: {exc}",
        )
    if len(source) > MAX_SCRATCH_RUNTIME_BYTES:
        _fail(
            "doctor_scratch_runtime_invalid",
            url,
            "Scratch runtime config exceeded the 64 KiB limit",
        )
    try:
        return json.loads(source)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(
            "doctor_scratch_runtime_invalid",
            url,
            f"Scratch runtime config is not valid UTF-8 JSON: {exc}",
        )


def probe_homepage_public(
    url: str,
    *,
    expected_index_sha256: str,
    timeout: int,
) -> None:
    """Fetch the public homepage and compare its exact canonical index bytes."""

    request = Request(
        url,
        headers={"Accept": "text/html", "User-Agent": "mcrctl-doctor/1"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            source = response.read(MAX_HOMEPAGE_INDEX_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        _fail(
            "doctor_homepage_unavailable",
            url,
            f"cannot fetch the public homepage: {exc}",
        )
    if len(source) > MAX_HOMEPAGE_INDEX_BYTES:
        _fail(
            "doctor_homepage_invalid",
            url,
            "public homepage index exceeded the 1 MiB limit",
        )
    if hashlib.sha256(source).hexdigest() != expected_index_sha256:
        _fail(
            "doctor_homepage_content_mismatch",
            url,
            "public homepage index does not match the index in the canonical public directory",
        )


def _public_get(url: str, timeout: int, limit: int) -> tuple[bytes, Any]:
    request = Request(
        url,
        headers={"Accept": "text/html", "User-Agent": "mcrctl-doctor/1"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            source = response.read(limit + 1)
            headers = response.headers
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        _fail(
            "doctor_wirescope_unavailable",
            url,
            f"cannot fetch the public browser handoff surface: {exc}",
        )
    if len(source) > limit:
        _fail(
            "doctor_wirescope_invalid",
            url,
            "public browser handoff response exceeded the size limit",
        )
    return source, headers


def probe_wirescope_public_handoff(
    scratch_url: str,
    wirescope_url: str,
    *,
    expected_index_sha256: str,
    timeout: int,
) -> None:
    """Check the two public origins needed by the Scratch MessageChannel handoff."""

    _scratch_source, scratch_headers = _public_get(
        scratch_url, timeout, MAX_WIRESCOPE_INDEX_BYTES
    )
    if scratch_headers.get("Referrer-Policy") != "strict-origin-when-cross-origin":
        _fail(
            "doctor_wirescope_header_mismatch",
            scratch_url,
            "Scratch origin must send its exact origin during the selection window",
        )
    index_source, wirescope_headers = _public_get(
        wirescope_url, timeout, MAX_WIRESCOPE_INDEX_BYTES
    )
    expected_headers = {
        "Cross-Origin-Opener-Policy": "unsafe-none",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'"
        ),
    }
    for name, expected in expected_headers.items():
        if wirescope_headers.get(name) != expected:
            _fail(
                "doctor_wirescope_header_mismatch",
                f"{wirescope_url}#{name}",
                "public WireScope response header does not match the locked handoff profile",
            )
    actual_sha256 = hashlib.sha256(index_source).hexdigest()
    if actual_sha256 != expected_index_sha256:
        _fail(
            "doctor_wirescope_content_mismatch",
            wirescope_url,
            "public WireScope index does not match the current canonical render",
        )


def validate_scratch_runtime_config(
    observed: object,
    *,
    expected: dict[str, Any],
    schema: dict[str, Any] | None = None,
) -> None:
    """Validate the live Scratch route contract before exact desired-state comparison."""

    if schema is not None:
        try:
            validate_runtime_config(observed, schema)
        except ScratchContractError as exc:
            suffix = exc.path.removeprefix("$").removeprefix(".")
            path = "scratch.runtime" if not suffix else f"scratch.runtime.{suffix}"
            _fail("doctor_scratch_runtime_invalid", path, exc.detail)

    if not isinstance(observed, dict):
        _fail(
            "doctor_scratch_runtime_invalid",
            "scratch.runtime",
            "runtime config must be a JSON object",
        )
    targets = observed.get("connection_targets")
    if not isinstance(targets, list) or not targets:
        _fail(
            "doctor_scratch_runtime_invalid",
            "scratch.runtime.connection_targets",
            "connection_targets must be a non-empty array",
        )
    ids: set[str] = set()
    if observed.get("schema_version") == 2:
        endpoints: set[tuple[str, int]] = set()
        for index, target in enumerate(targets):
            if not isinstance(target, dict) or set(target) != {"id", "label", "host", "port"}:
                _fail(
                    "doctor_scratch_runtime_invalid",
                    f"scratch.runtime.connection_targets[{index}]",
                    "v2 target must contain exactly id, label, host, and port",
                )
            if any(
                not isinstance(target[key], str) or not target[key].strip()
                for key in ("id", "label", "host")
            ):
                _fail(
                    "doctor_scratch_runtime_invalid",
                    f"scratch.runtime.connection_targets[{index}]",
                    "target string fields must be non-empty",
                )
            port = target["port"]
            if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
                _fail(
                    "doctor_scratch_runtime_invalid",
                    f"scratch.runtime.connection_targets[{index}].port",
                    "v2 target port must be an integer between 1 and 65535",
                )
            endpoint = (target["host"], port)
            if target["id"] in ids or endpoint in endpoints:
                _fail(
                    "doctor_scratch_runtime_invalid",
                    f"scratch.runtime.connection_targets[{index}]",
                    "target ids and (host, port) endpoints must be unique",
                )
            ids.add(target["id"])
            endpoints.add(endpoint)
        if observed.get("default_target_id") not in ids:
            _fail(
                "doctor_scratch_runtime_invalid",
                "scratch.runtime.default_target_id",
                "default_target_id must reference a target in connection_targets",
            )
    else:
        sandboxes: set[str] = set()
        for index, target in enumerate(targets):
            if not isinstance(target, dict) or set(target) != {"id", "label", "sandbox"}:
                _fail(
                    "doctor_scratch_runtime_invalid",
                    f"scratch.runtime.connection_targets[{index}]",
                    "target must contain exactly id, label, and sandbox",
                )
            if any(not isinstance(target[key], str) or not target[key].strip() for key in target):
                _fail(
                    "doctor_scratch_runtime_invalid",
                    f"scratch.runtime.connection_targets[{index}]",
                    "target fields must be non-empty strings",
                )
            if target["id"] in ids or target["sandbox"] in sandboxes:
                _fail(
                    "doctor_scratch_runtime_invalid",
                    f"scratch.runtime.connection_targets[{index}]",
                    "target ids and sandbox values must be unique",
                )
            ids.add(target["id"])
            sandboxes.add(target["sandbox"])
        if observed.get("default_sandbox") not in sandboxes:
            _fail(
                "doctor_scratch_runtime_invalid",
                "scratch.runtime.default_sandbox",
                "default_sandbox must be listed in connection_targets",
            )
    notices = observed.get("notices")
    if not isinstance(notices, list):
        _fail(
            "doctor_scratch_runtime_invalid",
            "scratch.runtime.notices",
            "runtime config requires a notices array",
        )
    for index, notice in enumerate(notices):
        if not isinstance(notice, dict) or not {"heading", "body"} <= set(notice):
            _fail(
                "doctor_scratch_runtime_invalid",
                f"scratch.runtime.notices[{index}]",
                "notice requires heading and body",
            )
        if any(
            not isinstance(notice[key], str) or not notice[key].strip()
            for key in ("heading", "body")
        ):
            _fail(
                "doctor_scratch_runtime_invalid",
                f"scratch.runtime.notices[{index}]",
                "notice heading and body must be non-empty strings",
            )
    if observed != expected:
        _fail(
            "doctor_scratch_runtime_mismatch",
            "scratch.runtime",
            "public Scratch runtime config does not match the current canonical render",
        )


def _nonempty_lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _single_inspect_record(
    result: subprocess.CompletedProcess[str],
    *,
    reason: str,
    path: object,
) -> dict[str, Any]:
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        _fail(reason, path, "Docker inspect output is not valid JSON")
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        _fail(reason, path, "Docker inspect must return exactly one object")
    return value[0]


def _component_value(lock: dict[str, Any], role: str, key: str) -> str:
    matches = [
        component.get(key)
        for component in lock["components"]
        if component.get("role") == role
    ]
    if len(matches) != 1 or not isinstance(matches[0], str) or not matches[0]:
        _fail(
            "doctor_contract_unsupported",
            f"components.{role}",
            f"doctor requires exactly one {role} component with {key}",
        )
    return matches[0]


def _auth_enforcement_required(lock: dict[str, Any]) -> bool:
    controls = lock["render_plan"].get("required_security_controls", [])
    if "mcremote-auth-enforced" in controls:
        return True
    components = [
        component
        for component in lock["components"]
        if component.get("role") == "mcremote-plugin"
    ]
    if len(components) != 1:
        return False
    artifact_id = components[0].get("artifact")
    artifacts = [
        artifact
        for artifact in lock["artifacts"]
        if artifact.get("id") == artifact_id
    ]
    return len(artifacts) == 1 and (
        artifacts[0].get("version") == "2100.0.0b2"
        and artifacts[0].get("sha256") == MCREMOTE_B2_SHA256
    )


def _service_ids(lock: dict[str, Any]) -> list[str]:
    services = [service["id"] for service in lock["render_plan"]["services"]]
    if not services or len(services) != len(set(services)):
        _fail(
            "doctor_contract_unsupported",
            "render_plan.services",
            "doctor requires a non-empty unique service projection",
        )
    return services


def _volume_identities(lock: dict[str, Any]) -> list[str]:
    assignments = {
        assignment["role"]: assignment["identity"]
        for assignment in lock["runtime"]["volumes"]
    }
    roles = [role["id"] for role in lock["render_plan"]["volume_roles"]]
    if not roles or set(assignments) != set(roles) or len(roles) != len(set(roles)):
        _fail(
            "doctor_contract_unsupported",
            "runtime.volumes",
            "doctor requires exactly one assignment for every declared volume role",
        )
    return [assignments[role] for role in roles]


def _expected_volume_labels(lock: dict[str, Any]) -> dict[str, str]:
    return {
        "io.mc-remote.owner": "mcrctl",
        "io.mc-remote.deployment": lock["deployment"]["name"],
        "io.mc-remote.environment": lock["environment"]["identity"],
        "io.mc-remote.world": lock["world"]["identity"],
        "io.mc-remote.created-by-lock": lock["lock_identity"],
    }


def _validate_volume(record: dict[str, Any], volume: str, lock: dict[str, Any]) -> None:
    labels = record.get("Labels")
    expected = _expected_volume_labels(lock)
    stable_labels = {
        key: value
        for key, value in expected.items()
        if key != "io.mc-remote.created-by-lock"
    }
    if (
        record.get("Name") != volume
        or record.get("Driver") != "local"
        or not isinstance(labels, dict)
        or set(labels) != set(expected)
        or any(labels.get(key) != value for key, value in stable_labels.items())
        or not isinstance(labels.get("io.mc-remote.created-by-lock"), str)
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            labels["io.mc-remote.created-by-lock"],
        )
        is None
    ):
        _fail(
            "doctor_volume_unmanaged",
            volume,
            "runtime volume does not match mcrctl ownership and creation provenance labels",
        )


def _validate_mcremote_artifact_mount(
    record: dict[str, Any],
    lock: dict[str, Any],
) -> None:
    source = record.get("Mounts")
    if not isinstance(source, list):
        _fail(
            "doctor_artifact_mount_mismatch",
            "runtime.mounts",
            "runtime mounts are unavailable",
        )
    mounts: list[RuntimeMount] = []
    for mount in source:
        if not isinstance(mount, dict) or not isinstance(
            mount.get("Destination"), str
        ):
            _fail(
                "doctor_artifact_mount_mismatch",
                "runtime.mounts",
                "runtime mount record is invalid",
            )
        mounts.append(
            RuntimeMount(
                kind=str(mount.get("Type", "")).lower(),
                source=(
                    mount.get("Source")
                    if isinstance(mount.get("Source"), str)
                    else None
                ),
                target=mount["Destination"],
                read_only=mount.get("RW") is False,
            )
        )
    try:
        validate_mcremote_mounts(mounts, lock)
    except RuntimeArtifactContractError as exc:
        _fail(
            "doctor_artifact_mount_mismatch",
            "runtime.mounts",
            str(exc),
        )


def _composition_input(lock: dict[str, Any], role: str) -> dict[str, Any]:
    matches = [item for item in lock["operator_inputs"] if item["role"] == role]
    if len(matches) != 1:
        _fail(
            "doctor_composition_mount_mismatch",
            f"operator_inputs.{role}",
            f"canonical composition requires exactly one {role} input",
        )
    return matches[0]["semantic"]


def _validate_canonical_composition_mounts(
    record: dict[str, Any],
    lock: dict[str, Any],
    *,
    service: str,
    output: Path | None = None,
) -> None:
    controls = set(lock["render_plan"].get("required_security_controls", []))
    if "exact-peripheral-plugin-set" not in controls:
        return
    raw_mounts = record.get("Mounts")
    if not isinstance(raw_mounts, list):
        _fail(
            "doctor_composition_mount_mismatch",
            f"services.{service}.mounts",
            "runtime mount records are unavailable",
        )
    mounts = [mount for mount in raw_mounts if isinstance(mount, dict)]
    artifact_store = Path(lock["runtime"]["artifact_store"]).resolve()
    if service == "minecraft":
        plugins = _composition_input(lock, "minecraft-plugins")["plugins"]
        backup = _composition_input(lock, "minecraft-backup")["host_path"]
        _mcremote_source, mcremote_target = expected_mcremote_mount(lock)
        expected_plugins = {
            f"/plugins/{plugin['filename']}": str(
                artifact_store / "sha256" / plugin["sha256"]
            )
            for plugin in plugins
        }
        expected_plugin_targets = set(expected_plugins) | {mcremote_target}
        actual_plugin_targets = {
            mount.get("Destination")
            for mount in mounts
            if isinstance(mount.get("Destination"), str)
            and PurePosixPath(mount["Destination"]).parent
            == PurePosixPath("/plugins")
            and PurePosixPath(mount["Destination"]).suffix.casefold() == ".jar"
        }
        if actual_plugin_targets != expected_plugin_targets:
            _fail(
                "doctor_composition_mount_mismatch",
                "services.minecraft.mounts./plugins",
                "live plugin targets differ from the exact canonical set",
            )
        for target, source in expected_plugins.items():
            matching = [mount for mount in mounts if mount.get("Destination") == target]
            if (
                len(matching) != 1
                or matching[0].get("Type") != "bind"
                or matching[0].get("RW") is not False
                or not isinstance(matching[0].get("Source"), str)
                or str(Path(matching[0]["Source"]).resolve()) != source
            ):
                _fail(
                    "doctor_composition_mount_mismatch",
                    target,
                    "peripheral plugin mount does not match its locked digest",
                )
        backup_matches = [
            mount for mount in mounts if mount.get("Destination") == "/backup"
        ]
        if (
            len(backup_matches) != 1
            or backup_matches[0].get("Type") != "bind"
            or backup_matches[0].get("RW") is not True
            or not isinstance(backup_matches[0].get("Source"), str)
            or Path(backup_matches[0]["Source"]).resolve() != Path(backup).resolve()
        ):
            _fail(
                "doctor_composition_mount_mismatch",
                "/backup",
                "live backup bind differs from the locked operator path",
            )
    elif service == "caddy":
        expected = artifact_store.parent / "homepage"
        matching = [
            mount for mount in mounts if mount.get("Destination") == "/srv/homepage"
        ]
        if (
            len(matching) != 1
            or matching[0].get("Type") != "bind"
            or matching[0].get("RW") is not False
            or not isinstance(matching[0].get("Source"), str)
            or Path(matching[0]["Source"]).resolve() != expected
        ):
            _fail(
                "doctor_composition_mount_mismatch",
                "/srv/homepage",
                "live homepage directory differs from the canonical public directory",
            )
        if lock["render_plan"].get("adapter_revision") in CANDIDATE_ROUTE_REVISIONS:
            expected = artifact_store.parent / "candidates"
            matching = [
                mount for mount in mounts if mount.get("Destination") == "/srv/candidates"
            ]
            if (
                len(matching) != 1
                or matching[0].get("Type") != "bind"
                or matching[0].get("RW") is not False
                or not isinstance(matching[0].get("Source"), str)
                or Path(matching[0]["Source"]).resolve() != expected
            ):
                _fail(
                    "doctor_composition_mount_mismatch",
                    "/srv/candidates",
                    "live release-candidate directory differs from the canonical one",
                )
        if lock.get("lts_web") is not None:
            if output is None:
                _fail(
                    "doctor_composition_mount_mismatch",
                    "/srv/wirescope-lts",
                    "render output directory is required to verify WireScope LTS mount",
                )
            expected_lts = (output / "wirescope-lts").resolve()
            matching = [
                mount
                for mount in mounts
                if mount.get("Destination") == "/srv/wirescope-lts"
            ]
            if (
                len(matching) != 1
                or matching[0].get("Type") != "bind"
                or matching[0].get("RW") is not False
                or not isinstance(matching[0].get("Source"), str)
                or Path(matching[0]["Source"]).resolve() != expected_lts
            ):
                _fail(
                    "doctor_composition_mount_mismatch",
                    "/srv/wirescope-lts",
                    "live WireScope LTS directory differs from the canonical one",
                )


def _validate_container(
    record: dict[str, Any],
    lock: dict[str, Any],
    expected_services: set[str],
    output: Path,
) -> tuple[str, str]:
    config = record.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    expected_labels = {
        "com.docker.compose.project": lock["deployment"]["name"],
        "io.mc-remote.deployment": lock["deployment"]["name"],
        "io.mc-remote.environment": lock["environment"]["identity"],
        "io.mc-remote.world": lock["world"]["identity"],
        "io.mc-remote.lock": lock["lock_identity"],
    }
    if not isinstance(labels, dict) or any(
        labels.get(key) != value for key, value in expected_labels.items()
    ):
        _fail(
            "doctor_runtime_unmanaged",
            lock["deployment"]["name"],
            "runtime container does not match the current lock",
        )
    service = labels.get("com.docker.compose.service")
    if service not in expected_services:
        _fail(
            "doctor_runtime_unmanaged",
            lock["deployment"]["name"],
            "runtime container service is not declared by the current lock",
        )
    if service == "minecraft":
        _validate_mcremote_artifact_mount(record, lock)
    _validate_canonical_composition_mounts(record, lock, service=service, output=output)

    state = record.get("State")
    if not isinstance(state, dict) or state.get("Running") is not True:
        _fail(
            "doctor_runtime_not_running",
            lock["deployment"]["name"],
            "current locked container is not running",
        )
    health = state.get("Health")
    if service == "minecraft" and (
        not isinstance(health, dict) or health.get("Status") != "healthy"
    ):
        _fail(
            "doctor_runtime_unhealthy",
            lock["deployment"]["name"],
            "current locked container is not healthy",
        )

    network = record.get("NetworkSettings")
    ports = network.get("Ports") if isinstance(network, dict) else None
    if not isinstance(ports, dict):
        _fail(
            "doctor_network_mismatch",
            lock["deployment"]["name"],
            "container port mappings are unavailable",
        )
    address = lock["network"]["bind_address"]
    expected_ports: dict[str, list[dict[str, str]]] = {}
    if service == "minecraft":
        expected_ports = {
            "25565/tcp": [
                {
                    "HostIp": address,
                    "HostPort": str(lock["network"]["java_port"]),
                }
            ],
            "25575/tcp": [
                {
                    "HostIp": address,
                    "HostPort": str(lock["network"]["mcremote_port"]),
                }
            ],
        }
        renderer_revision = lock["render_plan"]["adapter_revision"]
        if renderer_revision in GEYSER_PORT_REVISIONS:
            for protocol in ("tcp", "udp"):
                expected_ports[f"{GEYSER_PORT}/{protocol}"] = [
                    {"HostIp": address, "HostPort": str(GEYSER_PORT)}
                ]
        elif renderer_revision in SUPPORTED_RENDERER_REVISIONS:
            expected_ports["25565/udp"] = [
                {
                    "HostIp": address,
                    "HostPort": str(lock["network"]["java_port"]),
                }
            ]
    elif service == "caddy" and lock["render_plan"]["adapter_revision"] == "14":
        lan_routes = [
            item["semantic"]
            for item in lock["operator_inputs"]
            if item.get("role") == "lan-routes"
        ]
        if len(lan_routes) != 1:
            _fail(
                "doctor_network_mismatch",
                lock["deployment"]["name"],
                "compose@14 requires exactly one locked lan-routes input",
            )
        scratch_port = str(lan_routes[0]["scratch_port"])
        bridge_port = str(lan_routes[0]["bridge_port"])
        expected_ports = {
            f"{scratch_port}/tcp": [{"HostIp": address, "HostPort": scratch_port}],
            f"{bridge_port}/tcp": [{"HostIp": address, "HostPort": bridge_port}],
        }
    elif service == "caddy":
        expected_ports = {
            "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "80"}],
            "443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "443"}],
        }
    elif (
        service.startswith("unity-sandbox-")
        and lock["render_plan"]["adapter_revision"] in UNITY_SANDBOX_REVISIONS
    ):
        for sandbox in locked_unity_sandboxes(lock):
            if f"unity-sandbox-{sandbox['id']}" == service and "tcp_port" in sandbox:
                expected_ports = {
                    "25575/tcp": [
                        {"HostIp": address, "HostPort": str(sandbox["tcp_port"])}
                    ]
                }
                break
    published = {key: value for key, value in ports.items() if value}
    if published != expected_ports:
        _fail(
            "doctor_network_mismatch",
            lock["deployment"]["name"],
            "live published ports do not match the current lock",
        )
    return service, compose_render_status(labels, output)


def _validate_hello_result(
    response: object,
    *,
    protocol: str,
    minecraft_version: str,
    world: str,
) -> ProtocolHelloResult:
    if not isinstance(response, dict) or response.get("jsonrpc") != "2.0" or response.get("id") != 1:
        _fail(
            "protocol_hello_invalid",
            "protocol.hello",
            "response does not contain the expected JSON-RPC envelope",
        )

    result = response.get("result")
    if isinstance(result, dict):
        supported = result.get("supported_mc_versions")
        constants = result.get("world_constants")
        origin = result.get("origin")
        if result.get("protocol") != protocol:
            _fail(
                "protocol_hello_mismatch",
                "protocol.hello.protocol",
                "server protocol does not match the current lock",
            )
        if result.get("mc_version") != minecraft_version:
            _fail(
                "protocol_hello_mismatch",
                "protocol.hello.mc_version",
                "server Minecraft version does not match the current lock",
            )
        if (
            not isinstance(supported, list)
            or minecraft_version not in supported
            or "catalogHash" not in result
            or not isinstance(constants, dict)
            or "y_sea" not in constants
            or result.get("world") != world
            or not isinstance(origin, list)
            or len(origin) != 3
            or any(isinstance(value, bool) or not isinstance(value, int | float) for value in origin)
        ):
            _fail(
                "protocol_hello_invalid",
                "protocol.hello.result",
                "response is missing required public protocol fields",
            )
        return ProtocolHelloResult(
            status="ok",
            protocol=protocol,
            minecraft_version=minecraft_version,
        )

    error = response.get("error")
    data = error.get("data") if isinstance(error, dict) else None
    if isinstance(data, dict) and data.get("reason") == "auth_required":
        return ProtocolHelloResult(
            status="auth-required",
            protocol=None,
            minecraft_version=None,
        )
    _fail(
        "protocol_hello_failed",
        "protocol.hello",
        "server returned a non-success response other than auth_required",
    )


def probe_protocol_hello(
    address: str,
    port: int,
    protocol: str,
    minecraft_version: str,
    world: str,
    timeout: int,
    *,
    connector: Connector = socket.create_connection,
) -> ProtocolHelloResult:
    """Send one token-free JSON-RPC hello without exposing response credentials."""

    if timeout < 1 or timeout > 30:
        _fail(
            "doctor_timeout_invalid",
            "doctor.timeout",
            "protocol timeout must be between 1 and 30 seconds",
        )
    response = _exchange_jsonrpc_line(
        address,
        port,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "hello",
            "params": {"protocol": protocol},
        },
        timeout,
        connector=connector,
        unavailable_reason="protocol_hello_unavailable",
        timeout_reason="protocol_hello_timeout",
        invalid_reason="protocol_hello_invalid",
        unavailable_message="cannot connect to the locked McRemote endpoint",
        path="protocol.hello",
    )
    return _validate_hello_result(
        response,
        protocol=protocol,
        minecraft_version=minecraft_version,
        world=world,
    )


def _check_web_pair_surface(
    runtime_path: Path,
    *,
    scratch_host: str,
    wirescope_host: str,
    expected_index_sha256: str,
    schema: dict[str, Any] | None,
    timeout: int,
    scratch_runtime_probe: Any,
    wirescope_probe: Any,
) -> None:
    try:
        expected_runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(
            "doctor_scratch_runtime_invalid",
            runtime_path,
            f"cannot read canonical Scratch runtime config: {exc}",
        )
    observed_runtime = scratch_runtime_probe(
        f"https://{scratch_host}/mc-remote-runtime-config.json", timeout
    )
    validate_scratch_runtime_config(
        observed_runtime,
        expected=expected_runtime,
        schema=schema,
    )
    expected_wirescope_url = f"https://{wirescope_host}/"
    if expected_runtime.get("wirescope_url") != expected_wirescope_url:
        _fail(
            "doctor_wirescope_config_mismatch",
            "scratch.runtime.wirescope_url",
            "Scratch runtime does not point at the canonical public WireScope origin",
        )
    wirescope_probe(
        f"https://{scratch_host}/",
        expected_wirescope_url,
        expected_index_sha256=expected_index_sha256,
        timeout=timeout,
    )


def _served_external_target_endpoints(
    lock: dict[str, Any],
    pairs: list[dict[str, str]],
    lts_web: dict[str, Any] | None,
) -> list[tuple[str, int]]:
    connection_input = _locked_connection_input(lock)
    if not connection_input:
        return []
    by_id = {target["id"]: target for target in connection_input["targets"]}
    served_ids: set[str] = set()
    pair_scratches = {pair["scratch"] for pair in pairs}
    for selection in connection_input.get("selections", []):
        if selection.get("scratch") in pair_scratches:
            served_ids.update(selection.get("target_ids", []))
    if lts_web is not None:
        served_ids.update(lts_web["semantic"]["target_ids"])
    endpoints = {
        (connection_target_host(by_id[tid]), connection_target_port(by_id[tid]))
        for tid in served_ids
        if tid in by_id and connection_target_external(by_id[tid])
    }
    return sorted(endpoints)


def doctor_toml_project(
    project_root: Path,
    output: Path,
    *,
    docker_context: str,
    data_root: Traversable,
    timeout: int = 5,
    runner: CommandRunner = _default_runner,
    hello_probe=probe_protocol_hello,
    homepage_probe=None,
    scratch_runtime_probe=None,
    wirescope_probe=None,
    unity_health_probe=None,
    unity_web_probe=None,
    unity_ws_probe=None,
    unity_redirect_probe=None,
    unity_tcp_probe=None,
    external_target_probe=None,
) -> TomlDoctorResult:
    """Check one current Compose runtime without mutating host state."""

    if homepage_probe is None:
        homepage_probe = probe_homepage_public
    if scratch_runtime_probe is None:
        scratch_runtime_probe = probe_scratch_runtime_config
    if wirescope_probe is None:
        wirescope_probe = probe_wirescope_public_handoff
    if unity_health_probe is None:
        unity_health_probe = probe_unity_sandbox_health
    if unity_web_probe is None:
        unity_web_probe = probe_unity_sandbox_web
    if unity_ws_probe is None:
        unity_ws_probe = probe_unity_sandbox_ws_sync
    if unity_redirect_probe is None:
        unity_redirect_probe = probe_unity_sandbox_redirect
    if unity_tcp_probe is None:
        unity_tcp_probe = probe_unity_sandbox_tcp_auth
    if external_target_probe is None:
        external_target_probe = probe_external_mcremote_auth

    if not DOCKER_CONTEXT.fullmatch(docker_context):
        _fail(
            "docker_context_invalid",
            "doctor.docker_context",
            "Docker context must be an explicit name token",
        )
    if timeout < 1 or timeout > 30:
        _fail(
            "doctor_timeout_invalid",
            "doctor.timeout",
            "protocol timeout must be between 1 and 30 seconds",
        )
    try:
        verification = verify_toml_render_output(
            project_root,
            output,
            data_root=data_root,
        )
    except RenderContractError as exc:
        raise DoctorContractError(exc.reason, exc.path, str(exc)) from exc
    lock = verification.lock
    output = verification.output
    protocol = _component_value(lock, "mcremote-plugin", "protocol")
    minecraft_version = _component_value(lock, "paper-server", "minecraft_version")
    services = _service_ids(lock)
    volumes = _volume_identities(lock)

    context = _run(
        runner,
        ["docker", "context", "inspect", docker_context],
        timeout=30,
        reason="docker_context_unavailable",
        path=docker_context,
    )
    context_record = _single_inspect_record(
        context,
        reason="docker_context_unavailable",
        path=docker_context,
    )
    endpoints = context_record.get("Endpoints")
    docker_endpoint = endpoints.get("docker") if isinstance(endpoints, dict) else None
    docker_host = docker_endpoint.get("Host") if isinstance(docker_endpoint, dict) else None
    if not isinstance(docker_host, str) or not docker_host.startswith("unix://"):
        _fail(
            "docker_context_not_local",
            docker_context,
            "doctor checks only a local unix-socket Docker context on the target host",
        )

    docker_prefix = ["docker", "--context", docker_context]
    daemon = _run(
        runner,
        docker_prefix + ["version", "--format", "{{.Server.Version}}"],
        timeout=30,
        reason="docker_unavailable",
        path="docker.daemon",
    )
    if len(_nonempty_lines(daemon)) != 1:
        _fail("docker_unavailable", "docker.daemon", "Docker daemon version is unavailable")
    compose = _run(
        runner,
        docker_prefix + ["compose", "version", "--short"],
        timeout=30,
        reason="docker_compose_unavailable",
        path="docker.compose",
    )
    if len(_nonempty_lines(compose)) != 1:
        _fail("docker_compose_unavailable", "docker.compose", "Docker Compose v2 is unavailable")

    compose_base = docker_prefix + [
        "compose",
        "--ansi",
        "never",
        "--project-directory",
        str(output),
        "--file",
        str(output / "compose.yaml"),
    ]
    _run(
        runner,
        compose_base + ["config", "--quiet"],
        timeout=60,
        reason="compose_config_invalid",
        path=output / "compose.yaml",
    )

    container_result = _run(
        runner,
        docker_prefix
        + [
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={lock['deployment']['name']}",
        ],
        timeout=30,
        reason="doctor_runtime_inspect_failed",
        path=lock["deployment"]["name"],
    )
    containers = _nonempty_lines(container_result)
    if len(containers) != len(services):
        reason = "doctor_runtime_missing" if not containers else "doctor_runtime_unmanaged"
        _fail(
            reason,
            lock["deployment"]["name"],
            "doctor requires exactly the current lock's Compose service count",
        )
    actual_services = set()
    render_statuses = set()
    for container_id in containers:
        container = _single_inspect_record(
            _run(
                runner,
                docker_prefix + ["inspect", container_id],
                timeout=30,
                reason="doctor_runtime_inspect_failed",
                path=lock["deployment"]["name"],
            ),
            reason="doctor_runtime_inspect_failed",
            path=lock["deployment"]["name"],
        )
        service, render_status = _validate_container(
            container,
            lock,
            set(services),
            output,
        )
        actual_services.add(service)
        render_statuses.add(render_status)
    if actual_services != set(services):
        _fail(
            "doctor_runtime_unmanaged",
            lock["deployment"]["name"],
            "runtime does not contain every service declared by the current lock",
        )
    if render_statuses == {"current"}:
        runtime_render_status = "current"
    elif render_statuses <= {"current", "additional-compose-files"}:
        runtime_render_status = "additional-compose-files"
    else:
        runtime_render_status = "noncanonical"

    for volume in volumes:
        volume_record = _single_inspect_record(
            _run(
                runner,
                docker_prefix + ["volume", "inspect", volume],
                timeout=30,
                reason="doctor_volume_missing",
                path=volume,
            ),
            reason="doctor_volume_unmanaged",
            path=volume,
        )
        _validate_volume(volume_record, volume, lock)

    homepage_status = "not-applicable"
    scratch_runtime_status = "not-applicable"
    wirescope_status = "not-applicable"
    unity_sandbox_status = "not-applicable"
    unity_sandboxes: list[dict[str, Any]] = []
    pairs: list[dict[str, str]] = []
    web_pair_count = 0
    lts_web_status = "not-applicable"
    external_target_status = "not-applicable"
    external_targets: list[tuple[str, int]] = []
    if lock["render_plan"]["adapter_revision"] in PUBLIC_RENDERER_REVISIONS:
        routes = _locked_public_routes(lock)
        if lock["render_plan"]["adapter_revision"] in PUBLIC_RENDERER_REVISIONS:
            index_path = (
                Path(lock["runtime"]["artifact_store"]).parent
                / "homepage"
                / "index.html"
            )
            try:
                index_source = index_path.read_bytes()
            except OSError as exc:
                _fail(
                    "doctor_homepage_invalid",
                    index_path,
                    f"cannot read the locked homepage index: {exc}",
                )
            if len(index_source) > MAX_HOMEPAGE_INDEX_BYTES:
                _fail(
                    "doctor_homepage_invalid",
                    index_path,
                    "locked homepage index exceeded the 1 MiB limit",
                )
            homepage_probe(
                f"https://{routes['homepage']}/",
                expected_index_sha256=hashlib.sha256(index_source).hexdigest(),
                timeout=timeout,
            )
            homepage_status = "current"
        schema = None
        contract = lock.get("scratch_runtime_contract")
        if contract is not None:
            try:
                schema = load_runtime_config_schema(contract, data_root=data_root)
            except ScratchContractError as exc:
                _fail(exc.reason, exc.path, exc.detail)
        index_path = output / "wirescope" / "index.html"
        try:
            expected_index_sha256 = hashlib.sha256(index_path.read_bytes()).hexdigest()
        except OSError as exc:
            _fail(
                "doctor_wirescope_invalid",
                index_path,
                f"cannot read canonical WireScope index: {exc}",
            )
        # Every Scratch/WireScope name pair gets the same checks as the primary pair.
        pairs = web_pairs(routes)
        for index, pair in enumerate(pairs):
            runtime_path = output / (
                "runtime/scratch.json" if index == 0 else web_pair_runtime_path(pair)
            )
            _check_web_pair_surface(
                runtime_path,
                scratch_host=pair["scratch"],
                wirescope_host=pair["wirescope"],
                expected_index_sha256=expected_index_sha256,
                schema=schema,
                timeout=timeout,
                scratch_runtime_probe=scratch_runtime_probe,
                wirescope_probe=wirescope_probe,
            )
        web_pair_count = len(pairs)
        dual_line = lock["render_plan"]["adapter_revision"] in DUAL_LINE_REVISIONS
        lts_web = locked_lts_web(lock) if dual_line else None
        if lts_web is not None:
            lts_semantic = lts_web["semantic"]
            lts_contract = lts_web["bundle"]["scratch_runtime_contract"]
            try:
                lts_schema = load_runtime_config_schema(lts_contract, data_root=data_root)
            except ScratchContractError as exc:
                _fail(exc.reason, exc.path, exc.detail)
            lts_index_path = output / "wirescope-lts" / "index.html"
            try:
                lts_index_sha256 = hashlib.sha256(lts_index_path.read_bytes()).hexdigest()
            except OSError as exc:
                _fail(
                    "doctor_wirescope_invalid",
                    lts_index_path,
                    f"cannot read canonical WireScope LTS index: {exc}",
                )
            _check_web_pair_surface(
                output / "runtime/scratch-lts.json",
                scratch_host=lts_semantic["scratch"],
                wirescope_host=lts_semantic["wirescope"],
                expected_index_sha256=lts_index_sha256,
                schema=lts_schema,
                timeout=timeout,
                scratch_runtime_probe=scratch_runtime_probe,
                wirescope_probe=wirescope_probe,
            )
            web_pair_count += 1
            lts_web_status = "current"
        scratch_runtime_status = "current"
        wirescope_status = "current"
        if dual_line:
            external_targets = _served_external_target_endpoints(
                lock, pairs, lts_web
            )
            for external_host, external_port in external_targets:
                external_target_probe(external_host, external_port, timeout)
            if external_targets:
                external_target_status = "healthy"
        if lock["render_plan"]["adapter_revision"] in UNITY_SANDBOX_REVISIONS:
            unity_sandboxes = locked_unity_sandboxes(lock)
            if unity_sandboxes:
                v3_unity = (
                    lock["render_plan"]["adapter_revision"] in UNITY_SANDBOX_V3_REVISIONS
                )
                for sandbox in unity_sandboxes:
                    for hostname in unity_sandbox_web_hostnames(sandbox):
                        unity_health_probe(
                            f"https://{hostname}/healthz",
                            timeout=timeout,
                        )
                        if v3_unity:
                            unity_web_probe(
                                f"https://{hostname}/",
                                timeout=timeout,
                            )
                            unity_ws_probe(
                                hostname,
                                timeout=timeout,
                            )
                    if v3_unity:
                        expected_location = f"https://{sandbox['hostname']}/"
                        for redirect_host in unity_sandbox_redirect_hostnames(sandbox):
                            unity_redirect_probe(
                                f"https://{redirect_host}/",
                                expected_location=expected_location,
                                timeout=timeout,
                            )
                    if "tcp_port" in sandbox:
                        unity_tcp_probe(
                            lock["network"]["bind_address"],
                            sandbox["tcp_port"],
                            timeout,
                        )
                unity_sandbox_status = "healthy"

    network = lock["network"]
    hello = hello_probe(
        network["bind_address"],
        network["mcremote_port"],
        protocol,
        minecraft_version,
        lock["world"]["identity"],
        timeout,
    )
    if _auth_enforcement_required(lock) and hello.status != "auth-required":
        _fail(
            "doctor_auth_not_enforced",
            "protocol.hello",
            "the locked McRemote release requires authentication, but token-free hello succeeded",
        )
    return TomlDoctorResult(
        deployment=lock["deployment"]["name"],
        environment=lock["environment"]["identity"],
        lock_identity=lock["lock_identity"],
        docker_context=docker_context,
        runtime_status="healthy",
        render_status=runtime_render_status,
        network_scope=(
            "loopback"
            if ip_address(network["bind_address"]).is_loopback
            else (
                "public"
                if lock["environment"]["exposure"] == "public"
                else "non-loopback"
            )
        ),
        bind_address=network["bind_address"],
        java_port=network["java_port"],
        mcremote_port=network["mcremote_port"],
        protocol_status=hello.status,
        protocol=hello.protocol,
        minecraft_version=hello.minecraft_version,
        homepage_status=homepage_status,
        scratch_runtime_status=scratch_runtime_status,
        wirescope_status=wirescope_status,
        web_pair_count=web_pair_count,
        unity_sandbox_status=unity_sandbox_status,
        unity_sandbox_count=len(unity_sandboxes),
        lts_web_status=lts_web_status,
        external_target_status=external_target_status,
        external_target_count=len(external_targets),
    )
