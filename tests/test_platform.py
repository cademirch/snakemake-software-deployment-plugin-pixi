import asyncio

import pytest
from rattler.platform import Platform

from snakemake_interface_common.exceptions import WorkflowError
from snakemake_interface_software_deployment_plugins import (
    EnvSpecSourceFile,
    ShellExecutable,
)

from snakemake_software_deployment_plugin_pixi import Env, EnvSpec


@pytest.fixture
def mixed_platform_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Platform, "current", staticmethod(lambda: Platform("linux-64")))
    (tmp_path / "pixi.toml").write_text("[workspace]\n")
    (tmp_path / "pixi.lock").write_text(
        """version: 7
platforms:
- name: linux-64
- name: osx-arm64
environments:
  default:
    channels: []
    packages:
      linux-64:
      - conda: https://example.test/linux-64/tool-1.0-0.conda
      - conda: https://example.test/noarch/common-1.0-0.conda
      - pypi: https://example.test/wheel-1.0-py3-none-any.whl
      osx-arm64:
      - conda: https://example.test/osx-arm64/tool-2.0-0.conda
      - conda: https://example.test/noarch/common-1.0-0.conda
      - pypi: https://example.test/wheel-2.0-py3-none-any.whl
packages:
- conda: https://example.test/linux-64/tool-1.0-0.conda
- conda: https://example.test/noarch/common-1.0-0.conda
- conda: https://example.test/osx-arm64/tool-2.0-0.conda
- pypi: https://example.test/wheel-1.0-py3-none-any.whl
  name: wheel
  version: '1.0'
- pypi: https://example.test/wheel-2.0-py3-none-any.whl
  name: wheel
  version: '2.0'
"""
    )
    return Env(
        spec=EnvSpec(workspace=EnvSpecSourceFile(tmp_path, cached=tmp_path)),
        within=None,
        settings=None,
        shell_executable=ShellExecutable("bash", "-c"),
        mountpoints=[],
        envvars=set(),
        tempdir=tmp_path,
        cache_prefix=tmp_path,
        deployment_prefix=tmp_path,
        pinfile_prefix=tmp_path,
    )


def test_pin_and_report_use_current_platform(mixed_platform_env, capsys):
    env = mixed_platform_env
    asyncio.run(env.pin())
    conda_section, pypi_section = env.pinfile.read_text().split("@PYPI\n")
    assert conda_section.splitlines()[0] == "@EXPLICIT"
    assert sorted(conda_section.splitlines()[1:]) == [
        "https://example.test/linux-64/tool-1.0-0.conda",
        "https://example.test/noarch/common-1.0-0.conda",
    ]
    assert pypi_section.splitlines() == [
        "https://example.test/wheel-1.0-py3-none-any.whl",
    ]
    assert set(asyncio.run(env.get_cache_assets())) == {
        "tool-1.0-0.conda",
        "common-1.0-0.conda",
        "wheel-1.0-py3-none-any.whl",
    }
    assert {(item.name, item.version) for item in env.report_software()} == {
        ("tool", "1.0"),
        ("common", "1.0"),
        ("wheel", "1.0"),
    }
    assert capsys.readouterr().out == ""


def test_unsupported_platform_fails_instead_of_using_foreign_packages(
    mixed_platform_env, monkeypatch
):
    monkeypatch.setattr(Platform, "current", staticmethod(lambda: Platform("win-64")))
    with pytest.raises(WorkflowError, match="win-64"):
        asyncio.run(mixed_platform_env.pin())


def test_empty_supported_environment_has_no_packages(mixed_platform_env):
    env = mixed_platform_env
    env.lockfile_path.write_text(
        """version: 7
platforms:
- name: linux-64
environments:
  default:
    channels: []
    packages: {}
packages: []
"""
    )
    asyncio.run(env.pin())
    assert env.pinfile.read_text() == "@EXPLICIT\n"
    assert list(env.report_software()) == []
