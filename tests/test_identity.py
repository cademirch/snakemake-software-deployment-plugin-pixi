from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from snakemake.deployment import SoftwareDeploymentManager
from snakemake_interface_software_deployment_plugins import EnvSpecSourceFile

from snakemake_software_deployment_plugin_pixi import EnvSpec

if TYPE_CHECKING:
    from snakemake.workflow import Workflow


@pytest.fixture
def manager(tmp_path):
    workflow = SimpleNamespace(
        deployment_settings=SimpleNamespace(
            deployment_methods={"pixi"},
            deployment_prefix=tmp_path / "deployments",
            cache_prefix=tmp_path / "cache",
            pinfile_prefix=tmp_path / "pins",
        ),
        software_deployment_provider_settings={},
        source_cache_path=tmp_path / "sources",
        envvars=set(),
    )
    # Supply just the workflow settings consumed by the real manager.
    return SoftwareDeploymentManager(cast("Workflow", workflow))


def make_spec(workspace, **options):
    spec = EnvSpec(workspace=EnvSpecSourceFile(workspace, cached=workspace), **options)
    spec.technical_init()
    return spec


@pytest.mark.parametrize("has_lock", [False, True])
def test_manager_keeps_distinct_workspaces(manager, tmp_path, has_lock):
    workspaces = [tmp_path / "first", tmp_path / "second"]
    for workspace in workspaces:
        workspace.mkdir()
        (workspace / "pixi.toml").write_text("[workspace]\n")
        if has_lock:
            (workspace / "pixi.lock").write_text("identical lock content")

    first, second = [manager.get_env(make_spec(path)) for path in workspaces]

    assert first is not second
    assert first.workspace_path == workspaces[0]
    assert second.workspace_path == workspaces[1]
    assert manager.get_env(make_spec(workspaces[0])) is first


@pytest.mark.parametrize(
    ("first_options", "second_options"),
    [
        ({}, {"frozen": True}),
        ({"frozen": True}, {}),
        ({}, {"locked": True}),
        ({"locked": True}, {}),
        ({"frozen": True}, {"locked": True}),
        ({"locked": True}, {"frozen": True}),
    ],
)
def test_manager_preserves_lock_policy(
    manager, tmp_path, first_options, second_options
):
    (tmp_path / "pixi.toml").write_text("[workspace]\n")
    first = manager.get_env(make_spec(tmp_path, **first_options))
    second = manager.get_env(make_spec(tmp_path, **second_options))

    assert first is not second
    assert second.spec.frozen == second_options.get("frozen", False)
    assert second.spec.locked == second_options.get("locked", False)
    assert manager.get_env(make_spec(tmp_path, **second_options)) is second


def test_manifest_changes_invalidate_environment_hash(manager, tmp_path):
    manifest = tmp_path / "pixi.toml"
    manifest.write_text('[dependencies]\npython = "3.12.*"\n')
    (tmp_path / "pixi.lock").write_text("unchanged lock content")
    before = manager.get_env(make_spec(tmp_path)).hash()

    manifest.write_text('[dependencies]\npython = "3.13.*"\n')
    # A new workflow run creates a new manager and rechecks software identity.
    after = SoftwareDeploymentManager(manager.workflow).get_env(make_spec(tmp_path))

    assert after is not None
    assert after.hash() != before
