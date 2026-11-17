import shlex
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from threading import Lock
from typing import Iterable, List

import aiofiles

from snakemake_interface_common.exceptions import WorkflowError
from snakemake_interface_software_deployment_plugins.settings import CommonSettings
from snakemake_interface_software_deployment_plugins import (
    EnvBase,
    PinnableEnvBase,
    EnvSpecBase,
    SoftwareReport,
    EnvSpecSourceFile,
)

from rattler import LockFile, PypiLockedPackage
from rattler.platform import Platform
from rattler.repo_data import RepoDataRecord

from ._fingerprint import environment_fingerprint


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
    """Select an environment from an explicit local Pixi manifest.

    Usage in Snakefiles::

        rule align:
            software:
                pixi(manifest="envs/biotools/pixi.toml", env="alignment")

        rule default:
            software:
                pixi(manifest="pyproject.toml")

    Relative manifest paths belong to the file defining the rule, including
    included rules and modules. The environment name defaults to "default".

    To enable post-link scripts (for example, graphviz plugin registration),
    set run-post-link-scripts = "insecure" in the workspace's .pixi/config.toml.
    """

    manifest: EnvSpecSourceFile
    env: str = "default"
    frozen: bool = False
    locked: bool = False

    def __post_init__(self):
        if (
            not isinstance(self.manifest, EnvSpecSourceFile)
            or self.manifest.path_or_uri is None
            or not str(self.manifest.path_or_uri).strip()
        ):
            raise WorkflowError(
                "Pixi manifest is required. Provide the path to a local "
                "pixi.toml or pyproject.toml file."
            )
        if self.frozen and self.locked:
            raise WorkflowError(
                "Cannot set both frozen=True and locked=True. "
                "Use frozen to install from the existing lockfile, or "
                "locked to require a lockfile that matches the manifest."
            )

    @classmethod
    def identity_attributes(cls) -> Iterable[str]:
        yield "manifest"
        yield "env"
        yield "frozen"
        yield "locked"

    @classmethod
    def source_path_attributes(cls) -> Iterable[str]:
        yield "manifest"

    def __str__(self) -> str:
        return f"pixi:{self.manifest.path_or_uri}:{self.env}"


class Env(PinnableEnvBase, EnvBase):
    spec: EnvSpec

    def __post_init__(self):
        self._manifest_base = Path.cwd()
        # Python 3.11 cached_property already holds a shared reentrant lock.
        # Adding an outer instance lock there can deadlock nested environments.
        self._shell_hook_lock = Lock() if sys.version_info >= (3, 12) else nullcontext()

    @cached_property
    def manifest_path(self) -> Path:
        """Resolve the exact original source file after Snakemake rebases it."""
        manifest = self.spec.manifest.path_or_uri
        if "://" in str(manifest):
            raise WorkflowError(
                "Pixi manifest must be a local file path; manifest URIs are not supported."
            )
        # The source cache contains only the manifest, not its sibling lockfile
        # or local workspace files. Anchor Snakemake's rebased original path.
        path = (self._manifest_base / manifest).resolve()
        if path.name not in {"pixi.toml", "pyproject.toml"} or path.is_dir():
            raise WorkflowError(
                f"Pixi manifest must identify a pixi.toml or pyproject.toml file: {path}"
            )
        if not path.is_file():
            raise WorkflowError(f"Pixi manifest file not found: {path}")
        return path

    @property
    def workspace_path(self) -> Path:
        """The workspace is the selected manifest's parent directory."""
        return self.manifest_path.parent

    @property
    def lockfile_path(self) -> Path:
        return _resolve_lockfile(self.workspace_path)

    @cached_property
    def lockfile(self) -> LockFile:
        return _parse_lockfile(self.lockfile_path)

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
        fields = (
            str(self.workspace_path.resolve()).encode(),
            manifest.name.encode(),
            environment_fingerprint(
                manifest,
                self.lockfile_path,
                self.spec.env,
                str(Platform.current()),
                self.spec.frozen,
            ),
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


def _is_remote_url(location: str) -> bool:
    """Check if a location string is a remote HTTP(S) URL."""
    return location.startswith("http://") or location.startswith("https://")
