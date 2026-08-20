import hashlib
import shutil
from pathlib import Path

import pytest

from mc_remote_stack.runtime_content import (
    RuntimeContentError,
    import_homepage_tree,
    import_runtime_file,
    verify_homepage_tree,
)


def test_runtime_file_import_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "plugin.jar"
    source.write_bytes(b"exact plugin bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    store = tmp_path / "store"

    first = import_runtime_file(source, store, expected_sha256=digest)
    second = import_runtime_file(source, store, expected_sha256=digest)

    assert first.path == store / "sha256" / digest
    assert first.status == "imported"
    assert second.status == "present"
    assert first.path.read_bytes() == b"exact plugin bytes"


def test_runtime_file_import_rejects_digest_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "plugin.jar"
    source.write_bytes(b"wrong bytes")

    with pytest.raises(RuntimeContentError) as exc_info:
        import_runtime_file(source, tmp_path / "store", expected_sha256="a" * 64)

    assert exc_info.value.reason == "runtime_content_digest_mismatch"


def test_runtime_file_import_does_not_publish_bytes_changed_during_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "plugin.jar"
    source.write_bytes(b"reviewed bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    store = tmp_path / "store"

    def copy_changed(_source, destination, _length) -> None:
        destination.write(b"changed during copy")

    monkeypatch.setattr(shutil, "copyfileobj", copy_changed)

    with pytest.raises(RuntimeContentError) as exc_info:
        import_runtime_file(source, store, expected_sha256=digest)

    assert exc_info.value.reason == "runtime_content_digest_mismatch"
    assert not (store / "sha256" / digest).exists()


def test_homepage_tree_import_and_verify_are_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "homepage"
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text("home\n", encoding="utf-8")
    (source / "assets" / "app.js").write_text("app\n", encoding="utf-8")
    store = tmp_path / "store"

    imported = import_homepage_tree(source, store)
    verified = verify_homepage_tree(imported.path, imported.tree_sha256)

    assert imported.path == store / "trees" / "sha256" / imported.tree_sha256
    assert imported.file_count == 2
    assert imported.total_bytes == 9
    assert verified.file_count == imported.file_count
    assert verified.total_bytes == imported.total_bytes


def test_homepage_tree_import_never_uses_an_absolute_cleanup_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "homepage"
    source.mkdir()
    (source / "index.html").write_text("home\n", encoding="utf-8")
    removed: list[Path] = []
    original_exists = Path.exists

    monkeypatch.setattr(
        Path,
        "exists",
        lambda path: True if path == Path("/__moved__") else original_exists(path),
    )
    monkeypatch.setattr(shutil, "rmtree", lambda path: removed.append(Path(path)))

    imported = import_homepage_tree(source, tmp_path / "store")

    assert imported.path.is_dir()
    assert Path("/__moved__") not in removed


def test_homepage_tree_import_does_not_publish_a_tree_changed_during_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "homepage"
    source.mkdir()
    (source / "index.html").write_text("reviewed\n", encoding="utf-8")
    store = tmp_path / "store"
    original_copyfile = shutil.copyfile

    def copy_changed(source_path, destination_path):
        result = original_copyfile(source_path, destination_path)
        Path(destination_path).write_text("changed\n", encoding="utf-8")
        return result

    monkeypatch.setattr(shutil, "copyfile", copy_changed)

    with pytest.raises(RuntimeContentError) as exc_info:
        import_homepage_tree(source, store)

    assert exc_info.value.reason == "runtime_content_digest_mismatch"
    assert not any((store / "trees" / "sha256").iterdir())


def test_homepage_tree_rejects_symbolic_links(tmp_path: Path) -> None:
    source = tmp_path / "homepage"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret\n", encoding="utf-8")
    (source / "index.html").symlink_to(outside)

    with pytest.raises(RuntimeContentError) as exc_info:
        import_homepage_tree(source, tmp_path / "store")

    assert exc_info.value.reason == "runtime_content_symlink_forbidden"
