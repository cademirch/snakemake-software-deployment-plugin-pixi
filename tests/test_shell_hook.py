import asyncio
import os
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock
from pathlib import Path

import pytest
from rattler import LockFile
from rattler.platform import Platform

from snakemake_interface_common.exceptions import WorkflowError
from snakemake_interface_software_deployment_plugins import (
    EnvSpecSourceFile,
    ShellExecutable,
)

from snakemake_software_deployment_plugin_pixi import Env, EnvSpec


@pytest.fixture
def make_env(tmp_path):
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    (workspace / "pixi.toml").write_text("[workspace]\n")

    def factory(shell="bash", name="tools", **options):
        return Env(
            spec=EnvSpec(
                workspace=EnvSpecSourceFile(workspace, cached=workspace),
                env=name,
                **options,
            ),
            within=None,
            settings=None,
            shell_executable=ShellExecutable(shell, "-c"),
            mountpoints=[],
            envvars=set(),
            tempdir=tmp_path,
            cache_prefix=tmp_path / "cache",
            deployment_prefix=tmp_path / "deployments",
            pinfile_prefix=tmp_path / "pinfiles",
        )

    return factory


@pytest.mark.parametrize(
    ("shell", "expected"),
    [
        ("/bin/bash", "bash"),
        ("dash", "bash"),
        ("sh", "bash"),
        ("ksh", "bash"),
        ("brush", "bash"),
        ("/bin/zsh", "zsh"),
        ("xonsh", "xonsh"),
        ("fish", "fish"),
    ],
)
def test_hook_uses_configured_shell(make_env, monkeypatch, shell, expected):
    env = make_env(shell=shell)
    hook = "# Preserve shell-specific syntax and newlines\nactivation-script\n"
    run = Mock(return_value=subprocess.CompletedProcess([], 0, hook, ""))
    monkeypatch.setattr(env, "run_cmd", run)

    assert env.decorate_shellcmd("user-command") == hook + "\nuser-command"
    args = shlex.split(run.call_args.args[0])
    assert args[:2] == ["pixi", "shell-hook"]
    assert args[args.index("--shell") + 1] == expected
    assert args[args.index("--environment") + 1] == "tools"
    assert args[args.index("--manifest-path") + 1] == str(env.manifest_path)
    assert run.call_args.kwargs == {"capture_output": True, "text": True, "check": True}


def test_hook_cached_per_environment(make_env, monkeypatch):
    tools = make_env()
    other = make_env(name="other")
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, "tools-hook", ""),
            subprocess.CompletedProcess([], 0, "other-hook", ""),
        ]
    )
    monkeypatch.setattr(Env, "run_cmd", run)

    assert "tools-hook\n" in tools.decorate_shellcmd("first")
    assert tools.decorate_shellcmd("second").endswith("\nsecond")
    assert "other-hook\n" in other.decorate_shellcmd("third")
    assert run.call_count == 2


def test_environment_identity_separates_activation_dialects(make_env):
    bash = make_env(shell="bash")
    sh = make_env(shell="sh")
    zsh = make_env(shell="zsh")
    fish = make_env(shell="fish")
    xonsh = make_env(shell="xonsh")

    # Snakemake deduplicates Env instances using their hash and equality.
    assert bash == sh
    assert len({bash, sh, zsh, fish, xonsh}) == 4


def test_concurrent_commands_share_hook_generation(make_env, monkeypatch):
    env = make_env()
    started = Event()
    release = Event()
    second_started = Event()

    def generate(*args, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return subprocess.CompletedProcess([], 0, "shared-hook", "")

    def decorate_second():
        second_started.set()
        return env.decorate_shellcmd("second")

    run = Mock(side_effect=generate)
    monkeypatch.setattr(env, "run_cmd", run)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(env.decorate_shellcmd, "first")
        try:
            assert started.wait(timeout=5)
            second = executor.submit(decorate_second)
            assert second_started.wait(timeout=5)
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
        finally:
            release.set()
        assert first.result(timeout=5).endswith("\nfirst")
        assert second.result(timeout=5).endswith("\nsecond")
    assert run.call_count == 1


def test_failed_hook_reports_context_and_is_not_cached(make_env, monkeypatch):
    env = make_env()
    run = Mock(
        side_effect=[
            subprocess.CalledProcessError(42, "pixi", stderr="environment not found"),
            subprocess.CompletedProcess([], 0, "successful-hook", ""),
        ]
    )
    monkeypatch.setattr(env, "run_cmd", run)

    with pytest.raises(WorkflowError) as error:
        env.decorate_shellcmd("must-not-run")
    message = str(error.value)
    for detail in ("tools", str(env.manifest_path), "42", "environment not found"):
        assert detail in message
    assert "successful-hook\n" in env.decorate_shellcmd("retry")


def test_hook_launch_error_is_workflow_error(make_env, monkeypatch):
    env = make_env()
    monkeypatch.setattr(env, "run_cmd", Mock(side_effect=FileNotFoundError("no shell")))
    with pytest.raises(WorkflowError, match="no shell"):
        env.decorate_shellcmd("must-not-run")


def test_unsupported_shell_fails_before_hook_generation(make_env, monkeypatch):
    env = make_env(shell="unsupported")
    run = Mock()
    monkeypatch.setattr(env, "run_cmd", run)
    with pytest.raises(WorkflowError, match="Unsupported shell.*unsupported"):
        env.decorate_shellcmd("must-not-run")
    run.assert_not_called()


@pytest.mark.parametrize("option", ["frozen", "locked"])
def test_hook_honors_lockfile_options(make_env, monkeypatch, option):
    env = make_env(**{option: True})
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "hook", ""))
    monkeypatch.setattr(env, "run_cmd", run)
    env.decorate_shellcmd("command")
    assert f"--{option}" in shlex.split(run.call_args.args[0])


def test_multiline_hook_activates_entire_command(make_env, monkeypatch):
    env = make_env()
    # A here-document needs its newlines. Both commands must see the activation.
    hook = "export PIXI_TEST_VALUE=$(cat <<'EOF'\nactivated\nEOF\n)\n"
    monkeypatch.setattr(
        env,
        "run_cmd",
        Mock(return_value=subprocess.CompletedProcess([], 0, hook, "")),
    )
    cmd = 'set -eu; printf "%s\\n" "$PIXI_TEST_VALUE" && printf "%s\\n" "$PIXI_TEST_VALUE"'
    result = env.shell_executable.run(
        env.decorate_shellcmd(cmd), capture_output=True, text=True, check=True
    )
    assert result.stdout == "activated\nactivated\n"


@pytest.mark.parametrize("shell", ["bash", "sh", "dash", "ksh", "zsh", "xonsh", "fish"])
def test_real_hook_activates_job(make_env, monkeypatch, tmp_path, shell):
    executable = shutil.which(shell) or str(Path(sys.executable).parent / shell)
    if not Path(executable).is_file():
        pytest.skip(f"{shell} not installed")
    if shutil.which("pixi") is None:
        pytest.skip("pixi not installed")
    for kind in ("CACHE", "CONFIG", "DATA"):
        monkeypatch.setenv(f"XDG_{kind}_HOME", str(tmp_path / kind.lower()))
    env = make_env(shell=executable)
    script = env.workspace_path / (
        "activate.fish" if shell == "fish" else "activate.sh"
    )
    script.write_text(
        "set -x ACTIVATED yes\n" if shell == "fish" else "export ACTIVATED=yes\n"
    )
    env.manifest_path.write_text(
        '[workspace]\nname="activation-test"\nchannels=[]\n'
        f'platforms=["{Platform.current()}"]\n[environments]\ntools=[]\n'
        f'[activation]\nscripts=["{script.name}"]\n'
    )
    monkeypatch.setenv("PIXI_OFFLINE", "true")
    command = (
        'print("job:" + $ACTIVATED)'
        if shell == "xonsh"
        else 'printf "job:%s\\n" "$ACTIVATED"'
    )
    result = env.shell_executable.run(
        env.decorate_shellcmd(command),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "job:yes\n"


def test_parent_activation_output_is_not_child_shell_code(
    make_env, monkeypatch, tmp_path
):
    parent = make_env()
    child = make_env(name="child")
    child.within = parent
    monkeypatch.setitem(
        parent.__dict__,
        "_shell_hook",
        "printf '%s\\n' 'Activating tools (ready)'\nexport PARENT_VALUE=ready\n",
    )
    # Keep real run_cmd and parent activation; replace only the external Pixi CLI.
    commands = tmp_path / "commands"
    commands.mkdir()
    pixi = commands / "pixi"
    pixi.write_text("#!/bin/sh\nprintf '%s\\n' 'export CHILD_VALUE=ready'\n")
    pixi.chmod(0o755)
    monkeypatch.setenv("PATH", str(commands) + ":" + os.environ["PATH"])

    result = child.shell_executable.run(
        child.managed_decorate_shellcmd(
            'printf "job:%s:%s\\n" "$PARENT_VALUE" "$CHILD_VALUE"'
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("job:ready:ready\n")
    assert "Activating tools (ready)" not in child.shell_hook


@pytest.mark.parametrize("retry", [False, True])
def test_preparation_refreshes_lock_metadata_without_changing_identity(
    make_env, monkeypatch, retry
):
    env = make_env()
    platform = Platform.current()
    source = LockFile.from_path(Path(__file__).parent / "test_workspace" / "pixi.lock")
    source_env = source.environment("tools")
    assert source_env is not None
    target = next(p for p in source_env.platforms() if str(p) == str(platform))
    records = source_env.conda_repodata_records_for_platform(target) or []

    def write_lock(version):
        lock = LockFile([target])
        for record in records:
            lock.add_conda_package("tools", target, record)
        lock.add_pypi_package(
            "tools",
            target,
            "marker",
            version,
            f"https://example.test/marker-{version}-py3-none-any.whl",
        )
        lock.to_path(env.lockfile_path)

    write_lock("1.0")
    identity = hash(env)
    assert ("marker", "1.0") in {(p.name, p.version) for p in env.report_software()}
    assert "marker-1.0-py3-none-any.whl" in asyncio.run(env.get_cache_assets())

    def prepare(*args, **kwargs):
        write_lock("2.0")
        return subprocess.CompletedProcess([], 0, "export READY=yes\n", "")

    if retry:

        def fail_after_resolving(*args, **kwargs):
            write_lock("2.0")
            raise subprocess.CalledProcessError(1, "pixi", stderr="install failed")

        monkeypatch.setattr(env, "run_cmd", fail_after_resolving)
        with pytest.raises(WorkflowError, match="install failed"):
            _ = env.shell_hook

    monkeypatch.setattr(env, "run_cmd", prepare)
    env.shell_hook
    reports = {(p.name, p.version) for p in env.report_software()}
    assert ("marker", "2.0") in reports
    assert ("marker", "1.0") not in reports
    assets = set(asyncio.run(env.get_cache_assets()))
    assert "marker-2.0-py3-none-any.whl" in assets
    assert "marker-1.0-py3-none-any.whl" not in assets
    assert hash(env) == identity
