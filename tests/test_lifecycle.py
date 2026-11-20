from pathlib import Path
from typing import Any, cast

import pytest

from snakemake_interface_common.exceptions import WorkflowError
from snakemake_interface_software_deployment_plugins import (
    EnvSpecSourceFile,
    ShellExecutable,
)

from snakemake_software_deployment_plugin_pixi import Env, EnvSpec


def make_env(tmp_path, manifest, **options):
    return Env(
        spec=EnvSpec(manifest=manifest, **options),
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


def test_manifest_is_required_and_workspace_alias_is_removed():
    constructor = cast(Any, EnvSpec)
    with pytest.raises(TypeError, match="manifest"):
        constructor()
    with pytest.raises(TypeError, match="workspace"):
        constructor(workspace=EnvSpecSourceFile("."))


@pytest.mark.parametrize(
    "value",
    [None, "", " \t", EnvSpecSourceFile(""), EnvSpecSourceFile(cast(Any, None))],
)
def test_empty_manifest_is_rejected(value):
    with pytest.raises(WorkflowError, match="manifest"):
        cast(Any, EnvSpec)(manifest=value)


def test_manifest_checks_wait_until_source_paths_are_resolved(tmp_path):
    source = EnvSpecSourceFile("envs/{sample}/pixi.toml")
    spec = EnvSpec(manifest=source)
    assert spec.env == "default"
    assert not spec.frozen and not spec.locked
    assert list(spec.source_path_attributes()) == ["manifest"]


@pytest.mark.parametrize("filename", ["pixi.toml", "pyproject.toml"])
def test_exact_original_manifest_anchors_workspace_and_lockfile(
    tmp_path, monkeypatch, filename
):
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    for name in ("pixi.toml", "pyproject.toml"):
        (workspace / name).write_text("[workspace]\n")
    cached = tmp_path / "source-cache" / filename
    source = EnvSpecSourceFile(Path("workspace with spaces") / filename, cached=cached)
    monkeypatch.chdir(tmp_path)
    env = make_env(tmp_path, source)

    monkeypatch.chdir(workspace)

    assert env.manifest_path == workspace / filename
    assert env.workspace_path == workspace
    assert env.lockfile_path == workspace / "pixi.lock"
    assert env.env_prefix() == workspace / ".pixi" / "envs" / "default"


@pytest.mark.parametrize("value", [".", "environment.toml", "missing/pixi.toml"])
def test_invalid_explicit_manifest_never_falls_back(tmp_path, value):
    (tmp_path / "pixi.toml").write_text("[workspace]\n")
    (tmp_path / "pyproject.toml").write_text("[tool.pixi.workspace]\n")
    (tmp_path / "environment.toml").write_text("[workspace]\n")
    env = make_env(tmp_path, EnvSpecSourceFile(tmp_path / value))
    with pytest.raises(WorkflowError, match="manifest"):
        _ = env.manifest_path


def test_missing_requested_filename_does_not_use_sibling(tmp_path):
    (tmp_path / "pixi.toml").write_text("[workspace]\n")
    env = make_env(tmp_path, EnvSpecSourceFile(tmp_path / "pyproject.toml"))
    with pytest.raises(WorkflowError, match="pyproject.toml"):
        _ = env.manifest_path


def test_manifest_rejects_remote_sources_even_if_cached_locally(tmp_path):
    source = EnvSpecSourceFile(
        "https://example.test/pixi.toml", cached=tmp_path / "pixi.toml"
    )
    env = make_env(tmp_path, source)
    with pytest.raises(WorkflowError, match="local.*file"):
        _ = env.manifest_path


def test_manifest_preserves_mutually_exclusive_lock_policy():
    with pytest.raises(WorkflowError, match="both frozen=True and locked=True"):
        EnvSpec(manifest=EnvSpecSourceFile("pixi.toml"), frozen=True, locked=True)
