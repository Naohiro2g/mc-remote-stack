import io
from pathlib import Path
from urllib.request import Request

import pytest

import mc_remote_stack.artifacts as artifacts_module
import mc_remote_stack.cli as cli_module
from mc_remote_stack.artifacts import (
    ArtifactFetchError,
    fetch_locked_artifacts,
    import_reviewed_artifact,
)
from mc_remote_stack.cli import main
from mc_remote_stack.toml_project import update_order_scalar

from .test_toml_render import (
    PAPER_BYTES,
    PAPER_SHA256,
    PLUGIN_BYTES,
    PLUGIN_SHA256,
    _render_fixture,
)


class _Response(io.BytesIO):
    def __init__(
        self,
        content: bytes,
        *,
        final_url: str,
        content_length: int | None = None,
    ) -> None:
        super().__init__(content)
        self._final_url = final_url
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def geturl(self) -> str:
        return self._final_url

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _remove_fixture_artifacts(artifact_store: Path) -> None:
    (artifact_store / "sha256" / PAPER_SHA256).unlink()
    (artifact_store / "sha256" / PLUGIN_SHA256).unlink()


def _fixture_opener(calls: list[str]):
    content_by_url = {
        "https://example.invalid/paper-fixture.jar": PAPER_BYTES,
        "https://example.invalid/mcremote-fixture.jar": PLUGIN_BYTES,
    }

    def open_url(request: Request, *, timeout: int) -> _Response:
        assert timeout == 60
        calls.append(request.full_url)
        content = content_by_url[request.full_url]
        return _Response(
            content,
            final_url=request.full_url,
            content_length=len(content),
        )

    return open_url


def _git_build_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    return _render_fixture(
        tmp_path,
        mcremote_artifact_source=f'''[[artifacts]]
id = "mcremote-jar"
kind = "git-build"
version = "2200.0.0b5"
repository = "https://github.com/Naohiro2g/McRemote"
commit = "{40 * '1'}"
source_subdirectory = "."
recipe = "./gradlew clean test build"
recipe_sha256 = "{64 * '2'}"
toolchain = "Java 21 + Gradle wrapper"
toolchain_sha256 = "{64 * '3'}"
build_input_sha256 = "{64 * '4'}"
output_filename = "mcremote-fixture.jar"
output_sha256 = "{PLUGIN_SHA256}"''',
    )


def test_import_reviewed_artifact_publishes_exact_git_build_bytes_atomically(
    tmp_path: Path,
) -> None:
    project, data_root, artifact_store = _git_build_fixture(tmp_path)
    destination = artifact_store / "sha256" / PLUGIN_SHA256
    destination.unlink()
    reviewed = tmp_path / "mcremote-fixture.jar"
    reviewed.write_bytes(PLUGIN_BYTES)

    imported = import_reviewed_artifact(
        project,
        reviewed,
        artifact_id="mcremote-jar",
        expected_sha256=PLUGIN_SHA256,
        data_root=data_root,
    )

    assert (imported.id, imported.status, imported.sha256) == (
        "mcremote-jar",
        "imported",
        PLUGIN_SHA256,
    )
    assert destination.read_bytes() == PLUGIN_BYTES
    assert destination.stat().st_mode & 0o777 == 0o644
    assert not list(destination.parent.glob(".import-reviewed-*"))


def test_import_reviewed_artifact_rehashes_existing_entry_without_replacing_it(
    tmp_path: Path,
) -> None:
    project, data_root, artifact_store = _git_build_fixture(tmp_path)
    destination = artifact_store / "sha256" / PLUGIN_SHA256
    before = destination.stat().st_mtime_ns
    reviewed = tmp_path / "mcremote-fixture.jar"
    reviewed.write_bytes(PLUGIN_BYTES)

    imported = import_reviewed_artifact(
        project,
        reviewed,
        artifact_id="mcremote-jar",
        expected_sha256=PLUGIN_SHA256,
        data_root=data_root,
    )

    assert imported.status == "present"
    assert destination.stat().st_mtime_ns == before


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("wrong-review-digest", "artifact_review_digest_mismatch"),
        ("wrong-source-bytes", "artifact_digest_mismatch"),
        ("wrong-filename", "artifact_filename_mismatch"),
        ("symlink-source", "artifact_source_invalid"),
        ("unsupported-kind", "artifact_import_kind_unsupported"),
    ],
)
def test_import_reviewed_artifact_fails_closed_before_store_mutation(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    if mutation == "unsupported-kind":
        project, data_root, artifact_store = _render_fixture(tmp_path)
    else:
        project, data_root, artifact_store = _git_build_fixture(tmp_path)
    destination = artifact_store / "sha256" / PLUGIN_SHA256
    destination.unlink()
    reviewed = tmp_path / "mcremote-fixture.jar"
    reviewed.write_bytes(PLUGIN_BYTES)
    expected = PLUGIN_SHA256
    if mutation == "wrong-review-digest":
        expected = "f" * 64
    elif mutation == "wrong-source-bytes":
        reviewed.write_bytes(b"not the reviewed bytes")
    elif mutation == "wrong-filename":
        reviewed = reviewed.rename(tmp_path / "renamed.jar")
    elif mutation == "symlink-source":
        target = tmp_path / "actual.jar"
        reviewed.rename(target)
        reviewed.symlink_to(target)

    with pytest.raises(ArtifactFetchError, match=reason):
        import_reviewed_artifact(
            project,
            reviewed,
            artifact_id="mcremote-jar",
            expected_sha256=expected,
            data_root=data_root,
        )

    assert not destination.exists()
    assert not list((artifact_store / "sha256").glob(".import-reviewed-*"))


def test_import_reviewed_artifact_refuses_to_overwrite_tampered_store_entry(
    tmp_path: Path,
) -> None:
    project, data_root, artifact_store = _git_build_fixture(tmp_path)
    destination = artifact_store / "sha256" / PLUGIN_SHA256
    destination.write_bytes(b"tampered")
    reviewed = tmp_path / "mcremote-fixture.jar"
    reviewed.write_bytes(PLUGIN_BYTES)

    with pytest.raises(ArtifactFetchError, match="artifact_store_tampered"):
        import_reviewed_artifact(
            project,
            reviewed,
            artifact_id="mcremote-jar",
            expected_sha256=PLUGIN_SHA256,
            data_root=data_root,
        )

    assert destination.read_bytes() == b"tampered"


def test_import_reviewed_artifact_rejects_stale_lock_before_source_or_store_access(
    tmp_path: Path,
) -> None:
    project, data_root, artifact_store = _git_build_fixture(tmp_path)
    destination = artifact_store / "sha256" / PLUGIN_SHA256
    destination.unlink()
    reviewed = tmp_path / "mcremote-fixture.jar"
    reviewed.write_bytes(PLUGIN_BYTES)
    update_order_scalar(project, ("network", "java_port"), 25566)

    with pytest.raises(ArtifactFetchError, match="stale_lock"):
        import_reviewed_artifact(
            project,
            reviewed,
            artifact_id="mcremote-jar",
            expected_sha256=PLUGIN_SHA256,
            data_root=data_root,
        )

    assert not destination.exists()
    assert not list((artifact_store / "sha256").glob(".import-reviewed-*"))


def test_cli_import_reviewed_reports_lock_identity_without_source_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, data_root, artifact_store = _git_build_fixture(tmp_path)
    (artifact_store / "sha256" / PLUGIN_SHA256).unlink()
    reviewed = tmp_path / "mcremote-fixture.jar"
    reviewed.write_bytes(PLUGIN_BYTES)
    monkeypatch.setattr(cli_module, "_preset_data_root", lambda: data_root)

    assert (
        main(
            [
                "artifact",
                "import-reviewed",
                str(reviewed),
                "--project",
                str(project),
                "--artifact-id",
                "mcremote-jar",
                "--expected-sha256",
                PLUGIN_SHA256,
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert f"status=imported id=mcremote-jar sha256={PLUGIN_SHA256}" in output
    assert str(reviewed) not in output


def test_fetch_locked_artifacts_downloads_only_exact_https_files(tmp_path: Path) -> None:
    project, data_root, artifact_store = _render_fixture(tmp_path)
    _remove_fixture_artifacts(artifact_store)
    calls: list[str] = []

    fetched = fetch_locked_artifacts(
        project,
        data_root=data_root,
        open_url=_fixture_opener(calls),
    )

    assert [(item.id, item.status) for item in fetched] == [
        ("paper-jar", "fetched"),
        ("mcremote-jar", "fetched"),
    ]
    assert calls == [
        "https://example.invalid/paper-fixture.jar",
        "https://example.invalid/mcremote-fixture.jar",
    ]
    assert (artifact_store / "sha256" / PAPER_SHA256).read_bytes() == PAPER_BYTES
    assert (artifact_store / "sha256" / PLUGIN_SHA256).read_bytes() == PLUGIN_BYTES
    assert all(item.path.stat().st_mode & 0o777 == 0o644 for item in fetched)


def test_fetch_locked_artifacts_rehashes_present_entries_without_network(
    tmp_path: Path,
) -> None:
    project, data_root, artifact_store = _render_fixture(tmp_path)
    before_mtimes = {
        digest: (artifact_store / "sha256" / digest).stat().st_mtime_ns
        for digest in (PAPER_SHA256, PLUGIN_SHA256)
    }

    def no_network(*args: object, **kwargs: object) -> _Response:
        raise AssertionError("network must not be used for verified store entries")

    fetched = fetch_locked_artifacts(project, data_root=data_root, open_url=no_network)

    assert [item.status for item in fetched] == ["present", "present"]
    assert {
        digest: (artifact_store / "sha256" / digest).stat().st_mtime_ns
        for digest in (PAPER_SHA256, PLUGIN_SHA256)
    } == before_mtimes


def test_fetch_locked_artifacts_refuses_tampered_store_entry(tmp_path: Path) -> None:
    project, data_root, artifact_store = _render_fixture(tmp_path)
    (artifact_store / "sha256" / PAPER_SHA256).write_bytes(b"tampered")

    with pytest.raises(ArtifactFetchError, match="artifact_store_tampered"):
        fetch_locked_artifacts(project, data_root=data_root)

    assert (artifact_store / "sha256" / PAPER_SHA256).read_bytes() == b"tampered"


def test_fetch_locked_artifacts_removes_digest_mismatch_download(tmp_path: Path) -> None:
    project, data_root, artifact_store = _render_fixture(tmp_path)
    _remove_fixture_artifacts(artifact_store)

    def wrong_content(request: Request, *, timeout: int) -> _Response:
        return _Response(b"wrong", final_url=request.full_url, content_length=5)

    with pytest.raises(ArtifactFetchError, match="artifact_digest_mismatch"):
        fetch_locked_artifacts(project, data_root=data_root, open_url=wrong_content)

    digest_store = artifact_store / "sha256"
    assert not (digest_store / PAPER_SHA256).exists()
    assert not list(digest_store.glob(".fetch-*"))


def test_fetch_locked_artifacts_enforces_stream_size_limit_without_content_length(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, data_root, artifact_store = _render_fixture(tmp_path)
    _remove_fixture_artifacts(artifact_store)
    monkeypatch.setattr(artifacts_module, "MAX_ARTIFACT_BYTES", 8)

    def oversized(request: Request, *, timeout: int) -> _Response:
        return _Response(
            b"123456789",
            final_url=request.full_url,
        )

    with pytest.raises(ArtifactFetchError, match="artifact_too_large"):
        fetch_locked_artifacts(project, data_root=data_root, open_url=oversized)

    assert not (artifact_store / "sha256" / PAPER_SHA256).exists()


def test_fetch_locked_artifacts_rejects_https_to_http_redirect(tmp_path: Path) -> None:
    project, data_root, artifact_store = _render_fixture(tmp_path)
    _remove_fixture_artifacts(artifact_store)

    def insecure_redirect(request: Request, *, timeout: int) -> _Response:
        return _Response(
            PAPER_BYTES,
            final_url="http://example.invalid/paper-fixture.jar",
            content_length=len(PAPER_BYTES),
        )

    with pytest.raises(ArtifactFetchError, match="artifact_redirect_insecure"):
        fetch_locked_artifacts(
            project,
            data_root=data_root,
            open_url=insecure_redirect,
        )

    assert not (artifact_store / "sha256" / PAPER_SHA256).exists()


def test_default_redirect_handler_rejects_downgrade_before_following() -> None:
    handler = artifacts_module._HttpsOnlyRedirectHandler()

    with pytest.raises(ValueError, match="credential-free HTTPS"):
        handler.redirect_request(
            Request("https://example.invalid/artifact.jar"),
            io.BytesIO(),
            302,
            "Found",
            {},
            "http://example.invalid/artifact.jar",
        )


def test_fetch_locked_artifacts_rejects_stale_lock_before_network_or_store_write(
    tmp_path: Path,
) -> None:
    project, data_root, artifact_store = _render_fixture(tmp_path)
    _remove_fixture_artifacts(artifact_store)
    (artifact_store / "sha256").rmdir()
    artifact_store.rmdir()
    update_order_scalar(project, ("network", "java_port"), 25566)

    def no_network(*args: object, **kwargs: object) -> _Response:
        raise AssertionError("network must not be used for a stale lock")

    with pytest.raises(ArtifactFetchError, match="stale_lock"):
        fetch_locked_artifacts(project, data_root=data_root, open_url=no_network)

    assert not artifact_store.exists()


def test_cli_artifact_fetch_reports_each_locked_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, data_root, artifact_store = _render_fixture(tmp_path)
    _remove_fixture_artifacts(artifact_store)
    calls: list[str] = []
    monkeypatch.setattr(cli_module, "_preset_data_root", lambda: data_root)
    monkeypatch.setattr(
        artifacts_module,
        "_default_open_url",
        _fixture_opener(calls),
    )

    assert main(["artifact", "fetch", "--project", str(project)]) == 0

    output = capsys.readouterr().out
    assert f"OK artifact status=fetched id=paper-jar sha256={PAPER_SHA256}" in output
    assert f"OK artifact status=fetched id=mcremote-jar sha256={PLUGIN_SHA256}" in output
