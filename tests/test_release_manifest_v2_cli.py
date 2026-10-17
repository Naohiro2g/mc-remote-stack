import json

from mc_remote_stack.cli import main

from .release_manifest_v2_fixture import contract_root, minecraft_v2, scratch_v2


def test_verify_prints_selectors_bytes_and_compatibility(tmp_path, capsys):
    root = contract_root(tmp_path)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(scratch_v2()))
    assert main(["release-manifest", "verify", str(path), "--contract-dir", str(root)]) == 0
    output = capsys.readouterr().out
    assert "bytes=0 os=linux arch=x64" in output
    path.write_text(json.dumps(minecraft_v2()[0]))
    assert main(["release-manifest", "verify", str(path), "--contract-dir", str(root)]) == 0
    output = capsys.readouterr().out
    assert "minecraft_versions=1.21.11,26.2" in output
    assert "paper_build=10" in output
    assert "java_version=21.0.12.1+1-1-24.04.4-Ubuntu" in output
    assert "external-digests=unchecked" in output
    assert "verified=true" not in output


def test_select_prints_only_the_explicit_asset_as_json(tmp_path, capsys):
    root = contract_root(tmp_path)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(scratch_v2()))
    args = [
        "release-manifest",
        "select",
        str(path),
        "scratch-local",
        "--kind",
        "https-file",
        "--contract-dir",
        str(root),
    ]
    assert main([*args, "--os", "linux", "--arch", "x64"]) == 0
    selected = json.loads(capsys.readouterr().out)
    assert selected["file"] == "linux-x64.zip"
    assert main(args) == 2
    assert "release_manifest_selector_required" in capsys.readouterr().out


def test_collect_checks_all_digests_before_success_and_preset(tmp_path, capsys):
    root = contract_root(tmp_path)
    document, declaration, assets = minecraft_v2()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document))
    declaration_path = tmp_path / "targets.json"
    declaration_path.write_bytes(declaration)
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    for name, source in assets.items():
        (asset_dir / name).write_bytes(source)
    preset_path = tmp_path / "preset.toml"
    preset_path.write_text(
        '''
[[components]]
id = "paper"
role = "paper-server"
artifact = "paper-jar"
minecraft_version = "26.2"
[[components]]
id = "mcremote"
role = "mcremote-plugin"
artifact = "plugin-jar"
[[artifacts]]
id = "paper-jar"
kind = "https-file"
sha256 = "'''
        + "a" * 64
        + '''"
[[artifacts]]
id = "plugin-jar"
kind = "https-file"
sha256 = "'''
        + document["artifacts"][0]["sha256"]
        + """"
"""
    )
    args = [
        "release-manifest",
        "collect",
        str(path),
        "--contract-dir",
        str(root),
        "--artifact",
        "jar:https-file",
        "--asset-dir",
        str(asset_dir),
        "--declaration-file",
        str(declaration_path),
        "--preset-file",
        str(preset_path),
        "--paper-build",
        "10",
        "--java-version",
        "21.0.12.1+1-1-24.04.4-Ubuntu",
    ]
    assert main(args) == 0
    output = capsys.readouterr().out
    assert "COLLECTION role=jar kind=https-file" in output
    assert "bytes=3" in output
    assert "MINECRAFT-PRESET minecraft_version=26.2 foundation=matched" in output
    (asset_dir / "second.json").write_bytes(b"tampered")
    assert main(args) == 2
    output = capsys.readouterr().out
    assert "release_manifest_record_digest_mismatch" in output
    assert "COLLECTION" not in output
    assert "foundation=matched" not in output


def test_v2_collect_requires_explicit_preset_for_jar(tmp_path, capsys):
    root = contract_root(tmp_path)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(minecraft_v2()[0]))
    assert (
        main(["release-manifest", "collect", str(path), "--contract-dir", str(root), "--artifact", "jar:https-file"])
        == 2
    )
    assert "release_manifest_foundation_unverified" in capsys.readouterr().out
