"""One-time conversion of reviewed Compose overlays into typed runtime inputs."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import tomlkit
import yaml

from .apply import CommandRunner, _default_runner, _run
from .artifacts import fetch_locked_artifacts
from .auth_migration import _compose_stack
from .deployment_update import (
    DeploymentUpdatePlan,
    DeploymentUpdateResult,
    _acquire_transaction_lock,
    _adapt_candidate_order,
    _copy_project_source,
    _DockerUpdateHost,
    _ensure_no_active_transaction,
    _load_state,
    _make_plan,
    _plan_payload,
    _prepare_transaction,
    _release_transaction_lock,
    _snapshot_paths,
    _updates_root,
    _validate_in_place_transition,
    apply_deployment_update,
    load_deployment_update_plan,
)
from .doctor import doctor_toml_project, probe_protocol_hello
from .preset_registry import load_profile
from .render import render_toml_project, verify_toml_render_output
from .resolver import load_lock, resolve_project
from .runtime_artifacts import expected_mcremote_mount
from .runtime_content import HomepageTree, import_homepage_tree, import_runtime_file
from .toml_project import load_order


class CompositionContractError(ValueError):
    """Stable fail-closed diagnostic for composition canonicalization."""

    def __init__(self, reason: str, path: Path | str, message: str) -> None:
        self.reason = reason
        self.path = str(path)
        super().__init__(f"{reason}: {path}: {message}")


@dataclass(frozen=True)
class DiscoveredPlugin:
    filename: str
    sha256: str
    source: Path


@dataclass(frozen=True)
class OverlayComposition:
    plugins: tuple[DiscoveredPlugin, ...]
    homepage_source: Path
    backup_path: Path
    external_config_path: Path | None
    overlay_files: tuple[Path, ...]


@dataclass(frozen=True)
class CanonicalCompositionPlan:
    transaction: DeploymentUpdatePlan
    plugin_count: int
    homepage_tree_sha256: str
    backup_path: Path


@dataclass(frozen=True)
class _BindMount:
    source: Path
    target: str
    read_only: bool
    source_file: Path


def _fail(reason: str, path: Path | str, message: str) -> None:
    raise CompositionContractError(reason, path, message)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        _fail("composition_source_unreadable", path, str(exc))
    return digest.hexdigest()


def _real_file(path: Path, *, source_file: Path) -> Path:
    if path.is_symlink():
        _fail("composition_source_symlink", source_file, f"symlink source is forbidden: {path}")
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        _fail("composition_source_unreadable", path, str(exc))
    if not stat.S_ISREG(mode):
        _fail("composition_source_not_regular", source_file, f"regular file required: {path}")
    return path.resolve()


def _real_directory(path: Path, *, source_file: Path) -> Path:
    if path.is_symlink():
        _fail("composition_source_symlink", source_file, f"symlink source is forbidden: {path}")
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        _fail("composition_source_unreadable", path, str(exc))
    if not stat.S_ISDIR(mode):
        _fail("composition_source_not_directory", source_file, f"directory required: {path}")
    return path.resolve()


def _mount(record: object, *, source_file: Path) -> _BindMount:
    if isinstance(record, str):
        parts = record.split(":")
        if len(parts) not in {2, 3}:
            _fail("composition_overlay_unknown", source_file, "unsupported short volume syntax")
        source, target = parts[:2]
        read_only = len(parts) == 3 and parts[2] == "ro"
    elif isinstance(record, dict):
        if set(record) - {"type", "source", "target", "read_only"}:
            _fail("composition_overlay_unknown", source_file, "volume has unsupported fields")
        if record.get("type") != "bind":
            _fail("composition_overlay_unknown", source_file, "only bind overlays can be adopted")
        source = record.get("source")
        target = record.get("target")
        read_only = record.get("read_only", False)
    else:
        _fail("composition_overlay_unknown", source_file, "volume record is invalid")
    if (
        not isinstance(source, str)
        or not Path(source).is_absolute()
        or not isinstance(target, str)
        or not PurePosixPath(target).is_absolute()
        or not isinstance(read_only, bool)
    ):
        _fail(
            "composition_overlay_unknown",
            source_file,
            "adopted bind mounts require absolute source and target paths",
        )
    return _BindMount(Path(source), target, read_only, source_file)


def _load_overlay(path: Path) -> dict[str, list[_BindMount]]:
    if path.is_symlink() or not path.is_file():
        _fail("composition_overlay_unreadable", path, "overlay must be one real file")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        _fail("composition_overlay_unreadable", path, str(exc))
    if not isinstance(value, dict) or set(value) != {"services"}:
        _fail("composition_overlay_unknown", path, "overlay must contain only services")
    services = value["services"]
    if not isinstance(services, dict) or not services:
        _fail("composition_overlay_unknown", path, "overlay services must be non-empty")
    parsed: dict[str, list[_BindMount]] = {}
    for name, service in services.items():
        if name not in {"minecraft", "caddy"}:
            _fail("composition_overlay_unknown", path, f"unsupported service mutation: {name}")
        if not isinstance(service, dict) or set(service) != {"volumes"}:
            _fail(
                "composition_overlay_unknown",
                path,
                f"{name} overlay may contain only volumes",
            )
        volumes = service["volumes"]
        if not isinstance(volumes, list) or not volumes:
            _fail("composition_overlay_unknown", path, f"{name} volumes must be non-empty")
        parsed.setdefault(name, []).extend(_mount(item, source_file=path) for item in volumes)
    return parsed


def _public_routes(lock: dict[str, Any]) -> dict[str, Any]:
    matches = [
        item
        for item in lock.get("operator_inputs", [])
        if item.get("role") == "public-routes"
    ]
    if len(matches) != 1 or matches[0].get("adapter") not in {
        "public-routes@1",
        "public-routes@2",
    }:
        _fail(
            "composition_source_lock_invalid",
            "operator_inputs.public-routes",
            "canonicalization requires one recognized public-routes input",
        )
    return matches[0]["semantic"]


def _expected_legacy_caddyfile(routes: dict[str, Any]) -> str:
    homepage_domains = ", ".join([routes["homepage"], *routes["homepage_aliases"]])
    return f'''# Generated by mcrctl compose@N. Do not edit.
{homepage_domains} {{
    root * /srv/homepage
    encode zstd gzip
    file_server
}}

{routes["scratch"]} {{
    reverse_proxy scratch:8080
}}

{routes["bridge"]} {{
    reverse_proxy bridge:8080
}}
'''


def _validate_legacy_caddyfile(path: Path, routes: dict[str, Any], source_file: Path) -> None:
    _real_file(path, source_file=source_file)
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _fail("composition_source_unreadable", path, str(exc))
    normalized = re.sub(r"compose@[1-9][0-9]*", "compose@N", source, count=1)
    if normalized != _expected_legacy_caddyfile(routes):
        _fail(
            "composition_homepage_caddyfile_unknown",
            path,
            "homepage Caddyfile contains behavior outside the known generated template",
        )


def _validate_external_config_subset(source: Path, generated: Path) -> None:
    source = _real_directory(source, source_file=source)
    for candidate in sorted(source.rglob("*")):
        if candidate.is_symlink():
            _fail("composition_source_symlink", candidate, "config symlinks are forbidden")
        mode = candidate.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            _fail("composition_source_not_regular", candidate, "config entry must be regular")
        relative = candidate.relative_to(source)
        target = generated / relative
        if not target.is_file() or target.is_symlink():
            _fail(
                "composition_external_config_drift",
                candidate,
                "external /config contains a file not reproduced by the canonical render",
            )
        try:
            source_bytes = candidate.read_bytes()
            target_bytes = target.read_bytes()
        except OSError as exc:
            _fail("composition_source_unreadable", candidate, str(exc))
        if source_bytes == target_bytes:
            continue
        try:
            source_text = source_bytes.decode("utf-8")
            target_text = target_bytes.decode("utf-8")
        except UnicodeDecodeError:
            _fail(
                "composition_external_config_drift",
                candidate,
                "external /config binary differs from the canonical render",
            )
        marker = r"^# Generated by mcrctl compose@[1-9][0-9]*\. Do not edit\.$"
        source_text = re.sub(marker, "# Generated by mcrctl compose@N. Do not edit.", source_text, count=1)
        target_text = re.sub(marker, "# Generated by mcrctl compose@N. Do not edit.", target_text, count=1)
        if source_text != target_text:
            _fail(
                "composition_external_config_drift",
                candidate,
                "external /config differs from the canonical render beyond its generator revision",
            )


def _inspect_overlay_composition(
    overlay_files: tuple[Path, ...],
    *,
    source_output: Path,
    source_lock: dict[str, Any],
) -> OverlayComposition:
    """Classify only the reviewed public-runtime overlay shapes; reject everything else."""

    if not overlay_files:
        _fail("composition_overlay_missing", source_output, "no additional Compose files are active")
    services: dict[str, list[_BindMount]] = {}
    for path in overlay_files:
        for service, mounts in _load_overlay(path.resolve()).items():
            services.setdefault(service, []).extend(mounts)
    if set(services) != {"minecraft", "caddy"}:
        _fail(
            "composition_overlay_unknown",
            source_output,
            "canonicalization requires the reviewed minecraft and caddy overlays",
        )

    routes = _public_routes(source_lock)
    _expected_mcremote_source, mcremote_target = expected_mcremote_mount(source_lock)
    plugins: list[DiscoveredPlugin] = []
    homepage_source: Path | None = None
    caddyfile_seen = False
    backup_path: Path | None = None
    external_config: Path | None = None
    targets: set[str] = set()
    plugin_names: set[str] = set()

    for mount in services["minecraft"]:
        if mount.target in targets:
            _fail("composition_overlay_unknown", mount.source_file, f"duplicate target {mount.target}")
        targets.add(mount.target)
        target = PurePosixPath(mount.target)
        if mount.target == "/plugins":
            _fail(
                "composition_plugin_directory_forbidden",
                mount.source_file,
                "whole /plugins mounts can mask the exact McRemote artifact",
            )
        if target.parent == PurePosixPath("/plugins") and target.suffix.casefold() == ".jar":
            normalized_name = target.name.casefold()
            if (
                mount.target == mcremote_target
                or "mcremote" in normalized_name
                or "mc-remote" in normalized_name
            ):
                _fail(
                    "composition_additional_mcremote_forbidden",
                    mount.source_file,
                    "McRemote remains preset-owned and cannot be adopted as a peripheral plugin",
                )
            if not mount.read_only:
                _fail("composition_overlay_unknown", mount.source_file, "plugin binds must be read-only")
            source = _real_file(mount.source, source_file=mount.source_file)
            folded = target.name.casefold()
            if folded in plugin_names:
                _fail("composition_overlay_unknown", mount.source_file, "duplicate plugin filename")
            plugin_names.add(folded)
            plugins.append(DiscoveredPlugin(target.name, _sha256_file(source), source))
        elif mount.target == "/backup":
            if mount.read_only:
                _fail("composition_overlay_unknown", mount.source_file, "backup bind must be writable")
            backup_path = _real_directory(mount.source, source_file=mount.source_file)
        elif mount.target == "/config":
            if not mount.read_only:
                _fail("composition_overlay_unknown", mount.source_file, "config bind must be read-only")
            external_config = _real_directory(mount.source, source_file=mount.source_file)
            _validate_external_config_subset(external_config, source_output / "minecraft")
        else:
            _fail(
                "composition_overlay_unknown",
                mount.source_file,
                f"unsupported minecraft mount target: {mount.target}",
            )

    for mount in services["caddy"]:
        if mount.target not in {"/etc/caddy/Caddyfile", "/srv/homepage"} or not mount.read_only:
            _fail(
                "composition_overlay_unknown",
                mount.source_file,
                f"unsupported caddy mount target: {mount.target}",
            )
        if mount.target == "/etc/caddy/Caddyfile":
            if caddyfile_seen:
                _fail("composition_overlay_unknown", mount.source_file, "duplicate Caddyfile mount")
            caddyfile_seen = True
            _validate_legacy_caddyfile(mount.source, routes, mount.source_file)
        else:
            if homepage_source is not None:
                _fail("composition_overlay_unknown", mount.source_file, "duplicate homepage mount")
            homepage_source = _real_directory(mount.source, source_file=mount.source_file)

    if not plugins or backup_path is None or homepage_source is None or not caddyfile_seen:
        _fail(
            "composition_overlay_incomplete",
            source_output,
            "plugin set, backup path, homepage tree, and generated Caddyfile are all required",
        )
    return OverlayComposition(
        tuple(sorted(plugins, key=lambda item: item.filename.casefold())),
        homepage_source,
        backup_path,
        external_config,
        tuple(path.resolve() for path in overlay_files),
    )


def _atomic_new_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _add_operator_input(
    document: tomlkit.TOMLDocument,
    *,
    role: str,
    adapter: str,
    path: str,
) -> None:
    inputs = document.get("operator_inputs")
    if inputs is None:
        inputs = tomlkit.aot()
        document["operator_inputs"] = inputs
    if not isinstance(inputs, list):
        _fail("composition_candidate_invalid", "operator_inputs", "operator inputs are not an array")
    if any(item.get("role") == role for item in inputs):
        _fail("composition_candidate_invalid", role, "composition input already exists")
    table = tomlkit.table()
    table.add("role", role)
    table.add("adapter", adapter)
    table.add("path", path)
    inputs.append(table)


def _plugins_toml(plugins: tuple[DiscoveredPlugin, ...]) -> bytes:
    document = tomlkit.document()
    records = tomlkit.aot()
    for plugin in plugins:
        table = tomlkit.table()
        table.add("filename", plugin.filename)
        table.add("sha256", plugin.sha256)
        records.append(table)
    document.add("plugins", records)
    return tomlkit.dumps(document).encode("utf-8")


def _homepage_toml(homepage: HomepageTree) -> bytes:
    document = tomlkit.document()
    document.add("tree_sha256", homepage.tree_sha256)
    document.add("file_count", homepage.file_count)
    document.add("total_bytes", homepage.total_bytes)
    return tomlkit.dumps(document).encode("utf-8")


def _backup_toml(path: Path) -> bytes:
    document = tomlkit.document()
    document.add("host_path", str(path))
    return tomlkit.dumps(document).encode("utf-8")


def _prepare_composition_candidate(
    project_root: Path,
    output: Path,
    destination: Path,
    *,
    target_profile: str,
    target_preset: str,
    input_overrides: dict[tuple[str, str], str],
    data_root: Traversable,
    composition: OverlayComposition,
    homepage: HomepageTree,
) -> None:
    _copy_project_source(project_root.resolve(), destination, output)
    order_path = destination / "mc-remote.toml"
    document = tomlkit.parse(order_path.read_text(encoding="utf-8"))
    _add_operator_input(
        document,
        role="minecraft-plugins",
        adapter="minecraft-plugins@1",
        path="operator/minecraft-plugins/plugins.toml",
    )
    _add_operator_input(
        document,
        role="homepage-static",
        adapter="homepage-static@1",
        path="operator/homepage-static/homepage.toml",
    )
    _add_operator_input(
        document,
        role="minecraft-backup",
        adapter="minecraft-backup@1",
        path="operator/minecraft-backup/backup.toml",
    )
    order_path.write_text(tomlkit.dumps(document), encoding="utf-8")
    _atomic_new_file(
        destination / "operator/minecraft-plugins/plugins.toml",
        _plugins_toml(composition.plugins),
    )
    _atomic_new_file(
        destination / "operator/homepage-static/homepage.toml",
        _homepage_toml(homepage),
    )
    _atomic_new_file(
        destination / "operator/minecraft-backup/backup.toml",
        _backup_toml(composition.backup_path),
    )
    _adapt_candidate_order(
        destination,
        target_profile=target_profile,
        target_preset=target_preset,
        input_overrides=input_overrides,
        data_root=data_root,
    )
    load_order(destination)


def _validate_composition_transition(
    source: dict[str, Any],
    target: dict[str, Any],
) -> None:
    additions = frozenset(
        {"minecraft-plugins", "homepage-static", "minecraft-backup"}
    )
    _validate_in_place_transition(
        source,
        target,
        allowed_operator_input_additions=additions,
        allow_renderer_adapter_change=True,
    )
    target_profile = target["input"]["profile"]["ref"]
    controls = set(target["render_plan"]["required_security_controls"])
    if not {
        "exact-peripheral-plugin-set",
        "content-addressed-homepage",
        "explicit-backup-bind",
    }.issubset(controls):
        _fail(
            "composition_profile_invalid",
            target_profile,
            "target profile does not own all canonical composition controls",
        )
    source_roles = {item["role"] for item in source["operator_inputs"]}
    target_roles = {item["role"] for item in target["operator_inputs"]}
    if target_roles != source_roles | additions:
        _fail(
            "composition_operator_inputs_invalid",
            "operator_inputs",
            "canonicalization may add only the three reviewed composition inputs",
        )


class CompositionPlanHost(Protocol):
    def discover_source_composition(
        self,
        output: Path,
        *,
        deployment: str,
        lock_identity: str,
    ) -> tuple[Path, ...]: ...

    def validate_plan(
        self,
        plan: DeploymentUpdatePlan,
        source_lock: dict[str, Any],
        target_lock: dict[str, Any],
        target_output: Path,
    ) -> None: ...


class _DockerCompositionHost(_DockerUpdateHost):
    """Use overlays only for source stop/rollback, never for the canonical target."""

    def _target_compose(self, output: Path) -> list[str]:
        return _compose_stack(output, self.docker_prefix, self.project_root, ())

    def validate_plan(
        self,
        plan: DeploymentUpdatePlan,
        source_lock: dict[str, Any],
        target_lock: dict[str, Any],
        target_output: Path,
    ) -> None:
        del source_lock
        doctor_toml_project(
            plan.project_root,
            plan.output,
            docker_context=self.docker_context,
            data_root=self.data_root,
            runner=self.runner,
            hello_probe=self.hello_probe,
        )
        original = self.preserved_compose_files
        try:
            self.preserved_compose_files = ()
            self._validate_target_compose(target_output, target_lock)
        finally:
            self.preserved_compose_files = original

    def pull_target(self, plan: DeploymentUpdatePlan, target_output: Path) -> None:
        _run(
            self.runner,
            self._target_compose(target_output)
            + ["pull", "--policy", "always", "--quiet", *plan.services],
            timeout=900,
            reason="composition_target_pull_failed",
            path="docker.compose",
        )

    def start_target(self, plan: DeploymentUpdatePlan, target_output: Path) -> None:
        _run(
            self.runner,
            self._target_compose(target_output)
            + [
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                str(self.wait_timeout),
                "--no-build",
                "--pull",
                "never",
                *plan.services,
            ],
            timeout=self.wait_timeout + 60,
            reason="composition_target_start_failed",
            path="docker.compose",
        )


def _plan_canonical_composition_locked(
    project_root: Path,
    output: Path,
    *,
    target_profile: str,
    target_preset: str,
    input_overrides: dict[tuple[str, str], str],
    docker_context: str,
    data_root: Traversable,
    host: CompositionPlanHost | None = None,
    runner: CommandRunner = _default_runner,
    hello_probe: Any = probe_protocol_hello,
) -> CanonicalCompositionPlan:
    project_root = project_root.resolve()
    output = output.absolute()
    source_verification = verify_toml_render_output(
        project_root,
        output,
        data_root=data_root,
        allow_historical_lock=True,
    )
    source_lock = source_verification.lock
    profile = load_profile(target_profile, data_root=data_root)
    if "canonical-runtime-composition" not in profile.data["capabilities"]["provided"]:
        _fail(
            "composition_profile_invalid",
            target_profile,
            "target profile must provide canonical-runtime-composition",
        )
    actual_host = host or _DockerCompositionHost(
        project_root=project_root,
        docker_context=docker_context,
        data_root=data_root,
        wait_timeout=300,
        runner=runner,
        hello_probe=hello_probe,
    )
    overlays = actual_host.discover_source_composition(
        output,
        deployment=source_lock["deployment"]["name"],
        lock_identity=source_lock["lock_identity"],
    )
    composition = _inspect_overlay_composition(
        overlays,
        source_output=output,
        source_lock=source_lock,
    )
    artifact_store = Path(source_lock["runtime"]["artifact_store"])
    for plugin in composition.plugins:
        import_runtime_file(
            plugin.source,
            artifact_store,
            expected_sha256=plugin.sha256,
        )
    homepage = import_homepage_tree(composition.homepage_source, artifact_store)
    updates = _updates_root(project_root)
    updates.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".composition.", suffix=".prepare", dir=updates))
    moved = False
    try:
        candidate = temporary / "candidate"
        _prepare_composition_candidate(
            project_root,
            output,
            candidate,
            target_profile=target_profile,
            target_preset=target_preset,
            input_overrides=input_overrides,
            data_root=data_root,
            composition=composition,
            homepage=homepage,
        )
        order = load_order(project_root).order
        resolve_project(
            candidate,
            data_root=data_root,
            allow_unverified=order["acknowledgements"]["allow_unverified"],
            allow_eol=order["acknowledgements"]["allow_eol"],
            resolved_at=source_lock["resolved_at"],
        )
        fetch_locked_artifacts(candidate, data_root=data_root)
        target_output = candidate / "generated"
        render_toml_project(candidate, target_output, data_root=data_root)
        target_lock = load_lock(candidate, data_root=data_root)
        _validate_composition_transition(source_lock, target_lock)
        payload = _plan_payload(
            project_root=project_root,
            output=output,
            docker_context=docker_context,
            source_lock=source_lock,
            target_lock=target_lock,
            preserved_sha256=tuple(_sha256_file(path) for path in overlays),
        )
        payload["kind"] = "composition-canonicalization"
        plan = _make_plan(payload, overlays)
        if isinstance(actual_host, _DockerCompositionHost):
            actual_host.preserved_compose_files = overlays
        actual_host.validate_plan(plan, source_lock, target_lock, target_output)
        _prepare_transaction(plan, temporary_root=temporary, source_output=output)
        moved = True
        return CanonicalCompositionPlan(
            plan,
            len(composition.plugins),
            homepage.tree_sha256,
            composition.backup_path,
        )
    finally:
        if not moved and temporary.exists():
            shutil.rmtree(temporary)


def plan_canonical_composition(
    project_root: Path,
    output: Path,
    *,
    target_profile: str,
    target_preset: str,
    input_overrides: dict[tuple[str, str], str],
    docker_context: str,
    data_root: Traversable,
    host: CompositionPlanHost | None = None,
    runner: CommandRunner = _default_runner,
    hello_probe: Any = probe_protocol_hello,
) -> CanonicalCompositionPlan:
    project_root = project_root.resolve()
    descriptor, _path = _acquire_transaction_lock(project_root)
    try:
        _ensure_no_active_transaction(project_root)
        return _plan_canonical_composition_locked(
            project_root,
            output,
            target_profile=target_profile,
            target_preset=target_preset,
            input_overrides=input_overrides,
            docker_context=docker_context,
            data_root=data_root,
            host=host,
            runner=runner,
            hello_probe=hello_probe,
        )
    finally:
        _release_transaction_lock(descriptor)


def apply_canonical_composition(
    project_root: Path,
    *,
    plan_id: str,
    confirmed: bool,
    data_root: Traversable,
    wait_timeout: int = 300,
    runner: CommandRunner = _default_runner,
    hello_probe: Any = probe_protocol_hello,
    progress=lambda _step: None,
) -> DeploymentUpdateResult:
    project_root = project_root.resolve()
    plan = load_deployment_update_plan(project_root, plan_id)
    if plan.kind != "composition-canonicalization":
        _fail(
            "composition_plan_kind_mismatch",
            plan_id,
            "plan is not a composition canonicalization transaction",
        )
    state = _load_state(project_root, plan_id)
    snapshots = _snapshot_paths(project_root, state)
    host = _DockerCompositionHost(
        project_root=project_root,
        docker_context=plan.docker_context,
        data_root=data_root,
        wait_timeout=wait_timeout,
        runner=runner,
        hello_probe=hello_probe,
        preserved_compose_files=snapshots,
    )
    return apply_deployment_update(
        project_root,
        plan_id=plan_id,
        confirmed=confirmed,
        data_root=data_root,
        wait_timeout=wait_timeout,
        host=host,
        runner=runner,
        hello_probe=hello_probe,
        progress=progress,
        expected_kind="composition-canonicalization",
    )
