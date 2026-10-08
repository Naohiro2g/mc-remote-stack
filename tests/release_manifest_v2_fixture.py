"""toolingの固定契約を使ったunit試験用の合成manifest。実機観測を表さない。"""

import hashlib
import json
from pathlib import Path

from .test_release_manifest import SCRATCH_V2301_0_0B7_POST1


def digest(source: bytes) -> str:
    return hashlib.sha256(source).hexdigest()


def scratch_v2() -> dict:
    document = json.loads(SCRATCH_V2301_0_0B7_POST1)
    document["schema_version"] = 2
    for artifact in document["artifacts"]:
        if artifact["kind"] == "https-file":
            artifact["bytes"] = 0
    for os, arch in [("windows", "x64"), ("macos", "arm64"), ("linux", "x64")]:
        document["artifacts"].append(
            {
                "role": "scratch-local",
                "kind": "https-file",
                "file": f"{os}-{arch}.zip",
                "sha256": digest(b""),
                "bytes": 0,
                "os": os,
                "arch": arch,
            }
        )
    return document


def minecraft_v2() -> tuple[dict, bytes, dict[str, bytes]]:
    declaration = json.dumps(["1.21.11", "26.2"]).encode()
    assets = {"neutral-name.jar": b"jar", "first.json": b"first record", "second.json": b"second record"}
    document = {
        "schema": "mc-remote.release-manifest",
        "schema_version": 2,
        "release_tag": "v2320.0.0b10",
        "source_commit": "1" * 40,
        "artifacts": [
            {"role": "jar", "kind": "https-file", "file": "neutral-name.jar", "sha256": digest(b"jar"), "bytes": 3}
        ],
        "minecraft_compatibility": {
            "declaration": {
                "path": "release/minecraft-targets.json",
                "sha256": digest(declaration),
                "minecraft_versions": ["1.21.11", "26.2"],
            },
            "verifications": [
                {
                    "minecraft_version": version,
                    "paper_build": build,
                    "server_sha256": "a" * 64,
                    "java_version": "21.0.12.1+1-1-24.04.4-Ubuntu",
                    "jar_sha256": digest(b"jar"),
                    "result": "PASS",
                    "record": {"file": file, "sha256": digest(assets[file])},
                }
                for version, build, file in [("1.21.11", 132, "first.json"), ("26.2", 10, "second.json")]
            ],
        },
    }
    return document, declaration, assets


def preset() -> dict:
    return {
        "components": [
            {"id": "paper-server", "role": "paper-server", "artifact": "paper", "minecraft_version": "26.2"},
            {"id": "mcremote", "role": "mcremote-plugin", "artifact": "mcremote"},
        ],
        "artifacts": [
            {"id": "paper", "kind": "https-file", "sha256": "a" * 64},
            {"id": "mcremote", "kind": "https-file", "sha256": digest(b"jar")},
        ],
    }


def contract_root(tmp_path: Path) -> Path:
    """実在する固定schemaと独立lockを試験用directoryへ収容する。"""
    root = tmp_path / "contract"
    (root / "schemas").mkdir(parents=True)
    bundled = Path(__file__).parents[1] / "src/mc_remote_stack/data"
    for relative in (
        "schemas/release-manifest.schema.json",
        "schemas/release-manifest-v2.schema.json",
        "release-manifest-lock.json",
    ):
        (root / relative).write_bytes((bundled / relative).read_bytes())
    return root
