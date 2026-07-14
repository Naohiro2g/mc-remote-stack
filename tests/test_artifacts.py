import hashlib
import zipfile
from pathlib import Path

from mc_remote_stack.artifacts import import_recovery_archive
from mc_remote_stack.yamlio import dump_mapping, load_mapping

from .helpers import make_renderable_project


def test_import_recovery_archive_extracts_only_locked_jars_by_hash(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    paper = b"exact paper jar"
    plugin = b"exact plugin jar"
    archive = tmp_path / "recovery.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("paper-26.1.2-72.jar", paper)
        zipped.writestr("plugins/AdvancedPortals.jar", plugin)
        zipped.writestr("plugins/DiscordSRV/config.yml", "BotToken: must-not-be-extracted\n")
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()

    lock = load_mapping(project.lock)
    lock["minecraft"]["paper"]["sha256"] = hashlib.sha256(paper).hexdigest()
    lock["minecraft"]["paper"]["origin"] = {
        "kind": "recovery_archive",
        "archive_sha256": archive_sha256,
        "member": "paper-26.1.2-72.jar",
    }
    advanced_portals = lock["plugins"]["AdvancedPortals"]
    advanced_portals["filename"] = "AdvancedPortals.jar"
    advanced_portals["sha256"] = hashlib.sha256(plugin).hexdigest()
    advanced_portals["origin"] = {
        "kind": "recovery_archive",
        "archive_sha256": archive_sha256,
        "member": "plugins/AdvancedPortals.jar",
    }
    dump_mapping(project.lock, lock)

    store = tmp_path / "artifact-store"
    imported = import_recovery_archive(project.root, archive, store)

    assert {artifact.name for artifact in imported} == {"Paper", "AdvancedPortals"}
    assert (store / hashlib.sha256(paper).hexdigest()).read_bytes() == paper
    assert (store / hashlib.sha256(plugin).hexdigest()).read_bytes() == plugin
    assert not any(path.name == "config.yml" for path in store.rglob("*"))


def test_import_recovery_archive_stops_on_archive_identity_mismatch(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    archive = tmp_path / "recovery.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("paper.jar", b"paper")

    lock = load_mapping(project.lock)
    lock["minecraft"]["paper"]["origin"] = {
        "kind": "recovery_archive",
        "archive_sha256": "0" * 64,
        "member": "paper.jar",
    }
    dump_mapping(project.lock, lock)

    try:
        import_recovery_archive(project.root, archive, tmp_path / "store")
    except ValueError as exc:
        assert "archive SHA-256" in str(exc)
    else:
        raise AssertionError("archive identity mismatch must stop import")
