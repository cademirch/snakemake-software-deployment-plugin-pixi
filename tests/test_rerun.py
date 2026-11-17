"""Verify Pixi software changes through Snakemake's persisted rerun metadata."""

import os
import shutil
import subprocess
import sys
from importlib.util import find_spec

import pytest
from rattler.platform import Platform


@pytest.mark.skipif(shutil.which("pixi") is None, reason="pixi not installed")
@pytest.mark.skipif(find_spec("snakemake") is None, reason="snakemake not installed")
def test_workflow_reruns_only_for_selected_environment_changes(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = workspace / "pixi.toml"
    activation = workspace / "activate.sh"
    activation.write_text("export SCRIPT_VALUE=original-script\n")

    def write_manifest(selected="original-env", other="unused", task="echo before"):
        manifest.write_text(
            '[workspace]\nname="rerun-test"\nchannels=[]\n'
            f'platforms=["{Platform.current()}"]\n'
            '[environments]\ntools=["selected"]\nother=["other"]\n'
            '[feature.selected.activation]\nscripts=["activate.sh"]\n'
            f'[feature.selected.activation.env]\nSELECTED_VALUE="{selected}"\n'
            f'[feature.other.activation.env]\nOTHER_VALUE="{other}"\n'
            f'[tasks]\nunrelated="{task}"\n'
        )

    write_manifest()
    command = 'printf "%s:%s\\n" "$SELECTED_VALUE" "$SCRIPT_VALUE" > {output:q}'
    (workspace / "Snakefile").write_text(
        "rule probe:\n"
        '    output: "result.txt"\n'
        '    software: pixi(workspace=".", env="tools")\n'
        f"    shell: {command!r}\n"
    )
    process_env = {
        **os.environ,
        "PIXI_OFFLINE": "true",
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }

    def run(args):
        result = subprocess.run(
            args,
            cwd=workspace,
            env=process_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    # Establish the lock snapshot before recording the first job's software hash.
    run(["pixi", "lock", "--manifest-path", str(manifest)])
    workflow = [
        sys.executable,
        "-m",
        "snakemake",
        "--software-deployment-method",
        "pixi",
        "--cores",
        "1",
        "result.txt",
    ]
    run(workflow)
    output = workspace / "result.txt"
    assert output.read_text() == "original-env:original-script\n"
    first_mtime = output.stat().st_mtime_ns

    run(workflow)
    assert output.stat().st_mtime_ns == first_mtime

    write_manifest(other="changed-unused", task="echo after")
    run(workflow)
    assert output.stat().st_mtime_ns == first_mtime

    write_manifest(selected="changed-env", other="changed-unused", task="echo after")
    run(workflow)
    assert output.read_text() == "changed-env:original-script\n"

    activation.write_text("export SCRIPT_VALUE=changed-script\n")
    run(workflow)
    assert output.read_text() == "changed-env:changed-script\n"
