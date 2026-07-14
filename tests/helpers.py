from pathlib import Path

from mc_remote_stack.project import accept_eula, init_project
from mc_remote_stack.yamlio import dump_mapping, load_mapping


def make_renderable_project(tmp_path: Path):
    project = init_project(tmp_path / "deployment", "official-vps")
    accept_eula(project.root)

    config = load_mapping(project.config)
    config["host"]["ssh_host"] = "vps.example.test"
    config["host"]["ssh_user"] = "operator"
    dump_mapping(project.config, config)

    lock = load_mapping(project.lock)
    lock["resolved"] = True
    lock["minecraft"] = {
        "version": "26.1.2",
        "paper": {
            "build": 72,
            "filename": "paper-26.1.2-72.jar",
            "sha256": f"{10:064x}",
            "origin": {"kind": "https", "url": "https://example.test/paper-26.1.2-72.jar"},
        },
    }
    lock["homepage"] = {
        "version": "2026-07-14",
        "filename": "mc-remote-homepage-2026-07-14.tar.gz",
        "sha256": f"{11:064x}",
        "origin": {"kind": "https", "url": "https://example.test/mc-remote-homepage-2026-07-14.tar.gz"},
    }
    for index, name in enumerate(lock["images"], start=1):
        lock["images"][name] = f"example.test/{name}@sha256:{index:064x}"
    for index, artifact in enumerate(lock["plugins"].values(), start=20):
        artifact["filename"] = f"plugin-{index}.jar"
        artifact["version"] = f"test-version-{index}"
        artifact["sha256"] = f"{index:064x}"
        artifact["origin"] = {"kind": "https", "url": f"https://example.test/plugins/{index}.jar"}
    dump_mapping(project.lock, lock)
    return project


def enable_renderable_staging(project) -> None:
    config = load_mapping(project.config)
    config["staging"]["enabled"] = True
    dump_mapping(project.config, config)

    lock = load_mapping(project.lock)
    lock["staging"]["image"] = f"example.test/minecraft-staging@sha256:{40:064x}"
    lock["staging"]["minecraft"] = {
        "version": "1.21.11",
        "paper": {
            "build": 132,
            "filename": "paper-1.21.11-132.jar",
            "sha256": f"{41:064x}",
            "origin": {"kind": "https", "url": "https://example.test/paper-1.21.11-132.jar"},
        },
    }
    for index, artifact in enumerate(lock["staging"]["plugins"].values(), start=42):
        artifact["filename"] = f"staging-plugin-{index}.jar"
        artifact["version"] = f"staging-version-{index}"
        artifact["sha256"] = f"{index:064x}"
        artifact["origin"] = {"kind": "https", "url": f"https://example.test/staging/{index}.jar"}
    dump_mapping(project.lock, lock)
