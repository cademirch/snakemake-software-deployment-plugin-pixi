import shlex
import subprocess
import sys
import tempfile
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
    """Extract conda records for the current platform, including noarch packages."""
    env = _get_lockfile_env(lockfile, env_name)
    platform = _get_current_lockfile_platform(lockfile, env, env_name)
    return env.conda_repodata_records_for_platform(platform) or []


def _get_pypi_packages_for_env(
    lockfile: LockFile,
    env_name: str,
) -> List[PypiLockedPackage]:
    """Extract PyPI locked packages for the current platform."""
    env = _get_lockfile_env(lockfile, env_name)
    platform = _get_current_lockfile_platform(lockfile, env, env_name)
    return env.pypi_packages_for_platform(platform) or []


def _get_current_lockfile_platform(lockfile: LockFile, env, env_name: str):
    # Pixi represents dependency-free environments with an empty packages map.
    platforms = env.platforms() or lockfile.platforms()
    current = str(Platform.current())
    for platform in platforms:
        if str(platform) == current:
            return platform
    raise WorkflowError(
        f"Pixi lockfile does not support the current platform "
        f"({current}) in environment '{env_name}'. "
        f"Available platforms: {', '.join(str(p) for p in platforms)}. "
        "Add your platform to the [workspace] platforms list in the manifest "
        "and run 'pixi lock' to regenerate the lockfile."
    )


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
        yield "frozen"
        yield "locked"

    @classmethod
    def source_path_attributes(cls) -> Iterable[str]:
        yield "workspace"

    def __str__(self) -> str:
        ws = self.workspace.path_or_uri if self.workspace is not None else "."
        return f"pixi:{ws}:{self.env}"


class Env(PinnableEnvBase, CacheableEnvBase, EnvBase):
    spec: EnvSpec

    def __post_init__(self):
        self._workspace_base = Path.cwd()
        # Python 3.11 cached_property already holds a shared reentrant lock.
        # Adding an outer instance lock there can deadlock nested environments.
        self._shell_hook_lock = Lock() if sys.version_info >= (3, 12) else nullcontext()

    @cached_property
    def workspace_path(self) -> Path:
        """Resolve the original local directory relative to the invocation directory."""
        if self.spec.workspace is not None:
            # Snakemake resolves path_or_uri relative to the defining rule, but its
            # file source cache cannot copy a workspace directory or its contents.
            workspace = self.spec.workspace.path_or_uri
            if "://" in str(workspace):
                raise WorkflowError(
                    "Pixi workspace must be a local directory path; "
                    "workspace URIs are not supported."
                )
            return (self._workspace_base / workspace).resolve()
        return self._workspace_base

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
            command = shlex.join(parts)
            if self.within is None:
                result = self.run_cmd(
                    command, capture_output=True, text=True, check=True
                )
                hook = result.stdout
            else:
                # This directory is mounted in parent environments. Redirect only
                # Pixi's output so parent activation messages never become code.
                self._deployment_prefix.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=self._deployment_prefix) as tmp:
                    output = Path(tmp) / "shell-hook"
                    self.run_cmd(
                        f"{command} > {shlex.quote(str(output))}",
                        text=True,
                        stderr=subprocess.PIPE,
                        check=True,
                    )
                    hook = output.read_text()
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
        # An earlier failed install or another environment can also have changed
        # the lockfile. Refresh metadata after preparation without changing the
        # cached hash that Snakemake already uses as a dictionary key.
        for attribute in (
            "lockfile",
            "conda_records",
            "pypi_packages",
            "_cache_assets",
        ):
            self.__dict__.pop(attribute, None)
        return hook

    def decorate_shellcmd(self, cmd: str) -> str:
        """Activate the environment in the configured shell before the command."""
        return f"{self.shell_hook}\n{cmd}"

    def contains_executable(self, executable: str) -> bool:
        # Snakemake selects a script's interpreter before decorating its command.
        # Prepare the environment before checking which executables it provides.
        _ = self.shell_hook
        return (self.env_prefix() / "bin" / executable).exists()

    def hash_include_within(self) -> bool:
        return True

    def record_hash(self, hash_object) -> None:
        """Keep workspace activation and lockfile policies distinct during reuse."""
        manifest = self.manifest_path
        lockfile_path = self.lockfile_path
        has_lockfile = lockfile_path.exists()
        fields = (
            str(self.workspace_path.resolve()).encode(),
            manifest.name.encode(),
            manifest.read_bytes(),
            str(has_lockfile).encode(),
            lockfile_path.read_bytes() if has_lockfile else b"",
            self.spec.env.encode(),
            self.pixi_shell.encode(),
            str(self.spec.frozen).encode(),
            str(self.spec.locked).encode(),
        )
        for value in fields:
            # Length framing prevents adjacent fields from sharing an encoding.
            hash_object.update(len(value).to_bytes(8, "big"))
            hash_object.update(value)

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
        # Cache discovery also runs during dry runs, before lazy Pixi preparation.
        # A missing unlocked lockfile must not prevent the first real install.
        if not self.lockfile_path.exists() and not (
            self.spec.frozen or self.spec.locked
        ):
            return ()
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
                        name=record.name.normalized,
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
