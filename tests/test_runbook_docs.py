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
