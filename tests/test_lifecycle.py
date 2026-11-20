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
