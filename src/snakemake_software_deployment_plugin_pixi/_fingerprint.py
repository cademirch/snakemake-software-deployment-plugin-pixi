"""Read-only software fingerprints for one Pixi environment and platform.

An unlocked fingerprint describes the requested inputs and the lock snapshot at
DAG construction, not the result of a future solve. Local package source trees,
ambient variables used by activation, and user/global Pixi config are outside
this snapshot. Never recompute an Env's identity after lazy Pixi preparation.
"""

import hashlib
import json
import os
from datetime import date, datetime, time
from fnmatch import fnmatchcase
from pathlib import Path
import tomllib
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from rattler.platform import Platform
from rattler import VirtualPackage, VirtualPackageOverrides

from snakemake_interface_common.exceptions import WorkflowError


_DEPENDENCIES = {
    "dependencies",
    "host-dependencies",
    "build-dependencies",
    "pypi-dependencies",
    "constraints",
    "dev-dependencies",
    "dev",
}
_OPTIONS = {
    "channels",
    "channel-priority",
    "pypi-options",
    "system-requirements",
    "build-variants",
    "solve-strategy",
    "exclude-newer",
    "pypi-exclude-newer",
}
_WORKSPACE_METADATA = {
    "name",
    "version",
    "description",
    "authors",
    "license",
    "license-file",
    "readme",
    "homepage",
    "repository",
    "documentation",
}
_STRUCTURAL = {"activation", "target", "tasks", "platforms"}
_ROOT_FIELDS = {"workspace", "project", "feature", "environments", "tool", "package"}
_ENV_FIELDS = {"features", "solve-group", "no-default-feature"}
_INSTALL_CONFIG = {
    "run-post-link-scripts",
    "no-symbolic-links",
    "no-hard-links",
    "no-ref-links",
    "pypi-config",
    "detached-environments",
}


def _canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _table(value, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ValueError(f"{label} must be a table with string keys")
    return value


def _sequence(value, label: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _strings(value, label: str) -> list[str]:
    values = _sequence(value, label)
    if not all(isinstance(item, str) for item in values):
        raise ValueError(f"{label} must contain strings")
    return values


def _read_toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _lock_projection(path: Path, env_name: str, platform: str):
    if not path.exists():
        # Pixi enforces frozen/locked policies during the existing lazy preparation.
        return None
    with path.open() as handle:
        lock = _table(yaml.safe_load(handle), "lockfile")
    if lock.get("version") not in (5, 6, 7):
        raise ValueError(
            f"unsupported Pixi lockfile version {lock.get('version')!r}; expected 5, 6, or 7"
        )
    unknown = lock.keys() - {"version", "environments", "packages", "platforms"}
    if unknown:
        raise ValueError(f"unsupported lockfile fields: {', '.join(sorted(unknown))}")
    environments = _table(lock.get("environments"), "lockfile environments")
    if env_name not in environments:
        return {"missing-environment": env_name}
    environment = _table(environments[env_name], f"lockfile environment {env_name}")
    by_platform = _table(environment.get("packages"), "lockfile environment packages")
    references = _sequence(by_platform.get(platform, []), "locked package references")
    records = _sequence(lock.get("packages"), "lockfile packages")
    index: dict[bytes, list[dict]] = {}
    for record in records:
        record = _table(record, "locked package")
        if lock["version"] == 5:
            kind, location = record.get("kind"), record.get("url")
            if kind not in {"conda", "pypi"} or not isinstance(location, str):
                raise ValueError("unsupported v5 locked package source")
        else:
            kind = _package_kind(record)
            location = record[kind]
        index.setdefault(_canonical([kind, location]), []).append(record)

    def resolve(reference, ancestors=()):
        reference = _table(reference, "locked package reference")
        kind = _package_kind(reference)
        identity = _canonical([kind, reference[kind]])
        if identity in ancestors:
            raise ValueError(
                "cyclic source package build/host dependencies in lockfile"
            )
        matches = index.get(identity, [])
        if kind == "conda":
            matches = [
                record
                for record in matches
                if all(
                    _conda_field(record, key) == reference[key]
                    for key in ("name", "version", "build", "subdir")
                    if key in reference
                )
            ]
        if len(matches) != 1:
            raise ValueError(
                f"locked package reference {reference[kind]!r} has {len(matches)} matching records"
            )
        record = dict(matches[0])
        for field in ("build_packages", "host_packages"):
            if field in record:
                record[field] = sorted(
                    (
                        resolve(item, (*ancestors, identity))
                        for item in _sequence(record[field], field)
                    ),
                    key=_canonical,
                )
        return {"reference": reference, "record": record}

    selected = [resolve(reference) for reference in references]
    platform_records = [
        entry
        for entry in _sequence(lock.get("platforms", []), "lockfile platforms")
        if _table(entry, "lockfile platform").get("name") == platform
    ]
    return {
        "metadata": {k: v for k, v in environment.items() if k != "packages"},
        "platform": platform_records,
        "missing-platform": bool(by_platform) and platform not in by_platform,
        "packages": sorted(selected, key=_canonical),
    }


def _conda_field(record: dict, key: str):
    if key in record:
        return record[key]
    location = record.get("conda", record.get("url"))
    if not isinstance(location, str):
        return None
    path = Path(unquote(urlsplit(location).path))
    if key == "subdir":
        return path.parent.name
    filename = path.name
    for suffix in (".conda", ".tar.bz2"):
        if filename.endswith(suffix):
            parts = filename[: -len(suffix)].rsplit("-", 2)
            if len(parts) == 3:
                return dict(zip(("name", "version", "build"), parts)).get(key)
    return None


def _package_kind(record: dict) -> str:
    kinds = record.keys() & {"conda", "pypi", "conda_source"}
    if len(kinds) != 1:
        raise ValueError(
            "locked package must have exactly one conda, conda_source, or pypi source"
        )
    kind = next(iter(kinds))
    if not isinstance(record[kind], (str, dict)):
        raise ValueError("unsupported locked package source")
    return kind


def _selected_platform(entries, platform: str):
    selected: list[str | dict[str, Any]] = []
    for entry in _sequence(entries, "platforms"):
        if isinstance(entry, str):
            if entry == platform:
                selected.append(entry)
        else:
            entry = _table(entry, "platform")
            if entry.get("name", entry.get("platform")) == platform:
                selected.append(entry)
    return selected


def _platform_candidates(
    document: dict, env_name: str, requested: str
) -> list[dict] | None:
    pixi = document.get("tool", {}).get("pixi", document)
    workspace = _table(pixi.get("workspace", pixi.get("project", {})), "workspace")
    declarations = _sequence(workspace.get("platforms", []), "platforms")
    candidates = []
    for declaration in declarations:
        if isinstance(declaration, str):
            name = subdir = declaration
        else:
            declaration = _table(declaration, "platform")
            name = declaration.get("name", declaration.get("platform"))
            subdir = declaration.get("platform", name)
            if (
                subdir == requested
                and "name" not in declaration
                and declaration.keys() - {"platform"}
            ):
                return None
        if requested in {name, subdir}:
            candidates.append(
                {"name": name, "subdir": subdir, "declaration": declaration}
            )
    if not candidates:
        return [{"name": requested, "subdir": requested, "declaration": requested}]
    env = _table(pixi.get("environments", {}), "environments").get(env_name, [])
    env = {"features": env} if isinstance(env, list) else _table(env, "environment")
    names = list(_strings(env.get("features", []), "environment features"))
    if not env.get("no-default-feature", False) and "default" not in names:
        names.append("default")
    features = _table(pixi.get("feature", {}), "features")
    scopes = [env] + [
        pixi if name == "default" else _table(features.get(name, {}), "feature")
        for name in names
    ]
    for scope in scopes:
        if "platforms" in scope:
            allowed = _strings(scope["platforms"], "feature platforms")
            candidates = [
                candidate for candidate in candidates if candidate["name"] in allowed
            ]
    return candidates


def _host_virtual_packages() -> dict:
    packages = [
        package.into_generic()
        for package in VirtualPackage.detect(
            overrides=VirtualPackageOverrides.from_env()
        )
    ]
    return {
        "packages": sorted(
            (package.name.normalized, str(package.version), package.build_string)
            for package in packages
        ),
        "overrides": {
            name: value
            for name, value in os.environ.items()
            if name.startswith("CONDA_OVERRIDE_")
        },
    }


def _matches_target(selector: str, platform: str, subdir: str) -> bool:
    if "*" in selector:
        # Pixi globs match the platform name, with only '*' as a wildcard.
        if not all(
            char.isascii() and (char.isalnum() or char in "-*") for char in selector
        ):
            raise ValueError(f"unsupported Pixi target selector {selector!r}")
        return fnmatchcase(platform, selector)
    if any(char in selector for char in "?[](){}"):
        raise ValueError(f"unsupported Pixi target selector {selector!r}")
    if selector in {platform, subdir}:
        return True
    family_attributes = {
        "linux": "is_linux",
        "unix": "is_unix",
        "win": "is_windows",
        "osx": "is_osx",
        "macos": "is_osx",
    }
    attribute = family_attributes.get(selector)
    return bool(getattr(Platform(subdir), attribute)) if attribute else False


def _activation_script(root: Path, script: str) -> dict:
    try:
        contents = (root / script).read_bytes()
    except (FileNotFoundError, IsADirectoryError):
        # Pixi warns and skips these paths. A later file creation must still
        # change the identity without making hash calculation prepare Pixi.
        return {"path": script, "missing": True}
    return {"path": script, "sha256": hashlib.sha256(contents).hexdigest()}


def _ordered_document(value):
    if isinstance(value, dict):
        return {
            "table": [[key, _ordered_document(item)] for key, item in value.items()]
        }
    if isinstance(value, list):
        return [_ordered_document(item) for item in value]
    if isinstance(value, (datetime, date, time)):
        return {type(value).__name__: value.isoformat()}
    return value


def _unnamed_platform_fingerprint(
    document: dict, root: Path, lockfile_path: Path, platform: str
) -> bytes:
    # Pixi generates labels for unnamed rich platforms. Rather than duplicate
    # that algorithm, conservatively retain the complete parsed inputs here.
    # Ordered tables preserve activation expansion and target precedence even
    # when a generated label is absent from a missing or stale lockfile.
    pixi = document.get("tool", {}).get("pixi", document)
    scripts = []

    def collect_scripts(scope, path):
        scope = _table(scope, "Pixi activation scope")
        activation = _table(scope.get("activation", {}), "activation")
        if "scripts" in activation:
            scripts.append(
                {
                    "scope": path,
                    "scripts": [
                        _activation_script(root, script)
                        for script in _strings(
                            activation["scripts"], "activation scripts"
                        )
                    ],
                }
            )
        for selector, target in _table(scope.get("target", {}), "targets").items():
            collect_scripts(target, [*path, "target", selector])

    collect_scripts(pixi, [])
    for name, feature in _table(pixi.get("feature", {}), "features").items():
        collect_scripts(feature, ["feature", name])
    for name, environment in _table(
        pixi.get("environments", {}), "environments"
    ).items():
        if isinstance(environment, dict):
            collect_scripts(environment, ["environments", name])
    lock = None
    if lockfile_path.exists():
        with lockfile_path.open() as handle:
            lock = _table(yaml.safe_load(handle), "lockfile")
    workspace = _table(pixi.get("workspace", pixi.get("project", {})), "workspace")
    variants = [
        {"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()}
        for name in _strings(
            workspace.get("build-variants-files", []), "build variant files"
        )
    ]
    config = root / ".pixi" / "config.toml"
    return _canonical(
        {
            "schema": 1,
            "conservative-unnamed-platform": platform,
            "host": _host_virtual_packages(),
            "manifest": _ordered_document(document),
            "lock": _ordered_document(lock),
            "scripts": scripts,
            "build-variants-files": variants,
            "installation": _ordered_document(_read_toml(config))
            if config.exists()
            else None,
        }
    )


def _feature_projection(
    feature: dict,
    platform: str,
    subdir: str,
    frozen: bool,
    root: Path,
    *,
    ignored=frozenset(),
    solver_only=False,
) -> dict:
    unknown = feature.keys() - _DEPENDENCIES - _OPTIONS - _STRUCTURAL - ignored
    if unknown:
        raise ValueError(
            f"unsupported Pixi feature fields: {', '.join(sorted(unknown))}"
        )
    targets = _table(feature.get("target", {}), "targets")
    scopes = [feature] + [
        _table(target, f"target {selector}")
        for selector, target in targets.items()
        if _matches_target(selector, platform, subdir)
    ]
    result: dict[str, Any] = {}
    activation_env = {}
    activations = []
    scripts = []
    for scope in scopes:
        if scope is not feature:
            unknown = scope.keys() - _DEPENDENCIES - {"activation", "tasks"}
            if unknown:
                raise ValueError(
                    f"unsupported Pixi target fields: {', '.join(sorted(unknown))}"
                )
        if not frozen:
            for key in _DEPENDENCIES:
                if key in scope:
                    # Pixi applies matching targets in manifest declaration order.
                    result.setdefault(key, {}).update(_table(scope[key], key))
        if "activation" in scope and not solver_only:
            activation = _table(scope["activation"], "activation")
            if activation.keys() - {"env", "scripts"}:
                raise ValueError("unsupported Pixi activation fields")
            activations.append(_table(activation.get("env", {}), "activation env"))
            if "scripts" in activation:
                scripts = _strings(activation["scripts"], "activation scripts")
    # Pixi resolves target activation from last matching scope to first, keeping
    # the first value for each key and preserving variable expansion order.
    for variables in reversed(activations):
        for key, value in variables.items():
            activation_env.setdefault(key, value)
    if activation_env or scripts:
        result["activation"] = {
            "env": list(activation_env.items()),
            "scripts": [_activation_script(root, script) for script in scripts],
        }
    for key in _OPTIONS:
        if key in feature and (not frozen or key == "pypi-options"):
            result[key] = feature[key]
    if "system-requirements" in result:
        requirements = _table(result["system-requirements"], "system requirements")
        result["system-requirements"] = {
            key: value
            for key, value in requirements.items()
            if key not in {"linux", "libc", "macos", "windows"}
            or key in {"linux", "libc"}
            and subdir.startswith("linux-")
            or key == "macos"
            and subdir.startswith("osx-")
            or key == "windows"
            and subdir.startswith("win-")
        }
    if not frozen and "platforms" in feature:
        result["platforms"] = _selected_platform(feature["platforms"], platform)
    return result


def _dependency_group(groups: dict, name: str, seen=()) -> list:
    if name in seen:
        raise ValueError(f"cyclic Python dependency group {name!r}")
    if name not in groups:
        raise ValueError(f"unknown Python dependency group {name!r}")
    result = []
    for item in _sequence(groups[name], f"dependency group {name}"):
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict) and set(item) == {"include-group"}:
            result.extend(
                _dependency_group(groups, item["include-group"], (*seen, name))
            )
        else:
            raise ValueError(f"unsupported requirement in dependency group {name!r}")
    return sorted(result)


def _python_extra(extras: dict, name: str, project_name: str, seen=()) -> list:
    if name in seen:
        raise ValueError(f"cyclic Python optional dependency {name!r}")
    if name not in extras:
        raise ValueError(f"unknown Python optional dependency {name!r}")
    result = []
    for value in _strings(extras[name], "Python optional dependencies"):
        result.append(value)
        requirement = Requirement(value)
        if canonicalize_name(requirement.name) == canonicalize_name(project_name):
            for extra in sorted(requirement.extras):
                result.extend(_python_extra(extras, extra, project_name, (*seen, name)))
    return sorted(result)


def _workspace_dependencies(selected_features: list, workspace: dict) -> dict:
    pool = _table(workspace.get("dependencies", {}), "workspace dependencies")
    used = {}
    for feature in selected_features:
        for field in _DEPENDENCIES - {"pypi-dependencies"}:
            for name, spec in feature.get(field, {}).items():
                if isinstance(spec, dict) and "workspace" in spec:
                    if spec["workspace"] is not True or name not in pool:
                        raise ValueError(
                            f"invalid inherited workspace dependency {name!r}"
                        )
                    used[name] = pool[name]
    return used


def _manifest_projection(
    document: dict,
    env_name: str,
    platform: str,
    frozen: bool,
    root: Path,
    *,
    solver_only=False,
) -> dict:
    is_pyproject = "pixi" in document.get("tool", {})
    pixi = _table(
        document["tool"]["pixi"] if is_pyproject else document, "Pixi manifest"
    )
    project = _table(
        document.get("project", {}) if is_pyproject else {}, "Python project"
    )
    extras = _table(
        project.get("optional-dependencies", {}), "Python optional dependencies"
    )
    groups = _table(
        document.get("dependency-groups", {}) if is_pyproject else {},
        "Python dependency groups",
    )
    features = _table(pixi.get("feature", {}), "features")
    environments = _table(pixi.get("environments", {}), "environments")
    if env_name not in environments and env_name != "default":
        raise ValueError(f"unknown Pixi environment {env_name!r}")
    env = environments.get(env_name, [])
    env = (
        {"features": env}
        if isinstance(env, list)
        else _table(env, f"environment {env_name}")
    )
    feature_names = list(_strings(env.get("features", []), "environment features"))
    no_default = env.get("no-default-feature", False)
    if not isinstance(no_default, bool):
        raise ValueError("no-default-feature must be a boolean")
    if not no_default and "default" not in feature_names:
        feature_names.append("default")
    workspace = _table(pixi.get("workspace", pixi.get("project", {})), "workspace")
    platform_entries = _selected_platform(workspace.get("platforms", []), platform)
    subdir = next(
        (
            entry.get("platform", platform)
            for entry in platform_entries
            if isinstance(entry, dict)
        ),
        platform,
    )
    selected_features = []
    for name in feature_names:
        if name == "default":
            feature = pixi
            ignored = _ROOT_FIELDS
        else:
            if name not in features and name not in extras and name not in groups:
                raise ValueError(f"unknown Pixi feature {name!r}")
            feature = _table(features.get(name, {}), f"feature {name}")
            ignored = frozenset()
        selected = _feature_projection(
            feature,
            platform,
            subdir,
            frozen,
            root,
            ignored=ignored,
            solver_only=solver_only,
        )
        if not frozen and is_pyproject:
            if name == "default":
                selected["python"] = {
                    "requires-python": project.get("requires-python"),
                    "dependencies": sorted(
                        _strings(project.get("dependencies", []), "Python dependencies")
                    ),
                }
            else:
                if name in extras:
                    selected["python-extra"] = _python_extra(
                        extras, name, project.get("name", "")
                    )
                if name in groups:
                    selected["python-group"] = _dependency_group(groups, name)
        if selected:
            selected_features.append(selected)
    inline = _feature_projection(
        env,
        platform,
        subdir,
        frozen,
        root,
        ignored=_ENV_FIELDS,
        solver_only=solver_only,
    )
    if inline:
        selected_features.append(inline)
    result: dict[str, Any] = {"features": selected_features}
    if "pypi-options" in workspace:
        result["workspace-pypi-options"] = workspace["pypi-options"]
    if not frozen:
        result["workspace"] = {
            key: value
            for key, value in workspace.items()
            if key
            not in _WORKSPACE_METADATA | {"platforms", "dependencies", "pypi-options"}
        }
        result["workspace"]["platforms"] = platform_entries
        if "build-variants-files" in workspace:
            result["workspace"]["build-variants-files"] = [
                {
                    "path": filename,
                    "sha256": hashlib.sha256(
                        (root / filename).read_bytes()
                    ).hexdigest(),
                }
                for filename in _strings(
                    workspace["build-variants-files"], "build variant files"
                )
            ]
        inherited = _workspace_dependencies(selected_features, workspace)
        if inherited:
            result["workspace"]["dependencies"] = inherited
        if "solve-group" in env:
            result["solve-group"] = env["solve-group"]
            if not solver_only:
                result["solve-group-peers"] = {
                    name: _manifest_projection(
                        document, name, platform, False, root, solver_only=True
                    )
                    for name, peer in environments.items()
                    if name != env_name
                    and isinstance(peer, dict)
                    and peer.get("solve-group") == env["solve-group"]
                }
    config = root / ".pixi" / "config.toml"
    if config.exists():
        result["installation"] = {
            key: value
            for key, value in _read_toml(config).items()
            if key in _INSTALL_CONFIG
        }
    return result


def environment_fingerprint(
    manifest_path: Path,
    lockfile_path: Path,
    env_name: str,
    platform: str,
    frozen: bool,
) -> bytes:
    """Return canonical bytes without solving, installing, or updating any files."""
    try:
        document = _read_toml(manifest_path)
        if manifest_path.name == "pyproject.toml" and "pixi" not in _table(
            document.get("tool", {}), "tool"
        ):
            manifest = {"missing-pixi-table": True}
        else:
            candidates = _platform_candidates(document, env_name, platform)
            if candidates is None:
                return _unnamed_platform_fingerprint(
                    document, manifest_path.parent, lockfile_path, platform
                )
            if candidates and (
                len(candidates) > 1
                or any(
                    isinstance(candidate["declaration"], dict)
                    for candidate in candidates
                )
            ):
                return _canonical(
                    {
                        "schema": 1,
                        "platform": candidates[0]["subdir"],
                        "host": _host_virtual_packages(),
                        "candidates": [
                            {
                                **candidate,
                                "manifest": _manifest_projection(
                                    document,
                                    env_name,
                                    candidate["name"],
                                    frozen,
                                    manifest_path.parent,
                                ),
                                "lock": _lock_projection(
                                    lockfile_path, env_name, candidate["name"]
                                ),
                            }
                            for candidate in candidates
                        ],
                    }
                )
            manifest = _manifest_projection(
                document, env_name, platform, frozen, manifest_path.parent
            )
        return _canonical(
            {
                "schema": 1,
                "platform": platform,
                "manifest": manifest,
                "lock": _lock_projection(lockfile_path, env_name, platform),
            }
        )
    except (OSError, ValueError, TypeError, yaml.YAMLError) as error:
        raise WorkflowError(
            f"Cannot fingerprint Pixi environment '{env_name}' from {manifest_path}: {error}"
        ) from error
