"""Software identity must follow the selected environment, not file layout."""

from pathlib import Path

import pytest
import yaml

from snakemake_interface_common.exceptions import WorkflowError

from snakemake_software_deployment_plugin_pixi._fingerprint import (
    environment_fingerprint,
)


MANIFEST = """\
[workspace]
name = "example"
channels = ["conda-forge"]
platforms = ["linux-64", "osx-arm64"]
[dependencies]
python = "3.12.*"
[feature.tools.dependencies]
curl = "8.*"
[feature.other.dependencies]
perl = "5.*"
[environments]
tools = ["tools"]
other = ["other"]
"""


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "pixi.toml").write_text(MANIFEST)
    lock = {
        "version": 7,
        "platforms": [
            {"name": "linux-64", "virtual-packages": ["__glibc=2.28"]},
            {"name": "osx-arm64", "virtual-packages": ["__osx=13"]},
        ],
        "environments": {
            "tools": {
                "channels": [{"url": "https://example.org/conda"}],
                "options": {"pypi-prerelease-mode": "if-necessary"},
                "packages": {
                    "linux-64": [
                        {"conda": "https://example.org/linux-64/curl-8-h0.conda"},
                        {"pypi": "./local", "extras": ["cli"]},
                    ],
                    "osx-arm64": [
                        {"conda": "https://example.org/osx-arm64/curl-8-h1.conda"}
                    ],
                },
            },
            "other": {
                "packages": {
                    "linux-64": [{"pypi": "https://example.org/unrelated.whl"}]
                }
            },
        },
        "packages": [
            {
                "conda": "https://example.org/linux-64/curl-8-h0.conda",
                "name": "curl",
                "version": "8",
                "build": "h0",
                "sha256": "aa",
            },
            {"pypi": "./local", "name": "local", "version": "1", "editable": True},
            {"conda": "https://example.org/osx-arm64/curl-8-h1.conda", "sha256": "bb"},
            {"pypi": "https://example.org/unrelated.whl", "sha256": "cc"},
        ],
    }
    write_lock(tmp_path, lock)
    return tmp_path, lock


def write_lock(path, lock):
    (path / "pixi.lock").write_text(yaml.safe_dump(lock, sort_keys=False))


def fingerprint(path, *, frozen=False, env="tools", platform="linux-64"):
    return environment_fingerprint(
        path / "pixi.toml", path / "pixi.lock", env, platform, frozen
    )


def test_unrelated_manifest_and_lock_changes_do_not_change_identity(workspace):
    path, lock = workspace
    before = fingerprint(path)
    (path / "pixi.toml").write_text(
        MANIFEST.replace('name = "example"', 'name = "renamed"').replace(
            'perl = "5.*"', 'perl = "9.*"'
        )
        + '\n# Documentation only\n[feature.other.tasks]\nlint = "echo changed"\n'
        + '[feature.tools.tasks]\nrun = "echo changed"\n'
        + '[feature.tools.target.osx-arm64.dependencies]\ncurl = "10.*"\n'
    )
    lock["packages"][2]["sha256"] = "unrelated platform"
    lock["packages"][3]["sha256"] = "unrelated environment"
    lock["platforms"][1]["virtual-packages"] = ["__osx=15"]
    write_lock(path, lock)
    assert fingerprint(path) == before


def test_lock_order_and_yaml_format_do_not_change_identity(workspace):
    path, lock = workspace
    before = fingerprint(path)
    lock["packages"].reverse()
    lock["environments"]["tools"]["packages"]["linux-64"].reverse()
    (path / "pixi.lock").write_text(
        "# comment\n" + yaml.safe_dump(lock, sort_keys=True)
    )
    assert fingerprint(path) == before


@pytest.mark.parametrize("field,value", [("build", "h2"), ("sha256", "dd")])
def test_selected_conda_artifact_changes_identity(workspace, field, value):
    path, lock = workspace
    before = fingerprint(path)
    lock["packages"][0][field] = value
    write_lock(path, lock)
    assert fingerprint(path) != before


@pytest.mark.parametrize(
    "change", ["editable", "extras", "options", "virtual-packages"]
)
def test_selected_pypi_and_platform_options_change_identity(workspace, change):
    path, lock = workspace
    before = fingerprint(path)
    if change == "editable":
        lock["packages"][1]["editable"] = False
    elif change == "extras":
        lock["environments"]["tools"]["packages"]["linux-64"][1]["extras"] = []
    elif change == "options":
        lock["environments"]["tools"]["options"]["pypi-prerelease-mode"] = "allow"
    else:
        lock["platforms"][0]["virtual-packages"] = ["__glibc=2.40"]
    write_lock(path, lock)
    assert fingerprint(path) != before


@pytest.mark.parametrize("frozen", [False, True])
def test_dependency_edits_affect_only_unfrozen_fingerprint(workspace, frozen):
    path, _ = workspace
    before = fingerprint(path, frozen=frozen)
    (path / "pixi.toml").write_text(MANIFEST.replace('curl = "8.*"', 'curl = "9.*"'))
    assert (fingerprint(path, frozen=frozen) == before) is frozen


def test_target_override_and_no_default_feature_ignore_unused_dependencies(workspace):
    path, _ = workspace
    manifest = MANIFEST.replace(
        'tools = ["tools"]', 'tools = {features=["tools"], no-default-feature=true}'
    )
    manifest += '\n[feature.tools.target.linux-64.dependencies]\ncurl = "10.*"\n'
    (path / "pixi.toml").write_text(manifest)
    before = fingerprint(path)
    (path / "pixi.toml").write_text(
        manifest.replace('python = "3.12.*"', 'python = "3.13.*"').replace(
            'curl = "8.*"', 'curl = "9.*"'
        )
    )
    assert fingerprint(path) == before


@pytest.mark.parametrize("frozen", [False, True])
def test_activation_scripts_and_variables_affect_identity(workspace, frozen):
    path, _ = workspace
    (path / "activate.sh").write_text("export TOOL_SETTING=first\n")
    manifest = (
        MANIFEST
        + '\n[feature.tools.activation]\nscripts=["activate.sh"]\nenv={MODE="first"}\n'
    )
    (path / "pixi.toml").write_text(manifest)
    before = fingerprint(path, frozen=frozen)
    (path / "activate.sh").write_text("export TOOL_SETTING=second\n")
    after = fingerprint(path, frozen=frozen)
    assert after != before
    (path / "pixi.toml").write_text(manifest.replace('MODE="first"', 'MODE="second"'))
    assert fingerprint(path, frozen=frozen) != after


def test_target_activation_overrides_unused_script(workspace):
    path, _ = workspace
    (path / "activate.sh").write_text("export TOOL_SETTING=first\n")
    manifest = (
        MANIFEST + '\n[feature.tools.activation]\nscripts=["missing-windows.bat"]\n'
    )
    manifest += '[feature.tools.target.unix.activation]\nscripts=["activate.sh"]\n'
    (path / "pixi.toml").write_text(manifest)
    before = fingerprint(path)
    (path / "pixi.toml").write_text(
        manifest.replace("missing-windows.bat", "another.bat")
    )
    assert fingerprint(path) == before


def test_workspace_install_configuration_affects_frozen_identity(workspace):
    path, _ = workspace
    (path / ".pixi").mkdir()
    config = path / ".pixi/config.toml"
    config.write_text('run-post-link-scripts = "false"\n')
    before = fingerprint(path, frozen=True)
    config.write_text('run-post-link-scripts = "insecure"\n')
    assert fingerprint(path, frozen=True) != before


@pytest.mark.parametrize("frozen", [False, True])
def test_missing_lock_is_read_only_until_lazy_preparation(workspace, frozen):
    path, _ = workspace
    (path / "pixi.lock").unlink()
    assert fingerprint(path, frozen=frozen)
    assert not (path / "pixi.lock").exists()
    assert not (path / ".pixi").exists()


@pytest.mark.parametrize("version", [5, 6, 7])
def test_reference_lock_versions_are_supported(workspace, version):
    path, lock = workspace
    lock["version"] = version
    if version < 7:
        del lock["platforms"]
    if version == 5:
        for record in lock["packages"]:
            kind = "conda" if "conda" in record else "pypi"
            record["kind"] = kind
            record["url"] = record.pop(kind)
    write_lock(path, lock)
    assert fingerprint(path)


@pytest.mark.parametrize("bad", ["version", "reference", "feature", "activation"])
def test_malformed_relevant_inputs_fail_clearly(workspace, bad):
    path, lock = workspace
    if bad == "version":
        lock["version"] = 999
        write_lock(path, lock)
    elif bad == "reference":
        lock["packages"].pop(0)
        write_lock(path, lock)
    elif bad == "feature":
        (path / "pixi.toml").write_text(
            MANIFEST.replace('tools = ["tools"]', 'tools = ["missing"]')
        )
    else:
        (path / "pixi.toml").write_text(
            MANIFEST + "\n[feature.tools.activation]\nscripts=[42]\n"
        )
    with pytest.raises(WorkflowError):
        fingerprint(path)


def test_pyproject_dependency_features_are_scoped(tmp_path):
    manifest = tmp_path / "pyproject.toml"
    source = """\
[project]
name = "example"
version = "1.0"
requires-python = ">=3.11"
dependencies = ["requests>=2"]
[project.optional-dependencies]
tools = ["click>=8"]
other = ["unrelated>=1"]
[tool.pixi.workspace]
channels = ["conda-forge"]
platforms = ["linux-64"]
[tool.pixi.environments]
tools = ["tools"]
"""
    manifest.write_text(source)

    def get():
        return environment_fingerprint(
            manifest, tmp_path / "pixi.lock", "tools", "linux-64", False
        )

    before = get()
    manifest.write_text(source.replace("unrelated>=1", "unrelated>=2"))
    assert get() == before
    manifest.write_text(source.replace("click>=8", "click>=9"))
    assert get() != before


@pytest.mark.parametrize(
    "fixture,env", [("test_workspace", "tools"), ("test_workspace_pypi", "pypitest")]
)
def test_repository_lockfiles_have_supported_reference_shapes(fixture, env):
    path = Path(__file__).parent / fixture
    assert environment_fingerprint(
        path / "pixi.toml", path / "pixi.lock", env, "linux-64", True
    )


@pytest.mark.parametrize(
    "field,first,second",
    [
        ("solve-strategy", '"highest"', '"lowest-version"'),
        ("exclude-newer", '"2026-01-01"', '"2026-02-01"'),
        ("conda-pypi-map", '{conda-forge="first.json"}', '{conda-forge="second.json"}'),
    ],
)
def test_workspace_solver_configuration_changes_identity(
    workspace, field, first, second
):
    path, _ = workspace
    source = MANIFEST.replace("[workspace]", f"[workspace]\n{field} = {first}")
    (path / "pixi.toml").write_text(source)
    before = fingerprint(path)
    (path / "pixi.toml").write_text(source.replace(first, second))
    assert fingerprint(path) != before


def test_workspace_dependency_inheritance_selects_only_used_specs(workspace):
    path, _ = workspace
    source = MANIFEST.replace('curl = "8.*"', "curl = {workspace=true}")
    source += '\n[workspace.dependencies]\ncurl = "8.*"\nunused = "1.*"\n'
    (path / "pixi.toml").write_text(source)
    before = fingerprint(path)
    (path / "pixi.toml").write_text(source.replace('unused = "1.*"', 'unused = "2.*"'))
    assert fingerprint(path) == before
    (path / "pixi.toml").write_text(source.replace('curl = "8.*"', 'curl = "9.*"'))
    assert fingerprint(path) != before


def test_platform_system_requirements_ignore_other_operating_systems(workspace):
    path, _ = workspace
    source = (
        MANIFEST + '\n[feature.tools.system-requirements]\nlinux="4.18"\nmacos="13"\n'
    )
    (path / "pixi.toml").write_text(source)
    before = fingerprint(path)
    (path / "pixi.toml").write_text(source.replace('macos="13"', 'macos="14"'))
    assert fingerprint(path) == before
    (path / "pixi.toml").write_text(source.replace('linux="4.18"', 'linux="5"'))
    assert fingerprint(path) != before


def test_solve_group_peer_constraints_are_relevant_but_peer_activation_is_not(
    workspace,
):
    path, _ = workspace
    source = MANIFEST.replace(
        'tools = ["tools"]', 'tools = {features=["tools"], solve-group="shared"}'
    )
    source = source.replace(
        'other = ["other"]', 'other = {features=["other"], solve-group="shared"}'
    )
    source += '\n[feature.other.activation]\nscripts=["not-present.sh"]\n'
    (path / "pixi.toml").write_text(source)
    before = fingerprint(path)
    (path / "pixi.toml").write_text(source.replace('perl = "5.*"', 'perl = "6.*"'))
    assert fingerprint(path) != before
    (path / "pixi.toml").write_text(
        source.replace("not-present.sh", "another-script.sh")
    )
    assert fingerprint(path) == before


def test_self_referencing_python_extras_include_transitive_requirements(tmp_path):
    manifest = tmp_path / "pyproject.toml"
    source = """\
[project]
name="example"
version="1.0"
[project.optional-dependencies]
tools=["example[testing]"]
testing=["pytest>=8"]
[tool.pixi.workspace]
channels=["conda-forge"]
platforms=["linux-64"]
[tool.pixi.environments]
tools=["tools"]
"""
    manifest.write_text(source)
    before = environment_fingerprint(
        manifest, tmp_path / "pixi.lock", "tools", "linux-64", False
    )
    manifest.write_text(source.replace("pytest>=8", "pytest>=9"))
    assert (
        environment_fingerprint(
            manifest, tmp_path / "pixi.lock", "tools", "linux-64", False
        )
        != before
    )


def test_missing_selected_lock_data_remains_lazy(workspace):
    path, lock = workspace
    before = fingerprint(path)
    del lock["environments"]["tools"]["packages"]["linux-64"]
    write_lock(path, lock)
    assert fingerprint(path) != before
    del lock["environments"]["tools"]
    write_lock(path, lock)
    assert fingerprint(path)


def test_inline_environment_dependencies_and_activation_are_selected(workspace):
    path, _ = workspace
    source = MANIFEST.replace(
        'tools = ["tools"]',
        'tools = {features=["tools"], activation={env={MODE="inline"}}}',
    )
    (path / "pixi.toml").write_text(source)
    before = fingerprint(path, frozen=True)
    (path / "pixi.toml").write_text(source.replace('MODE="inline"', 'MODE="changed"'))
    assert fingerprint(path, frozen=True) != before


def test_legacy_lock_selector_disambiguates_same_url(workspace):
    path, lock = workspace
    record = dict(lock["packages"][0], build="another", sha256="different")
    lock["packages"].append(record)
    lock["environments"]["tools"]["packages"]["linux-64"][0]["build"] = "h0"
    write_lock(path, lock)
    before = fingerprint(path)
    lock["packages"][-1]["sha256"] = "irrelevant variant"
    write_lock(path, lock)
    assert fingerprint(path) == before


def test_conda_source_fingerprint_includes_transitive_build_and_host_packages(
    workspace,
):
    path, lock = workspace
    source = "my-package[12345678] @ ./my-package"
    builder = "https://example.org/linux-64/build-tool-1.0.0-h0.conda"
    host = "https://example.org/linux-64/host-lib-2.0.0-h0.conda"
    lock["packages"].extend(
        [
            {
                "conda_source": source,
                "version": "0.1.0",
                "build": "py_0",
                "subdir": "noarch",
                "build_packages": [{"conda": builder}],
                "host_packages": [{"conda": host}],
            },
            {"conda": builder, "sha256": "build-first"},
            {"conda": host, "sha256": "host-first"},
        ]
    )
    write_lock(path, lock)
    unrelated = fingerprint(path)
    lock["packages"][-1]["sha256"] = "host-second"
    write_lock(path, lock)
    assert fingerprint(path) == unrelated
    lock["environments"]["tools"]["packages"]["linux-64"].append(
        {"conda_source": source}
    )
    write_lock(path, lock)
    before = fingerprint(path)
    lock["packages"][-2]["sha256"] = "build-second"
    write_lock(path, lock)
    after_build = fingerprint(path)
    assert after_build != before
    lock["packages"][-1]["sha256"] = "host-third"
    write_lock(path, lock)
    assert fingerprint(path) != after_build


def test_pyproject_without_pixi_table_defers_validation_to_pixi(tmp_path):
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text('[project]\nname="plain-python"\nversion="1.0"\n')
    assert environment_fingerprint(
        manifest, tmp_path / "pixi.lock", "default", "linux-64", False
    )


def test_legacy_selector_infers_conda_metadata_from_archive_url(workspace):
    path, lock = workspace
    record = lock["packages"][0]
    for field in ("name", "version", "build"):
        del record[field]
    lock["environments"]["tools"]["packages"]["linux-64"][0]["build"] = "h0"
    write_lock(path, lock)
    assert fingerprint(path)


def test_pyproject_without_pixi_dependencies_defer_to_pixi(tmp_path):
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        '[project]\nname="plain-python"\nversion="1.0"\ndependencies=["requests"]\n'
    )
    assert environment_fingerprint(
        manifest, tmp_path / "pixi.lock", "default", "linux-64", False
    )


def test_build_variant_file_contents_change_identity(workspace):
    path, _ = workspace
    source = MANIFEST.replace(
        "[workspace]", '[workspace]\nbuild-variants-files=["variants.yaml"]'
    )
    (path / "pixi.toml").write_text(source)
    variants = path / "variants.yaml"
    variants.write_text('python: ["3.12.*"]\n')
    before = fingerprint(path)
    variants.write_text('python: ["3.13.*"]\n')
    assert fingerprint(path) != before


def test_package_manifest_tables_are_not_default_feature_fields(workspace):
    path, _ = workspace
    before = fingerprint(path, frozen=True)
    (path / "pixi.toml").write_text(
        MANIFEST
        + '\n[package]\nname="my-package"\nversion="1.0"\n[package.build]\nbackend={name="pixi-build-python", version="*"}\n'
    )
    assert fingerprint(path, frozen=True) == before


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("selector", ["macos", "osx-*"])
def test_macos_alias_and_platform_glob_activation_affect_identity(
    workspace, frozen, selector
):
    path, _ = workspace
    source = (
        MANIFEST
        + f'\n[feature.tools.target."{selector}".activation.env]\nMODE="first"\n'
    )
    (path / "pixi.toml").write_text(source)
    before = fingerprint(path, frozen=frozen, platform="osx-arm64")
    (path / "pixi.toml").write_text(source.replace('MODE="first"', 'MODE="second"'))
    assert fingerprint(path, frozen=frozen, platform="osx-arm64") != before


@pytest.mark.parametrize("frozen", [False, True])
def test_activation_variable_expansion_order_affects_identity(workspace, frozen):
    path, _ = workspace
    prefix = MANIFEST + "\n[feature.tools.activation.env]\n"
    (path / "pixi.toml").write_text(prefix + 'A="a"\nB="$A"\n')
    before = fingerprint(path, frozen=frozen)
    (path / "pixi.toml").write_text(prefix + 'B="$A"\nA="a"\n')
    assert fingerprint(path, frozen=frozen) != before


@pytest.mark.parametrize("frozen", [False, True])
def test_target_activation_variables_precede_unshadowed_base_variables(
    workspace, frozen
):
    path, _ = workspace
    (path / "pixi.toml").write_text(
        MANIFEST
        + '\n[feature.tools.activation.env]\nA="default"\nB="$A"\n'
        + '[feature.tools.target.linux-64.activation.env]\nA="target"\nC="$B"\n'
    )
    before = fingerprint(path, frozen=frozen)
    (path / "pixi.toml").write_text(
        MANIFEST + '\n[feature.tools.activation.env]\nA="target"\nC="$B"\nB="$A"\n'
    )
    assert fingerprint(path, frozen=frozen) == before


@pytest.mark.parametrize(
    "selector,platform,matches",
    [
        ("macos", "linux-64", False),
        ("linux-*", "osx-arm64", False),
        ("*-arm64", "osx-arm64", True),
        ("osx**arm64", "osx-arm64", True),
        ("OSX-*", "osx-arm64", False),
        ("win", "win-64", True),
        ("windows", "win-64", False),
        ("unix", "emscripten-wasm32", True),
        ("unix", "wasi-wasm32", False),
    ],
)
def test_target_globs_and_os_aliases_match_pixi(workspace, selector, platform, matches):
    path, _ = workspace
    source = (
        MANIFEST
        + f'\n[feature.tools.target."{selector}".activation.env]\nMODE="first"\n'
    )
    (path / "pixi.toml").write_text(source)
    before = fingerprint(path, frozen=True, platform=platform)
    (path / "pixi.toml").write_text(source.replace('MODE="first"', 'MODE="second"'))
    assert (fingerprint(path, frozen=True, platform=platform) != before) is matches


def test_glob_matches_platform_name_instead_of_subdir(workspace):
    path, _ = workspace
    source = MANIFEST.replace(
        'platforms = ["linux-64", "osx-arm64"]',
        'platforms = [{name="custom-macos", platform="osx-arm64"}]',
    )
    source += '\n[feature.tools.target."osx-*".activation.env]\nMODE="first"\n'
    (path / "pixi.toml").write_text(source)
    before = fingerprint(path, frozen=True, platform="custom-macos")
    (path / "pixi.toml").write_text(source.replace('MODE="first"', 'MODE="second"'))
    assert fingerprint(path, frozen=True, platform="custom-macos") == before
    source = source.replace('"osx-*"', '"custom-*"')
    (path / "pixi.toml").write_text(source)
    before = fingerprint(path, frozen=True, platform="custom-macos")
    (path / "pixi.toml").write_text(source.replace('MODE="first"', 'MODE="second"'))
    assert fingerprint(path, frozen=True, platform="custom-macos") != before


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("initial_kind", ["missing", "directory"])
def test_missing_activation_script_is_lazy_and_creation_changes_identity(
    workspace, frozen, initial_kind
):
    path, _ = workspace
    (path / "pixi.toml").write_text(
        MANIFEST + '\n[feature.tools.activation]\nscripts=["activate.sh"]\n'
    )
    script = path / "activate.sh"
    if initial_kind == "directory":
        script.mkdir()
    before = fingerprint(path, frozen=frozen)
    if initial_kind == "directory":
        script.rmdir()
    script.write_text("export MODE=present\n")
    assert fingerprint(path, frozen=frozen) != before
    script.unlink()
    assert fingerprint(path, frozen=frozen) == before


def test_native_platform_selects_named_platform_lock_and_activation(workspace):
    path, lock = workspace
    source = (
        MANIFEST.replace(
            'platforms = ["linux-64", "osx-arm64"]',
            'platforms = [{name="custom", platform="osx-arm64"}]',
        )
        + '\n[feature.tools.target.custom.activation.env]\nMODE="first"\n'
    )
    (path / "pixi.toml").write_text(source)
    packages = lock["environments"]["tools"]["packages"]
    packages["custom"] = packages.pop("osx-arm64")
    lock["platforms"][1]["name"] = "custom"
    lock["platforms"][1]["subdir"] = "osx-arm64"
    write_lock(path, lock)
    before = fingerprint(path, frozen=True, platform="osx-arm64")
    (path / "pixi.toml").write_text(source.replace('MODE="first"', 'MODE="second"'))
    after_activation = fingerprint(path, frozen=True, platform="osx-arm64")
    assert after_activation != before
    lock["packages"][2]["sha256"] = "changed custom artifact"
    write_lock(path, lock)
    assert fingerprint(path, frozen=True, platform="osx-arm64") != after_activation


def test_named_platform_selection_respects_feature_intersection(workspace):
    path, _ = workspace
    source = MANIFEST.replace(
        'platforms = ["linux-64", "osx-arm64"]',
        'platforms = [{name="first", platform="osx-arm64"}, {name="second", platform="osx-arm64"}]',
    ).replace(
        "[feature.tools.dependencies]",
        '[feature.tools]\nplatforms=["second"]\n[feature.tools.dependencies]',
    )
    source += '\n[feature.tools.target.second.activation.env]\nMODE="selected"\n'
    (path / "pixi.toml").write_text(source)
    assert fingerprint(path, frozen=True, platform="osx-arm64") == fingerprint(
        path, frozen=True, platform="second"
    )


def test_named_platform_candidates_preserve_declaration_order(workspace):
    path, _ = workspace
    source = MANIFEST.replace(
        'platforms = ["linux-64", "osx-arm64"]',
        'platforms = [{name="first", platform="osx-arm64"}, {name="second", platform="osx-arm64"}]',
    )
    (path / "pixi.toml").write_text(source)
    before = fingerprint(path, frozen=True, platform="osx-arm64")
    (path / "pixi.toml").write_text(
        source.replace('name="first"', 'name="temporary"')
        .replace('name="second"', 'name="first"')
        .replace('name="temporary"', 'name="second"')
    )
    assert fingerprint(path, frozen=True, platform="osx-arm64") != before


def test_virtual_platform_requirements_change_frozen_candidate_identity(workspace):
    path, _ = workspace
    (path / "pixi.toml").write_text(
        MANIFEST.replace(
            'platforms = ["linux-64", "osx-arm64"]',
            'platforms = [{name="unusable", platform="osx-arm64", macos="99.0"}, {name="usable", platform="osx-arm64"}]',
        )
    )
    before = fingerprint(path, frozen=True, platform="osx-arm64")
    manifest = path / "pixi.toml"
    manifest.write_text(manifest.read_text().replace('macos="99.0"', 'macos="13.0"'))
    assert fingerprint(path, frozen=True, platform="osx-arm64") != before


@pytest.mark.parametrize("frozen", [False, True])
def test_dev_source_dependencies_are_valid_and_affect_unfrozen_identity(
    workspace, frozen
):
    path, _ = workspace
    source = MANIFEST + '\n[dev]\npackage={path="./first"}\n'
    (path / "pixi.toml").write_text(source)
    before = fingerprint(path, frozen=frozen)
    (path / "pixi.toml").write_text(source.replace("./first", "./second"))
    assert (fingerprint(path, frozen=frozen) == before) is frozen


def test_host_virtual_package_override_changes_named_candidate_identity(
    workspace, monkeypatch
):
    path, _ = workspace
    (path / "pixi.toml").write_text(
        MANIFEST.replace(
            'platforms = ["linux-64", "osx-arm64"]',
            'platforms = [{name="custom", platform="osx-arm64"}]',
        )
    )
    monkeypatch.setenv("CONDA_OVERRIDE_OSX", "13.0")
    before = fingerprint(path, frozen=True, platform="osx-arm64")
    monkeypatch.setenv("CONDA_OVERRIDE_OSX", "14.0")
    assert fingerprint(path, frozen=True, platform="osx-arm64") != before


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("with_lock", [False, True])
def test_unnamed_rich_platform_tracks_generated_target_activation(
    workspace, frozen, with_lock
):
    path, lock = workspace
    source = MANIFEST.replace(
        'platforms = ["linux-64", "osx-arm64"]',
        'platforms = ["osx-arm64", {platform="osx-arm64", macos="14.0"}]',
    )
    source += (
        "\n[feature.tools.target.osx-arm64-macos-14-0.activation]\n"
        'scripts=["activate.sh"]\n'
        "[feature.tools.target.osx-arm64-macos-14-0.activation.env]\n"
        'A="a"\nB="$A"\n'
    )
    (path / "pixi.toml").write_text(source)
    if not with_lock:
        (path / "pixi.lock").unlink()
    script = path / "activate.sh"
    script.write_text("export MODE=first\n")
    before = fingerprint(path, frozen=frozen, platform="osx-arm64")
    script.write_text("export MODE=second\n")
    after_script = fingerprint(path, frozen=frozen, platform="osx-arm64")
    assert after_script != before
    (path / "pixi.toml").write_text(source.replace('A="a"\nB="$A"', 'B="$A"\nA="a"'))
    assert fingerprint(path, frozen=frozen, platform="osx-arm64") != after_script
    if with_lock:
        before_lock = fingerprint(path, frozen=frozen, platform="osx-arm64")
        lock["packages"][2]["sha256"] = "new artifact"
        write_lock(path, lock)
        assert fingerprint(path, frozen=frozen, platform="osx-arm64") != before_lock


@pytest.mark.parametrize("frozen", [False, True])
def test_unnamed_rich_platform_preserves_target_precedence(workspace, frozen):
    path, _ = workspace
    source = MANIFEST.replace(
        'platforms = ["linux-64", "osx-arm64"]',
        'platforms = ["osx-arm64", {platform="osx-arm64", macos="14.0"}]',
    )
    osx = '\n[feature.tools.target.osx.activation.env]\nMODE="osx"\n'
    unix = '\n[feature.tools.target.unix.activation.env]\nMODE="unix"\n'
    (path / "pixi.toml").write_text(source + osx + unix)
    before = fingerprint(path, frozen=frozen, platform="osx-arm64")
    (path / "pixi.toml").write_text(source + unix + osx)
    assert fingerprint(path, frozen=frozen, platform="osx-arm64") != before
