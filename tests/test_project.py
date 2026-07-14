import os
from pathlib import Path

from mc_remote_stack.project import accept_eula, init_project
from mc_remote_stack.repo_check import check_repository
from mc_remote_stack.validation import try_load_project
from mc_remote_stack.yamlio import dump_mapping, load_mapping

from .helpers import make_renderable_project


def test_init_creates_safe_unresolved_project(tmp_path: Path) -> None:
    project = init_project(tmp_path / "deployment", "official-vps")

    assert project.config.exists()
    assert project.lock.exists()
    assert not check_repository(project.root)

    loaded, issues = try_load_project(project.root)
    assert loaded is not None
    assert any(issue.path.endswith("eula_accepted") for issue in issues)
    assert any(issue.path.endswith("images.caddy") for issue in issues)
    config = load_mapping(project.config)
    lock = load_mapping(project.lock)
    assert config["gameplay"] == {"gamemode": "creative", "force_gamemode": True, "hardcore": True}
    assert config["world"]["radius_blocks"] == 9984
    assert config["mcremote"]["default_origin"] == [200, 0, 200]
    assert lock["minecraft"] == {
        "version": "REPLACE_WITH_MINECRAFT_VERSION",
        "paper": {
            "build": "REPLACE_WITH_PAPER_BUILD",
            "filename": "REPLACE_WITH_PAPER_JAR",
            "sha256": "REPLACE",
            "origin": {"kind": "unresolved"},
        },
    }
    assert lock["homepage"] == {
        "version": "REPLACE_WITH_HOMEPAGE_VERSION",
        "filename": "REPLACE_WITH_HOMEPAGE_ARCHIVE",
        "sha256": "REPLACE",
        "origin": {"kind": "unresolved"},
    }
    assert {artifact["version"] for artifact in lock["plugins"].values()} == {"REPLACE_WITH_PLUGIN_VERSION"}
    assert any(issue.path.endswith("minecraft.version") for issue in issues)
    assert any(issue.path.endswith("minecraft.paper.build") for issue in issues)
    assert any(issue.path.endswith("homepage.sha256") for issue in issues)


def test_init_refuses_non_empty_directory(tmp_path: Path) -> None:
    root = tmp_path / "deployment"
    root.mkdir()
    (root / "keep.txt").write_text("user data", encoding="utf-8")

    try:
        init_project(root, "official-vps")
    except ValueError as exc:
        assert "non-empty" in str(exc)
    else:
        raise AssertionError("init must refuse a non-empty directory")


def test_repo_check_rejects_literal_secret(tmp_path: Path) -> None:
    project = init_project(tmp_path / "deployment", "official-vps")
    config = load_mapping(project.config)
    config["backup"]["password"] = "plain-text-password"
    dump_mapping(project.config, config)

    issues = check_repository(project.root)

    assert any(issue.path.endswith("backup.password") for issue in issues)


def test_repo_check_accepts_secret_reference(tmp_path: Path) -> None:
    project = init_project(tmp_path / "deployment", "official-vps")
    config = load_mapping(project.config)
    config["backup"]["credential"] = "secret://backup_transport"
    dump_mapping(project.config, config)

    assert not check_repository(project.root)


def test_repo_check_honors_project_ignores_before_git_init(tmp_path: Path) -> None:
    project = init_project(tmp_path / "deployment", "official-vps")
    generated = project.root / "generated"
    generated.mkdir()
    (generated / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    secrets = project.root / "secrets"
    secrets.mkdir()
    (secrets / "runtime.secret").write_text("not-for-git\n", encoding="utf-8")

    assert not check_repository(project.root)


def test_backup_output_must_be_outside_data(tmp_path: Path) -> None:
    project = init_project(tmp_path / "deployment", "official-vps")
    config = load_mapping(project.config)
    config["backup"]["output"] = "/data/Backups"
    config["deployment"]["eula_accepted"] = True
    dump_mapping(project.config, config)

    _, issues = try_load_project(project.root)

    assert any(issue.path.endswith("backup.output") for issue in issues)


def test_homepage_domain_rejects_caddyfile_injection(tmp_path: Path) -> None:
    project = init_project(tmp_path / "deployment", "official-vps")
    config = load_mapping(project.config)
    config["deployment"]["eula_accepted"] = True
    config["domains"]["homepage"] = "mc-remote.com {\nrespond hacked"
    dump_mapping(project.config, config)

    _, issues = try_load_project(project.root)

    assert any(issue.path.endswith("domains.homepage") for issue in issues)


def test_homepage_accepts_curated_source_archive_provenance(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    lock = load_mapping(project.lock)
    lock["homepage"]["origin"] = {
        "kind": "source_archive",
        "archive_sha256": f"{12:064x}",
        "archive_filename": "mc-remote-com-homepage.zip",
        "source_root": "public_html/",
        "excluded": [".htaccess", ".user.ini", "c2cc/.user.ini"],
    }
    dump_mapping(project.lock, lock)

    _, issues = try_load_project(project.root)

    assert not any(issue.path.startswith("mc-remote.lock.yml:homepage") for issue in issues)


def test_homepage_source_archive_rejects_parent_traversal(tmp_path: Path) -> None:
    project = make_renderable_project(tmp_path)
    lock = load_mapping(project.lock)
    lock["homepage"]["origin"] = {
        "kind": "source_archive",
        "archive_sha256": f"{12:064x}",
        "archive_filename": "mc-remote-com-homepage.zip",
        "source_root": "../public_html/",
        "excluded": [],
    }
    dump_mapping(project.lock, lock)

    _, issues = try_load_project(project.root)

    assert any(issue.path.endswith("homepage.origin.source_root") for issue in issues)


def test_accept_eula_records_explicit_acceptance(tmp_path: Path) -> None:
    project = init_project(tmp_path / "deployment", "official-vps")

    accept_eula(project.root)

    config = load_mapping(project.config)
    assert config["deployment"]["eula_accepted"] is True
    assert config["deployment"]["eula_accepted_at"].endswith("+00:00")


def test_secret_store_is_outside_project_and_mode_0600(tmp_path: Path, monkeypatch) -> None:
    from mc_remote_stack.secrets import list_secrets, set_secret

    project = init_project(tmp_path / "deployment", "official-vps")
    secret_home = tmp_path / "private-secrets"
    monkeypatch.setenv("MC_REMOTE_SECRET_HOME", str(secret_home))

    destination = set_secret("official-vps", "backup_transport", "not-printed")

    assert not destination.is_relative_to(project.root)
    assert destination.read_text(encoding="utf-8") == "not-printed\n"
    assert os.stat(destination).st_mode & 0o777 == 0o600
    assert list_secrets("official-vps") == ["backup_transport"]
