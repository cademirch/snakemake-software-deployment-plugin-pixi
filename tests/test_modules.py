"""Exercise exact local manifests through Snakemake's rule source resolution."""

import os
import shutil
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

import pytest
from rattler.platform import Platform


pytestmark = [
    pytest.mark.skipif(shutil.which("pixi") is None, reason="pixi not installed"),
    pytest.mark.skipif(
        find_spec("snakemake") is None, reason="snakemake not installed"
    ),
]


def write_manifest(path, origin):
    prefix = "tool.pixi." if path.name == "pyproject.toml" else ""
    # Pyproject's default feature implicitly adds Python; exclude it for offline
    # probes and place activation in a dependency-free feature instead.
    path.write_text(
        f'[{prefix}workspace]\nname="manifest-probe"\nchannels=[]\n'
        f'platforms=["{Platform.current()}"]\n'
        f"[{prefix}environments]\n"
        'default={features=["probe"], no-default-feature=true}\n'
        'tools={features=["probe"], no-default-feature=true}\n'
        f'[{prefix}feature.probe.activation.env]\nPROBE_ORIGIN="{origin}"\n'
    )


def offline_environment(tmp_path):
    return {
        **os.environ,
        "PIXI_OFFLINE": "true",
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }


def run_workflow(snakefile, work, invoke, process_env, *targets, launcher=None):
    command = [
        sys.executable,
        "-m",
        "snakemake",
        "--snakefile",
        str(snakefile),
        "--directory",
        str(work),
        "--software-deployment-method",
        "pixi",
        "--cores",
        "1",
        *targets,
    ]
    if launcher is not None:
        command = [
            "pixi",
            "run",
            "--manifest-path",
            str(launcher),
            "-e",
            "tools",
            *command,
        ]
    return subprocess.run(
        command,
        cwd=invoke,
        env=process_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.mark.parametrize("module_filename", ["pixi.toml", "pyproject.toml"])
@pytest.mark.parametrize("launch_mode", ["inherited-variables", "pixi-run"])
def test_explicit_manifests_in_local_modules(tmp_path, module_filename, launch_mode):
    project = tmp_path / "project"
    module = project / "modules" / "child"
    rules = module / "rules"
    work = tmp_path / "work"
    invoke = tmp_path / "invoke"
    launcher = tmp_path / "launcher"
    for directory in (rules, work, invoke, launcher):
        directory.mkdir(parents=True)

    def rule_text(name, manifest, output):
        command = (
            'printf "%s\\n" "$PROBE_ORIGIN" "$PIXI_PROJECT_MANIFEST" '
            '"$PIXI_ENVIRONMENT_NAME" > {output:q}'
        )
        return (
            f"rule {name}:\n"
            f"    output: {output!r}\n"
            f"    software: pixi(manifest={manifest!r}, env='tools')\n"
            f"    shell: {command!r}\n"
        )

    parent_manifest = project / "pixi.toml"
    child_manifest = module / module_filename
    launcher_manifest = launcher / "pixi.toml"
    write_manifest(parent_manifest, "parent")
    write_manifest(launcher_manifest, "launcher")
    # Distinct activation values expose any fallback to the other filename.
    write_manifest(module / "pixi.toml", "module-pixi.toml")
    write_manifest(module / "pyproject.toml", "module-pyproject.toml")
    snakefile = project / "Snakefile"
    snakefile.write_text(
        rule_text("parent_probe", "pixi.toml", "parent.txt")
        + '\nmodule child:\n    snakefile: "modules/child/Snakefile"\n'
        + "use rule child_probe from child as imported_probe\n"
    )
    (module / "Snakefile").write_text('include: "rules/probe.smk"\n')
    (rules / "probe.smk").write_text(
        rule_text("child_probe", f"../{module_filename}", "child.txt")
    )
    process_env = {
        **offline_environment(tmp_path),
        "PIXI_PROJECT_MANIFEST": str(launcher_manifest),
        "PIXI_PROJECT_ROOT": str(launcher),
        "PIXI_ENVIRONMENT_NAME": "unrelated-launcher-env",
    }
    result = run_workflow(
        snakefile,
        work,
        invoke,
        process_env,
        "parent.txt",
        "child.txt",
        launcher=launcher_manifest if launch_mode == "pixi-run" else None,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    for filename, origin, manifest in (
        ("parent.txt", "parent", parent_manifest),
        ("child.txt", f"module-{module_filename}", child_manifest),
    ):
        actual_origin, actual_manifest, actual_env = (
            (work / filename).read_text().splitlines()
        )
        assert actual_origin == origin
        assert Path(actual_manifest).is_absolute()
        assert Path(actual_manifest).resolve() == manifest.resolve()
        assert actual_env == "tools"
    assert (project / "pixi.lock").is_file()
    assert (module / "pixi.lock").is_file()
    assert (launcher / "pixi.lock").exists() == (launch_mode == "pixi-run")
    for directory in (work, invoke):
        assert not (directory / "pixi.lock").exists()


def test_plain_pyproject_does_not_fall_back_to_pixi_manifest(tmp_path):
    project = tmp_path / "project"
    work = tmp_path / "work"
    invoke = tmp_path / "invoke"
    for directory in (project, work, invoke):
        directory.mkdir()
    write_manifest(project / "pixi.toml", "wrong-sibling")
    manifest = project / "pyproject.toml"
    manifest.write_text('[project]\nname="not-pixi"\nversion="0.1.0"\n')
    snakefile = project / "Snakefile"
    snakefile.write_text(
        "rule probe:\n"
        '    output: "result.txt"\n'
        '    software: pixi(manifest="pyproject.toml")\n'
        '    shell: "echo unexpected-fallback > {output:q}"\n'
    )
    result = run_workflow(
        snakefile, work, invoke, offline_environment(tmp_path), "result.txt"
    )

    assert result.returncode != 0
    assert "Failed to generate pixi shell hook" in result.stderr
    assert str(manifest) in result.stderr
    assert not (work / "result.txt").exists()
    assert not (project / "pixi.lock").exists()
