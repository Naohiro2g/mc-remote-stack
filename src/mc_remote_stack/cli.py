"""mcrctl command-line entry point."""

import argparse
import ftplib
import getpass
import json
import os
import zipfile
from importlib.resources import files
from pathlib import Path

from . import __version__
from .apply import ApplyContractError, apply_toml_project
from .archive import inspect_archive
from .artifacts import (
    ArtifactFetchError,
    fetch_locked_artifacts,
    import_reviewed_artifact,
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
from .deployment_interface import (
    DeploymentInterfaceError,
    apply_interface_order,
    doctor_interface_deployment,
    prepare_interface_deployment,
    write_interface_preview,
)
from .deployment_update import (
    DeploymentUpdateContractError,
    apply_deployment_update,
    load_deployment_update_plan,
    plan_deployment_update,
)
from .doctor import DoctorContractError, doctor_toml_project
from .homepage_sync import HomepageSyncError, sync_homepage
from .operator_environment import (
    OperatorEnvironmentError,
    check_operator_environment,
)
from .operator_inputs import OperatorInputError, resolve_operator_inputs
from .preset_registry import (
    PresetDataError,
    load_preset,
    load_preset_catalog,
    load_profile,
)
from .release_manifest import ReleaseManifestError, parse_release_manifest
from .render import RenderContractError, render_toml_project
from .repo_check import Issue, check_repository
from .resolver import ResolutionError, inspect_lock, load_lock, resolve_project
from .restore import (
    WorldRestoreError,
    apply_world_restore,
    plan_world_restore,
)
from .runtime_audit import audit_minecraft_log
from .runtime_content import RuntimeContentError
from .secrets import list_secrets, set_secret
from .toml_project import (
    ProjectOrderError,
    init_toml_project,
    load_order,
    update_order_scalar,
)


def _add_project_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project",
        help="deployment project directory (default: the current directory)",
    )
    parser.set_defaults(project_required=True)


def _current_project() -> Path | None:
    current = Path.cwd()
    return current if (current / "mc-remote.toml").is_file() else None


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
        | DeploymentUpdateContractError
        | OperatorEnvironmentError
        | OperatorInputError
        | PresetDataError
        | ProjectOrderError
        | ReleaseManifestError
        | RenderContractError
        | ResolutionError
        | RuntimeContentError
        | HomepageSyncError
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
        f"content-sha256={entry['content_sha256']}"
    )


def _cmd_release_manifest_verify(args: argparse.Namespace) -> int:
    path = Path(args.path)
    try:
        source = path.read_bytes()
    except OSError as exc:
        return _print_reason_failure(
            "release-manifest verify", "release_manifest_read_failed", path, str(exc)
        )
    try:
        manifest = parse_release_manifest(source, path=path)
    except ReleaseManifestError as exc:
        return _print_structured_failure("release-manifest verify", exc)
    print(
        f"RELEASE-MANIFEST release_tag={manifest['release_tag']} "
        f"source_commit={manifest['source_commit']}"
    )
    if "bundled_wirescope_source_commit" in manifest:
        print(
            "RELEASE-MANIFEST bundled_wirescope_source_commit="
            + manifest["bundled_wirescope_source_commit"]
        )
    for artifact in manifest["artifacts"]:
        if artifact["kind"] == "oci":
            print(
                f"ARTIFACT role={artifact['role']} kind=oci "
                f"locator={artifact['locator']} digest={artifact['digest']}"
            )
        else:
            print(
                f"ARTIFACT role={artifact['role']} kind=https-file "
                f"file={artifact['file']} sha256={artifact['sha256']}"
            )
    return 0


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
            "content_sha256": preset.content_sha256,
            "required_profile_capabilities": requirements["profile_capabilities"],
            "allowed_channels": requirements["allowed_channels"],
        }
    _print_preset_summary(entry)
    print(f"PRESET description={preset.data['preset']['description']}")
    print(
        "PRESET required-profile-capabilities="
        + ",".join(entry["required_profile_capabilities"])
    )
    print("PRESET allowed-channels=" + ",".join(entry["allowed_channels"]))
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
    print(f"OK initialized project={paths.root}")
    print(f"NEXT mcrctl accept-eula --project {paths.root} --yes")
    print(f"NEXT mcrctl resolve --project {paths.root}")
    return 0


def _generated_output(args: argparse.Namespace) -> Path:
    return Path(args.output) if args.output else Path(args.project) / "generated"


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


def _require_toml_project(operation: str, project_path: Path) -> int | None:
    if _uses_toml_project(project_path):
        return None
    return _print_reason_failure(
        operation,
        "toml_project_required",
        project_path.resolve(),
        "project must contain mc-remote.toml",
    )


def _cmd_validate(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    if (failure := _require_toml_project("validate", project_path)) is not None:
        return failure
    return _cmd_toml_validate(project_path)


def _cmd_accept_eula(args: argparse.Namespace) -> int:
    if not args.yes:
        print("FAIL EULA acceptance requires --yes after reading https://aka.ms/MinecraftEULA")
        return 2
    project_path = Path(args.project)
    if (failure := _require_toml_project("accept-eula", project_path)) is not None:
        return failure
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


def _deployment_name(project_path: str) -> tuple[str | None, int]:
    if (failure := _require_toml_project("secret", Path(project_path))) is not None:
        return None, failure
    try:
        order = load_order(Path(project_path))
    except ProjectOrderError as exc:
        return None, _print_structured_failure("secret", exc)
    return order.order["deployment"]["name"], 0


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


def _cmd_operator_check(args: argparse.Namespace) -> int:
    try:
        result = check_operator_environment(
            Path(args.project),
            docker_context=args.docker_context,
            check_bootstrap_ports=args.bootstrap_ports,
        )
    except OperatorEnvironmentError as exc:
        return _print_structured_failure("operator check", exc)
    print(
        f"OK operator status={result.status} user={result.operator} uid={result.uid} "
        f"project={result.project_root}"
    )
    print(
        f"OK operator toolchain=ready python={result.python_version} "
        f"docker={result.docker_version} compose={result.compose_version}"
    )
    print(f"OK operator docker-context={result.docker_context} access=direct")
    if result.bootstrap_ports is not None:
        java_port, mcremote_port = result.bootstrap_ports
        print(
            "OK operator bootstrap-network=ready "
            f"java-port={java_port} mcremote-port={mcremote_port}"
        )
    return 0


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
    for warning in warnings:
        print(f"WARN {warning}")
    return 1 if warnings else 0


def _cmd_plan(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    if (failure := _require_toml_project("plan", project_path)) is not None:
        return failure
    return _cmd_toml_plan(project_path)


def _cmd_render(args: argparse.Namespace) -> int:
    project_path = Path(args.project)
    if (failure := _require_toml_project("render", project_path)) is not None:
        return failure
    try:
        result = render_toml_project(
            project_path,
            _generated_output(args),
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


def _cmd_apply(args: argparse.Namespace) -> int:
    if args.dry_run and args.order is None:
        return _print_reason_failure(
            "apply", "apply_order_required", "mc-remote.toml",
            "pass one mc-remote.toml path for --dry-run",
        )
    if args.order is not None:
        try:
            if args.dry_run:
                if args.output is None:
                    return _print_reason_failure(
                        "apply", "preview_output_required", "apply.output",
                        "pass --output for the review copy",
                    )
                prepared = prepare_interface_deployment(Path(args.order))
                output = write_interface_preview(prepared, Path(args.output))
                print(
                    f"PLAN apply deployment={prepared.lock['deployment']} "
                    f"lock={prepared.lock['lock_identity']} output={output}"
                )
                return 0
            result = apply_interface_order(
                Path(args.order),
                docker_context=args.docker_context or "default",
                progress=lambda step: print(f"PROGRESS apply step={step}", flush=True),
            )
        except DeploymentInterfaceError as exc:
            return _print_structured_failure("apply", exc)
        print(
            f"OK apply deployment={result.deployment} mode={result.mode} "
            f"lock={result.lock_identity}"
        )
        return 0
    if args.project is None:
        return _print_reason_failure(
            "apply",
            "apply_order_required",
            "mc-remote.toml",
            "pass one mc-remote.toml path",
        )
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
            _generated_output(args),
            expected_lock_identity=args.expected_lock_identity,
            docker_context=args.docker_context or "default",
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
        RuntimeContentError,
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
    return 0


def _deployment_update_input_overrides(
    values: list[str],
) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for value in values:
        logical, separator, scalar = value.partition("=")
        role, dot, key = logical.partition(".")
        identity = (role, key)
        if (
            not separator
            or not dot
            or not role
            or not key
            or not scalar
            or identity in result
        ):
            raise DeploymentUpdateContractError(
                "update_input_override_invalid",
                value,
                "use one unique ROLE.KEY=VALUE string per typed operator input",
            )
        result[identity] = scalar
    return result


def _deployment_update_input_files(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        role, separator, source = value.partition("=")
        if not separator or not role or not source or role in result:
            raise DeploymentUpdateContractError(
                "update_input_file_invalid",
                value,
                "use one unique ROLE=PATH string per reviewed operator input file",
            )
        result[role] = Path(source)
    return result


def _cmd_deployment_update_plan(args: argparse.Namespace) -> int:
    project = Path(args.project)
    output = Path(args.output) if args.output else project / "generated"
    try:
        check_operator_environment(
            project,
            docker_context=args.docker_context,
        )
        overrides = _deployment_update_input_overrides(args.set_input)
        input_files = _deployment_update_input_files(args.replace_input)
        plan = plan_deployment_update(
            project,
            output,
            target_profile=args.to_profile,
            target_preset=args.to_preset,
            input_overrides=overrides,
            input_files=input_files,
            docker_context=args.docker_context,
            data_root=_preset_data_root(),
            allow_unverified=args.allow_unverified,
            allow_eol=args.allow_eol,
        )
    except (
        ArtifactFetchError,
        DeploymentUpdateContractError,
        DoctorContractError,
        OperatorEnvironmentError,
        PresetDataError,
        ProjectOrderError,
        RenderContractError,
        ResolutionError,
        RuntimeContentError,
    ) as exc:
        return _print_structured_failure("deployment update plan", exc)
    except OSError as exc:
        print(f"FAIL deployment update plan: {exc}")
        return 2
    print(
        f"PLAN deployment-update id={plan.plan_id} deployment={plan.deployment} "
        f"environment={plan.environment} context={plan.docker_context}"
    )
    print(
        f"PLAN release={plan.source_profile}/{plan.source_preset}"
        f"->{plan.target_profile}/{plan.target_preset}"
    )
    print(
        f"PLAN lock={plan.source_lock_identity}->{plan.target_lock_identity} "
        "stateful-volumes=in-place"
    )
    print(
        f"PLAN live-compose=auto-discovered "
        f"additional-files={len(plan.preserved_compose_files)}"
    )
    print(
        "PLAN failure-policy=restore-source-projection; "
        "world/session/pairing-state-not-rolled-back"
    )
    print(
        f"NEXT mcrctl deployment update apply --project {plan.project_root} "
        f"--plan-id {plan.plan_id} --yes"
    )
    return 0


def _cmd_deployment_update_apply(args: argparse.Namespace) -> int:
    try:
        project = Path(args.project)
        plan = load_deployment_update_plan(project, args.plan_id)
        check_operator_environment(
            project,
            docker_context=plan.docker_context,
        )
        result = apply_deployment_update(
            project,
            plan_id=args.plan_id,
            confirmed=args.yes,
            data_root=_preset_data_root(),
            wait_timeout=args.wait_timeout,
            progress=lambda step: print(
                f"PROGRESS deployment-update step={step}",
                flush=True,
            ),
        )
    except (
        DeploymentUpdateContractError,
        DoctorContractError,
        OperatorEnvironmentError,
        PresetDataError,
        ProjectOrderError,
        RenderContractError,
        ResolutionError,
    ) as exc:
        return _print_structured_failure("deployment update apply", exc)
    except OSError as exc:
        print(f"FAIL deployment update apply: {exc}")
        return 2
    print(
        f"OK deployment-update status={result.status} plan={result.plan_id} "
        f"source-lock={result.source_lock_identity} "
        f"target-lock={result.target_lock_identity} phase={result.phase}"
    )
    return 0


def _cmd_homepage_sync(args: argparse.Namespace) -> int:
    project = Path(args.project)
    try:
        result = sync_homepage(
            project,
            project / "generated",
            data_root=_preset_data_root(),
        )
    except (HomepageSyncError, ProjectOrderError, RenderContractError, ResolutionError) as exc:
        return _print_structured_failure("homepage sync", exc)
    except OSError as exc:
        print(f"FAIL homepage sync: {exc}")
        return 2
    print(f"OK homepage sync path={result.public_directory}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    if args.deployment is not None:
        try:
            result = doctor_interface_deployment(
                args.deployment,
                docker_context=args.docker_context,
                timeout=args.timeout,
            )
        except DeploymentInterfaceError as exc:
            return _print_structured_failure("doctor", exc)
        print(
            f"OK doctor deployment={result.deployment} "
            f"runtime={result.runtime_status} images={result.image_status} "
            f"network={result.network_status} "
            f"scratch-runtime={result.scratch_runtime_status} "
            f"bridge-allowlist={result.bridge_allowlist_status} "
            f"bridge-upstream={result.bridge_upstream_status} "
            f"auth={result.auth_status} "
            f"lock={result.lock_identity}"
        )
        return 0
    if args.project is None:
        current = _current_project()
        if current is None:
            return _print_reason_failure(
                "doctor",
                "doctor_deployment_required",
                "deployment",
                "run inside a deployment project directory or pass one deployment identity",
            )
        args.project = str(current)
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
    if result.homepage_status == "current":
        print("OK doctor homepage=current")
    if result.scratch_runtime_status == "current":
        print("OK doctor scratch-runtime=current")
    if result.wirescope_status == "current":
        print("OK doctor wirescope=current handoff=cross-origin")
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
            _generated_output(args),
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
            _generated_output(args),
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


def _cmd_artifact_import_reviewed(args: argparse.Namespace) -> int:
    try:
        imported = import_reviewed_artifact(
            Path(args.project),
            Path(args.source),
            artifact_id=args.artifact_id,
            expected_sha256=args.expected_sha256,
            data_root=_preset_data_root(),
        )
    except (
        ArtifactFetchError,
        PresetDataError,
        ProjectOrderError,
        ResolutionError,
    ) as exc:
        return _print_structured_failure("artifact import-reviewed", exc)
    except OSError as exc:
        print(f"FAIL artifact import-reviewed: {exc}")
        return 2
    print(
        f"OK artifact status={imported.status} id={imported.id} "
        f"sha256={imported.sha256} path={imported.path}"
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
    if (failure := _require_toml_project("backup", path)) is not None:
        return None, failure
    if transport_config is None:
        print(
            "FAIL backup: --transport-config must point to a private "
            "mode-0600 file"
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

    release_manifest_parser = subparsers.add_parser(
        "release-manifest", help="cross-repo release manifest (DEC 2026-09-06-04)"
    )
    release_manifest_subparsers = release_manifest_parser.add_subparsers(
        dest="release_manifest_command", required=True
    )
    release_manifest_verify_parser = release_manifest_subparsers.add_parser(
        "verify", help="schema-validate one downloaded manifest.json"
    )
    release_manifest_verify_parser.add_argument("path")
    release_manifest_verify_parser.set_defaults(handler=_cmd_release_manifest_verify)

    resolve_parser = subparsers.add_parser("resolve", help="resolve one TOML deployment project")
    _add_project_argument(resolve_parser)
    resolve_parser.add_argument("--allow-unverified", action="store_true", help=argparse.SUPPRESS)
    resolve_parser.add_argument("--allow-eol", action="store_true")
    resolve_parser.set_defaults(handler=_cmd_resolve)

    validate_parser = subparsers.add_parser("validate", help="validate deployment config and lock")
    _add_project_argument(validate_parser)
    validate_parser.set_defaults(handler=_cmd_validate)

    eula_parser = subparsers.add_parser("accept-eula", help="record explicit Minecraft EULA acceptance")
    _add_project_argument(eula_parser)
    eula_parser.add_argument("--yes", action="store_true")
    eula_parser.set_defaults(handler=_cmd_accept_eula)

    secret_parser = subparsers.add_parser("secret", help="local secret store operations")
    secret_subparsers = secret_parser.add_subparsers(dest="secret_command", required=True)
    secret_set_parser = secret_subparsers.add_parser("set", help="store a secret without adding it to Git")
    secret_set_parser.add_argument("name")
    _add_project_argument(secret_set_parser)
    secret_set_parser.add_argument("--from-file")
    secret_set_parser.set_defaults(handler=_cmd_secret_set)
    secret_list_parser = secret_subparsers.add_parser("list", help="list secret names without values")
    _add_project_argument(secret_list_parser)
    secret_list_parser.set_defaults(handler=_cmd_secret_list)

    repo_parser = subparsers.add_parser("repo", help="deployment repository operations")
    repo_subparsers = repo_parser.add_subparsers(dest="repo_command", required=True)
    check_parser = repo_subparsers.add_parser("check", help="check for secret and generated-file leakage")
    _add_project_argument(check_parser)
    check_parser.set_defaults(handler=_cmd_repo_check)

    operator_parser = subparsers.add_parser(
        "operator",
        help="prepare and verify the trusted deployment operator environment",
    )
    operator_subparsers = operator_parser.add_subparsers(
        dest="operator_command",
        required=True,
    )
    operator_check_parser = operator_subparsers.add_parser(
        "check",
        help="verify tools, direct Docker access, and project ownership",
    )
    _add_project_argument(operator_check_parser)
    operator_check_parser.add_argument("--docker-context", default="default")
    operator_check_parser.add_argument(
        "--bootstrap-ports",
        action="store_true",
        help="verify that the order's declared TCP ports are bindable before artifact fetch",
    )
    operator_check_parser.set_defaults(handler=_cmd_operator_check)

    plan_parser = subparsers.add_parser("plan", help="show deployment intent and blockers")
    _add_project_argument(plan_parser)
    plan_parser.set_defaults(handler=_cmd_plan)

    render_parser = subparsers.add_parser("render", help="render validated runtime configuration")
    _add_project_argument(render_parser)
    render_parser.add_argument("--output")
    render_parser.set_defaults(handler=_cmd_render)

    apply_parser = subparsers.add_parser(
        "apply",
        help="create or update one deployment from mc-remote.toml",
    )
    apply_parser.add_argument("order", nargs="?", help="compact mc-remote.toml order")
    apply_parser.add_argument(
        "--dry-run", action="store_true", help="export settings for review without Docker or downloads"
    )
    apply_parser.add_argument("--project", help=argparse.SUPPRESS)
    apply_parser.add_argument("--output", help="empty directory for --dry-run output")
    apply_parser.add_argument("--expected-lock-identity", help=argparse.SUPPRESS)
    apply_parser.add_argument("--docker-context")
    apply_parser.add_argument("--bootstrap", action="store_true", help=argparse.SUPPRESS)
    apply_parser.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    apply_parser.add_argument("--allow-unverified", action="store_true", help=argparse.SUPPRESS)
    apply_parser.add_argument("--allow-eol", action="store_true", help=argparse.SUPPRESS)
    apply_parser.add_argument(
        "--wait-timeout", type=int, default=300, help=argparse.SUPPRESS
    )
    apply_parser.set_defaults(handler=_cmd_apply)

    deployment_parser = subparsers.add_parser(
        "deployment",
        help="release-independent deployment lifecycle operations",
    )
    deployment_subparsers = deployment_parser.add_subparsers(
        dest="deployment_command",
        required=True,
    )
    deployment_update_parser = deployment_subparsers.add_parser(
        "update",
        help="prepare or apply one same-volume release update",
    )
    deployment_update_subparsers = deployment_update_parser.add_subparsers(
        dest="deployment_update_command",
        required=True,
    )
    deployment_update_plan_parser = deployment_update_subparsers.add_parser(
        "plan",
        help="prepare an exact update from live provenance",
    )
    _add_project_argument(deployment_update_plan_parser)
    deployment_update_plan_parser.add_argument("--output")
    deployment_update_plan_parser.add_argument("--docker-context", default="default")
    deployment_update_plan_parser.add_argument("--to-profile", required=True)
    deployment_update_plan_parser.add_argument("--to-preset", required=True)
    deployment_update_plan_parser.add_argument("--set-input", action="append", default=[])
    deployment_update_plan_parser.add_argument(
        "--replace-input", action="append", default=[]
    )
    deployment_update_plan_parser.add_argument("--allow-unverified", action="store_true", help=argparse.SUPPRESS)
    deployment_update_plan_parser.add_argument("--allow-eol", action="store_true")
    deployment_update_plan_parser.set_defaults(handler=_cmd_deployment_update_plan)
    deployment_update_apply_parser = deployment_update_subparsers.add_parser(
        "apply",
        help="apply or retry one exact durable update plan",
    )
    _add_project_argument(deployment_update_apply_parser)
    deployment_update_apply_parser.add_argument("--plan-id", required=True)
    deployment_update_apply_parser.add_argument("--wait-timeout", type=int, default=300)
    deployment_update_apply_parser.add_argument("--yes", action="store_true")
    deployment_update_apply_parser.set_defaults(handler=_cmd_deployment_update_apply)

    homepage_parser = subparsers.add_parser(
        "homepage", help="official homepage operations"
    )
    homepage_subparsers = homepage_parser.add_subparsers(
        dest="homepage_command", required=True
    )
    homepage_sync_parser = homepage_subparsers.add_parser(
        "sync", help="synchronize knowledge main to the public directory"
    )
    _add_project_argument(homepage_sync_parser)
    homepage_sync_parser.set_defaults(handler=_cmd_homepage_sync)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="read-only check of one current TOML Docker runtime and protocol",
    )
    doctor_parser.add_argument("deployment", nargs="?", help="compact deployment identity")
    doctor_parser.add_argument("--project")
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
        _add_project_argument(action_parser)
        action_parser.add_argument("--output")
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
    _add_project_argument(fetch_parser)
    fetch_parser.set_defaults(handler=_cmd_artifact_fetch)
    import_reviewed_parser = artifact_subparsers.add_parser(
        "import-reviewed",
        help="import one reviewed git-build output named by the current TOML lock",
    )
    import_reviewed_parser.add_argument("source")
    _add_project_argument(import_reviewed_parser)
    import_reviewed_parser.add_argument("--artifact-id", required=True)
    import_reviewed_parser.add_argument("--expected-sha256", required=True)
    import_reviewed_parser.set_defaults(handler=_cmd_artifact_import_reviewed)
    backup_parser = subparsers.add_parser("backup", help="encrypted backup transfer operations")
    backup_subparsers = backup_parser.add_subparsers(dest="backup_command", required=True)
    transfer_parser = backup_subparsers.add_parser(
        "transfer",
        help="encrypt an archive and upload it with explicit FTPS",
    )
    transfer_parser.add_argument("archive")
    _add_project_argument(transfer_parser)
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
    _add_project_argument(drain_parser)
    drain_parser.add_argument("--transport-config")
    drain_parser.set_defaults(handler=_cmd_backup_drain)
    list_parser = backup_subparsers.add_parser(
        "list",
        help="list completed encrypted archives on the configured FTPS target",
    )
    _add_project_argument(list_parser)
    list_parser.add_argument("--transport-config")
    list_parser.set_defaults(handler=_cmd_backup_list)
    download_record_parser = backup_subparsers.add_parser(
        "download-record",
        help="download and validate the recovery sidecar for one named ciphertext",
    )
    download_record_parser.add_argument("remote_name")
    _add_project_argument(download_record_parser)
    download_record_parser.add_argument("--transport-config")
    download_record_parser.add_argument("--output", required=True)
    download_record_parser.set_defaults(handler=_cmd_backup_download_record)
    download_parser = backup_subparsers.add_parser(
        "download",
        help="download one named ciphertext and verify its transfer record",
    )
    download_parser.add_argument("remote_name")
    _add_project_argument(download_parser)
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
    if getattr(args, "project_required", False) and args.project is None:
        current = _current_project()
        if current is None:
            return _print_reason_failure(
                args.command,
                "project_required",
                Path.cwd(),
                "run inside a deployment project directory or pass --project",
            )
        args.project = str(current)
    project_argument = getattr(args, "project", None)
    if project_argument is None and args.command == "init":
        project_argument = args.path
    if project_argument is not None and os.geteuid() == 0:
        return _print_reason_failure(
            getattr(args, "operator_command", None) or "mcrctl",
            "operator_root_forbidden",
            Path(project_argument).resolve(),
            "run mcrctl as the project-owning operator; never use sudo mcrctl",
        )
    return args.handler(args)
