import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest

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

    assert tools.decorate_shellcmd("first") == "tools-hook\nfirst"
    assert tools.decorate_shellcmd("second") == "tools-hook\nsecond"
    assert other.decorate_shellcmd("third") == "other-hook\nthird"
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
        assert first.result(timeout=5) == "shared-hook\nfirst"
        assert second.result(timeout=5) == "shared-hook\nsecond"
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
    assert env.decorate_shellcmd("retry") == "successful-hook\nretry"


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
