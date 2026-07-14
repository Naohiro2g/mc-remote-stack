"""mcrctl command-line entry point."""

import argparse
import ftplib
import getpass
import json
import zipfile
from pathlib import Path

from . import __version__
from .archive import inspect_archive
from .artifacts import import_recovery_archive
from .backup import BackupTransferError, transfer_archive
from .project import accept_eula, init_project
from .render import RenderError, render_project
from .repo_check import check_repository
from .secrets import list_secrets, set_secret
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


def _cmd_init(args: argparse.Namespace) -> int:
    try:
        paths = init_project(Path(args.path), args.profile)
    except ValueError as exc:
        print(f"FAIL init: {exc}")
        return 2
    print(f"OK initialized {paths.root}")
    print("NEXT review mc-remote.yml, set secrets, accept EULA, then resolve immutable artifacts")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    _, issues = try_load_project(Path(args.project))
    return _print_issues(issues)


def _cmd_accept_eula(args: argparse.Namespace) -> int:
    if not args.yes:
        print("FAIL EULA acceptance requires --yes after reading https://aka.ms/MinecraftEULA")
        return 2
    try:
        paths = accept_eula(Path(args.project))
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


def _cmd_plan(args: argparse.Namespace) -> int:
    project, issues = try_load_project(Path(args.project))
    issues.extend(check_repository(Path(args.project)))
    if project is not None:
        print(f"PLAN deployment={project.config.get('deployment', {}).get('name', 'unknown')}")
        print("PLAN services=caddy,scratch-stable,scratch-dev,bridge,minecraft")
        print("PLAN public-ports=80/tcp,443/tcp,25565/tcp,25565/udp,25575/tcp")
        print("PLAN rcon=disabled backup-source=@server backup-output=/backup/outbox")
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
    project, issues = try_load_project(Path(args.project))
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
    init_parser.add_argument("--profile", default="official-vps")
    init_parser.set_defaults(handler=_cmd_init)

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

    archive_parser = subparsers.add_parser("archive", help="recovery archive operations")
    archive_subparsers = archive_parser.add_subparsers(dest="archive_command", required=True)
    inspect_parser = archive_subparsers.add_parser("inspect", help="verify and inventory a ZIP without extracting it")
    inspect_parser.add_argument("archive")
    inspect_parser.add_argument("--json", action="store_true")
    inspect_parser.set_defaults(handler=_cmd_archive_inspect)

    artifact_parser = subparsers.add_parser("artifact", help="immutable artifact store operations")
    artifact_subparsers = artifact_parser.add_subparsers(dest="artifact_command", required=True)
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
