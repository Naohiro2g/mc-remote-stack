"""Pin the exact files the public VPS profile renders.

Any change to what a vps-server@12 deployment receives must show up here as a
deliberate golden update. Regenerate with:

    MCRCTL_UPDATE_GOLDEN=1 uv run pytest tests/test_vps_render_golden.py
"""

import json
import os
from pathlib import Path

from mc_remote_stack.render import render_toml_project
from mc_remote_stack.resolver import load_lock

from .vps_fixture import build_vps_fixture

GOLDEN = Path(__file__).parent / "golden" / "vps-server-12-render.json"


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


def test_vps_server_12_render_matches_golden(tmp_path: Path) -> None:
    fixture = build_vps_fixture(tmp_path)
    render_toml_project(fixture.project, fixture.output, data_root=fixture.data_root)
    lock_identity = load_lock(fixture.project, data_root=fixture.data_root)["lock_identity"]

    actual = _normalized_output(tmp_path, fixture.output, lock_identity)

    if os.environ.get("MCRCTL_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(
            json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    assert actual == json.loads(GOLDEN.read_text(encoding="utf-8"))
