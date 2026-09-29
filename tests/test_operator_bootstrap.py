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
    assert "RUNTIME_ROOT=/var/lib/mc-remote" in script
    assert 'usermod -aG "$runtime_group"' in script
    assert "runtime root is not traversable" in script
    assert "UV_BOOTSTRAP_VERSION=" in script
    assert "UV_NO_MODIFY_PATH=1" in script
    assert 'UV_BIN="$HOME/.local/bin/uv"' in script
    assert "ensure_uv_on_login_path" in script
    for shell_profile in (".profile", ".bash_profile", ".bash_login", ".bashrc"):
        assert shell_profile in script
    assert 'resolves_to uv "$UV_BIN"' in script
    # Non-interactive, non-login SSH commands (`ssh host 'uv ...'`, the shape
    # runbook automation actually uses) source none of the profiles above, so
    # PATH edits alone leave uv invisible there. /usr/local/bin is on PATH
    # for every shell sshd starts, interactive or not.
    assert "/usr/local/bin/uv" in script
    assert "readlink -f" in script
    assert "--link" in script
    assert '"$UV_BIN" sync --extra dev' in script
    assert "sudo mcrctl" not in script
    assert "curl -LsSf" not in script or "| sh" not in script
    install_branch = script.split('if [[ "$mode" == install ]]', 1)[1]
    assert "python install" not in script
    assert "link_operator_commands" in install_branch
    assert 'elif [[ ! -x "$MCRCTL_BIN" ]]' in install_branch
    assert 'elif ! resolves_to mcrctl "$MCRCTL_BIN"' in install_branch
    assert "Run this same script with --link." in install_branch


def test_operator_commands_are_plain_commands_restored_by_link() -> None:
    script = (REPO_ROOT / "tools" / "bootstrap-ubuntu-operator.sh").read_text(
        encoding="utf-8"
    )
    link_function = script.split("link_operator_commands() {", 1)[1].split("}", 1)[0]

    assert 'sudo ln -sf "$UV_BIN" /usr/local/bin/uv' in link_function
    assert 'sudo ln -sf "$MCRCTL_BIN" /usr/local/bin/mcrctl' in link_function
    link_mode = script.split('if [[ "$mode" == link ]]', 1)[1].split("\nfi\n", 1)[0]
    assert "apt-get" not in link_mode
    assert 'exec "$0" --check' in link_mode


def test_repo_environment_python_follows_the_pinned_minor() -> None:
    script = (REPO_ROOT / "tools" / "bootstrap-ubuntu-operator.sh").read_text(
        encoding="utf-8"
    )
    pinned = (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip()

    assert pinned == "3.12"
    assert '"$repo_root/.python-version"' in script
    assert 'python-preference = "system"' in (REPO_ROOT / "pyproject.toml").read_text(
        encoding="utf-8"
    )


def test_fresh_host_runbook_uses_operator_bootstrap_as_the_only_tool_setup_entry() -> None:
    guide = (REPO_ROOT / "docs" / "fresh-host-bootstrap-guide_ja.md").read_text(
        encoding="utf-8"
    )

    assert "bootstrap-ubuntu-operator.sh --check" in guide
    assert "bootstrap-ubuntu-operator.sh --install" in guide
    assert "mcrctl operator check" in guide
    assert "/var/lib/mc-remote" in guide
    assert "runtime group" in guide
    assert "sudo mcrctl" not in guide
