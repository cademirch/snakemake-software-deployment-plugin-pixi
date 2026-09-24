import asyncio
import shutil
from pathlib import Path

import pytest

from snakemake_interface_common.exceptions import WorkflowError
from snakemake_interface_software_deployment_plugins import (
    EnvSpecSourceFile,
    ShellExecutable,
)

from snakemake_software_deployment_plugin_pixi import Env, EnvSpec


def make_env(tmp_path, workspace=None, **options):
    return Env(
        spec=EnvSpec(workspace=workspace, env="tools", **options),
        within=None,
        settings=None,
        shell_executable=ShellExecutable("bash", "-c"),
        mountpoints=[],
        envvars=set(),
        tempdir=tmp_path,
        cache_prefix=tmp_path / "cache",
        deployment_prefix=tmp_path / "deployments",
        pinfile_prefix=tmp_path / "pinfiles",
    )


def test_implicit_workspace_remains_anchored_to_original_directory(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pixi.toml").write_text("[workspace]\n")
    monkeypatch.chdir(workspace)
    env = make_env(tmp_path)

    monkeypatch.chdir(tmp_path)

    assert env.manifest_path == workspace / "pixi.toml"


def test_workspace_rejects_remote_directory_sources(tmp_path):
    env = make_env(
        tmp_path,
        workspace=EnvSpecSourceFile("https://example.test/workspace", cached=tmp_path),
    )

    with pytest.raises(WorkflowError, match="local directory"):
        _ = env.workspace_path


def test_cache_discovery_defers_unlocked_workspace_until_lock_exists(tmp_path):
    workspace = EnvSpecSourceFile(tmp_path, cached=tmp_path)
    env = make_env(tmp_path, workspace=workspace)

    assert list(asyncio.run(env.get_cache_assets())) == []
    assert not (tmp_path / "pixi.lock").exists()
    assert not (tmp_path / ".pixi").exists()

    shutil.copyfile(
        Path(__file__).parent / "test_workspace" / "pixi.lock",
        tmp_path / "pixi.lock",
    )
    assert any(
        asset.startswith("curl-") for asset in asyncio.run(env.get_cache_assets())
    )


@pytest.mark.parametrize("option", ["frozen", "locked"])
def test_cache_discovery_rejects_missing_strict_lockfile(tmp_path, option):
    env = make_env(
        tmp_path,
        workspace=EnvSpecSourceFile(tmp_path, cached=tmp_path),
        **{option: True},
    )

    with pytest.raises(WorkflowError, match="Pixi lockfile not found"):
        asyncio.run(env.get_cache_assets())
