"""
Test the plugin by actually running Snakemake on a minimal workflow that uses pixi as the sdm
provider.

This is a true end-to-end test: it shells out to the `snakemake` CLI, lets the
pixi plugin deploy the "tools" environment, runs the rule inside it, and then
asserts on the produced output. Crucially it checks that `curl` resolved from
*inside* the deployed pixi prefix -- otherwise the test could pass against a
system curl and prove nothing about this plugin.

To avoid Snakemake's source-cache mangling a cross-directory workspace path,
we copy tests/test_workspace into a tmp dir and drop the fixture Snakefile
beside it, so the workspace *is* the workflow root and pixi(env="tools")
resolves to cwd.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
SNAKEFILE = TESTS_DIR / "fixtures" / "Snakefile"
WORKSPACE = TESTS_DIR / "test_workspace"

PIXI_AVAILABLE = shutil.which("pixi") is not None
SNAKEMAKE_AVAILABLE = shutil.which("snakemake") is not None


@pytest.mark.skipif(not PIXI_AVAILABLE, reason="pixi not installed")
@pytest.mark.skipif(not SNAKEMAKE_AVAILABLE, reason="snakemake not on PATH")
@pytest.mark.parametrize("shell", ["bash", "sh", "zsh"])
def test_real_workflow(tmp_path, shell):
    """Run Snakemake end-to-end with pixi deploying the `tools` env."""
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} not installed")
    # Build a self-contained workflow dir: the workspace (with its committed
    # pixi.lock) plus the fixture Snakefile at its root.
    ws = tmp_path / "workflow"
    shutil.copytree(WORKSPACE, ws)
    shutil.copyfile(SNAKEFILE, ws / "Snakefile")

    result = subprocess.run(
        [
            "snakemake",
            "--directory",
            str(ws),
            "--software-deployment-method",
            "pixi",
            "--cores",
            "1",
            "--set-resources",
            f"curl_version:shell_exec={shell}",
        ],
        cwd=ws,
        capture_output=True,
        text=True,
        check=False,
    )

    # 1. Snakemake must succeed. Surface stderr so a deploy failure is
    #    debuggable rather than a bare exit code.
    assert result.returncode == 0, (
        f"snakemake failed (exit {result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )

    # 2. The rule's shell block ran and produced output.
    output = ws / "results" / "curl_version.txt"
    assert output.exists(), (
        f"expected output not produced\n--- stderr ---\n{result.stderr}"
    )

    text = output.read_text()

    # 3. The strong assertion: curl resolved from inside the deployed pixi
    #    prefix, proving the command ran after pixi activation and not against a
    #    system curl on PATH.
    expected_prefix = str(ws / ".pixi" / "envs" / "tools" / "bin")
    assert expected_prefix in text, (
        f"curl did not resolve from the pixi env (expected prefix "
        f"{expected_prefix!r})\n--- output ---\n{text}"
    )

    # Sanity: the version banner made it into the file too.
    assert "curl" in text.lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
