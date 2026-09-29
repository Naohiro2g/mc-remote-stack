import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _shell_blocks(source: str) -> list[str]:
    return re.findall(r"(?ms)^```sh\n(.*?)^```$", source)


def test_ssh_hardening_dropin_precedes_cloud_init() -> None:
    guide = (REPO_ROOT / "docs" / "fresh-host-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"/etc/ssh/sshd_config\.d/(?P<name>[0-9][0-9]-mc-remote-bootstrap\.conf)",
        guide,
    )

    assert match is not None
    assert match.group("name") < "50-cloud-init.conf"


def test_public_vps_runbook_is_one_positive_canonical_path() -> None:
    guide = (REPO_ROOT / "docs" / "public-vps-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )

    assert len(guide.splitlines()) <= 185
    assert "mcrctl deployment update plan" in guide
    assert "mcrctl deployment update apply" in guide
    assert "mcrctl doctor" in guide
    assert "uv sync --locked\n" in guide
    assert "--extra dev" not in guide
    assert guide.index("mcrctl operator check") < guide.index("mcrctl deployment update plan")
    assert guide.index("mcrctl deployment update plan") < guide.index(
        "mcrctl deployment update apply"
    ) < guide.index("mcrctl doctor")
    assert "00-hub/release-operations-responsibility-design_ja.md" in guide
    assert "00-hub/release-gate-notes_ja.md" in guide
    assert "同一world volumeを継承する既存deployment" in guide
    assert "Stack担当がbackstage inventoryを読み" in guide
    assert "release済みset" in guide
    assert "gate coordinatorを通常handoffの必須者にしない" in guide

    # A run-through found notices silently carried over across a preset bump
    # (an existing operator notice duplicated the target release's own
    # product notice). The fix is a procedural checkpoint, not a doctor
    # check: ask the human for an explicit preset/notice request right after
    # collection, instead of inheriting whatever the prior project had.
    assert "収集直後のrequest確認" in guide
    assert "継承／編集／追加／削除" in guide
    assert guide.index("release artifact／preset準備runbook") < guide.index(
        "収集直後のrequest確認"
    )


def test_operator_uv_has_one_canonical_install_path() -> None:
    bootstrap = (REPO_ROOT / "tools" / "bootstrap-ubuntu-operator.sh").read_text(
        encoding="utf-8"
    )
    fresh_host = (REPO_ROOT / "docs" / "fresh-host-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )

    assert 'UV_BIN="$HOME/.local/bin/uv"' in bootstrap
    assert "ensure_uv_on_login_path" in bootstrap
    assert 'resolves_to uv "$UV_BIN"' in bootstrap
    assert "$HOME/.local/bin/uv" in fresh_host


def test_operator_runbooks_use_plain_mcrctl_and_check_first() -> None:
    for document in _documents():
        assert "uv run --project" not in document.read_text(encoding="utf-8"), document.name

    for relative_path in (
        "docs/public-vps-bootstrap-guide_ja.md",
        "docs/homepage-sync-guide_ja.md",
        "docs/backup-and-restore-guide_ja.md",
    ):
        guide = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        commands = re.findall(r"(?m)^mcrctl [a-z-]+(?: [a-z-]+)?", guide)
        assert commands[0] == "mcrctl operator check", relative_path
        assert "fresh-host-bootstrap-guide_ja.md#pathが通っていないとき" in guide


def test_fresh_host_guide_restores_commands_without_reinstalling() -> None:
    guide = (REPO_ROOT / "docs" / "fresh-host-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )
    recovery = guide.split("## PATHが通っていないとき", 1)[1]

    assert "bootstrap-ubuntu-operator.sh --check" in recovery
    assert "bootstrap-ubuntu-operator.sh --link" in recovery


def test_release_artifact_intake_is_one_canonical_path_before_deployment() -> None:
    guide_path = "docs/release-preset-preparation-guide_ja.md"
    guide = (REPO_ROOT / guide_path).read_text(encoding="utf-8")
    public_vps = (REPO_ROOT / "docs/public-vps-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )

    assert guide_path in (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert guide_path.split("/", 1)[1] in public_vps

    assert len(guide.splitlines()) <= 220
    for required_input in (
        "release name",
        "release tag",
        "manifest.json",
        "GitHub Releases",
        "GHCR",
        "Paper",
        "OCI registry",
    ):
        assert required_input in guide

    assert "mcrctl release-manifest verify" in guide
    assert 'gh api "repos/Naohiro2g/McRemote/releases/tags/$MC_REMOTE_TAG"' in guide
    assert 'gh release download "$MC_REMOTE_TAG"' in guide
    assert "sha256sum" in guide
    assert "docker buildx imagetools inspect" in guide
    assert "実物のidentity" in guide
    assert "manifestが示すidentity" in guide
    assert "git-build artifact" in guide
    for provenance_field in (
        "repository",
        "full commit",
        "recipe",
        "toolchain",
        "build input",
        "output SHA-256",
    ):
        assert provenance_field in guide
    assert "mcrctl artifact import-reviewed" in guide
    assert "scratch-contracts/$SCRATCH_SOURCE_COMMIT" in guide
    assert "src/mc_remote_stack/data/preset_registry/<name>/<revision>/preset.toml" in guide
    assert "uv run tools/rebuild-preset-catalog.py" in guide
    assert "uv run mcrctl preset show" in guide
    assert "uv run pytest" in guide
    assert "uv run ruff check ." in guide

    # This exact commit is the well-documented root cause of the 4c893bd/0be46fc
    # artifact-mismatch incident (DECISIONS_ja.md 2026-09-06-02); it must never
    # reappear in this guide as a trustworthy worked example.
    assert "4c893bd" not in guide
    assert 'git -C "$SCRATCH_SOURCE" archive' not in guide

    assert guide.index("release tag") < guide.index("GitHub Releases")
    assert guide.index("GitHub Releases") < guide.index("preset_registry/<name>/<revision>")
    assert guide.index("preset_registry/<name>/<revision>") < guide.index("uv run pytest")


def test_deployment_interface_implementation_has_no_incident_commit_example() -> None:
    doc = (
        REPO_ROOT / "docs" / "deployment-interface-implementation_ja.md"
    ).read_text(encoding="utf-8")

    assert "4c893bd" not in doc
    assert "manifest.json" in doc


def _documents() -> list[Path]:
    return [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md"))]


def test_readme_leads_to_the_current_procedures() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    for page in (
        "docs/fresh-host-bootstrap-guide_ja.md",
        "docs/release-preset-preparation-guide_ja.md",
        "docs/public-vps-bootstrap-guide_ja.md",
        "docs/homepage-sync-guide_ja.md",
        "docs/backup-and-restore-guide_ja.md",
    ):
        assert f"]({page})" in readme


def test_relative_document_links_resolve() -> None:
    for document in _documents():
        source = document.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", source):
            if re.match(r"[a-z]+://", target):
                continue
            assert (document.parent / target).exists(), f"{document.name}: {target}"


def test_current_deployment_runbook_shell_blocks_parse() -> None:
    for document in _documents():
        relative_path = document.relative_to(REPO_ROOT)
        source = document.read_text(encoding="utf-8")
        for index, block in enumerate(_shell_blocks(source), start=1):
            result = subprocess.run(
                ["bash", "-n"],
                input=block,
                text=True,
                capture_output=True,
                check=False,
            )
            assert result.returncode == 0, (
                f"{relative_path} shell block {index}: {result.stderr}"
            )


def test_preset_catalog_has_a_supported_rebuild_command() -> None:
    tool_path = REPO_ROOT / "tools" / "rebuild-preset-catalog.py"
    tool = tool_path.read_text(encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(tool_path), "--check"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )

    assert "build_preset_catalog" in tool
    assert "preset_catalog.toml" in tool
    assert result.returncode == 0, result.stdout + result.stderr
    assert "status=unchanged" in result.stdout


def test_fresh_host_guide_returns_to_the_single_deployment_runbook() -> None:
    guide = (REPO_ROOT / "docs" / "fresh-host-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )

    assert len(guide.splitlines()) <= 180
    assert "public-vps-bootstrap-guide_ja.md" in guide
    assert "mcrctl init" not in guide
    assert "mcrctl resolve" not in guide
    assert "mcrctl render" not in guide
    assert "mcrctl apply" not in guide
