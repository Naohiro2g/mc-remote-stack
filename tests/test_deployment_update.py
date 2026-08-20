import json
import shutil
import subprocess
import tomllib
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest

from mc_remote_stack.cli import main
from mc_remote_stack.deployment_update import (
    DeploymentUpdateContractError,
    DeploymentUpdatePlan,
    DeploymentUpdateResult,
    _discover_runtime_compose_files,
    _make_plan,
    _plan_root,
    _prepare_candidate_order,
    _publish_project,
    _state_from_plan,
    _validate_in_place_transition,
    _validate_required_effective_mounts,
    _write_json,
    apply_deployment_update,
)
from mc_remote_stack.toml_project import init_toml_project, update_order_scalar


def _public_order(tmp_path: Path) -> Path:
    project = init_toml_project(
        tmp_path / "official-public-beta",
        deployment_name="official-public-beta",
        profile="vps-server@8",
        environment_identity="official-public-beta",
        channel="beta",
        exposure="public",
        purpose="integration",
        preset="public-web-paper@3",
        artifact_store=str(tmp_path / "artifacts"),
        runtime_volumes={
            "caddy-config": "official-public-beta-b4-caddy-config",
            "caddy-data": "official-public-beta-b4-caddy-data",
            "minecraft-data": "official-public-beta-b4-minecraft-data",
        },
        world_identity="official-public-beta-world",
        bind_address="0.0.0.0",
        java_port=25565,
        mcremote_port=25575,
        minecraft_eula=True,
    ).root
    with (project / "mc-remote.toml").open("a", encoding="utf-8") as stream:
        stream.write(
            '''
[[operator_inputs]]
role = "public-routes"
adapter = "public-routes@1"
path = "operator/public-routes/routes.toml"

[[operator_inputs]]
role = "minecraft-server"
adapter = "minecraft-server@1"
path = "operator/minecraft-server/server.toml"

[[operator_inputs]]
role = "connection-targets"
adapter = "connection-targets@1"
path = "operator/connection-targets/targets.toml"
'''
        )
    routes = project / "operator/public-routes/routes.toml"
    routes.parent.mkdir(parents=True)
    routes.write_text(
        '''homepage = "mc-remote.com"
homepage_aliases = ["www.mc-remote.com"]
scratch = "scratch-beta.mc-remote.com"
bridge = "bridge-beta.mc-remote.com"
minecraft = "sb-beta.mc-remote.com"
''',
        encoding="utf-8",
    )
    server = project / "operator/minecraft-server/server.toml"
    server.parent.mkdir(parents=True)
    server.write_text('motd = "McRemote Sandbox Server"\n', encoding="utf-8")
    targets = project / "operator/connection-targets/targets.toml"
    targets.parent.mkdir(parents=True)
    targets.write_text(
        '''default_sandbox = "sb-beta.mc-remote.com"
[[targets]]
id = "beta"
label = "公開ベータ"
sandbox = "sb-beta.mc-remote.com"
''',
        encoding="utf-8",
    )
    return project


def test_candidate_order_upgrades_required_adapter_and_typed_input_without_touching_source(
    tmp_path: Path,
) -> None:
    source = _public_order(tmp_path)
    before_order = (source / "mc-remote.toml").read_bytes()
    before_routes = (source / "operator/public-routes/routes.toml").read_bytes()
    candidate = tmp_path / "candidate"

    _prepare_candidate_order(
        source,
        source / "generated",
        candidate,
        target_profile="vps-server@9",
        target_preset="public-web-paper@4",
        input_overrides={
            ("public-routes", "wirescope"): "wirescope-beta.mc-remote.com"
        },
        data_root=files("mc_remote_stack").joinpath("data"),
    )

    order = tomllib.loads((candidate / "mc-remote.toml").read_text(encoding="utf-8"))
    routes = tomllib.loads(
        (candidate / "operator/public-routes/routes.toml").read_text(encoding="utf-8")
    )
    adapters = {item["role"]: item["adapter"] for item in order["operator_inputs"]}
    assert order["deployment"]["profile"] == "vps-server@9"
    assert order["environment"]["preset"] == "public-web-paper@4"
    assert adapters["public-routes"] == "public-routes@2"
    assert routes["wirescope"] == "wirescope-beta.mc-remote.com"
    assert (source / "mc-remote.toml").read_bytes() == before_order
    assert (source / "operator/public-routes/routes.toml").read_bytes() == before_routes


def test_candidate_order_adds_notice_without_advancing_artifact_preset(
    tmp_path: Path,
) -> None:
    source = _public_order(tmp_path)
    update_order_scalar(source, ("deployment", "profile"), "vps-server@10")
    update_order_scalar(source, ("environment", "preset"), "public-web-paper@4")
    order_path = source / "mc-remote.toml"
    order_path.write_text(
        order_path.read_text(encoding="utf-8")
        + '''
[[operator_inputs]]
role = "minecraft-plugins"
adapter = "minecraft-plugins@1"
path = "operator/minecraft-plugins/plugins.toml"

[[operator_inputs]]
role = "homepage-static"
adapter = "homepage-static@1"
path = "operator/homepage-static/homepage.toml"

[[operator_inputs]]
role = "minecraft-backup"
adapter = "minecraft-backup@1"
path = "operator/minecraft-backup/backup.toml"
''',
        encoding="utf-8",
    )
    for relative in (
        "operator/minecraft-plugins/plugins.toml",
        "operator/homepage-static/homepage.toml",
        "operator/minecraft-backup/backup.toml",
    ):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("placeholder = true\n", encoding="utf-8")
    candidate = tmp_path / "candidate"

    _prepare_candidate_order(
        source,
        source / "generated",
        candidate,
        target_profile="vps-server@11",
        target_preset="public-web-paper@4",
        input_overrides={
            ("connection-targets", "notice_heading"): "WireScope beta",
            ("connection-targets", "notice_body"): "Observe traffic.",
            ("connection-targets", "notice_href"): (
                "https://wirescope-beta.mc-remote.com/"
            ),
            ("connection-targets", "notice_label"): "Open WireScope",
        },
        data_root=files("mc_remote_stack").joinpath("data"),
    )

    order = tomllib.loads((candidate / "mc-remote.toml").read_text(encoding="utf-8"))
    targets = tomllib.loads(
        (candidate / "operator/connection-targets/targets.toml").read_text(
            encoding="utf-8"
        )
    )
    adapters = {item["role"]: item["adapter"] for item in order["operator_inputs"]}
    assert order["deployment"]["profile"] == "vps-server@11"
    assert order["environment"]["preset"] == "public-web-paper@4"
    assert adapters["connection-targets"] == "connection-targets@2"
    assert targets["notice_href"] == "https://wirescope-beta.mc-remote.com/"


def _transition_lock(*, target: bool = False) -> dict:
    return {
        "deployment": {"name": "official-public-beta"},
        "environment": {
            "identity": "official-public-beta",
            "channel": "beta",
            "exposure": "public",
            "purpose": "integration",
        },
        "input": {
            "profile": {"ref": "vps-server@9" if target else "vps-server@8"},
            "preset": {
                "ref": "public-web-paper@4" if target else "public-web-paper@3"
            },
        },
        "runtime": {
            "artifact_store": "/artifacts",
            "volumes": [
                {"role": "caddy-config", "identity": "caddy-config"},
                {"role": "caddy-data", "identity": "caddy-data"},
                {"role": "minecraft-data", "identity": "minecraft-data"},
            ],
        },
        "world": {"identity": "official-public-beta-world"},
        "network": {
            "bind_address": "0.0.0.0",
            "java_port": 25565,
            "mcremote_port": 25575,
        },
        "agreements": {"minecraft_eula": True},
        "secret_references": [],
        "render_plan": {
            "adapter": "compose",
            "adapter_revision": "11" if target else "10",
            "services": [
                {"id": name, "role": name}
                for name in ("caddy", "scratch", "bridge", "minecraft")
            ],
            "volume_roles": [
                {"id": "caddy-config", "kind": "runtime-data"},
                {"id": "caddy-data", "kind": "runtime-data"},
                {"id": "minecraft-data", "kind": "world"},
            ],
            "required_security_controls": (
                ["online-mode", "wirescope-cross-origin-handoff"]
                if target
                else ["online-mode"]
            ),
        },
    }


def test_in_place_transition_keeps_stateful_identity_and_allows_release_projection() -> None:
    _validate_in_place_transition(_transition_lock(), _transition_lock(target=True))


def test_in_place_transition_allows_profile_only_projection() -> None:
    target = _transition_lock(target=True)
    target["input"]["preset"] = _transition_lock()["input"]["preset"]

    _validate_in_place_transition(_transition_lock(), target)


def test_in_place_transition_rejects_volume_rotation() -> None:
    target = _transition_lock(target=True)
    target["runtime"]["volumes"][2]["identity"] = "rotated-world"

    with pytest.raises(DeploymentUpdateContractError) as exc_info:
        _validate_in_place_transition(_transition_lock(), target)

    assert exc_info.value.reason == "update_stateful_identity_changed"


def test_in_place_transition_rejects_a_release_downgrade() -> None:
    with pytest.raises(DeploymentUpdateContractError) as exc_info:
        _validate_in_place_transition(
            _transition_lock(target=True),
            _transition_lock(),
        )

    assert exc_info.value.reason == "update_not_forward"


class _InspectRunner:
    def __init__(self, record: dict) -> None:
        self.record = record
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        if "ps" in command:
            return subprocess.CompletedProcess(command, 0, "minecraft-container\n", "")
        if "inspect" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps([self.record]), "")
        raise AssertionError(command)


def test_runtime_compose_files_are_discovered_from_live_minecraft_provenance(
    tmp_path: Path,
) -> None:
    project = tmp_path / "deployment"
    output = project / "generated"
    output.mkdir(parents=True)
    canonical = output / "compose.yaml"
    canonical.write_text("services: {}\n", encoding="utf-8")
    plugins = project / "recovery/compose.plugins.yaml"
    homepage = project / "recovery/compose.homepage.yaml"
    plugins.parent.mkdir(parents=True)
    plugins.write_text("services:\n  minecraft: {}\n", encoding="utf-8")
    homepage.write_text("services:\n  caddy: {}\n", encoding="utf-8")
    lock_identity = "sha256:" + "1" * 64
    runner = _InspectRunner(
        {
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "official-public-beta",
                    "com.docker.compose.service": "minecraft",
                    "com.docker.compose.project.config_files": ",".join(
                        str(path.resolve()) for path in (canonical, plugins, homepage)
                    ),
                    "io.mc-remote.deployment": "official-public-beta",
                    "io.mc-remote.lock": lock_identity,
                }
            },
            "State": {"Running": True},
        }
    )

    discovered = _discover_runtime_compose_files(
        output,
        deployment="official-public-beta",
        lock_identity=lock_identity,
        docker_context="default",
        runner=runner,
    )

    assert discovered == (plugins.resolve(), homepage.resolve())


def test_wirescope_target_preflight_rejects_overlay_that_masks_generated_docroot(
    tmp_path: Path,
) -> None:
    lock = {
        "render_plan": {
            "required_security_controls": ["wirescope-cross-origin-handoff"]
        }
    }
    services = {
        "caddy": {
            "volumes": [
                {
                    "type": "bind",
                    "source": str(tmp_path / "legacy-homepage"),
                    "target": "/srv/wirescope",
                    "read_only": True,
                }
            ]
        }
    }

    with pytest.raises(DeploymentUpdateContractError) as exc_info:
        _validate_required_effective_mounts(services, lock, tmp_path / "generated")

    assert exc_info.value.reason == "update_target_control_masked"


def test_project_publish_removes_operator_inputs_absent_from_restored_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = init_toml_project(
        tmp_path / "source",
        deployment_name="fixture",
        profile="home-server@1",
        environment_identity="fixture",
        channel="beta",
        exposure="isolated",
        purpose="integration",
        preset="mcremote-paper@1",
        artifact_store=str(tmp_path / "artifacts"),
        runtime_volumes={"minecraft-data": "fixture-data"},
        world_identity="fixture-world",
        bind_address="127.0.0.1",
        java_port=25565,
        mcremote_port=25575,
        minecraft_eula=True,
    ).root
    destination = tmp_path / "destination"
    shutil.copytree(source, destination)
    for root in (source, destination):
        (root / "mc-remote.lock.toml").write_text(
            "schema_version = 1\n", encoding="utf-8"
        )
    with (destination / "mc-remote.toml").open("a", encoding="utf-8") as stream:
        stream.write(
            '''
[[operator_inputs]]
role = "minecraft-plugins"
adapter = "minecraft-plugins@1"
path = "operator/minecraft-plugins/plugins.toml"
'''
        )
    obsolete = destination / "operator/minecraft-plugins/plugins.toml"
    obsolete.parent.mkdir(parents=True)
    obsolete.write_text(
        '[[plugins]]\nfilename = "WorldEdit.jar"\nsha256 = "' + "a" * 64 + '"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "mc_remote_stack.deployment_update.render_toml_project",
        lambda *_args, **_kwargs: None,
    )

    _publish_project(
        source,
        destination,
        destination / "generated",
        data_root=files("mc_remote_stack").joinpath("data"),
    )

    assert not obsolete.exists()
    assert not obsolete.parent.exists()


class _RecordingUpdateHost:
    def __init__(self, *, fail_target: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_target = fail_target

    def pull_target(self, plan, target_output: Path) -> None:
        self.calls.append("pull-target")

    def stop_source(self, plan, source_output: Path) -> None:
        self.calls.append("stop-source")

    def start_target(self, plan, target_output: Path) -> None:
        self.calls.append("start-target")

    def verify_target(self, plan, target_output: Path) -> None:
        self.calls.append("verify-target")
        if self.fail_target:
            raise DeploymentUpdateContractError(
                "doctor_protocol_unavailable",
                "minecraft",
                "fixture",
            )

    def start_source(self, plan, source_output: Path) -> None:
        self.calls.append("start-source")

    def verify_source(self, plan, source_output: Path) -> None:
        self.calls.append("verify-source")


def _durable_update(tmp_path: Path) -> tuple[Path, DeploymentUpdatePlan, dict, dict]:
    project = tmp_path / "deployment"
    output = project / "generated"
    output.mkdir(parents=True)
    source_identity = "sha256:" + "1" * 64
    target_identity = "sha256:" + "2" * 64
    plan = _make_plan(
        {
            "project_root": str(project.resolve()),
            "output": str(output.absolute()),
            "docker_context": "default",
            "source_lock_identity": source_identity,
            "target_lock_identity": target_identity,
            "source_profile": "vps-server@8",
            "target_profile": "vps-server@9",
            "source_preset": "public-web-paper@3",
            "target_preset": "public-web-paper@4",
            "deployment": "official-public-beta",
            "environment": "official-public-beta",
            "services": ["caddy", "scratch", "bridge", "minecraft"],
            "volumes": [
                ["caddy-config", "caddy-config"],
                ["caddy-data", "caddy-data"],
                ["minecraft-data", "minecraft-data"],
            ],
            "preserved_compose_sha256": [],
        },
        (),
    )
    root = _plan_root(project, plan.plan_id)
    for path in (
        root / "source-project",
        root / "source-render",
        root / "candidate/generated",
    ):
        path.mkdir(parents=True)
    for path in (root / "source-project", root / "candidate"):
        (path / "mc-remote.toml").write_text("schema_version = 1\n", encoding="utf-8")
        (path / "mc-remote.lock.toml").write_text("schema_version = 1\n", encoding="utf-8")
    _write_json(root / "state.json", _state_from_plan(plan))
    source_lock = {"lock_identity": source_identity}
    target_lock = {"lock_identity": target_identity}
    return project, plan, source_lock, target_lock


def test_apply_uses_same_volumes_and_closes_exact_durable_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, plan, source_lock, target_lock = _durable_update(tmp_path)
    host = _RecordingUpdateHost()
    published: list[str] = []
    monkeypatch.setattr(
        "mc_remote_stack.deployment_update.load_lock",
        lambda path, **_kwargs: target_lock if path.name == "candidate" else source_lock,
    )
    monkeypatch.setattr(
        "mc_remote_stack.deployment_update.verify_toml_render_output",
        lambda *_args, **_kwargs: SimpleNamespace(lock=source_lock),
    )
    monkeypatch.setattr(
        "mc_remote_stack.deployment_update._publish_project",
        lambda source, *_args, **_kwargs: published.append(source.name),
    )

    result = apply_deployment_update(
        project,
        plan_id=plan.plan_id,
        confirmed=True,
        data_root=files("mc_remote_stack").joinpath("data"),
        host=host,
    )

    assert result.status == "complete"
    assert result.phase == "complete"
    assert host.calls == ["pull-target", "stop-source", "start-target", "verify-target"]
    assert published == ["candidate"]
    assert plan.volumes == (
        ("caddy-config", "caddy-config"),
        ("caddy-data", "caddy-data"),
        ("minecraft-data", "minecraft-data"),
    )


def test_failed_target_doctor_restores_source_projection_and_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, plan, source_lock, target_lock = _durable_update(tmp_path)
    host = _RecordingUpdateHost(fail_target=True)
    published: list[str] = []
    monkeypatch.setattr(
        "mc_remote_stack.deployment_update.load_lock",
        lambda path, **_kwargs: target_lock if path.name == "candidate" else source_lock,
    )
    monkeypatch.setattr(
        "mc_remote_stack.deployment_update.verify_toml_render_output",
        lambda *_args, **_kwargs: SimpleNamespace(lock=source_lock),
    )
    monkeypatch.setattr(
        "mc_remote_stack.deployment_update._publish_project",
        lambda source, *_args, **_kwargs: published.append(source.name),
    )

    with pytest.raises(DeploymentUpdateContractError) as exc_info:
        apply_deployment_update(
            project,
            plan_id=plan.plan_id,
            confirmed=True,
            data_root=files("mc_remote_stack").joinpath("data"),
            host=host,
        )

    assert exc_info.value.reason == "doctor_protocol_unavailable"
    assert published == ["candidate", "source-project"]
    assert host.calls == [
        "pull-target",
        "stop-source",
        "start-target",
        "verify-target",
        "start-source",
        "verify-source",
    ]
    state = json.loads(
        (_plan_root(project, plan.plan_id) / "state.json").read_text(encoding="utf-8")
    )
    assert state["phase"] == "rolled-back"


def test_cli_exposes_two_command_update_without_volume_or_compose_path_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "deployment"
    source = "sha256:" + "1" * 64
    target = "sha256:" + "2" * 64
    plan_id = "sha256:" + "3" * 64
    plan = DeploymentUpdatePlan(
        plan_id=plan_id,
        project_root=project.resolve(),
        output=(project / "generated").absolute(),
        docker_context="default",
        source_lock_identity=source,
        target_lock_identity=target,
        source_profile="vps-server@8",
        target_profile="vps-server@9",
        source_preset="public-web-paper@3",
        target_preset="public-web-paper@4",
        deployment="official-public-beta",
        environment="official-public-beta",
        services=("caddy", "scratch", "bridge", "minecraft"),
        volumes=(("minecraft-data", "same-world"),),
        preserved_compose_files=(project / "recovery/plugins.yaml",),
        preserved_compose_sha256=("4" * 64,),
    )
    received: dict[str, object] = {}

    def fake_plan(*_args, **kwargs):
        received.update(kwargs)
        return plan

    monkeypatch.setattr("mc_remote_stack.cli.plan_deployment_update", fake_plan)
    monkeypatch.setattr(
        "mc_remote_stack.cli.load_deployment_update_plan",
        lambda *_args, **_kwargs: plan,
    )
    monkeypatch.setattr(
        "mc_remote_stack.cli.check_operator_environment",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        "mc_remote_stack.cli.apply_deployment_update",
        lambda *_args, **_kwargs: DeploymentUpdateResult(
            "complete", plan_id, source, target, "complete"
        ),
    )

    assert (
        main(
            [
                "deployment",
                "update",
                "plan",
                "--project",
                str(project),
                "--to-profile",
                "vps-server@9",
                "--to-preset",
                "public-web-paper@4",
                "--set-input",
                "public-routes.wirescope=wirescope-beta.mc-remote.com",
            ]
        )
        == 0
    )
    assert (
        received["input_overrides"]
        == {("public-routes", "wirescope"): "wirescope-beta.mc-remote.com"}
    )
    assert (
        main(
            [
                "deployment",
                "update",
                "apply",
                "--project",
                str(project),
                "--plan-id",
                plan_id,
                "--yes",
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "live-compose=auto-discovered" in text
    assert "stateful-volumes=in-place" in text
    assert "--target-volume" not in text
    assert "--preserve-compose-file" not in text
    assert "OK deployment-update status=complete" in text
