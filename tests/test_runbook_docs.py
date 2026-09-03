import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


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
    assert "$HOME/.local/bin/uv" in guide
    assert '"$MC_REMOTE_PROJECT/mc-remote.toml"' in guide
    assert "mcrctl deployment update plan" in guide
    assert "mcrctl deployment update apply" in guide
    assert "mcrctl doctor" in guide
    assert guide.index("mcrctl deployment update plan") < guide.index(
        "mcrctl deployment update apply"
    ) < guide.index("mcrctl doctor")
    assert "00-hub/release-operations-responsibility-design_ja.md" in guide
    assert "00-hub/release-gate-notes_ja.md" in guide
    for discarded_record_marker in (
        "適用記録",
        "history-only",
        "migration public-",
        "現行b2",
        "2026-08-21",
        "2026-08-29",
        "2026-09-03",
    ):
        assert discarded_record_marker not in guide


def test_operator_uv_has_one_canonical_install_path() -> None:
    bootstrap = (REPO_ROOT / "tools" / "bootstrap-ubuntu-operator.sh").read_text(
        encoding="utf-8"
    )
    fresh_host = (REPO_ROOT / "docs" / "fresh-host-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )

    assert 'UV_BIN="$HOME/.local/bin/uv"' in bootstrap
    assert "command -v uv" not in bootstrap
    assert "$HOME/.local/bin/uv" in fresh_host


def test_readmes_point_to_current_vps_procedure_instead_of_dated_records() -> None:
    for name in ("README.md", "README_ja.md"):
        readme = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "public-vps-bootstrap-guide_ja.md" in readme
        assert "most recent dated apply record" not in readme
        assert "直近の適用記録" not in readme
        assert "server-runbook-migration-notes_ja.md" not in readme

    assert "## Operational runbooks" in (REPO_ROOT / "README.md").read_text(
        encoding="utf-8"
    )
    assert "## 正準 runbook" in (REPO_ROOT / "README_ja.md").read_text(
        encoding="utf-8"
    )


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


def test_normal_dev_runbook_is_server_only_and_gate_coordinator_driven() -> None:
    guide = (REPO_ROOT / "docs" / "normal-dev-environment-guide_ja.md").read_text(
        encoding="utf-8"
    )
    readme = (REPO_ROOT / "README_ja.md").read_text(encoding="utf-8")

    assert "normal-dev-environment-guide_ja.md" in readme
    assert "home-server@5" in guide
    assert "dev-integration" in guide
    assert "channel: `dev`" in guide
    assert "exposure: `lan-only`" in guide
    assert "25565" in guide
    assert "25575" in guide
    assert "25566" not in guide
    assert "25576" not in guide
    assert "Minecraft client" in guide
    assert "開発者workstation" in guide
    assert "GUI、browser、Minecraft Launcherをserver hostへ導入しない" in guide
    assert "EXACT_PRESET_REF" in guide
    assert "exact set未凍結中は設定しない" in guide
    assert "BOOTSTRAP_CONTRACTS" in guide
    assert "profile追加だけでは初回applyを許可しない" in guide
    assert "mcrctl operator check" in guide
    assert "exact set未凍結中は`--install`を実行しない" in guide
    assert "coordinatorがhost installを明示許可" in guide
    assert "別portを選ぶ" not in guide
    assert "backstage inventoryで所有者、用途、期待状態を確定" in guide
    assert "未知のlistenerを許容しない" in guide
    assert "mcrctl resolve" in guide
    assert "mcrctl plan" in guide
    assert "mcrctl artifact fetch" in guide
    assert "mcrctl artifact import-reviewed" in guide
    assert (
        "McRemoteのpush済みsource commit、artifact名、version、bytes、SHA-256、"
        "credential-free HTTPS取得元"
        not in guide
    )
    assert "git-build provenance" in guide
    assert "review済みbytes import" in guide
    assert "normal-dev-exact-preset.template.toml" in guide
    assert "mcrctl render" in guide
    assert "mcrctl apply" in guide
    assert "mcrctl doctor" in guide
    assert "mcrctl deployment update plan" in guide
    assert "mcrctl deployment update apply" in guide
    assert "candidate deployは未許可" in guide
    assert "sudo mcrctl" not in guide
    assert "ケータリング" not in guide


def test_normal_dev_runbook_documents_reasoned_unverified_acknowledgement() -> None:
    guide = (REPO_ROOT / "docs" / "normal-dev-environment-guide_ja.md").read_text(
        encoding="utf-8"
    )

    assert "[acknowledgements]" in guide
    assert "allow_unverified = true" in guide
    assert (
        'unverified_reason = "b5 exact compatibility set integration evidence is being established"'
        in guide
    )
    assert "acknowledgement_reason_required" in guide
    assert "unverified_not_acknowledged" in guide
    assert "orderとlockを手編集しない" in guide
    assert "gate coordinatorへ戻す" in guide
    assert '"$MCRCTL" validate --project "$MC_REMOTE_PROJECT"' in guide
    assert '"$MCRCTL" resolve --project "$MC_REMOTE_PROJECT" --allow-unverified' in guide
    mcrctl_assignment = 'MCRCTL="$MC_REMOTE_STACK/.venv/bin/mcrctl"'
    validate_command = '"$MCRCTL" validate --project "$MC_REMOTE_PROJECT"'
    assert guide.index(mcrctl_assignment) < guide.index(validate_command)


def test_normal_dev_exact_preset_template_has_review_slots_without_candidate_values() -> None:
    template = (
        REPO_ROOT / "examples" / "normal-dev-exact-preset.template.toml"
    ).read_text(encoding="utf-8")

    assert 'allowed_channels = ["dev"]' in template
    assert 'kind = "git-build"' in template
    assert 'id = "mcremote-jar"' in template
    assert 'repository = "<REVIEWED_HTTPS_REPOSITORY>"' in template
    assert 'commit = "<REVIEWED_FULL_COMMIT_SHA>"' in template
    assert 'output_sha256 = "<REVIEWED_OUTPUT_SHA256>"' in template
    assert 'BOOTSTRAP_CONTRACT = ["home-server@5"' in template
    parsed = tomllib.loads(template)
    assert parsed["requirements"]["allowed_channels"] == ["dev"]
    assert parsed["artifacts"][-1]["kind"] == "git-build"
    assert "6214a6a5efe5180c1cd0f374089736908b07ee34" not in template
    assert "f293e63a77f178bc8d3cba8276e95124f2ee6b3eca77c15867a6fc5e5f166531" not in template
