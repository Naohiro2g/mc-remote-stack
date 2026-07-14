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
