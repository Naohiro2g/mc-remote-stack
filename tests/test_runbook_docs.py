import re
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


def test_public_vps_runbook_uses_the_real_toml_init_cli() -> None:
    guide = (REPO_ROOT / "docs" / "public-vps-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )

    assert "mcrctl init-toml" not in guide
    assert "mcrctl init \"$MC_REMOTE_PROJECT\" \\\n  --format toml" in guide
    assert "--profile vps-server@6" in guide
    assert "--preset public-web-paper@2" in guide
    assert "--volume caddy-data=official-public-beta-caddy-data" in guide
    assert 'adapter = "public-routes@1"' in guide
    assert 'adapter = "minecraft-server@1"' in guide
    assert "--exposure public" in guide


def test_public_vps_runbook_keeps_human_apply_checkpoint() -> None:
    guide = (REPO_ROOT / "docs" / "public-vps-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )

    assert 'REVIEWED_LOCK_IDENTITY="sha256:<planで確認した64-hex>"' in guide
    assert "--expected-lock-identity \"$REVIEWED_LOCK_IDENTITY\"" in guide
    assert "--bootstrap" in guide
    assert "--yes" in guide
    assert "--allow-unverified" in guide


def test_public_vps_runbook_never_runs_mcrctl_through_sudo() -> None:
    guide = (REPO_ROOT / "docs" / "public-vps-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )

    assert not re.search(r"sudo\s+[^\n]*mcrctl", guide)
    assert "mcrctl operator check" in guide
    assert "docker group" in guide
    assert "runtime group" in guide


def test_public_vps_runbook_puts_generic_same_volume_update_before_history() -> None:
    guide = (REPO_ROOT / "docs" / "public-vps-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )
    current = guide.split("## 1. 通常のrelease更新", 1)[1].split(
        "## 2. 新規host bootstrapと歴史的救済", 1
    )[0]
    commands = "\n".join(re.findall(r"```sh\n(.*?)```", current, re.S))

    assert "mcrctl\" deployment update plan" in current
    assert '--to-profile "$REVIEWED_NEXT_PROFILE"' in current
    assert '--to-preset "$REVIEWED_NEXT_PRESET"' in current
    assert "次releaseが確定するまで実行しない" in current
    assert "mcrctl\" deployment update apply" in current
    assert "--plan-id \"$REVIEWED_UPDATE_PLAN\"" in current
    assert "stateful volumeは同じidentity" in current
    assert "Compose pathを手入力しない" in current
    assert "--target-volume" not in commands
    assert "--preserve-compose-file" not in commands


def test_public_vps_runbook_canonicalizes_live_overlays_before_normal_updates() -> None:
    guide = (REPO_ROOT / "docs" / "public-vps-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )
    current = guide.split("## 1. 通常のrelease更新", 1)[1].split(
        "## 2. 新規host bootstrapと歴史的救済", 1
    )[0]
    commands = "\n".join(re.findall(r"```sh\n(.*?)```", current, re.S))

    assert "mcrctl\" deployment composition plan" in current
    assert "--to-profile vps-server@10" in current
    assert "--to-preset public-web-paper@4" in current
    assert "public-routes.wirescope=wirescope-beta.mc-remote.com" in current
    assert "mcrctl\" deployment composition apply" in current
    assert "--plan-id \"$REVIEWED_COMPOSITION_PLAN\"" in current
    assert current.index("deployment composition plan") < current.index(
        "deployment update plan"
    )
    assert "周辺plugin、homepage tree、backup bind" in current
    assert "sha256:9137ec654fae162c0246576ddbb414ddf8521bd6985c8fea54dd215509589e0f" in current
    assert "sha256:9da2e50bacc8091308eb989bc9f3bf159528cc9f25b4afe00fa3282070ff8b5e" in current
    assert "render=current" in current
    assert "compatibility=unverified" in current
    assert "--to-profile vps-server@12" in current
    assert "--to-preset public-web-paper@5" in current
    assert '--replace-input "connection-targets=$REVIEWED_NOTICE_INPUT"' in current
    assert "staleなMcRemote JAR" in current
    assert "render=current" in current
    assert "--preserve-compose-file" not in commands


def test_public_vps_runbook_uses_ordered_notice_file_and_keeps_release_notice_last() -> None:
    guide = (REPO_ROOT / "docs" / "public-vps-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )

    assert "--replace-input \"connection-targets=$REVIEWED_NOTICE_INPUT\"" in guide
    assert "今後のリリース予定" in guide
    assert "WireScope（ワイヤースコープ）ライブ画面" in guide
    assert "マイクラリモコンScratchクライアント ver.2100.0.0b4" in guide
    assert "presetから自動的に末尾" in guide
    assert "sha256:50bc44760750c452c4c7fcc21d76d5e826bfd130cb439862335cbd4a6b5e88b1" in guide
    assert "doctor_network_mismatch" in guide
    assert "同じplan IDをresume" in guide
    assert "mkdtemp()" in guide
    assert "runtime_content_permissions_invalid" in guide
    assert "OK doctor homepage=current" in guide


def test_public_vps_runbook_repairs_the_whole_project_tree_before_mutation() -> None:
    guide = (REPO_ROOT / "docs" / "public-vps-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )

    assert "project配下を再帰的に検査" in guide
    assert "--repair-project \"$MC_REMOTE_PROJECT\"" in guide
    assert "--repair-artifact-store \"$MC_REMOTE_ARTIFACT_STORE\"" in guide
    assert "sudoedit" not in guide


def test_public_vps_runbook_uses_resumable_auth_migration_transaction() -> None:
    guide = (REPO_ROOT / "docs" / "public-vps-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )

    assert "Docker / Composeをrunbookから直接操作してこの境界を迂回しない" in guide
    assert "mcrctl\" migration auth-enforcement plan" in guide
    assert "mcrctl\" migration auth-enforcement apply" in guide
    assert '--preserve-compose-file "$MC_REMOTE_PROJECT/recovery/compose.recovery-plugins.yaml"' in guide
    assert '--preserve-compose-file "$MC_REMOTE_PROJECT/recovery/compose.homepage.yaml"' in guide
    assert '--auth-config-root "$AUTH_CONFIG_ROOT"' in guide
    assert "review済みの追加Composeだけをexact SHA-256で保存" in guide
    assert "旧runtimeへ自動復帰しない" in guide


def test_public_vps_runbook_does_not_treat_b2_to_b3_as_bootstrap() -> None:
    guide = (REPO_ROOT / "docs" / "public-vps-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )

    assert "vps-server@5` / `public-web-paper@1`" in guide
    assert "vps-server@6` / `public-web-paper@2`" in guide
    assert "b2からb3への更新に`--bootstrap`を使わない" in guide
    assert "live Docker inspect / doctor" in guide
    assert 'mcrctl" migration public-b3 plan' in guide
    assert 'mcrctl" migration public-b3 apply' in guide
    assert "source volumeは削除しない" in guide
    assert "source-auth-config.yml" in guide


def test_public_b3_runbook_reuses_exact_runtime_compose_provenance() -> None:
    guide = (REPO_ROOT / "docs" / "public-vps-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )
    section = guide.split("### 現行b2 VPSの停止境界", 1)[1].split("## 3.", 1)[0]

    assert "com.docker.compose.project.config_files" in section
    assert '--preserve-compose-file "$SOURCE_RECOVERY_COMPOSE"' in section
    assert '--preserve-compose-file "$SOURCE_HOMEPAGE_COMPOSE"' in section
    assert (
        '--preserve-compose-file "$MC_REMOTE_PROJECT/recovery/'
        'compose.recovery-plugins.yaml"'
    ) not in section
    assert (
        '--preserve-compose-file "$MC_REMOTE_PROJECT/recovery/'
        'compose.homepage.yaml"'
    ) not in section


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


def test_fresh_host_guide_keeps_canonicalized_local_content_recoverable() -> None:
    guide = (REPO_ROOT / "docs" / "fresh-host-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )

    assert "artifact store全体" in guide
    assert "trees/sha256" in guide
    assert "配布元が確立した意味ではない" in guide


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
    assert "25566" in guide
    assert "25576" in guide
    assert "Minecraft client" in guide
    assert "開発者workstation" in guide
    assert "GUI、browser、Minecraft Launcherをserver hostへ導入しない" in guide
    assert "EXACT_PRESET_REF" in guide
    assert "exact set未凍結中は設定しない" in guide
    assert "mcrctl operator check" in guide
    assert "mcrctl resolve" in guide
    assert "mcrctl plan" in guide
    assert "mcrctl artifact fetch" in guide
    assert "mcrctl render" in guide
    assert "mcrctl apply" in guide
    assert "mcrctl doctor" in guide
    assert "mcrctl deployment update plan" in guide
    assert "mcrctl deployment update apply" in guide
    assert "candidate deployは未許可" in guide
    assert "sudo mcrctl" not in guide
    assert "ケータリング" not in guide
