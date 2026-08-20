import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_ubuntu_operator_bootstrap_is_auditable_and_prepares_real_tools() -> None:
    script_path = REPO_ROOT / "tools" / "bootstrap-ubuntu-operator.sh"
    script = script_path.read_text(encoding="utf-8")

    assert os.access(script_path, os.X_OK)
    assert "--check" in script
    assert "--install" in script
    assert "--repair-project" in script
    assert "--repair-artifact-store" in script
    assert "download.docker.com/linux/ubuntu" in script
    assert "docker-ce-cli" in script
    assert "docker-compose-plugin" in script
    assert "usermod -aG docker" in script
    assert "UV_BOOTSTRAP_VERSION=" in script
    assert "UV_NO_MODIFY_PATH=1" in script
    assert "uv sync --extra dev" in script
    assert "sudo mcrctl" not in script
    assert "curl -LsSf" not in script or "| sh" not in script
    install_branch = script.split('if [[ "$mode" == install ]]', 1)[1]
    assert '"$uv_bin" python install 3.11' in install_branch
    assert 'elif [[ ! -x "$repo_root/.venv/bin/mcrctl" ]]' in install_branch


def test_fresh_host_runbook_uses_operator_bootstrap_as_the_only_tool_setup_entry() -> None:
    guide = (REPO_ROOT / "docs" / "fresh-host-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )

    assert "tools/bootstrap-ubuntu-operator.sh --check" in guide
    assert "tools/bootstrap-ubuntu-operator.sh --install" in guide
    assert "mcrctl operator check" in guide
    assert "sudo mcrctl" not in guide
