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
from .auth_migration import (
    AuthMigrationContractError,
    apply_auth_enforcement_migration,
    apply_public_b3_upgrade,
    apply_public_b4_upgrade,
    plan_auth_enforcement_migration,
    plan_public_b3_upgrade,
    plan_public_b4_upgrade,
)
from .backup import (
    BackupTransferError,
    decrypt_downloaded_archive,
    download_remote_archive,
    download_remote_record,
    list_remote_archives,
    load_backup_endpoint,
    ready_outbox_archives,
    transfer_archive,
)
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
from .restore import (
    WorldRestoreError,
    apply_world_restore,
    plan_world_restore,
)
from .runtime_audit import audit_minecraft_log
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
        | AuthMigrationContractError
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
    if _uses_toml_project(Path(project_path)):
        try:
            order = load_order(Path(project_path))
        except ProjectOrderError as exc:
            return None, _print_structured_failure("secret", exc)
        return order.order["deployment"]["name"], 0
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


def _migration_target_volumes(
    assignments: list[str],
    *,
    operation: str,
    project: Path,
) -> tuple[dict[str, str] | None, int]:
    volumes: dict[str, str] = {}
    for assignment in assignments:
        if assignment.count("=") != 1:
            return None, _print_reason_failure(
                operation,
                "invalid_volume_assignment",
                project,
                f"--target-volume must use ROLE=IDENTITY exactly once: {assignment!r}",
            )
        role, identity = assignment.split("=", 1)
        if not role or not identity:
            return None, _print_reason_failure(
                operation,
                "invalid_volume_assignment",
                project,
                f"--target-volume requires non-empty ROLE and IDENTITY: {assignment!r}",
            )
        if role in volumes:
            return None, _print_reason_failure(
                operation,
                "duplicate_volume_assignment",
                project,
                f"--target-volume role is assigned more than once: {role}",
            )
        volumes[role] = identity
    return volumes, 0


def _print_auth_migration_plan(plan) -> None:
    print(
        f"PLAN migration=auth-enforcement deployment={plan.deployment} "
        f"environment={plan.environment} context={plan.docker_context}"
    )
    print(
        f"PLAN source-profile={plan.source_profile} "
        f"source-lock={plan.source_lock_identity}"
    )
    print(
        f"PLAN target-profile={plan.target_profile} "
        f"target-lock={plan.target_lock_identity}"
    )
    for role, source, target in plan.volume_migrations:
        print(f"PLAN volume={role}:{source}->{target}")
    for path, sha256 in zip(
        plan.preserved_compose_files,
        plan.preserved_compose_sha256,
        strict=True,
    ):
        print(f"PLAN preserve-compose={path} sha256={sha256}")
    if plan.auth_config_root is not None:
        print(f"PLAN auth-config-root={plan.auth_config_root}")
        print(
            "PLAN preserved-composition="
            f"{plan.preserved_composition_identity}"
        )
    print("PLAN failure-policy=retain-phase-and-resume-target no-source-runtime-rollback")


def _cmd_auth_migration_plan(args: argparse.Namespace) -> int:
    project = Path(args.project)
    volumes, status = _migration_target_volumes(
        args.target_volume,
        operation="migration auth-enforcement plan",
        project=project.resolve(),
    )
    if volumes is None:
        return status
    try:
        plan = plan_auth_enforcement_migration(
            project,
            Path(args.output),
            docker_context=args.docker_context,
            target_volumes=volumes,
            preserved_compose_files=tuple(Path(path) for path in args.preserve_compose_file),
            auth_config_root=(Path(args.auth_config_root) if args.auth_config_root else None),
            data_root=_preset_data_root(),
            allow_unverified=args.allow_unverified,
            allow_eol=args.allow_eol,
        )
    except (
        AuthMigrationContractError,
        PresetDataError,
        ProjectOrderError,
        RenderContractError,
        ResolutionError,
    ) as exc:
        return _print_structured_failure("migration auth-enforcement plan", exc)
    except OSError as exc:
        print(f"FAIL migration auth-enforcement plan: {exc}")
        return 2
    _print_auth_migration_plan(plan)
    return 0


def _cmd_auth_migration_apply(args: argparse.Namespace) -> int:
    project = Path(args.project)
    volumes, status = _migration_target_volumes(
        args.target_volume,
        operation="migration auth-enforcement apply",
        project=project.resolve(),
    )
    if volumes is None:
        return status
    try:
        result = apply_auth_enforcement_migration(
            project,
            Path(args.output),
            docker_context=args.docker_context,
            target_volumes=volumes,
            preserved_compose_files=tuple(Path(path) for path in args.preserve_compose_file),
            auth_config_root=(Path(args.auth_config_root) if args.auth_config_root else None),
            expected_source_lock_identity=args.expected_source_lock_identity,
            expected_target_lock_identity=args.expected_target_lock_identity,
            expected_preserved_composition_identity=(
                args.expected_preserved_composition_identity
            ),
            data_root=_preset_data_root(),
            confirmed=args.yes,
            allow_unverified=args.allow_unverified,
            allow_eol=args.allow_eol,
            wait_timeout=args.wait_timeout,
            progress=lambda step: print(
                f"PROGRESS migration auth-enforcement step={step}",
                flush=True,
            ),
        )
    except (
        AuthMigrationContractError,
        PresetDataError,
        ProjectOrderError,
        RenderContractError,
        ResolutionError,
    ) as exc:
        return _print_structured_failure("migration auth-enforcement apply", exc)
    except OSError as exc:
        print(f"FAIL migration auth-enforcement apply: {exc}")
        return 2
    print(
        f"OK migration auth-enforcement status={result.status} "
        f"source-lock={result.source_lock_identity} "
        f"target-lock={result.target_lock_identity} phase={result.phase}"
    )
    if args.allow_unverified:
        print("WARN migration used the one-shot unverified acknowledgement")
    return 0


def _print_public_b3_plan(plan) -> None:
    print(
        f"PLAN migration=public-b3 deployment={plan.deployment} "
        f"environment={plan.environment} context={plan.docker_context}"
    )
    print(
        f"PLAN source-profile={plan.source_profile} source-lock={plan.source_lock_identity}"
    )
    print(
        f"PLAN target-profile={plan.target_profile} target-lock={plan.target_lock_identity}"
    )
    print("PLAN release=public-web-paper@1->public-web-paper@2")
    for role, source, target in plan.volume_migrations:
        print(f"PLAN volume={role}:{source}->{target}")
    for path, sha256 in zip(
        plan.preserved_compose_files,
        plan.preserved_compose_sha256,
        strict=True,
    ):
        print(f"PLAN preserve-compose={path} sha256={sha256}")
    if plan.auth_config_root is not None:
        print(f"PLAN auth-config-root={plan.auth_config_root}")
        print(f"PLAN preserved-composition={plan.preserved_composition_identity}")
    print("PLAN failure-policy=retain-source-volumes-and-resume-target")


def _cmd_public_b3_plan(args: argparse.Namespace) -> int:
    project = Path(args.project)
    volumes, status = _migration_target_volumes(
        args.target_volume,
        operation="migration public-b3 plan",
        project=project.resolve(),
    )
    if volumes is None:
        return status
    try:
        plan = plan_public_b3_upgrade(
            project,
            Path(args.output),
            docker_context=args.docker_context,
            target_volumes=volumes,
            preserved_compose_files=tuple(Path(path) for path in args.preserve_compose_file),
            auth_config_root=(Path(args.auth_config_root) if args.auth_config_root else None),
            data_root=_preset_data_root(),
            allow_unverified=args.allow_unverified,
            allow_eol=args.allow_eol,
        )
    except (
        AuthMigrationContractError,
        PresetDataError,
        ProjectOrderError,
        RenderContractError,
        ResolutionError,
    ) as exc:
        return _print_structured_failure("migration public-b3 plan", exc)
    except OSError as exc:
        print(f"FAIL migration public-b3 plan: {exc}")
        return 2
    _print_public_b3_plan(plan)
    return 0


def _cmd_public_b3_apply(args: argparse.Namespace) -> int:
    project = Path(args.project)
    volumes, status = _migration_target_volumes(
        args.target_volume,
        operation="migration public-b3 apply",
        project=project.resolve(),
    )
    if volumes is None:
        return status
    try:
        result = apply_public_b3_upgrade(
            project,
            Path(args.output),
            docker_context=args.docker_context,
            target_volumes=volumes,
            preserved_compose_files=tuple(Path(path) for path in args.preserve_compose_file),
            auth_config_root=(Path(args.auth_config_root) if args.auth_config_root else None),
            expected_source_lock_identity=args.expected_source_lock_identity,
            expected_target_lock_identity=args.expected_target_lock_identity,
            expected_preserved_composition_identity=(
                args.expected_preserved_composition_identity
            ),
            data_root=_preset_data_root(),
            confirmed=args.yes,
            allow_unverified=args.allow_unverified,
            allow_eol=args.allow_eol,
            wait_timeout=args.wait_timeout,
            progress=lambda step: print(
                f"PROGRESS migration public-b3 step={step}",
                flush=True,
            ),
        )
    except (
        AuthMigrationContractError,
        PresetDataError,
        ProjectOrderError,
        RenderContractError,
        ResolutionError,
    ) as exc:
        return _print_structured_failure("migration public-b3 apply", exc)
    except OSError as exc:
        print(f"FAIL migration public-b3 apply: {exc}")
        return 2
    print(
        f"OK migration public-b3 status={result.status} "
        f"source-lock={result.source_lock_identity} "
        f"target-lock={result.target_lock_identity} phase={result.phase}"
    )
    if args.allow_unverified:
        print("WARN migration used the one-shot unverified acknowledgement")
    return 0


def _print_public_b4_plan(plan) -> None:
    print(
        f"PLAN migration=public-b4 deployment={plan.deployment} "
        f"environment={plan.environment} context={plan.docker_context}"
    )
    print(
        f"PLAN source-profile={plan.source_profile} source-lock={plan.source_lock_identity}"
    )
    print(
        f"PLAN target-profile={plan.target_profile} target-lock={plan.target_lock_identity}"
    )
    print("PLAN release=public-web-paper@2->public-web-paper@3")
    for role, source, target in plan.volume_migrations:
        print(f"PLAN volume={role}:{source}->{target}")
    for path, sha256 in zip(
        plan.preserved_compose_files,
        plan.preserved_compose_sha256,
        strict=True,
    ):
        print(f"PLAN preserve-compose={path} sha256={sha256}")
    if plan.auth_config_root is not None:
        print(f"PLAN auth-config-root={plan.auth_config_root}")
        print(f"PLAN preserved-composition={plan.preserved_composition_identity}")
    print("PLAN failure-policy=retain-source-volumes-and-resume-target")


def _cmd_public_b4_plan(args: argparse.Namespace) -> int:
    project = Path(args.project)
    volumes, status = _migration_target_volumes(
        args.target_volume,
        operation="migration public-b4 plan",
        project=project.resolve(),
    )
    if volumes is None:
        return status
    try:
        plan = plan_public_b4_upgrade(
            project,
            Path(args.output),
            docker_context=args.docker_context,
            target_volumes=volumes,
            preserved_compose_files=tuple(
                Path(path) for path in args.preserve_compose_file
            ),
            auth_config_root=(
                Path(args.auth_config_root) if args.auth_config_root else None
            ),
            data_root=_preset_data_root(),
            allow_unverified=args.allow_unverified,
            allow_eol=args.allow_eol,
        )
    except (
        AuthMigrationContractError,
        PresetDataError,
        ProjectOrderError,
        RenderContractError,
        ResolutionError,
    ) as exc:
        return _print_structured_failure("migration public-b4 plan", exc)
    except OSError as exc:
        print(f"FAIL migration public-b4 plan: {exc}")
        return 2
    _print_public_b4_plan(plan)
    return 0


def _cmd_public_b4_apply(args: argparse.Namespace) -> int:
    project = Path(args.project)
    volumes, status = _migration_target_volumes(
        args.target_volume,
        operation="migration public-b4 apply",
        project=project.resolve(),
    )
    if volumes is None:
        return status
    try:
        result = apply_public_b4_upgrade(
            project,
            Path(args.output),
            docker_context=args.docker_context,
            target_volumes=volumes,
            preserved_compose_files=tuple(
                Path(path) for path in args.preserve_compose_file
            ),
            auth_config_root=(
                Path(args.auth_config_root) if args.auth_config_root else None
            ),
            expected_source_lock_identity=args.expected_source_lock_identity,
            expected_target_lock_identity=args.expected_target_lock_identity,
            expected_preserved_composition_identity=(
                args.expected_preserved_composition_identity
            ),
            data_root=_preset_data_root(),
            confirmed=args.yes,
            allow_unverified=args.allow_unverified,
            allow_eol=args.allow_eol,
            wait_timeout=args.wait_timeout,
            acknowledge_credential_health=args.acknowledge_credential_health,
            progress=lambda step: print(
                f"PROGRESS migration public-b4 step={step}",
                flush=True,
            ),
        )
    except (
        AuthMigrationContractError,
        PresetDataError,
        ProjectOrderError,
        RenderContractError,
        ResolutionError,
    ) as exc:
        return _print_structured_failure("migration public-b4 apply", exc)
    except OSError as exc:
        print(f"FAIL migration public-b4 apply: {exc}")
        return 2
    print(
        f"OK migration public-b4 status={result.status} "
        f"source-lock={result.source_lock_identity} "
        f"target-lock={result.target_lock_identity} phase={result.phase}"
    )
    if args.allow_unverified:
        print("WARN migration used the one-shot unverified acknowledgement")
    if args.acknowledge_credential_health:
        print("WARN migration used the one-shot credential health acknowledgement")
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
    render_level = (
        "OK" if result.render_status == "current" else "WARN"
    )
    print(
        f"{render_level} doctor lock={result.lock_identity} "
        f"render={result.render_status} context={result.docker_context}"
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
    if result.scratch_runtime_status == "current":
        print("OK doctor scratch-runtime=current")
    if result.wirescope_status == "current":
        print("OK doctor wirescope=current handoff=cross-origin")
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
            for library in descriptor.get("runtime_libraries", []):
                print(
                    f"PLUGIN-RUNTIME-LIBRARY plugin={plugin['filename']} "
                    f"coordinate={library}"
                )
    return 0 if inventory["crc_ok"] else 2


def _print_world_restore_plan(result) -> None:
    print(
        f"PLAN world-restore lock={result.lock_identity} "
        f"archive-sha256={result.archive_sha256} volume={result.volume}"
    )
    print(
        f"PLAN world-restore entries={result.world_entry_count} "
        f"uncompressed-bytes={result.world_uncompressed_size_bytes} "
        f"rollback={result.rollback_name}"
    )
    for source, destination in result.world_mapping:
        print(f"PLAN world-restore world={source}->{destination}")


def _cmd_world_restore_plan(args: argparse.Namespace) -> int:
    print(
        "STEP world restore plan verify-render-and-archive "
        "(large archives can take several minutes)",
        flush=True,
    )
    try:
        result = plan_world_restore(
            Path(args.project),
            Path(args.output),
            Path(args.archive),
            source_world=args.source_world,
            expected_archive_sha256=args.expected_archive_sha256,
            expected_lock_identity=args.expected_lock_identity,
            data_root=_preset_data_root(),
        )
    except (WorldRestoreError, OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"FAIL world restore plan: {exc}")
        return 2
    _print_world_restore_plan(result)
    return 0


def _cmd_world_restore_apply(args: argparse.Namespace) -> int:
    try:
        result = apply_world_restore(
            Path(args.project),
            Path(args.output),
            Path(args.archive),
            source_world=args.source_world,
            expected_archive_sha256=args.expected_archive_sha256,
            expected_lock_identity=args.expected_lock_identity,
            docker_context=args.docker_context,
            data_root=_preset_data_root(),
            confirmed=args.yes,
            wait_timeout=args.wait_timeout,
            progress=lambda step: print(f"STEP world restore {step}", flush=True),
        )
    except (WorldRestoreError, OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"FAIL world restore apply: {exc}")
        return 2
    mapping = ",".join(
        f"{source}->{destination}"
        for source, destination in result.world_mapping
    )
    print(
        f"OK world restore status={result.status} lock={result.lock_identity} "
        f"archive-sha256={result.archive_sha256} volume={result.volume} "
        f"world={mapping} rollback={result.rollback_name}"
    )
    print(
        "WARN prior world roots are retained in the rollback directory; "
        "do not remove them until operator validation is complete"
    )
    return 0


def _cmd_runtime_audit_log(args: argparse.Namespace) -> int:
    try:
        result = audit_minecraft_log(Path(args.log))
    except OSError as exc:
        print(f"FAIL runtime audit-log: {exc}")
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not result["events"]:
        print("RUNTIME-EVENT none")
    for event in result["events"]:
        host = event["host"] or "not-observed"
        print(
            f"RUNTIME-EVENT category={event['category']} "
            f"component={event['component']} host={host} count={event['count']}"
        )
    for limitation in result["limitations"]:
        print(f"WARN runtime audit-log {limitation}")
    return 0


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
    project, status = _load_backup_project(
        args.project,
        transport_config=args.transport_config,
    )
    if project is None:
        return status
    archive_name = Path(args.archive).name

    def report_progress(phase: str) -> None:
        print(
            f"STEP backup transfer archive={archive_name} phase={phase}",
            flush=True,
        )

    try:
        result = transfer_archive(
            project,
            Path(args.archive),
            verify_download=args.verify_download,
            progress=report_progress,
        )
    except (BackupTransferError, ftplib.Error, OSError, ValueError) as exc:
        print(f"FAIL backup transfer: {exc}")
        return 2
    print(
        f"OK backup transfer status={result.status} remote={result.remote_name} "
        f"sha256={result.encrypted_sha256} size-bytes={result.encrypted_size_bytes} record={result.record_path}"
    )
    return 0


def _cmd_backup_drain(args: argparse.Namespace) -> int:
    project, status = _load_backup_project(
        args.project,
        transport_config=args.transport_config,
    )
    if project is None:
        return status
    try:
        print(
            "STEP backup drain phase=scan "
            "verification=activation-marker+stable-age+zip-crc",
            flush=True,
        )
        archives = ready_outbox_archives(
            Path(args.outbox),
            activated_after=Path(args.after),
        )
        if not archives:
            print("OK backup drain status=none-ready archives=0")
            return 0
        for archive in archives:
            print(
                f"STEP backup drain archive={archive.name} "
                "verification=stable-age+zip-crc",
                flush=True,
            )

            def report_progress(
                phase: str,
                archive_name: str = archive.name,
            ) -> None:
                print(
                    f"STEP backup drain archive={archive_name} "
                    f"phase={phase}",
                    flush=True,
                )

            result = transfer_archive(
                project,
                archive,
                verify_download=True,
                progress=report_progress,
            )
            print(
                f"OK backup drain archive={archive.name} "
                f"status={result.status} remote={result.remote_name} "
                f"sha256={result.encrypted_sha256} "
                f"size-bytes={result.encrypted_size_bytes}"
            )
    except (BackupTransferError, ftplib.Error, OSError, ValueError) as exc:
        print(f"FAIL backup drain: {exc}")
        return 2
    print(f"OK backup drain status=complete archives={len(archives)}")
    return 0


def _load_backup_project(
    project_path: str,
    *,
    transport_config: str | None,
):
    path = Path(project_path)
    if _uses_toml_project(path):
        if transport_config is None:
            print(
                "FAIL backup: TOML deployment requires --transport-config "
                "pointing to a private mode-0600 file"
            )
            return None, 2
        try:
            order = load_order(path)
            endpoint = load_backup_endpoint(
                Path(transport_config),
                deployment_name=order.order["deployment"]["name"],
            )
        except (BackupTransferError, ProjectOrderError, OSError) as exc:
            print(f"FAIL backup: {exc}")
            return None, 2
        return endpoint, 0
    project, issues = try_load_project(path)
    failures = [issue for issue in issues if issue.severity == "FAIL"]
    if project is None or failures:
        return None, _print_issues(issues)
    return project, 0


def _cmd_backup_list(args: argparse.Namespace) -> int:
    project, status = _load_backup_project(
        args.project,
        transport_config=args.transport_config,
    )
    if project is None:
        return status
    try:
        archives = list_remote_archives(project)
    except (BackupTransferError, ftplib.Error, OSError, ValueError) as exc:
        print(f"FAIL backup list: {exc}")
        return 2
    if not archives:
        print("REMOTE none")
        return 0
    for archive in archives:
        record = "present" if archive.record_present else "missing"
        print(
            f"REMOTE name={archive.name} "
            f"size-bytes={archive.size_bytes} record={record}"
        )
    return 0


def _cmd_backup_download(args: argparse.Namespace) -> int:
    project, status = _load_backup_project(
        args.project,
        transport_config=args.transport_config,
    )
    if project is None:
        return status
    try:
        result = download_remote_archive(
            project,
            args.remote_name,
            record_path=Path(args.record),
            output=Path(args.output),
        )
    except (BackupTransferError, ftplib.Error, OSError, ValueError) as exc:
        print(f"FAIL backup download: {exc}")
        return 2
    print(
        f"OK backup download status={result.status} "
        f"remote={result.remote_name} sha256={result.encrypted_sha256} "
        f"size-bytes={result.encrypted_size_bytes} "
        f"output={result.encrypted_path} record={result.record_path}"
    )
    return 0


def _cmd_backup_download_record(args: argparse.Namespace) -> int:
    project, status = _load_backup_project(
        args.project,
        transport_config=args.transport_config,
    )
    if project is None:
        return status
    try:
        result = download_remote_record(
            project,
            args.remote_name,
            output=Path(args.output),
        )
    except (BackupTransferError, ftplib.Error, OSError, ValueError) as exc:
        print(f"FAIL backup download-record: {exc}")
        return 2
    print(
        f"OK backup download-record status={result.status} "
        f"remote={result.remote_name} remote-record={result.remote_record_name} "
        f"output={result.record_path}"
    )
    return 0


def _cmd_backup_decrypt(args: argparse.Namespace) -> int:
    try:
        result = decrypt_downloaded_archive(
            Path(args.encrypted),
            record_path=Path(args.record),
            identity=Path(args.identity),
            output=Path(args.output),
        )
    except (BackupTransferError, OSError, ValueError) as exc:
        print(f"FAIL backup decrypt: {exc}")
        return 2
    print(
        f"OK backup decrypt status={result.status} "
        f"sha256={result.archive_sha256} output={result.archive_path} "
        f"record={result.record_path}"
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

    migration_parser = subparsers.add_parser(
        "migration",
        help="durable deployed-state migrations",
    )
    migration_subparsers = migration_parser.add_subparsers(
        dest="migration_command",
        required=True,
    )
    auth_migration_parser = migration_subparsers.add_parser(
        "auth-enforcement",
        help="migrate a running b2 deployment to enforced authentication",
    )
    auth_migration_subparsers = auth_migration_parser.add_subparsers(
        dest="auth_migration_command",
        required=True,
    )
    for action in ("plan", "apply"):
        action_parser = auth_migration_subparsers.add_parser(
            action,
            help=f"{action} the auth-enforcement deployed-state migration",
        )
        action_parser.add_argument("--project", required=True)
        action_parser.add_argument("--output", required=True)
        action_parser.add_argument("--docker-context", required=True)
        action_parser.add_argument("--target-volume", action="append", required=True)
        action_parser.add_argument("--preserve-compose-file", action="append", default=[])
        action_parser.add_argument("--auth-config-root")
        action_parser.add_argument("--allow-unverified", action="store_true")
        action_parser.add_argument("--allow-eol", action="store_true")
        if action == "apply":
            action_parser.add_argument(
                "--expected-source-lock-identity",
                required=True,
            )
            action_parser.add_argument(
                "--expected-target-lock-identity",
                required=True,
            )
            action_parser.add_argument(
                "--expected-preserved-composition-identity",
            )
            action_parser.add_argument("--wait-timeout", type=int, default=300)
            action_parser.add_argument("--yes", action="store_true")
            action_parser.set_defaults(handler=_cmd_auth_migration_apply)
        else:
            action_parser.set_defaults(handler=_cmd_auth_migration_plan)

    public_b3_parser = migration_subparsers.add_parser(
        "public-b3",
        help="upgrade the exact public b2 deployment to the b3 compatibility set",
    )
    public_b3_subparsers = public_b3_parser.add_subparsers(
        dest="public_b3_command",
        required=True,
    )
    for action in ("plan", "apply"):
        action_parser = public_b3_subparsers.add_parser(
            action,
            help=f"{action} the exact public b2-to-b3 deployed-state migration",
        )
        action_parser.add_argument("--project", required=True)
        action_parser.add_argument("--output", required=True)
        action_parser.add_argument("--docker-context", required=True)
        action_parser.add_argument("--target-volume", action="append", required=True)
        action_parser.add_argument("--preserve-compose-file", action="append", default=[])
        action_parser.add_argument("--auth-config-root")
        action_parser.add_argument("--allow-unverified", action="store_true")
        action_parser.add_argument("--allow-eol", action="store_true")
        if action == "apply":
            action_parser.add_argument("--expected-source-lock-identity", required=True)
            action_parser.add_argument("--expected-target-lock-identity", required=True)
            action_parser.add_argument("--expected-preserved-composition-identity")
            action_parser.add_argument("--wait-timeout", type=int, default=300)
            action_parser.add_argument("--yes", action="store_true")
            action_parser.set_defaults(handler=_cmd_public_b3_apply)
        else:
            action_parser.set_defaults(handler=_cmd_public_b3_plan)

    public_b4_parser = migration_subparsers.add_parser(
        "public-b4",
        help="upgrade the exact public b3 deployment to the b4 compatibility set",
    )
    public_b4_subparsers = public_b4_parser.add_subparsers(
        dest="public_b4_command",
        required=True,
    )
    for action in ("plan", "apply"):
        action_parser = public_b4_subparsers.add_parser(
            action,
            help=f"{action} the exact public b3-to-b4 deployed-state migration",
        )
        action_parser.add_argument("--project", required=True)
        action_parser.add_argument("--output", required=True)
        action_parser.add_argument("--docker-context", required=True)
        action_parser.add_argument("--target-volume", action="append", required=True)
        action_parser.add_argument("--preserve-compose-file", action="append", default=[])
        action_parser.add_argument("--auth-config-root")
        action_parser.add_argument("--allow-unverified", action="store_true")
        action_parser.add_argument("--allow-eol", action="store_true")
        if action == "apply":
            action_parser.add_argument("--expected-source-lock-identity", required=True)
            action_parser.add_argument("--expected-target-lock-identity", required=True)
            action_parser.add_argument("--expected-preserved-composition-identity")
            action_parser.add_argument("--wait-timeout", type=int, default=300)
            action_parser.add_argument(
                "--acknowledge-credential-health",
                action="store_true",
            )
            action_parser.add_argument("--yes", action="store_true")
            action_parser.set_defaults(handler=_cmd_public_b4_apply)
        else:
            action_parser.set_defaults(handler=_cmd_public_b4_plan)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="read-only check of one current TOML Docker runtime and protocol",
    )
    doctor_parser.add_argument("--project", required=True)
    doctor_parser.add_argument("--output")
    doctor_parser.add_argument("--docker-context", default="default")
    doctor_parser.add_argument("--timeout", type=int, default=5)
    doctor_parser.set_defaults(handler=_cmd_doctor)

    runtime_parser = subparsers.add_parser(
        "runtime",
        help="sanitized runtime diagnostics",
    )
    runtime_subparsers = runtime_parser.add_subparsers(
        dest="runtime_command",
        required=True,
    )
    runtime_audit_parser = runtime_subparsers.add_parser(
        "audit-log",
        help="classify explicit dependency downloads and update checks in a log",
    )
    runtime_audit_parser.add_argument("log")
    runtime_audit_parser.add_argument("--json", action="store_true")
    runtime_audit_parser.set_defaults(handler=_cmd_runtime_audit_log)

    world_parser = subparsers.add_parser("world", help="world lifecycle operations")
    world_subparsers = world_parser.add_subparsers(
        dest="world_command",
        required=True,
    )
    world_restore_parser = world_subparsers.add_parser(
        "restore",
        help="lock-bound world-only restore",
    )
    world_restore_subparsers = world_restore_parser.add_subparsers(
        dest="world_restore_command",
        required=True,
    )
    for action in ("plan", "apply"):
        action_parser = world_restore_subparsers.add_parser(
            action,
            help=f"{action} an exact world-only restore",
        )
        action_parser.add_argument("archive")
        action_parser.add_argument("--project", required=True)
        action_parser.add_argument("--output", required=True)
        action_parser.add_argument("--source-world", required=True)
        action_parser.add_argument(
            "--expected-archive-sha256",
            required=True,
        )
        action_parser.add_argument(
            "--expected-lock-identity",
            required=True,
        )
        if action == "apply":
            action_parser.add_argument("--docker-context", default="default")
            action_parser.add_argument("--wait-timeout", type=int, default=300)
            action_parser.add_argument("--yes", action="store_true")
            action_parser.set_defaults(handler=_cmd_world_restore_apply)
        else:
            action_parser.set_defaults(handler=_cmd_world_restore_plan)

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
    transfer_parser.add_argument("--transport-config")
    transfer_parser.add_argument("--verify-download", action="store_true")
    transfer_parser.set_defaults(handler=_cmd_backup_transfer)
    drain_parser = backup_subparsers.add_parser(
        "drain",
        help=(
            "verify and transfer stable outbox ZIPs created after an "
            "activation marker"
        ),
    )
    drain_parser.add_argument("outbox")
    drain_parser.add_argument("--after", required=True)
    drain_parser.add_argument("--project", required=True)
    drain_parser.add_argument("--transport-config")
    drain_parser.set_defaults(handler=_cmd_backup_drain)
    list_parser = backup_subparsers.add_parser(
        "list",
        help="list completed encrypted archives on the configured FTPS target",
    )
    list_parser.add_argument("--project", required=True)
    list_parser.add_argument("--transport-config")
    list_parser.set_defaults(handler=_cmd_backup_list)
    download_record_parser = backup_subparsers.add_parser(
        "download-record",
        help="download and validate the recovery sidecar for one named ciphertext",
    )
    download_record_parser.add_argument("remote_name")
    download_record_parser.add_argument("--project", required=True)
    download_record_parser.add_argument("--transport-config")
    download_record_parser.add_argument("--output", required=True)
    download_record_parser.set_defaults(handler=_cmd_backup_download_record)
    download_parser = backup_subparsers.add_parser(
        "download",
        help="download one named ciphertext and verify its transfer record",
    )
    download_parser.add_argument("remote_name")
    download_parser.add_argument("--project", required=True)
    download_parser.add_argument("--transport-config")
    download_parser.add_argument("--record", required=True)
    download_parser.add_argument("--output", required=True)
    download_parser.set_defaults(handler=_cmd_backup_download)
    decrypt_parser = backup_subparsers.add_parser(
        "decrypt",
        help="decrypt a downloaded ciphertext and verify the source archive hash",
    )
    decrypt_parser.add_argument("encrypted")
    decrypt_parser.add_argument("--record", required=True)
    decrypt_parser.add_argument("--identity", required=True)
    decrypt_parser.add_argument("--output", required=True)
    decrypt_parser.set_defaults(handler=_cmd_backup_decrypt)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)
