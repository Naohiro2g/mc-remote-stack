"""Pin the exact files the public VPS profiles render.

Any change to what a vps-server@12, @13, @14, @15, or @16 deployment receives must
show up here as a deliberate golden update. Regenerate with:

    MCRCTL_UPDATE_GOLDEN=1 uv run pytest tests/test_vps_render_golden.py
"""

import json
import os
from pathlib import Path

import pytest

from mc_remote_stack.render import render_toml_project
from mc_remote_stack.resolver import load_lock

from .vps_fixture import build_vps_fixture

GOLDEN_DIRECTORY = Path(__file__).parent / "golden"


def _normalized_output(tmp_path: Path, output: Path, lock_identity: str) -> dict[str, str]:
    rendered: dict[str, str] = {}
    for path in sorted(output.rglob("*")):
        # The manifest lists file digests, which depend on the temporary paths.
        if not path.is_file() or path.name == "render-manifest.json":
            continue
        text = path.read_text(encoding="utf-8")
        for temporary in {str(tmp_path.resolve()), str(tmp_path)}:
            text = text.replace(temporary, "<tmp>")
        rendered[path.relative_to(output).as_posix()] = text.replace(
            lock_identity, "<lock-identity>"
        )
    return rendered


@pytest.mark.parametrize(
    "profile",
    ["vps-server@12", "vps-server@13", "vps-server@14", "vps-server@15", "vps-server@16"],
)
def test_vps_server_render_matches_golden(tmp_path: Path, profile: str) -> None:
    golden = GOLDEN_DIRECTORY / f"{profile.replace('@', '-')}-render.json"
    fixture = build_vps_fixture(tmp_path, profile=profile)
    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)
    lock_identity = load_lock(fixture.project, data_root=fixture.data_root)["lock_identity"]

    actual = _normalized_output(tmp_path, fixture.output, lock_identity)

    if os.environ.get("MCRCTL_UPDATE_GOLDEN") == "1":
        golden.parent.mkdir(exist_ok=True)
        golden.write_text(
            json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    assert actual == json.loads(golden.read_text(encoding="utf-8"))
