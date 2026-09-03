import shlex
from dataclasses import dataclass
from pathlib import Path
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
    platforms: Optional[List[Platform]] = None,
) -> List[RepoDataRecord]:
    """Extract conda RepoDataRecords for a given environment and platform(s)."""
    env = _get_lockfile_env(lockfile, env_name)

    if platforms is None:
        platforms = [Platform.current()]

    records = []
    for platform in platforms:
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
    platforms: Optional[List[Platform]] = None,
) -> List[PypiLockedPackage]:
    """Extract PyPI locked packages for a given environment and platform(s)."""
    env = _get_lockfile_env(lockfile, env_name)

    if platforms is None:
        platforms = [Platform.current()]

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
                "Use frozen to prevent any lockfile updates, or "
                "locked to allow only lockfile updates (no manifest changes)."
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
        self._lockfile_cache: Optional[LockFile] = None
        self._conda_records_cache: Optional[List[RepoDataRecord]] = None
        self._pypi_packages_cache: Optional[List[PypiLockedPackage]] = None
        self._cache_assets: Optional[dict] = None

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

    @property
    def lockfile(self) -> LockFile:
        if self._lockfile_cache is None:
            self._lockfile_cache = _parse_lockfile(self.lockfile_path)
        return self._lockfile_cache

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

    @property
    def conda_records(self) -> List[RepoDataRecord]:
        if self._conda_records_cache is None:
            self._conda_records_cache = _get_conda_records_for_env(
                self.lockfile, self.spec.env, self._platforms()
            )
        return self._conda_records_cache

    @property
    def pypi_packages(self) -> List[PypiLockedPackage]:
        if self._pypi_packages_cache is None:
            self._pypi_packages_cache = _get_pypi_packages_for_env(
                self.lockfile, self.spec.env, self._platforms()
            )
        return self._pypi_packages_cache

    def env_prefix(self) -> Path:
        """The conda prefix where pixi installs the environment."""
        return self.workspace_path / ".pixi" / "envs" / self.spec.env

    def _pixi_run_prefix(self) -> str:
        """Build the `pixi run` command prefix with proper quoting."""
        parts = [
            "pixi",
            "run",
            "-e",
            shlex.quote(self.spec.env),
            "--manifest-path",
            shlex.quote(str(self.manifest_path)),
        ]
        if self.spec.frozen:
            parts.append("--frozen")
        elif self.spec.locked:
            parts.append("--locked")
        return " ".join(parts)

    def decorate_shellcmd(self, cmd: str) -> str:
        """Wrap the command with `pixi run` to execute inside the environment.

        Using `pixi run` rather than raw conda activation ensures that
        pixi's own activation hooks, post-link scripts, and environment
        variables are all applied correctly.
        """
        return f"{self._pixi_run_prefix()} -- {cmd}"

    def contains_executable(self, executable: str) -> bool:
        return (self.env_prefix() / "bin" / executable).exists()

    def hash_include_within(self) -> bool:
        return True

    def record_hash(self, hash_object) -> None:
        """Hash the lockfile content + environment name.

        The lockfile pins every package version, hash, and URL for each
        platform, so hashing its content gives us a complete picture of what
        the environment should contain.
        """
        lockfile_path = self.lockfile_path
        if lockfile_path.exists():
            hash_object.update(lockfile_path.read_bytes())
        hash_object.update(self.spec.env.encode())

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
                    if pkg.is_editable or not _is_remote_url(pkg.location):
                        continue
                    await f.write(f"{pkg.location}\n")

    def is_cacheable(self) -> bool:
        return True

    async def get_cache_assets(self) -> Iterable[str]:
        if self._cache_assets is None:
            self._cache_assets = {}
            # Conda packages
            for record in self.conda_records:
                name = _record_to_asset_name(record)
                self._cache_assets[name] = ("conda", record)
            # PyPI packages (only remote URLs, skip editable/local path deps)
            for pkg in self.pypi_packages:
                if pkg.is_editable or not _is_remote_url(pkg.location):
                    continue
                name = _pypi_to_asset_name(pkg)
                self._cache_assets[name] = ("pypi", pkg)
        return self._cache_assets.keys()

    async def cache_asset(self, asset: str, to_path: Path) -> None:
        assert self._cache_assets is not None, (
            "bug: get_cache_assets must be called before cache_asset"
        )
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
