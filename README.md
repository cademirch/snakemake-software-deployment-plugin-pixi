# Snakemake Pixi Plugin

Use [pixi](https://pixi.prefix.dev/) environments in Snakemake rules.
The environments can contain conda and PyPI packages.

This plugin is experimental.
It requires pixi on `PATH` and `snakemake>=10`.

## Usage

Set `manifest` to the exact `pixi.toml` or `pyproject.toml` file and `env` to the environment name.
Relative manifest paths are relative to the file that defines the rule.
The manifest and its workspace must be local and accessible to the job.
This example uses an `alignment` environment that contains `samtools`:

```python
rule sort_bam:
    input:
        "mapped.bam",
    output:
        "sorted.bam",
    software:
        pixi(manifest="envs/biotools/pixi.toml", env="alignment")
    shell:
        "samtools sort -o {output} {input}"
```

When you run Snakemake, enable the plugin:

```sh
snakemake --software-deployment-method pixi --cores 1
```

### Options

| Option | Default | Purpose |
| --- | --- | --- |
| `manifest` | Required | Exact path to `pixi.toml` or `pyproject.toml`. |
| `env` | `"default"` | Name of the pixi environment. |
| `frozen` | `False` | Use the existing lockfile without updates. |
| `locked` | `False` | Require a lockfile that matches the manifest. |

The `frozen` and `locked` options cannot both be `True`.

### Paths, includes, and modules

The plugin uses the exact manifest you provide. It does not search parent directories or try another manifest filename.
Both supported filenames can exist in one directory. The specified file wins, and Pixi validates its contents.
The workspace is that file's parent directory, with a sibling `pixi.lock` and environments under `.pixi/envs`.

For example, a rule in `workflow/rules/align.smk` can select `workflow/pixi.toml` with `manifest="../pixi.toml"`.
This also applies to included files inside imported modules: the path belongs to the file that defines the rule.
A parent workflow and a module can select different manifests with the same environment name.

Changing the work directory with `snakemake -d` does not change this selection.
Running through `pixi run snakemake` or `pixi shell` does not supply an implicit manifest or environment name.
To share a workspace between rules or modules, specify its manifest explicitly in each rule.

This is a breaking API change. Replace `workspace="envs/biotools"` with `manifest="envs/biotools/pixi.toml"`.
Calls such as `pixi(env="alignment")` now require `manifest=`.

### Shells

The plugin uses the shell configured in Snakemake.
Supported shells are Bash, dash, sh, ksh, brush, Zsh, Xonsh, and fish.

The plugin runs Pixi's activation script before each rule command.


### Package cache and software changes

Pixi manages package downloads and the environments in `.pixi/envs`.
The plugin does not download a second copy into Snakemake's software cache.
Existing copies in that cache are not removed automatically.

Snakemake detects software changes from the selected environment on the current platform.
Fingerprints support Pixi lockfile formats 5, 6, and 7.
The fingerprint includes locked package records, activation variables and scripts, and relevant installation configuration.
Comments, task definitions, and changes to unrelated environments or native platforms do not change it.
For named platform alternatives on the current native platform, the fingerprint includes all candidates and the host capabilities that affect their selection.
With `frozen=True`, dependency declarations do not affect the fingerprint because Pixi uses the existing lockfile.
Otherwise, the fingerprint also includes the selected dependency declarations.

For platform declarations with extra requirements but no explicit name, the fingerprint conservatively includes the full manifest and lockfile, plus activation scripts and host capabilities.
This avoids guessing Pixi's generated platform names, but unrelated edits can cause reruns in these workspaces, including with `frozen=True`.
Give each such platform an explicit `name` to use the narrower fingerprint.

Pixi still prepares environments when a job needs them.
If that preparation creates or updates `pixi.lock`, the next workflow run can rerun affected jobs once more.
For stable fingerprints from the first run, create the lockfile before running Snakemake.
Local and editable package source identities are included, but arbitrary edits inside their source trees are not tracked.
User-wide Pixi configuration is not part of the fingerprint.

## Development

From this repository, install the development environment:

```sh
pixi install -e dev
```

This environment includes Snakemake and an editable installation of the plugin.

```sh
pixi run -e dev test                         # Plugin tests
pixi run -e dev pytest tests/test_real.py -v  # End-to-end workflow test
pixi run -e dev lint                         # Lint
pixi run -e dev format                       # Format
pixi run -e dev typecheck                    # Type checks
```
