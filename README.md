# Snakemake Pixi Plugin

Use [pixi](https://pixi.prefix.dev/) environments in Snakemake rules.
The environments can contain conda and PyPI packages.

This plugin is experimental.
It requires pixi on `PATH` and `snakemake>=10`.

## Usage

Set `workspace` to the pixi workspace directory and `env` to the environment name.
Relative workspace paths are relative to the file that defines the rule.
The workspace must be a local directory that the job can access.
This example uses an `alignment` environment that contains `samtools`:

```python
rule sort_bam:
    input:
        "mapped.bam",
    output:
        "sorted.bam",
    software:
        pixi(workspace="envs/biotools/", env="alignment")
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
| `workspace` | Current directory | Directory with `pixi.toml` or `pyproject.toml`. |
| `env` | `"default"` | Name of the pixi environment. |
| `frozen` | `False` | Use the existing lockfile without updates. |
| `locked` | `False` | Require a lockfile that matches the manifest. |

The `frozen` and `locked` options cannot both be `True`.

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
Comments, task definitions, and changes to unrelated environments or platforms do not change it.
With `frozen=True`, dependency declarations do not affect the fingerprint because Pixi uses the existing lockfile.
Otherwise, the fingerprint also includes the selected dependency declarations.

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
