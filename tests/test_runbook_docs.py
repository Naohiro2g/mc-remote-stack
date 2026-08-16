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
    section = guide.split("### 現行b2 VPSの停止境界", 1)[1].split("## 1.", 1)[0]

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
