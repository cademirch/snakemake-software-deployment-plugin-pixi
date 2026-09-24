# Snakemake Pixi Plugin

Use [pixi](https://pixi.prefix.dev/) environments in Snakemake rules.
The environments can contain conda and PyPI packages.

This plugin is experimental.
It requires pixi on `PATH` and `snakemake>=10`.

## Usage

Set `workspace` to the pixi workspace directory and `env` to the environment name.
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


