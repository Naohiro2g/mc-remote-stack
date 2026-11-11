import hashlib
import json
import os
import shutil
import stat
from contextlib import contextmanager

import pytest
import tomlkit

from mc_remote_stack.deployment_interface import (
    _publish_render,
    prepare_interface_deployment,
    write_interface_preview,
)
from mc_remote_stack.deployment_update import (
    DeploymentUpdateContractError,
    _copy_project_source,
    _make_plan,
    _plan_payload,
    _plan_root,
    _prepare_transaction,
    _publish_project,
    apply_deployment_update,
)
from mc_remote_stack.render import render_toml_project
from mc_remote_stack.render_modes import set_render_modes
from mc_remote_stack.resolver import load_lock, resolve_project
from mc_remote_stack.secrets import set_secret

from .test_deployment_interface import _fixture as interface_fixture
from .test_deployment_update import _public_order
from .vps_fixture import build_vps_fixture

MASKS = [pytest.param(0o077, id="umask-077"), pytest.param(0o022, id="umask-022"),
         pytest.param(0o002, id="umask-002")]


@contextmanager
def process_umask(value):
    previous = os.umask(value)
    try:
        yield
    finally:
        os.umask(previous)


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_unclassified_render_files_stay_private_and_symlinks_are_rejected(tmp_path):
    root = tmp_path / "rendered"
    private = root / "unclassified/fixture.key"
    private.parent.mkdir(parents=True)
    private.write_text("synthetic-private-data")
    set_render_modes(root)
    assert mode(private) == 0o600
    assert mode(private.parent) == 0o700
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    outside.chmod(0o600)
    (root / "Caddyfile").symlink_to(outside)
    with pytest.raises(ValueError, match="symlinks"):
        set_render_modes(root)
    assert mode(outside) == 0o600


def assert_container_inputs_readable(root):
    # These exact modes give an unrelated container UID read/traverse access,
    # without depending on a privileged test process's os.access result.
    for path in root.rglob("*"):
        if path.is_file() and (path.name == "Caddyfile" or path.relative_to(root).parts[0]
                               in {"runtime", "minecraft", "wirescope"}):
            assert mode(path) & 0o004, f"container cannot read {path}: {mode(path):04o}"
            assert mode(path) == 0o644, f"noncanonical file mode {path}: {mode(path):04o}"
            parent = path.parent
            while parent != root:
                assert mode(parent) == 0o755, f"container cannot traverse {parent}: {mode(parent):04o}"
                parent = parent.parent


@pytest.mark.parametrize("mask", MASKS)
@pytest.mark.parametrize("profile", ["vps-server@12", "vps-server@18"])
def test_render_modes_are_independent_of_umask(tmp_path, mask, profile):
    fixture = build_vps_fixture(tmp_path, profile=profile, minecraft_heap="3G" if profile == "vps-server@18" else None)
    output = fixture.project / "generated"
    with process_umask(mask):
        render_toml_project(fixture.project, output, data_root=fixture.data_root)
    assert_container_inputs_readable(output)
    assert mode(output) == 0o700
    assert mode(output / "compose.yaml") == 0o600
    assert mode(output / "render-manifest.json") == 0o600


@pytest.mark.parametrize("mask", MASKS)
def test_compact_interface_modes_and_private_state(tmp_path, mask):
    order, data_root = interface_fixture(tmp_path)
    prepared = prepare_interface_deployment(order, data_root=data_root)
    with process_umask(mask):
        preview = write_interface_preview(prepared, tmp_path / "preview")
        rendered = _publish_render(prepared, tmp_path / "state")
    for root in [preview, rendered]:
        assert_container_inputs_readable(root)
        assert mode(root) == 0o700
        assert mode(root / "compose.yaml") == 0o600
        assert mode(root / "mc-remote.lock.json") == 0o600


@pytest.mark.parametrize("mask", MASKS)
def test_secret_store_stays_owner_only(tmp_path, monkeypatch, mask):
    monkeypatch.setenv("MC_REMOTE_SECRET_HOME", str(tmp_path / "secrets"))
    with process_umask(mask):
        secret = set_secret("fixture", "fixture_secret", "synthetic-test-value")
        set_secret("fixture", "fixture_secret", "replacement-test-value")
    assert mode(secret) == 0o600
    assert mode(secret.parent) == 0o700


@pytest.mark.parametrize("mask", MASKS)
def test_transaction_preserves_private_overlay_mode(tmp_path, mask):
    project = tmp_path / "project"
    output = project / "generated"
    output.mkdir(parents=True)
    (project / "mc-remote.toml").write_text("schema_version = 1\n")
    (project / "mc-remote.lock.toml").write_text("schema_version = 1\n")
    (output / "compose.yaml").write_text("services: {}\n")
    overlay = tmp_path / "private-overlay.yaml"
    overlay.write_text("services: {fixture: {environment: {TEST_SECRET: synthetic}}}\n")
    overlay.chmod(0o600)
    plan = _make_plan({
        "project_root": str(project.resolve()), "output": str(output.absolute()), "docker_context": "default",
        "source_lock_identity": "sha256:" + "1" * 64, "target_lock_identity": "sha256:" + "2" * 64,
        "source_profile": "vps-server@18", "target_profile": "vps-server@18",
        "source_preset": "fixture@1", "target_preset": "fixture@2",
        "deployment": "fixture", "environment": "fixture", "services": ["minecraft"],
        "volumes": [["minecraft-data", "fixture-data"]],
        "preserved_compose_sha256": [hashlib.sha256(overlay.read_bytes()).hexdigest()],
    }, (overlay,))
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    with process_umask(mask):
        _prepare_transaction(plan, temporary_root=staging, source_output=output)
    root = _plan_root(project, plan.plan_id)
    state = json.loads((root / "state.json").read_text())
    snapshot = root / "preserved-compose" / state["preserved_compose_files"][0]["snapshot"]
    assert snapshot.read_bytes() == overlay.read_bytes()
    assert mode(snapshot) == 0o600


@pytest.mark.parametrize("mask", MASKS)
def test_new_operator_input_keeps_private_mode(tmp_path, monkeypatch, mask):
    source = _public_order(tmp_path)
    destination = tmp_path / "destination"
    shutil.copytree(source, destination)
    for root in [source, destination]:
        (root / "mc-remote.lock.toml").write_text("schema_version = 1\n")
    order = source / "mc-remote.toml"
    order.write_text(order.read_text() + '''
[[operator_inputs]]
role = "minecraft-plugins"
adapter = "minecraft-plugins@1"
path = "operator/minecraft-plugins/plugins.toml"
''')
    private = source / "operator/minecraft-plugins/plugins.toml"
    private.parent.mkdir()
    private.write_text("# synthetic private operator input\n")
    private.chmod(0o600)
    monkeypatch.setattr("mc_remote_stack.deployment_update.render_toml_project", lambda *_a, **_k: None)
    with process_umask(mask):
        _publish_project(source, destination, destination / "generated", data_root=tmp_path)
    copied = destination / private.relative_to(source)
    assert copied.read_bytes() == private.read_bytes()
    assert mode(copied) == 0o600


def test_render_replaces_identical_bytes_with_unreadable_modes(tmp_path):
    fixture = build_vps_fixture(tmp_path)
    output = fixture.project / "generated"
    render_toml_project(fixture.project, output, data_root=fixture.data_root)
    before = {str(p.relative_to(output)): p.read_bytes() for p in output.rglob("*") if p.is_file()}
    mtimes = {str(p.relative_to(output)): p.stat().st_mtime_ns for p in output.rglob("*") if p.is_file()}
    (output / "Caddyfile").chmod(0o600)
    (output / "wirescope").chmod(0o700)
    result = render_toml_project(fixture.project, output, data_root=fixture.data_root)
    assert result.status == "replaced"
    assert_container_inputs_readable(output)
    assert before == {str(p.relative_to(output)): p.read_bytes() for p in output.rglob("*") if p.is_file()}
    assert mtimes == {str(p.relative_to(output)): p.stat().st_mtime_ns for p in output.rglob("*") if p.is_file()}


@pytest.mark.parametrize("mask", MASKS)
@pytest.mark.parametrize("rollback", [False, True], ids=["update", "automatic-rollback"])
def test_update_and_rollback_publish_readable_inputs(tmp_path, mask, rollback):
    fixture = build_vps_fixture(tmp_path, profile="vps-server@18", minecraft_heap="3G")
    project = fixture.project
    output = project / "generated"
    private_input = project / "operator/public-routes/routes.toml"
    private_input.chmod(0o600)
    render_toml_project(project, output, data_root=fixture.data_root)
    source = load_lock(project, data_root=fixture.data_root)
    staging = tmp_path / "transaction"
    staging.mkdir()
    candidate = staging / "candidate"
    _copy_project_source(project, candidate, output)
    server = candidate / "operator/minecraft-server/server.toml"
    document = tomlkit.parse(server.read_text())
    document["motd"] = "Updated fixture"
    server.write_text(tomlkit.dumps(document))
    resolve_project(candidate, data_root=fixture.data_root, resolved_at=source["resolved_at"])
    render_toml_project(candidate, candidate / "generated", data_root=fixture.data_root)
    target = load_lock(candidate, data_root=fixture.data_root)
    plan = _make_plan(_plan_payload(
        project_root=project, output=output, docker_context="default",
        source_lock=source, target_lock=target, preserved_sha256=(),
    ), ())
    _prepare_transaction(plan, temporary_root=staging, source_output=output)
    started = []

    class Host:
        def pull_target(self, *_args):
            pass

        def stop_source(self, *_args):
            pass

        def start_target(self, _plan, rendered):
            assert_container_inputs_readable(rendered)
            started.append("target")

        def verify_target(self, *_args):
            if rollback:
                raise DeploymentUpdateContractError("doctor_fixture_failure", "fixture", "injected failure")

        def start_source(self, _plan, rendered):
            assert_container_inputs_readable(rendered)
            started.append("source")

        def verify_source(self, *_args):
            pass

    with process_umask(mask):
        if rollback:
            with pytest.raises(DeploymentUpdateContractError, match="doctor_fixture_failure"):
                apply_deployment_update(project, plan_id=plan.plan_id, confirmed=True,
                                        data_root=fixture.data_root, host=Host())
        else:
            result = apply_deployment_update(project, plan_id=plan.plan_id, confirmed=True,
                                             data_root=fixture.data_root, host=Host())
            assert result.phase == "complete"
    state = json.loads((_plan_root(project, plan.plan_id) / "state.json").read_text())
    assert state["phase"] == ("rolled-back" if rollback else "complete")
    assert started == (["target", "source"] if rollback else ["target"])
    assert mode(private_input) == 0o600
    assert load_lock(project, data_root=fixture.data_root)["lock_identity"] == (
        source["lock_identity"] if rollback else target["lock_identity"])
