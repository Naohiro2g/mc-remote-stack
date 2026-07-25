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
    assert "--profile vps-server@2" in guide
    assert "--preset public-web-paper@1" in guide
    assert "--volume caddy-data=official-public-beta-caddy-data" in guide
    assert 'adapter = "public-routes@1"' in guide
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
