import shlex
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from threading import Lock
from typing import Iterable, List, Optional

import aiofiles
import httpx

from snakemake_interface_common.exceptions import WorkflowError
from snakemake_interface_software_deployment_plugins.settings import CommonSettings
from snakemake_interface_software_deployment_plugins import (
    EnvBase,
    CacheableEnvBase,
    PinnableEnvBase,
    EnvSpecBase,
    SoftwareReport,
    EnvSpecSourceFile,
)

from rattler import LockFile, PypiLockedPackage
from rattler.platform import Platform
from rattler.repo_data import RepoDataRecord


common_settings = CommonSettings(
    # "pixi" is its own kind — it is not a drop-in for the conda directive.
    # Users write  software: pixi(...)  in their Snakefiles.
    provides="pixi",
)


PIXI_LOCK_FILENAME = "pixi.lock"


def _resolve_lockfile(workspace: Path) -> Path:
    """Return the path to pixi.lock inside the given workspace directory."""
    return workspace / PIXI_LOCK_FILENAME


def _parse_lockfile(lockfile_path: Path) -> LockFile:
    """Parse a pixi.lock file using rattler."""
    if not lockfile_path.exists():
        raise WorkflowError(
            f"Pixi lockfile not found at {lockfile_path}. "
            "Run 'pixi install' in the workspace first, or set frozen=False "
            "to allow Snakemake to generate the lockfile."
        )
    return LockFile.from_path(lockfile_path)


def _get_conda_records_for_env(
    lockfile: LockFile,
    env_name: str,
) -> List[RepoDataRecord]:
    """Extract conda RepoDataRecords for a given environment and platform(s)."""
    env = _get_lockfile_env(lockfile, env_name)
    platforms = env.platforms()

    records = []
    for platform in platforms:
        print(f"{platform=}")
        platform_records = env.conda_repodata_records_for_platform(platform)
        if platform_records is not None:
            records.extend(platform_records)

    if not records:
        available_platforms = env.platforms()
        raise WorkflowError(
            f"Pixi lockfile has no packages for the current platform "
            f"({Platform.current()}) in environment '{env_name}'. "
            f"Available platforms: {', '.join(str(p) for p in available_platforms)}. "
            f"Add your platform to the [workspace] platforms list in the manifest "
            f"and run 'pixi lock' to regenerate the lockfile."
        )

    return records


def _get_pypi_packages_for_env(
    lockfile: LockFile,
    env_name: str,
) -> List[PypiLockedPackage]:
    """Extract PyPI locked packages for a given environment and platform(s)."""
    env = _get_lockfile_env(lockfile, env_name)
    platforms = env.platforms()

    packages = []
    for platform in platforms:
        platform_packages = env.pypi_packages_for_platform(platform)
        if platform_packages is not None:
            packages.extend(platform_packages)
    return packages


def _get_lockfile_env(lockfile: LockFile, env_name: str):
    """Get a lockfile environment by name, raising a clear error if missing."""
    env = lockfile.environment(env_name)
    if env is None:
        available = [name for name, _ in lockfile.environments()]
        raise WorkflowError(
            f"Pixi environment '{env_name}' not found in lockfile. "
            f"Available environments: {', '.join(available)}"
        )
    return env


@dataclass(eq=False)
class EnvSpec(EnvSpecBase):
    """Specification for a pixi environment.

    Usage in Snakefiles::

        rule align:
            software:
                pixi(workspace="envs/biotools/", env="alignment")

        rule call:
            software:
                # workspace defaults to the workflow root
                pixi(env="calling")

        rule default:
            software:
                # env defaults to "default"
                pixi(workspace="envs/biotools/")

    To enable post-link scripts (e.g. for graphviz dot plugin registration),
    add to the workspace's .pixi/config.toml::

        run-post-link-scripts = "insecure"

    Or run: pixi config set --local run-post-link-scripts insecure
    """

    workspace: Optional[EnvSpecSourceFile] = None
    env: str = "default"
    frozen: bool = False
    locked: bool = False

    def __post_init__(self):
        if self.frozen and self.locked:
            raise WorkflowError(
                "Cannot set both frozen=True and locked=True. "
                "Use frozen to install from the existing lockfile, or "
                "locked to require a lockfile that matches the manifest."
            )

    @classmethod
    def identity_attributes(cls) -> Iterable[str]:
        yield "workspace"
        yield "env"

    @classmethod
    def source_path_attributes(cls) -> Iterable[str]:
        yield "workspace"

    def __str__(self) -> str:
        ws = self.workspace.path_or_uri if self.workspace is not None else "."
        return f"pixi:{ws}:{self.env}"


class Env(PinnableEnvBase, CacheableEnvBase, EnvBase):
    spec: EnvSpec

    def __post_init__(self):
        # Python 3.11 cached_property already holds a shared reentrant lock.
        # Adding an outer instance lock there can deadlock nested environments.
        self._shell_hook_lock = Lock() if sys.version_info >= (3, 12) else nullcontext()

    @property
    def workspace_path(self) -> Path:
        """Resolve the workspace directory."""
        if self.spec.workspace is not None:
            assert self.spec.workspace.cached is not None
            return self.spec.workspace.cached
        # If no workspace specified, use the workflow root (deployment prefix parent).
        # The framework should resolve this via source_path_attributes.
        return Path(".")

    @property
    def lockfile_path(self) -> Path:
        return _resolve_lockfile(self.workspace_path)

    @cached_property
    def lockfile(self) -> LockFile:
        return _parse_lockfile(self.lockfile_path)

    @property
    def manifest_path(self) -> Path:
        """Path to pixi.toml or pyproject.toml in the workspace."""
        ws = self.workspace_path
        for name in ("pixi.toml", "pyproject.toml"):
            p = ws / name
            if p.exists():
                return p
        raise WorkflowError(
            f"No pixi manifest (pixi.toml or pyproject.toml) found in {ws}"
        )

    def _platforms(self) -> List[Platform]:
        return [Platform.current(), Platform("noarch")]

    @cached_property
    def conda_records(self) -> List[RepoDataRecord]:
        return _get_conda_records_for_env(self.lockfile, self.spec.env)

    @cached_property
    def pypi_packages(self) -> List[PypiLockedPackage]:
        return _get_pypi_packages_for_env(self.lockfile, self.spec.env)

    def env_prefix(self) -> Path:
        """The conda prefix where pixi installs the environment."""
        return self.workspace_path / ".pixi" / "envs" / self.spec.env

    @property
    def pixi_shell(self) -> str:
        """Map the configured shell to a pixi activation script type."""
        name = self.shell_executable.name
        if name in ("bash", "dash", "sh", "ksh", "brush"):
            return "bash"
        if name in ("zsh", "xonsh", "fish"):
            return name
        raise WorkflowError(
            "Unsupported shell executable for "
            f"snakemake-software-deployment-plugin-pixi: {name}"
        )

    @property
    def shell_hook(self) -> str:
        """Serialize access so concurrent jobs generate the hook only once."""
        # cached_property stores the value before this outer lock is released.
        with self._shell_hook_lock:
            return self._shell_hook

    @cached_property
    def _shell_hook(self) -> str:
        """Generate and cache the activation script for this environment."""
        manifest = self.manifest_path.absolute()
        parts = [
            "pixi",
            "shell-hook",
            "--shell",
            self.pixi_shell,
            "--environment",
            self.spec.env,
            "--manifest-path",
            str(manifest),
        ]
        if self.spec.frozen:
            parts.append("--frozen")
        elif self.spec.locked:
            parts.append("--locked")
        try:
            result = self.run_cmd(
                shlex.join(parts), capture_output=True, text=True, check=True
            )
        except subprocess.CalledProcessError as e:
            raise WorkflowError(
                f"Failed to generate pixi shell hook for environment "
                f"'{self.spec.env}' from {manifest} (exit {e.returncode}):\n"
                f"{e.stderr or str(e)}"
            ) from e
        except OSError as e:
            raise WorkflowError(
                f"Could not run pixi shell-hook for environment "
                f"'{self.spec.env}' from {manifest}: {e}"
            ) from e
        return result.stdout

    def decorate_shellcmd(self, cmd: str) -> str:
        """Activate the environment in the configured shell before the command."""
        return f"{self.shell_hook}\n{cmd}"

    def contains_executable(self, executable: str) -> bool:
        return (self.env_prefix() / "bin" / executable).exists()

    def hash_include_within(self) -> bool:
        return True

    def record_hash(self, hash_object) -> None:
        """Hash the lockfile content, environment name, and activation shell.

        The lockfile pins every package version, hash, and URL for each
        platform, so hashing its content gives us a complete picture of what
        the environment should contain.
        """
        lockfile_path = self.lockfile_path
        if lockfile_path.exists():
            hash_object.update(lockfile_path.read_bytes())
        hash_object.update(self.spec.env.encode())
        # Snakemake deduplicates Env instances by this hash. Different shell
        # dialects must not share an instance and its cached activation script.
        hash_object.update(b"\0shell\0")
        hash_object.update(self.pixi_shell.encode())

    def is_pinnable(self) -> bool:
        return True

    @classmethod
    def pinfile_extension(cls) -> str:
        return f".{Platform.current()}.pixi.pin.txt"

    async def pin(self) -> None:
        """Generate an explicit pinfile from the pixi lockfile.

        This converts the pixi.lock entries for the current platform into
        the same @EXPLICIT format used by conda, enabling Snakemake's
        standard pinning workflow. PyPI packages are listed in a separate
        section with a @PYPI header.
        """
        async with aiofiles.open(self.pinfile, "w") as f:
            await f.write("@EXPLICIT\n")
            for record in self.conda_records:
                await f.write(f"{record.url}\n")

            pypi_pkgs = self.pypi_packages
            if pypi_pkgs:
                await f.write("@PYPI\n")
                for pkg in pypi_pkgs:
                    if not _is_remote_url(pkg.location):
                        continue
                    await f.write(f"{pkg.location}\n")

    def is_cacheable(self) -> bool:
        return True

    @cached_property
    def _cache_assets(self) -> dict:
        assets: dict = {}
        # Conda packages
        for record in self.conda_records:
            name = _record_to_asset_name(record)
            assets[name] = ("conda", record)
        # HTTP(S) artifacts only; local/editable and Git sources are excluded.
        for pkg in self.pypi_packages:
            if not _is_remote_url(pkg.location):
                continue
            name = _pypi_to_asset_name(pkg)
            assets[name] = ("pypi", pkg)
        return assets

    async def get_cache_assets(self) -> Iterable[str]:
        return self._cache_assets.keys()

    async def cache_asset(self, asset: str, to_path: Path) -> None:
        kind, record = self._cache_assets[asset]
        url = record.url if kind == "conda" else record.location
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            async with aiofiles.open(to_path, "wb") as f:
                async for chunk in response.aiter_bytes(chunk_size=1024):
                    await f.write(chunk)

    def report_software(self) -> Iterable[SoftwareReport]:
        """Report packages from the lockfile for this environment."""
        reports = []
        try:
            for record in self.conda_records:
                reports.append(
                    SoftwareReport(
                        name=str(record.name),
                        version=str(record.version),
                    )
                )
            for pkg in self.pypi_packages:
                reports.append(
                    SoftwareReport(
                        name=str(pkg.name),
                        version=str(pkg.version),
                    )
                )
        except Exception:
            pass
        return reports


def _record_to_asset_name(record: RepoDataRecord) -> str:
    return record.url.split("/")[-1]


def _pypi_to_asset_name(pkg: PypiLockedPackage) -> str:
    return pkg.location.split("/")[-1]


def _is_remote_url(location: str) -> bool:
    """Check if a location string is a remote HTTP(S) URL."""
    return location.startswith("http://") or location.startswith("https://")
