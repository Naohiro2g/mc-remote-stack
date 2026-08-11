from __future__ import annotations

import email
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LICENSE_PATH = PROJECT_ROOT / "LICENSE"


def test_project_declares_pep639_mit_license() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["license"] == "MIT"
    assert pyproject["project"]["license-files"] == ["LICENSE"]
    assert any(requirement.startswith("setuptools>=77") for requirement in pyproject["build-system"]["requires"])

    license_text = LICENSE_PATH.read_text(encoding="utf-8")
    assert license_text.startswith(
        "MIT License\n\nCopyright (c) 2026 Naohiro Tsuji and contributors.\n"
    )
    assert "Permission is hereby granted, free of charge" in license_text
    assert 'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND' in license_text


def test_wheel_and_sdist_include_pep639_license(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", project / "pyproject.toml")
    shutil.copy2(PROJECT_ROOT / "README.md", project / "README.md")
    shutil.copy2(LICENSE_PATH, project / "LICENSE")
    shutil.copytree(PROJECT_ROOT / "src", project / "src")

    dist = tmp_path / "dist"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(dist),
            str(project),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    wheel_path, = dist.glob("*.whl")
    sdist_path, = dist.glob("*.tar.gz")
    expected_license = LICENSE_PATH.read_bytes()

    with zipfile.ZipFile(wheel_path) as wheel:
        wheel_names = wheel.namelist()
        wheel_license, = (name for name in wheel_names if name.endswith(".dist-info/licenses/LICENSE"))
        wheel_metadata, = (name for name in wheel_names if name.endswith(".dist-info/METADATA"))
        assert wheel.read(wheel_license) == expected_license
        metadata = email.message_from_bytes(wheel.read(wheel_metadata))
        assert metadata["License-Expression"] == "MIT"
        assert metadata.get_all("License-File") == ["LICENSE"]

    with tarfile.open(sdist_path, mode="r:gz") as sdist:
        sdist_members = sdist.getmembers()
        sdist_license, = (member for member in sdist_members if member.name.endswith("/LICENSE"))
        sdist_metadata, = (
            member
            for member in sdist_members
            if member.name.endswith("/PKG-INFO") and member.name.count("/") == 1
        )
        license_file = sdist.extractfile(sdist_license)
        metadata_file = sdist.extractfile(sdist_metadata)
        assert license_file is not None
        assert metadata_file is not None
        assert license_file.read() == expected_license
        metadata = email.message_from_bytes(metadata_file.read())
        assert metadata["License-Expression"] == "MIT"
        assert metadata.get_all("License-File") == ["LICENSE"]
