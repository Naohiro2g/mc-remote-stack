from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_temurin_paper_runtime_uses_pinned_base_and_jre_archive() -> None:
    dockerfile = (ROOT / "containers/temurin-paper-runtime/Dockerfile").read_text()
    workflow = (ROOT / ".github/workflows/publish-temurin-paper-runtime.yml").read_text()

    assert yaml.compose(workflow) is not None
    base_image = (
        "itzg/minecraft-server:2026.7.2-java25@sha256:"
        "6ec1110e4d9236d00ae9436a3e4a5929583e5b19cc94b756a7c603f7cf647a77"
    )
    assert base_image in dockerfile
    assert "COPY runtime/ /opt/java/openjdk/" in dockerfile
    assert "OpenJDK25U-jre_x64_linux_hotspot_25.0.5_8.tar.gz" in workflow
    assert "6fac98f0c8202efc3135a9e9e42aa61963ec42d1c5cbb40caf231e7b14a24ef6" in workflow
    assert "packages: write" in workflow
    assert "secrets.GITHUB_TOKEN" in workflow
    assert "provenance: false" in workflow
