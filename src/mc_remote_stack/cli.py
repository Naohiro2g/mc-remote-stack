"""mcrctl command-line entry point."""

import argparse
import ftplib
import getpass
import json
import zipfile
from importlib.resources import files
from pathlib import Path

from . import __version__
from .apply import ApplyContractError, apply_toml_project
from .archive import inspect_archive
from .artifacts import (
    ArtifactFetchError,
    fetch_locked_artifacts,
    import_recovery_archive,
)
from .backup import BackupTransferError, transfer_archive
from .doctor import DoctorContractError, doctor_toml_project
from .operator_inputs import OperatorInputError, resolve_operator_inputs
from .preset_registry import (
    PresetDataError,
    load_preset,
    load_preset_catalog,
    load_profile,
)
from .project import accept_eula, init_project
from .render import RenderContractError, RenderError, render_project, render_toml_project
from .repo_check import check_repository
from .resolver import ResolutionError, inspect_lock, load_lock, resolve_project
from .secrets import list_secrets, set_secret
from .toml_project import (
    ProjectOrderError,
    init_toml_project,
    load_order,
    update_order_scalar,
)
from .validation import Issue, try_load_project


def _print_issues(issues: list[Issue]) -> int:
    for issue in issues:
        print(f"{issue.severity} {issue.path}: {issue.message}")
    if any(issue.severity == "FAIL" for issue in issues):
        return 2
    if issues:
        return 1
    print("OK")
    return 0


def _preset_data_root():
    return files("mc_remote_stack").joinpath("data")


def _print_structured_failure(
    operation: str,
    exc: (
        ArtifactFetchError
        | ApplyContractError
        | DoctorContractError
        | OperatorInputError
        | PresetDataError
        | ProjectOrderError
        | RenderContractError
        | ResolutionError
    ),
) -> int:
    print(f"FAIL {operation} reason={exc.reason} path={exc.path}")
    print(f"DETAIL {exc}")
    return 2


def _print_reason_failure(operation: str, reason: str, path: Path, message: str) -> int:
    print(f"FAIL {operation} reason={reason} path={path}")
    print(f"DETAIL {message}")
    return 2


def _catalog_entries(*, include_eol: bool) -> list[dict]:
    catalog = load_preset_catalog(data_root=_preset_data_root())
    entries = catalog["preset_catalog"]["presets"]
    if include_eol:
        return entries
    return [entry for entry in entries if entry["status"] != "eol"]


def _print_preset_summary(entry: dict) -> None:
    print(
        f"PRESET ref={entry['ref']} status={entry['status']} "
        f"compatibility={entry['compatibility_status']} "
        f"content-sha256={entry['content_sha256']}"
    )


def _cmd_preset_list(args: argparse.Namespace) -> int:
    try:
        entries = _catalog_entries(include_eol=args.all)
    except PresetDataError as exc:
        return _print_structured_failure("preset list", exc)
    if not entries:
        print("PRESET none")
        return 0
    for entry in entries:
        _print_preset_summary(entry)
    return 0


def _cmd_preset_show(args: argparse.Namespace) -> int:
    try:
        preset = load_preset(args.ref, data_root=_preset_data_root())
        catalog = load_preset_catalog(data_root=_preset_data_root())
    except PresetDataError as exc:
        return _print_structured_failure("preset show", exc)

    entry = next(
        (
            candidate
            for candidate in catalog["preset_catalog"]["presets"]
            if candidate["ref"] == preset.ref
        ),
        None,
    )
    if entry is None:
        requirements = preset.data["requirements"]
        entry = {
            "ref": preset.ref,
            "status": "not-offered",
            "compatibility_status": "unverified",
            "content_sha256": preset.content_sha256,
            "required_profile_capabilities": requirements["profile_capabilities"],
            "allowed_channels": requirements["allowed_channels"],
            "compatibility_records": [],
        }
    _print_preset_summary(entry)
    print(f"PRESET description={preset.data['preset']['description']}")
    print(
        "PRESET required-profile-capabilities="
        + ",".join(entry["required_profile_capabilities"])
    )
    print("PRESET allowed-channels=" + ",".join(entry["allowed_channels"]))
    records = ",".join(entry["compatibility_records"]) or "none"
    print(f"PRESET compatibility-records={records}")
    for component in preset.data["components"]:
        print(
            f"COMPONENT id={component['id']} role={component['role']} "
            f"artifact={component['artifact']}"
        )
    for artifact in preset.data["artifacts"]:
        identity = " ".join(
            f"{key.replace('_', '-')}={artifact[key]}"
            for key in (
                "digest",
                "sha256",
                "commit",
                "recipe_sha256",
                "toolchain_sha256",
                "build_input_sha256",
                "archive_sha256",
                "member",
                "output_sha256",
            )
            if key in artifact
        )
        print(f"ARTIFACT id={artifact['id']} kind={artifact['kind']} {identity}")
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    try:
        result = resolve_project(
            Path(args.project),
            data_root=_preset_data_root(),
            allow_unverified=args.allow_unverified,
            allow_eol=args.allow_eol,
        )
    except (PresetDataError, ResolutionError) as exc:
        return _print_structured_failure("resolve", exc)
    except OSError as exc:
        print(f"FAIL resolve: {exc}")
        return 2
    print(f"OK resolve status={result.status} lock={result.lock_identity}")
    for warning in result.warnings:
        print(f"WARN {warning}")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    if args.format == "toml":
        required_arguments = (
            ("--deployment-name", args.deployment_name),
            ("--profile", args.profile),
            ("--environment-identity", args.environment_identity),
            ("--channel", args.channel),
            ("--exposure", args.exposure),
            ("--purpose", args.purpose),
            ("--preset", args.preset),
            ("--artifact-store", args.artifact_store),
            ("--volume", args.volume),
            ("--world-identity", args.world_identity),
            ("--bind-address", args.bind_address),
            ("--java-port", args.java_port),
            ("--mcremote-port", args.mcremote_port),
        )
        missing = [name for name, value in required_arguments if value is None or value == []]
        project_path = Path(args.path).resolve()
        if missing:
            return _print_reason_failure(
                "init",
                "missing_toml_init_argument",
                project_path,
                "missing required TOML init arguments: " + ", ".join(missing),
            )

        runtime_volumes: dict[str, str] = {}
        for assignment in args.volume:
            if assignment.count("=") != 1:
                return _print_reason_failure(
                    "init",
                    "invalid_volume_assignment",
                    project_path,
                    f"--volume must use ROLE=IDENTITY exactly once: {assignment!r}",
                )
            role, identity = assignment.split("=", 1)
            if not role or not identity:
                return _print_reason_failure(
                    "init",
                    "invalid_volume_assignment",
                    project_path,
                    f"--volume requires non-empty ROLE and IDENTITY: {assignment!r}",
                )
            if role in runtime_volumes:
                return _print_reason_failure(
                    "init",
                    "duplicate_volume_assignment",
                    project_path,
                    f"--volume role is assigned more than once: {role}",
                )
            runtime_volumes[role] = identity

        try:
            paths = init_toml_project(
                Path(args.path),
                deployment_name=args.deployment_name,
                profile=args.profile,
                environment_identity=args.environment_identity,
                channel=args.channel,
                exposure=args.exposure,
                purpose=args.purpose,
                preset=args.preset,
                artifact_store=args.artifact_store,
                runtime_volumes=runtime_volumes,
                world_identity=args.world_identity,
                bind_address=args.bind_address,
                java_port=args.java_port,
                mcremote_port=args.mcremote_port,
            )
        except ProjectOrderError as exc:
            return _print_structured_failure("init", exc)
        except (OSError, ValueError) as exc:
            print(f"FAIL init: {exc}")
            return 2
        print(f"OK initialized format=toml project={paths.root}")
        print(f"NEXT mcrctl accept-eula --project {paths.root} --yes")
        print(f"NEXT mcrctl resolve --project {paths.root}")
        return 0

    toml_only_arguments = (
        ("--deployment-name", args.deployment_name),
        ("--environment-identity", args.environment_identity),
        ("--channel", args.channel),
        ("--exposure", args.exposure),
        ("--purpose", args.purpose),
        ("--preset", args.preset),
        ("--artifact-store", args.artifact_store),
        ("--volume", args.volume),
        ("--world-identity", args.world_identity),
        ("--bind-address", args.bind_address),
        ("--java-port", args.java_port),
        ("--mcremote-port", args.mcremote_port),
    )
    unexpected = [
        name
        for name, value in toml_only_arguments
        if value is not None and value != []
    ]
    if unexpected:
        return _print_reason_failure(
            "init",
            "toml_init_argument_requires_format",
            Path(args.path).resolve(),
            f"{', '.join(unexpected)} require --format toml",
        )

    try:
        paths = init_project(Path(args.path), args.profile or "official-vps")
    except ValueError as exc:
        print(f"FAIL init: {exc}")
        return 2
    print(f"OK initialized {paths.root}")
    print("NEXT review mc-remote.yml, set secrets, accept EULA, then resolve immutable artifacts")
    return 0


def _uses_toml_project(project: Path) -> bool:
    root = project.resolve()
    return (root / "mc-remote.toml").exists() or (root / "mc-remote.lock.toml").exists()


def _cmd_toml_validate(project_path: Path) -> int:
    try:
        order = load_order(project_path)
        profile = load_profile(
            order.order["deployment"]["profile"],
            data_root=_preset_data_root(),
        )
        resolve_operator_inputs(order, profile.data)
    except (OperatorInputError, PresetDataError, ProjectOrderError) as exc:
        return _print_structured_failure("validate", exc)
    if not order.paths.lock.exists():
        print("OK validate format=toml order=valid lock=missing")
        return 0
    try:
        inspection = inspect_lock(project_path, data_root=_preset_data_root())
    except (PresetDataError, ProjectOrderError, ResolutionError) as exc:
        return _print_structured_failure("validate", exc)
    if inspection.status == "stale":
        return _print_reason_failure(
            "validate",
            "stale_lock",
            order.paths.lock,
            "order or exact bundled input changed; run mcrctl resolve explicitly",
        )
    print(
        "OK validate format=toml order=valid lock=valid "
        f"identity={inspection.current_lock_identity}"
    )
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    if _uses_toml_project(project_path):
        return _cmd_toml_validate(project_path)
    _, issues = try_load_project(project_path)
    return _print_issues(issues)


def _cmd_accept_eula(args: argparse.Namespace) -> int:
    if not args.yes:
        print("FAIL EULA acceptance requires --yes after reading https://aka.ms/MinecraftEULA")
        return 2
    project_path = Path(args.project)
    if _uses_toml_project(project_path):
        try:
            changed = update_order_scalar(
                project_path,
                ("agreements", "minecraft_eula"),
                True,
            )
        except ProjectOrderError as exc:
            return _print_structured_failure("accept-eula", exc)
        except (OSError, ValueError) as exc:
            print(f"FAIL accept-eula: {exc}")
            return 2
        status = "recorded" if changed else "already-recorded"
        print(
            f"OK {status} explicit EULA acceptance in "
            f"{project_path.resolve() / 'mc-remote.toml'}"
        )
        return 0
    try:
        paths = accept_eula(project_path)
    except (OSError, ValueError) as exc:
        print(f"FAIL accept-eula: {exc}")
        return 2
    print(f"OK recorded explicit EULA acceptance in {paths.config}")
    return 0


def _deployment_name(project_path: str) -> tuple[str | None, int]:
    project, issues = try_load_project(Path(project_path))
    load_failures = [issue for issue in issues if issue.path == str(Path(project_path).resolve())]
    if project is None or load_failures:
        return None, _print_issues(load_failures or issues)
    name = project.config.get("deployment", {}).get("name")
    if not isinstance(name, str):
        print("FAIL mc-remote.yml:deployment.name must be a string")
        return None, 2
    return name, 0


def _cmd_secret_set(args: argparse.Namespace) -> int:
    deployment_name, status = _deployment_name(args.project)
    if deployment_name is None:
        return status
    try:
        if args.from_file:
            value = Path(args.from_file).read_text(encoding="utf-8").rstrip("\r\n")
        else:
            value = getpass.getpass(f"Secret {args.name}: ")
        destination = set_secret(deployment_name, args.name, value)
    except (OSError, ValueError) as exc:
        print(f"FAIL secret set: {exc}")
        return 2
    print(f"OK stored {args.name} for {deployment_name} at {destination.parent} (value hidden)")
    return 0


def _cmd_secret_list(args: argparse.Namespace) -> int:
    deployment_name, status = _deployment_name(args.project)
    if deployment_name is None:
        return status
    names = list_secrets(deployment_name)
    if not names:
        print("WARN no local secrets stored")
        return 1
    for name in names:
        print(name)
    return 0


def _cmd_repo_check(args: argparse.Namespace) -> int:
    return _print_issues(check_repository(Path(args.project)))


def _artifact_identity(artifact: dict) -> str:
    return " ".join(
        f"{key.replace('_', '-')}={artifact[key]}"
        for key in (
            "digest",
            "sha256",
            "commit",
            "archive_sha256",
            "member",
            "output_sha256",
        )
        if key in artifact
    )


def _cmd_toml_plan(project_path: Path) -> int:
    try:
        order = load_order(project_path)
        inspection = inspect_lock(project_path, data_root=_preset_data_root())
    except (PresetDataError, ProjectOrderError, ResolutionError) as exc:
        return _print_structured_failure("plan", exc)
    if inspection.status == "missing":
        return _print_reason_failure(
            "plan",
            "lock_missing",
            order.paths.lock,
            "resolve the project before plan or render",
        )
    if inspection.status == "stale":
        return _print_reason_failure(
            "plan",
            "stale_lock",
            order.paths.lock,
            "order or exact bundled input changed; run mcrctl resolve explicitly",
        )
    try:
        lock = load_lock(project_path, data_root=_preset_data_root())
    except (PresetDataError, ResolutionError) as exc:
        return _print_structured_failure("plan", exc)

    environment = lock["environment"]
    print(
        f"PLAN deployment={lock['deployment']['name']} "
        f"environment={environment['identity']}"
    )
    print(
        f"PLAN channel={environment['channel']} exposure={environment['exposure']} "
        f"purpose={environment['purpose']}"
    )
    print(
        f"PLAN profile={lock['input']['profile']['ref']} "
        f"content-sha256={lock['input']['profile']['content_sha256']}"
    )
    print(
        f"PLAN preset={lock['input']['preset']['ref']} "
        f"content-sha256={lock['input']['preset']['content_sha256']}"
    )
    print(
        f"PLAN selection={lock['selection']['kind']} "
        f"compatibility={lock['compatibility']['status']} "
        f"lifecycle={lock['preset_lifecycle']['status']}"
    )
    print(f"PLAN artifact-store={lock['runtime']['artifact_store']}")
    for volume in lock["runtime"]["volumes"]:
        print(f"PLAN runtime-volume={volume['role']}:{volume['identity']}")
    print(f"PLAN world={lock['world']['identity']}")
    network = lock["network"]
    print(
        f"PLAN network-bind={network['bind_address']} "
        f"java-port={network['java_port']} mcremote-port={network['mcremote_port']}"
    )
    eula_status = "accepted" if lock["agreements"]["minecraft_eula"] else "not-accepted"
    print(f"PLAN minecraft-eula={eula_status}")
    for artifact in lock["artifacts"]:
        print(
            f"PLAN artifact={artifact['id']} kind={artifact['kind']} "
            f"{_artifact_identity(artifact)}"
        )
    for operator_input in lock["operator_inputs"]:
        print(
            f"PLAN operator-input={operator_input['role']} "
            f"adapter={operator_input['adapter']} path={operator_input['path']} "
            f"semantic-sha256={operator_input['semantic_sha256']}"
        )
    volume_roles = ",".join(
        f"{role['id']}:{role['kind']}" for role in lock["render_plan"]["volume_roles"]
    )
    print(f"PLAN volume-roles={volume_roles}")
    security_controls = ",".join(lock["render_plan"]["required_security_controls"])
    print(f"PLAN security-controls={security_controls}")
    print(f"PLAN lock=unchanged identity={lock['lock_identity']}")

    warnings: list[str] = []
    lifecycle_warning = lock["preset_lifecycle"].get("warning")
    if lifecycle_warning:
        warnings.append(
            f"preset {lock['preset_lifecycle']['status']}: {lifecycle_warning}"
        )
    if lock["compatibility"]["status"] == "unverified":
        warnings.append("compatibility evidence does not cover all required claims")
    for warning in warnings:
        print(f"WARN {warning}")
    return 1 if warnings else 0


def _cmd_plan(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    if _uses_toml_project(project_path):
        return _cmd_toml_plan(project_path)
    project, issues = try_load_project(project_path)
    issues.extend(check_repository(project_path))
    if project is not None:
        print(f"PLAN deployment={project.config.get('deployment', {}).get('name', 'unknown')}")
        print(
            "PLAN services=caddy,scratch-stable,scratch-beta,bridge-stable,bridge-beta,minecraft-stable,minecraft-beta"
        )
        print("PLAN public-ports=80/tcp,443/tcp,25565/tcp,25565/udp,25575/tcp")
        print("PLAN rcon=disabled backup-source=@server backup-output=/backup/outbox")
        beta = project.config.get("beta", {})
        if isinstance(beta, dict) and beta.get("enabled") is True:
            ports = beta.get("minecraft", {})
            print("PLAN beta=enabled activation=compose-profile:beta default=dormant")
            print(
                "PLAN beta-public-ports="
                f"{ports.get('java_port', 'unknown')}/tcp,"
                f"{ports.get('bedrock_port', 'unknown')}/udp,"
                f"{ports.get('mcremote_port', 'unknown')}/tcp"
            )
        else:
            print("PLAN beta=disabled")
        transport = project.config.get("backup", {}).get("transport")
        if isinstance(transport, dict):
            encryption = transport.get("encryption", {})
            print(
                f"PLAN backup-transport={transport.get('type', 'unknown')} "
                f"backup-encryption={encryption.get('type', 'unknown')} "
                f"backup-remote={transport.get('host', 'unknown')}:{transport.get('remote_directory', 'unknown')}"
            )
        else:
            print(f"PLAN backup-transport={transport}")
    return _print_issues(issues)


def _cmd_render(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    if _uses_toml_project(project_path):
        try:
            result = render_toml_project(
                project_path,
                Path(args.output),
                data_root=_preset_data_root(),
            )
        except (PresetDataError, ProjectOrderError, RenderContractError, ResolutionError) as exc:
            return _print_structured_failure("render", exc)
        except OSError as exc:
            print(f"FAIL render: {exc}")
            return 2
        print(
            f"OK render status={result.status} "
            f"adapter={result.adapter}@{result.adapter_revision} "
            f"lock={result.lock_identity} output={result.output}"
        )
        return 0

    project, issues = try_load_project(project_path)
    failures = [issue for issue in issues if issue.severity == "FAIL"]
    if project is None or failures:
        return _print_issues(issues)
    try:
        paths = render_project(project, Path(args.output))
    except (OSError, RenderError) as exc:
        print(f"FAIL render: {exc}")
        return 2
    for path in paths:
        print(f"OK rendered {path}")
    return 1 if issues else 0


def _cmd_apply(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    if not _uses_toml_project(project_path):
        return _print_reason_failure(
            "apply",
            "apply_requires_toml",
            project_path.resolve(),
            "bootstrap apply supports only one-environment TOML deployment projects",
        )
    try:
        result = apply_toml_project(
            project_path,
            Path(args.output),
            expected_lock_identity=args.expected_lock_identity,
            docker_context=args.docker_context,
            data_root=_preset_data_root(),
            bootstrap=args.bootstrap,
            confirmed=args.yes,
            allow_unverified=args.allow_unverified,
            allow_eol=args.allow_eol,
            wait_timeout=args.wait_timeout,
            progress=lambda step: print(
                f"PROGRESS apply step={step}",
                flush=True,
            ),
        )
    except (
        ApplyContractError,
        PresetDataError,
        ProjectOrderError,
        RenderContractError,
        ResolutionError,
    ) as exc:
        return _print_structured_failure("apply", exc)
    except OSError as exc:
        print(f"FAIL apply: {exc}")
        return 2
    print(
        f"OK apply status={result.status} bootstrap=true "
        f"lock={result.lock_identity} compose-project={result.compose_project} "
        f"service={result.service} volume={result.volume}"
    )
    if args.allow_unverified:
        print("WARN live bootstrap used the one-shot unverified acknowledgement")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    if not _uses_toml_project(project_path):
        return _print_reason_failure(
            "doctor",
            "doctor_requires_toml",
            project_path.resolve(),
            "doctor supports only one-environment TOML deployment projects",
        )
    output = Path(args.output) if args.output else project_path / "generated"
    try:
        result = doctor_toml_project(
            project_path,
            output,
            docker_context=args.docker_context,
            data_root=_preset_data_root(),
            timeout=args.timeout,
        )
    except (
        DoctorContractError,
        PresetDataError,
        ProjectOrderError,
        RenderContractError,
        ResolutionError,
    ) as exc:
        return _print_structured_failure("doctor", exc)
    except OSError as exc:
        print(f"FAIL doctor: {exc}")
        return 2

    print(
        f"OK doctor runtime={result.runtime_status} "
        f"deployment={result.deployment} environment={result.environment}"
    )
    print(
        f"OK doctor lock={result.lock_identity} "
        f"render=current context={result.docker_context}"
    )
    print(
        f"OK doctor network={result.network_scope} bind={result.bind_address} "
        f"java-port={result.java_port} mcremote-port={result.mcremote_port}"
    )
    if result.protocol_status == "ok":
        print(
            f"OK doctor protocol={result.protocol} "
            f"mc-version={result.minecraft_version} auth=not-required"
        )
    else:
        print("OK doctor protocol=responsive auth=required")
    if result.compatibility_status == "unverified":
        print("WARN doctor compatibility=unverified")
    return 0


def _cmd_archive_inspect(args: argparse.Namespace) -> int:
    try:
        inventory = inspect_archive(Path(args.archive))
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"FAIL archive inspect: {exc}")
        return 2
    if args.json:
        print(json.dumps(inventory, ensure_ascii=False, indent=2))
    else:
        print(f"ARCHIVE name={inventory['archive_name']}")
        print(f"ARCHIVE sha256={inventory['archive_sha256']}")
        print(
            "ARCHIVE "
            f"compressed-bytes={inventory['compressed_size_bytes']} "
            f"uncompressed-bytes={inventory['uncompressed_size_bytes']} "
            f"entries={inventory['entry_count']} regions={inventory['region_files']} "
            f"ignored-nested-plugin-jars={inventory['ignored_nested_plugin_jars']}"
        )
        print(f"ARCHIVE crc={'OK' if inventory['crc_ok'] else 'FAIL'}")
        for server_jar in inventory["server_jars"]:
            print(
                f"SERVER-JAR filename={server_jar['filename']} sha256={server_jar['sha256']} "
                f"size-bytes={server_jar['size_bytes']}"
            )
        for plugin in inventory["plugin_jars"]:
            descriptor = plugin["descriptor"]
            identity = ""
            if descriptor["status"] == "ok":
                identity = f" plugin={descriptor['name']}@{descriptor['version']}"
            print(
                f"PLUGIN filename={plugin['filename']} sha256={plugin['sha256']} size-bytes={plugin['size_bytes']}"
                f" descriptor={descriptor['status']}{identity}"
            )
    return 0 if inventory["crc_ok"] else 2


def _cmd_artifact_import_archive(args: argparse.Namespace) -> int:
    try:
        imported = import_recovery_archive(
            Path(args.project),
            Path(args.archive),
            Path(args.store) if args.store else None,
        )
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"FAIL artifact import-archive: {exc}")
        return 2
    for artifact in imported:
        print(
            f"OK artifact {artifact.status} name={artifact.name} filename={artifact.filename} "
            f"sha256={artifact.sha256} path={artifact.path}"
        )
    return 0


def _cmd_artifact_fetch(args: argparse.Namespace) -> int:
    try:
        fetched = fetch_locked_artifacts(
            Path(args.project),
            data_root=_preset_data_root(),
        )
    except (
        ArtifactFetchError,
        PresetDataError,
        ProjectOrderError,
        ResolutionError,
    ) as exc:
        return _print_structured_failure("artifact fetch", exc)
    except OSError as exc:
        print(f"FAIL artifact fetch: {exc}")
        return 2
    if not fetched:
        print("OK artifact status=none kind=https-file")
        return 0
    for artifact in fetched:
        print(
            f"OK artifact status={artifact.status} id={artifact.id} "
            f"sha256={artifact.sha256} path={artifact.path}"
        )
    return 0


def _cmd_backup_transfer(args: argparse.Namespace) -> int:
    project, issues = try_load_project(Path(args.project))
    failures = [issue for issue in issues if issue.severity == "FAIL"]
    if project is None or failures:
        return _print_issues(issues)
    try:
        result = transfer_archive(project, Path(args.archive), verify_download=args.verify_download)
    except (BackupTransferError, ftplib.Error, OSError, ValueError) as exc:
        print(f"FAIL backup transfer: {exc}")
        return 2
    print(
        f"OK backup transfer status={result.status} remote={result.remote_name} "
        f"sha256={result.encrypted_sha256} size-bytes={result.encrypted_size_bytes} record={result.record_path}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcrctl")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create a deployment project")
    init_parser.add_argument("path")
    init_parser.add_argument(
        "--format",
        choices=("legacy-yaml", "toml"),
        default="legacy-yaml",
    )
    init_parser.add_argument("--deployment-name")
    init_parser.add_argument("--profile")
    init_parser.add_argument("--environment-identity")
    init_parser.add_argument("--channel")
    init_parser.add_argument("--exposure")
    init_parser.add_argument("--purpose")
    init_parser.add_argument("--preset")
    init_parser.add_argument("--artifact-store")
    init_parser.add_argument("--volume", action="append")
    init_parser.add_argument("--world-identity")
    init_parser.add_argument("--bind-address")
    init_parser.add_argument("--java-port", type=int)
    init_parser.add_argument("--mcremote-port", type=int)
    init_parser.set_defaults(handler=_cmd_init)

    preset_parser = subparsers.add_parser("preset", help="bundled preset discovery")
    preset_subparsers = preset_parser.add_subparsers(dest="preset_command", required=True)
    preset_list_parser = preset_subparsers.add_parser("list", help="list offered exact preset revisions")
    preset_list_parser.add_argument("--all", action="store_true", help="include EOL revisions")
    preset_list_parser.set_defaults(handler=_cmd_preset_list)
    preset_show_parser = preset_subparsers.add_parser("show", help="show one exact preset revision")
    preset_show_parser.add_argument("ref")
    preset_show_parser.set_defaults(handler=_cmd_preset_show)

    resolve_parser = subparsers.add_parser("resolve", help="resolve one TOML deployment project")
    resolve_parser.add_argument("--project", required=True)
    resolve_parser.add_argument("--allow-unverified", action="store_true")
    resolve_parser.add_argument("--allow-eol", action="store_true")
    resolve_parser.set_defaults(handler=_cmd_resolve)

    validate_parser = subparsers.add_parser("validate", help="validate deployment config and lock")
    validate_parser.add_argument("--project", required=True)
    validate_parser.set_defaults(handler=_cmd_validate)

    eula_parser = subparsers.add_parser("accept-eula", help="record explicit Minecraft EULA acceptance")
    eula_parser.add_argument("--project", required=True)
    eula_parser.add_argument("--yes", action="store_true")
    eula_parser.set_defaults(handler=_cmd_accept_eula)

    secret_parser = subparsers.add_parser("secret", help="local secret store operations")
    secret_subparsers = secret_parser.add_subparsers(dest="secret_command", required=True)
    secret_set_parser = secret_subparsers.add_parser("set", help="store a secret without adding it to Git")
    secret_set_parser.add_argument("name")
    secret_set_parser.add_argument("--project", required=True)
    secret_set_parser.add_argument("--from-file")
    secret_set_parser.set_defaults(handler=_cmd_secret_set)
    secret_list_parser = secret_subparsers.add_parser("list", help="list secret names without values")
    secret_list_parser.add_argument("--project", required=True)
    secret_list_parser.set_defaults(handler=_cmd_secret_list)

    repo_parser = subparsers.add_parser("repo", help="deployment repository operations")
    repo_subparsers = repo_parser.add_subparsers(dest="repo_command", required=True)
    check_parser = repo_subparsers.add_parser("check", help="check for secret and generated-file leakage")
    check_parser.add_argument("--project", required=True)
    check_parser.set_defaults(handler=_cmd_repo_check)

    plan_parser = subparsers.add_parser("plan", help="show deployment intent and blockers")
    plan_parser.add_argument("--project", required=True)
    plan_parser.set_defaults(handler=_cmd_plan)

    render_parser = subparsers.add_parser("render", help="render validated runtime configuration")
    render_parser.add_argument("--project", required=True)
    render_parser.add_argument("--output", required=True)
    render_parser.set_defaults(handler=_cmd_render)

    apply_parser = subparsers.add_parser(
        "apply",
        help="apply an exact managed TOML render to the local Docker daemon",
    )
    apply_parser.add_argument("--project", required=True)
    apply_parser.add_argument("--output", required=True)
    apply_parser.add_argument("--expected-lock-identity", required=True)
    apply_parser.add_argument("--docker-context", required=True)
    apply_parser.add_argument("--bootstrap", action="store_true")
    apply_parser.add_argument("--yes", action="store_true")
    apply_parser.add_argument("--allow-unverified", action="store_true")
    apply_parser.add_argument("--allow-eol", action="store_true")
    apply_parser.add_argument("--wait-timeout", type=int, default=300)
    apply_parser.set_defaults(handler=_cmd_apply)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="read-only check of one current TOML Docker runtime and protocol",
    )
    doctor_parser.add_argument("--project", required=True)
    doctor_parser.add_argument("--output")
    doctor_parser.add_argument("--docker-context", default="default")
    doctor_parser.add_argument("--timeout", type=int, default=5)
    doctor_parser.set_defaults(handler=_cmd_doctor)

    archive_parser = subparsers.add_parser("archive", help="recovery archive operations")
    archive_subparsers = archive_parser.add_subparsers(dest="archive_command", required=True)
    inspect_parser = archive_subparsers.add_parser("inspect", help="verify and inventory a ZIP without extracting it")
    inspect_parser.add_argument("archive")
    inspect_parser.add_argument("--json", action="store_true")
    inspect_parser.set_defaults(handler=_cmd_archive_inspect)

    artifact_parser = subparsers.add_parser("artifact", help="immutable artifact store operations")
    artifact_subparsers = artifact_parser.add_subparsers(dest="artifact_command", required=True)
    fetch_parser = artifact_subparsers.add_parser(
        "fetch",
        help="fetch exact HTTPS files named by the current TOML lock",
    )
    fetch_parser.add_argument("--project", required=True)
    fetch_parser.set_defaults(handler=_cmd_artifact_fetch)
    import_archive_parser = artifact_subparsers.add_parser(
        "import-archive",
        help="import only lock-named JARs from a recovery ZIP",
    )
    import_archive_parser.add_argument("archive")
    import_archive_parser.add_argument("--project", required=True)
    import_archive_parser.add_argument("--store")
    import_archive_parser.set_defaults(handler=_cmd_artifact_import_archive)

    backup_parser = subparsers.add_parser("backup", help="encrypted backup transfer operations")
    backup_subparsers = backup_parser.add_subparsers(dest="backup_command", required=True)
    transfer_parser = backup_subparsers.add_parser(
        "transfer",
        help="encrypt an archive and upload it with explicit FTPS",
    )
    transfer_parser.add_argument("archive")
    transfer_parser.add_argument("--project", required=True)
    transfer_parser.add_argument("--verify-download", action="store_true")
    transfer_parser.set_defaults(handler=_cmd_backup_transfer)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)
