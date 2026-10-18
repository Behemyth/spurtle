"""Development setup, run automatically as a PDM ``post_install`` hook.

Registers the repository ``examples/`` directories as projects in the
dev-mode user config, and links a sibling ``../porringer`` checkout into
the environment as an editable install when one exists.

Porringer is deliberately not imported here: the hook must work against
whichever Porringer version is installed, including none.
"""

import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path

from spurtle.config import load_user_config, save_user_config, set_dev_mode

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES_DIR = _REPO_ROOT / "examples"
_PORRINGER_DIR = _REPO_ROOT.parent / "porringer"


def _register_examples() -> None:
    """Add every ``examples/`` subdirectory to the dev config and prune stale ones."""
    set_dev_mode(True)
    config = load_user_config()

    examples_root = _EXAMPLES_DIR.resolve()
    example_dirs = sorted(
        str(child.resolve()) for child in _EXAMPLES_DIR.iterdir() if child.is_dir()
    )
    current = config.projects or []

    kept: list[str] = []
    for entry in current:
        path = Path(entry).resolve()
        if examples_root in path.parents and str(path) not in example_dirs:
            print(f"  - {path.name} (removed)")
            continue
        kept.append(entry)

    known = {str(Path(entry).resolve()) for entry in kept}
    added = [path for path in example_dirs if path not in known]
    for path in added:
        print(f"  + {Path(path).name} ({path})")

    projects = kept + added
    if projects != current:
        save_user_config(config.model_copy(update={"projects": projects}))
    elif example_dirs:
        print(f"  {len(example_dirs)} example project(s) already registered")


def _installed_porringer() -> str:
    try:
        return importlib.metadata.version("porringer")
    except importlib.metadata.PackageNotFoundError:
        return "none"


def _link_porringer() -> None:
    """Install ``../porringer`` as editable, replacing the locked distribution."""
    # Stopgap until a Porringer tooling profile installs sibling editables.
    if not (_PORRINGER_DIR / "pyproject.toml").is_file():
        print(
            f"  using locked porringer {_installed_porringer()} (no checkout at {_PORRINGER_DIR})"
        )
        return

    python = sys.executable
    has_pip = (
        subprocess.run(
            [python, "-m", "pip", "--version"], capture_output=True, check=False
        ).returncode
        == 0
    )
    if not has_pip:
        subprocess.run(
            [python, "-m", "ensurepip", "--default-pip"],
            capture_output=True,
            check=False,
        )

    result = subprocess.run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            "--editable",
            str(_PORRINGER_DIR),
        ],
        check=False,
    )
    if result.returncode != 0:
        print(
            f"  warning: failed to link {_PORRINGER_DIR}; using porringer {_installed_porringer()}"
        )
        return

    print(f"  linked porringer HEAD from {_PORRINGER_DIR} ({_installed_porringer()})")


def main() -> None:
    """Run the dev setup steps; skipped entirely on CI."""
    if os.environ.get("CI"):
        return

    if _EXAMPLES_DIR.is_dir():
        _register_examples()
    _link_porringer()


if __name__ == "__main__":
    main()
