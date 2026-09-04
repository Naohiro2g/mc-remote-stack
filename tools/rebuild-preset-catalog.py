#!/usr/bin/env python3
"""Rebuild the checked-in preset catalog from its registry and policy."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from mc_remote_stack.preset_registry import build_preset_catalog


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether the checked-in catalog is current without writing it",
    )
    arguments = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    data_root = repo_root / "src" / "mc_remote_stack" / "data"
    destination = data_root / "preset_catalog.toml"
    rendered = build_preset_catalog(data_root=data_root)

    if destination.exists() and destination.read_bytes() == rendered:
        print(f"OK preset catalog status=unchanged path={destination}")
        return 0

    if arguments.check:
        print(f"FAIL preset catalog status=stale path={destination}")
        return 1

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".preset-catalog.", suffix=".tmp", dir=data_root
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    print(f"OK preset catalog status=rebuilt path={destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
