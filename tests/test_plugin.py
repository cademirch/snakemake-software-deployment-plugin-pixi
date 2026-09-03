import os
from pathlib import Path
from typing import Optional, Type

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
    """pixi(locked=True) — lockfile updates ok, manifest changes blocked."""

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
