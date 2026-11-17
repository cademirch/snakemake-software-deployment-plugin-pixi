# Explicit Pixi manifests and issue #1 fixes

Approved implementation order: fix issue #1 first, then change the manifest API.
Remote execution, source staging, and workspace packaging remain out of scope.

## Phase 1: software cache and fingerprints

- [x] Reproduce Snakemake downloading unused package assets with shared caching enabled.
- [x] Remove the cache capability and download code. Pixi remains responsible for its package cache.
- [x] Reproduce unrelated manifest edits rerunning a workflow through persisted software metadata.
- [x] Replace whole-file hashes with canonical data for the selected environment and current platform.
- [x] Include selected locked artifacts, activation variables, activation script contents, and relevant installation configuration.
- [x] Include selected dependency declarations for unlocked environments, including effective targets, Python extras/groups, and solve-group constraints.
- [x] Preserve exact manifest/workspace identity, environment name, shell dialect, and lock policy in the hash.
- [x] Preserve lazy installation and the immutable hash used as a dictionary key during a run.
- [x] Test, review format compatibility, and commit the issue fixes separately.

Commit: `05187cf`.
Initial verification before the manifest migration: 122 tests passed, 30 capability skips, 90.72% coverage. Ruff and Pyrefly passed. Later review corrections are included in this commit and the final verification below.

The fingerprint reads Pixi lockfile formats 5, 6, and 7. It follows selected package references, including source build dependencies.
Missing lock data remains a snapshot marker until Pixi prepares the environment.
If preparation creates or updates the lockfile, a later workflow invocation can rerun affected jobs once more.
Arbitrary edits inside local package source trees, ambient activation variables, and global Pixi configuration are outside this snapshot.
Named alternatives for the current native platform include all candidate declarations, their projections, and host virtual packages in the fingerprint.
Unnamed platform declarations with extra requirements use a conservative full manifest/lockfile projection, including activation scripts, because Pixi generates their names.
This fallback can rerun jobs for unrelated edits; explicit platform names enable the narrower projection.

## Phase 2: explicit manifests

```python
software:
    pixi(manifest="../envs/pixi.toml", env="alignment")
```

- [x] Replace optional `workspace` with required `manifest: EnvSpecSourceFile`, without a compatibility alias.
- [x] Reject absent or empty manifest arguments. Defer filesystem checks until Snakemake resolves rule source paths and wildcards.
- [x] Accept an exact local `pixi.toml` or `pyproject.toml` file, without directory or filename discovery.
- [x] Derive the workspace from the selected manifest's parent; keep its sibling lockfile and existing `.pixi/envs` layout.
- [x] Preserve the original source path rather than the cached manifest copy, which lacks sibling workspace files.
- [x] Anchor Snakemake's rebased path at environment construction so subsequent cwd changes cannot redirect it.
- [x] Preserve default environment `default`, default false lock policies, and their mutual exclusion.
- [x] Keep the phase 1 cache and fingerprint fixes intact.
- [x] Migrate examples, docstrings, fixtures, and all repository callers.
- [x] Verify local modules and nested includes with distinct source, invocation, and `-d` directories.
- [x] Verify both manifest filenames coexist without fallback, including under an actual `pixi run` launcher.
- [x] Verify a plain Python `pyproject.toml` reaches Pixi and fails without selecting a valid sibling manifest.
- [x] Run the full suite, lint, formatting, type checks, and independent review; commit separately.

Relative paths belong to the file defining the rule, including an included rule inside a local imported module.
Neither the working directory nor inherited `PIXI_PROJECT_*`/`PIXI_ENVIRONMENT_NAME` variables supplies an implicit workspace.
The plugin checks the selected path and reads inputs needed for fingerprinting. Pixi validates the manifest used for activation.

Final verification: 179 tests passed, 25 expected capability skips, 92.48% coverage. Ruff formatting/lint, Pyrefly, and whitespace checks passed. Independent review found no remaining actionable issues.

## Verification boundaries

Use the existing Pixi development environment and force imports from the isolated checkout.
Do not install missing tools without asking. Preserve unrelated untracked user files.
Do not delete old Snakemake cache contents automatically.
