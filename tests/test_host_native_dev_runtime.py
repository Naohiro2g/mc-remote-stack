import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_host_native_dev_runtime_tool_has_reviewed_lifecycle_contract() -> None:
    script_path = REPO_ROOT / "tools" / "host-native-dev-runtime.sh"
    script = script_path.read_text(encoding="utf-8")

    assert os.access(script_path, os.X_OK)
    assert "check|install|verify" in script
    assert 'cd "$RUNTIME_ROOT/data"' in script
    assert "mcremote credential bootstrap" in script
    assert "mcremote credential status" in script
    assert "Credential domain: HEALTHY / healthy / id=" in script
    assert "Credential domain health: HEALTHY (healthy)" in script
    assert "Server started at port 25575" in script
    assert "Done (" in script
    assert "systemctl restart" in script
    assert "auth_required" in script
    assert "screen" not in script
    assert "docker group membership" not in script


def test_host_native_dev_runtime_self_test_rejects_partial_readiness() -> None:
    script_path = REPO_ROOT / "tools" / "host-native-dev-runtime.sh"

    result = subprocess.run(
        [script_path, "self-test"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "PASS readiness barrier requires health+socket+paper-done" in result.stdout
