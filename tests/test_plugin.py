import asyncio
import os
from pathlib import Path
from typing import Optional, Type

import pytest
from rattler import LockFile

from snakemake_interface_software_deployment_plugins.tests import (
    TestSoftwareDeploymentBase,
)
from snakemake_interface_software_deployment_plugins import (
    EnvSpecBase,
    EnvBase,
    EnvSpecSourceFile,
)
from snakemake_interface_software_deployment_plugins.settings import (
    SoftwareDeploymentSettingsBase,
)

from snakemake_software_deployment_plugin_pixi import Env, EnvSpec


TESTS_DIR = Path(__file__).parent


class _PixiTestBase(TestSoftwareDeploymentBase):
    """Shared defaults for all pixi tests."""

    __test__ = False

    def get_env_cls(self) -> Type[EnvBase]:
        return Env

    def get_settings_cls(self) -> Optional[Type[SoftwareDeploymentSettingsBase]]:
        return None

    def get_settings(self) -> Optional[SoftwareDeploymentSettingsBase]:
        return None


class TestPixi(_PixiTestBase):
    """Basic: pixi(workspace=..., env=...)"""

    __test__ = True

    def get_env_spec(self) -> EnvSpecBase:
        return EnvSpec(
            workspace=EnvSpecSourceFile(TESTS_DIR / "test_workspace"),
            env="tools",
        )

    def get_contained_executable(self) -> str:
        return "curl"

    def get_test_cmd(self) -> str:
        return "curl --version"


class TestPixiFrozen(_PixiTestBase):
    """pixi(frozen=True) — lockfile must exist, no updates allowed."""

    __test__ = True

    def get_env_spec(self) -> EnvSpecBase:
        return EnvSpec(
            workspace=EnvSpecSourceFile(TESTS_DIR / "test_workspace"),
            env="tools",
            frozen=True,
        )

    def get_contained_executable(self) -> str:
        return "curl"

    def get_test_cmd(self) -> str:
        return "curl --version"


class TestPixiLocked(_PixiTestBase):
    """pixi(locked=True) — lockfile must match the manifest."""

    __test__ = True

    def get_env_spec(self) -> EnvSpecBase:
        return EnvSpec(
            workspace=EnvSpecSourceFile(TESTS_DIR / "test_workspace"),
            env="tools",
            locked=True,
        )

    def get_contained_executable(self) -> str:
        return "curl"

    def get_test_cmd(self) -> str:
        return "curl --version"


class TestPixiNotFrozen(_PixiTestBase):
    """pixi(frozen=False) — pixi may re-solve if manifest changed."""

    __test__ = True

    def get_env_spec(self) -> EnvSpecBase:
        return EnvSpec(
            workspace=EnvSpecSourceFile(TESTS_DIR / "test_workspace"),
            env="tools",
            frozen=False,
        )

    def get_contained_executable(self) -> str:
        return "curl"

    def get_test_cmd(self) -> str:
        return "curl --version"


class TestPixiPypi(_PixiTestBase):
    """Workspace with PyPI dependencies."""

    __test__ = True

    def get_env_spec(self) -> EnvSpecBase:
        return EnvSpec(
            workspace=EnvSpecSourceFile(TESTS_DIR / "test_workspace_pypi"),
            env="pypitest",
        )

    def get_contained_executable(self) -> str:
        return "python"

    def get_test_cmd(self) -> str:
        return (
            "python -c 'import humanfriendly; print(humanfriendly.format_size(1024))'"
        )


class TestPixiImplicitWorkspace(_PixiTestBase):
    """pixi(env="default") with no workspace — resolves to cwd."""

    __test__ = True

    def get_env_spec(self) -> EnvSpecBase:
        return EnvSpec()  # workspace=None → ".", env="default"

    def get_contained_executable(self) -> str:
        return "curl"

    def get_test_cmd(self) -> str:
        return "curl --version"

    def _get_env(self, tmp_path):
        old_cwd = os.getcwd()
        os.chdir(TESTS_DIR / "test_workspace")
        try:
            return super()._get_env(tmp_path)
        finally:
            os.chdir(old_cwd)


@pytest.fixture
def pypi_env_with_local_packages(tmp_path):
    env = TestPixiPypi()._get_env(tmp_path)
    assert isinstance(env, Env)
    source_lockfile = LockFile.from_path(
        TESTS_DIR / "test_workspace_pypi" / "pixi.lock"
    )
    platform = source_lockfile.platforms()[0]
    source = source_lockfile.environment("pypitest")
    assert source is not None
    lockfile = LockFile([platform])
    for record in source.conda_repodata_records_for_platform(platform) or []:
        lockfile.add_conda_package("pypitest", platform, record)
    for pkg in source.pypi_packages_for_platform(platform) or []:
        lockfile.add_pypi_package(
            "pypitest", platform, pkg.name, pkg.version, pkg.location
        )
    for name, location in (
        ("remote-wheel", "https://example.test/remote_wheel-1.0-py3-none-any.whl"),
        ("local-source", "./local-source"),
        ("local-wheel", "file:///tmp/local_wheel-1.0-py3-none-any.whl"),
        ("git-source", "git+https://example.test/project.git#abcdef"),
    ):
        lockfile.add_pypi_package("pypitest", platform, name, "1.0", location)
    lockfile.to_path(tmp_path / "pixi.lock")
    env.spec.workspace = EnvSpecSourceFile(tmp_path, cached=tmp_path)
    return env


def test_pypi_pin_includes_remote_packages_only(pypi_env_with_local_packages):
    env = pypi_env_with_local_packages
    asyncio.run(env.pin())
    pypi_urls = env.pinfile.read_text().split("@PYPI\n", 1)[1].splitlines()

    assert "https://example.test/remote_wheel-1.0-py3-none-any.whl" in pypi_urls
    assert any("humanfriendly" in url for url in pypi_urls)
    assert all(url.startswith(("https://", "http://")) for url in pypi_urls)


def test_pypi_cache_includes_remote_packages_only(pypi_env_with_local_packages):
    assets = set(asyncio.run(pypi_env_with_local_packages.get_cache_assets()))

    # The async API remains reusable; it must not cache a spent coroutine.
    assert set(asyncio.run(pypi_env_with_local_packages.get_cache_assets())) == assets

    assert "remote_wheel-1.0-py3-none-any.whl" in assets
    assert any("humanfriendly" in asset for asset in assets)
    assert "local-source" not in assets
    assert "local_wheel-1.0-py3-none-any.whl" not in assets
    assert "project.git#abcdef" not in assets
