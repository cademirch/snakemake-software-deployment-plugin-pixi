import shutil
import subprocess
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
WORKSPACES = [
    TESTS_DIR / "test_workspace",
    TESTS_DIR / "test_workspace_pypi",
]

PIXI_AVAILABLE = shutil.which("pixi") is not None


def _ensure_lockfile(workspace: Path) -> None:
    """Run `pixi install` in the workspace if pixi.lock doesn't exist."""
    lockfile = workspace / "pixi.lock"
    if not lockfile.exists():
        if not PIXI_AVAILABLE:
            pytest.skip(
                f"pixi not installed and {lockfile} doesn't exist -- "
                "run 'pixi install' in test workspaces first"
            )
        subprocess.run(
            ["pixi", "install"],
            cwd=workspace,
            check=True,
            capture_output=True,
        )


@pytest.fixture()
def require_pixi_workspaces():
    """Ensure all test workspaces have lockfiles. Used by integration tests."""
    for ws in WORKSPACES:
        _ensure_lockfile(ws)
