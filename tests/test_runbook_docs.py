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

    assert len(guide.splitlines()) <= 180
    assert "uv run" in guide
    assert '"$MC_REMOTE_PROJECT/mc-remote.toml"' in guide
    assert "mcrctl deployment update plan" in guide
    assert "mcrctl deployment update apply" in guide
    assert "mcrctl doctor" in guide
    assert guide.index("mcrctl deployment update plan") < guide.index(
        "mcrctl deployment update apply"
    ) < guide.index("mcrctl doctor")
    assert "00-hub/release-operations-responsibility-design_ja.md" in guide
    assert "00-hub/release-gate-notes_ja.md" in guide
    assert "同一world volumeを継承する既存deployment" in guide
    assert "Stack担当がbackstage inventoryを読み" in guide
    assert "release済みset" in guide
    assert "gate coordinatorを通常handoffの必須者にしない" in guide
    assert "compact state adoption" in guide


def test_operator_uv_has_one_canonical_install_path() -> None:
    bootstrap = (REPO_ROOT / "tools" / "bootstrap-ubuntu-operator.sh").read_text(
        encoding="utf-8"
    )
    fresh_host = (REPO_ROOT / "docs" / "fresh-host-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )

    assert 'UV_BIN="$HOME/.local/bin/uv"' in bootstrap
    assert "ensure_uv_on_login_path" in bootstrap
    assert "command -v uv" in bootstrap
    assert "$HOME/.local/bin/uv" in fresh_host


def test_operator_runbooks_use_bare_uv_after_bootstrap() -> None:
    for relative_path in (
        "docs/fresh-host-bootstrap-guide_ja.md",
        "docs/public-vps-bootstrap-guide_ja.md",
    ):
        guide = (REPO_ROOT / relative_path).read_text(encoding="utf-8")

        assert re.search(r"(?m)^uv (?:run|sync|--version)(?: |$)", guide)


def test_readmes_point_to_current_vps_procedure() -> None:
    for name in ("README.md", "README_ja.md"):
        readme = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "public-vps-bootstrap-guide_ja.md" in readme

    assert "## Operational runbooks" in (REPO_ROOT / "README.md").read_text(
        encoding="utf-8"
    )
    assert "## 正準 runbook" in (REPO_ROOT / "README_ja.md").read_text(
        encoding="utf-8"
    )


def test_readmes_start_from_a_short_request_and_lock_handed_off_artifact_identity() -> None:
    readme_ja = (REPO_ROOT / "README_ja.md").read_text(encoding="utf-8")
    readme_en = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "短い依頼" in readme_ja
    assert "Stack担当が" in readme_ja
    assert "直接作成・編集" in readme_ja
    assert "handoffが採用するartifact identity" in readme_ja
    assert "exact identity" in readme_ja
    assert "git-build artifact" in readme_ja
    assert "short request" in readme_en
    assert "handoff selects" in readme_en


def test_release_artifact_intake_is_one_canonical_path_before_deployment() -> None:
    guide_path = "docs/release-preset-preparation-guide_ja.md"
    guide = (REPO_ROOT / guide_path).read_text(encoding="utf-8")
    public_vps = (REPO_ROOT / "docs/public-vps-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )
    normal_dev = (REPO_ROOT / "docs/normal-dev-environment-guide_ja.md").read_text(
        encoding="utf-8"
    )

    for readme_name in ("README.md", "README_ja.md"):
        assert guide_path in (REPO_ROOT / readme_name).read_text(encoding="utf-8")
    assert guide_path.split("/", 1)[1] in public_vps
    assert guide_path.split("/", 1)[1] in normal_dev

    assert len(guide.splitlines()) <= 220
    for required_input in (
        "release name",
        "component release handoff",
        "Scratch contract handoff",
        "GitHub Releases",
        "GHCR",
        "Paper",
        "OCI registry",
    ):
        assert required_input in guide

    assert 'gh api "repos/Naohiro2g/McRemote/releases/tags/$MC_REMOTE_TAG"' in guide
    assert 'gh release download "$MC_REMOTE_TAG"' in guide
    assert "sha256sum" in guide
    assert "docker buildx imagetools inspect" in guide
    assert "handoffが採用するartifact identity" in guide
    assert "実物のidentity" in guide
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
    assert 'git -C "$SCRATCH_SOURCE" archive' in guide
    assert "scratch-contracts/$SCRATCH_COMMIT" in guide
    assert "src/mc_remote_stack/data/preset_registry/<name>/<revision>/preset.toml" in guide
    assert "uv run tools/rebuild-preset-catalog.py" in guide
    assert "uv run mcrctl preset show" in guide
    assert "uv run pytest" in guide
    assert "uv run ruff check ." in guide

    assert guide.index("component release handoff") < guide.index("GitHub Releases")
    assert guide.index("GitHub Releases") < guide.index("preset_registry/<name>/<revision>")
    assert guide.index("preset_registry/<name>/<revision>") < guide.index("uv run pytest")


def test_current_deployment_runbook_shell_blocks_parse() -> None:
    for relative_path in (
        "docs/release-preset-preparation-guide_ja.md",
        "docs/public-vps-bootstrap-guide_ja.md",
        "docs/normal-dev-environment-guide_ja.md",
    ):
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
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


def test_readmes_report_the_current_b4_credential_alpha_boundary() -> None:
    japanese = (REPO_ROOT / "README_ja.md").read_text(encoding="utf-8")
    english = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    for readme in (japanese, english):
        assert "mcremote-paper@6" in readme
        assert (
            "331633ef15a729658496e89fe49cb8a5eb5ebcb2ec86937b7e5313528d7ec997"
            in readme
        )
        assert "doctor_credential_health_unsupported" in readme

    assert "b4利用者機能を律速しない" in japanese
    assert "does not block the b4 user-facing feature gate" in english


def test_deployment_workflow_design_replaces_release_named_normal_operations() -> None:
    design = (REPO_ROOT / "docs" / "deployment-operator-workflow-design_ja.md").read_text(
        encoding="utf-8"
    )
    readme = (REPO_ROOT / "README_ja.md").read_text(encoding="utf-8")

    assert "mcrctl deployment update plan" in design
    assert "mcrctl deployment update apply" in design
    assert "通常更新に`public-bN`" in design
    assert "保存済みScratch／Python建築コード" in design
    assert "停止前preflight" in design
    assert "手編集ゼロ" in design
    assert "composition plan" in design
    assert "周辺plugin JAR" in design
    assert "homepage tree" in design
    assert "backup bind" in design
    assert "deployment-operator-workflow-design_ja.md" in readme


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


def test_normal_dev_runbook_tracks_the_current_host_native_runtime() -> None:
    guide = (REPO_ROOT / "docs" / "normal-dev-environment-guide_ja.md").read_text(
        encoding="utf-8"
    )
    readme = (REPO_ROOT / "README_ja.md").read_text(encoding="utf-8")

    assert "normal-dev-environment-guide_ja.md" in readme
    assert len(guide.splitlines()) <= 180
    assert "dev-integration" in guide
    assert "host-native" in guide
    assert "run.sh" in guide
    assert "Screen" in guide
    assert "通常dev topologyをhost-native方式" in guide
    assert "ケータリング型で構築する通常dev" in guide
    assert "compact `apply`／`doctor`" in guide
    assert "channel: `dev`" in guide
    assert "exposure: `lan-only`" in guide
    assert "release-preset-preparation-guide_ja.md" in guide
    assert "backstage inventory" in guide
    assert "authorized next action" in guide
    assert "human operator（release済みset）" in guide
    assert 'SERVER_ROOT="<backstage handoff>"' in guide
    assert 'SCREEN_SESSION="<backstage handoff>"' in guide
    assert "sha256sum" in guide
    assert "stop" in guide
    assert "operator-backup" in guide
    assert "Credential domain health: HEALTHY" in guide
    assert "auth_required" in guide

    assert guide.index("## 2. read-only preflight") < guide.index(
        "## 3. artifact staging"
    ) < guide.index("## 4. 正常停止と一件交換") < guide.index(
        "## 5. 起動とreadiness"
    )
