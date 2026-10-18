"""Shared startup preamble for frozen and CLI entry points.

Encapsulates the one-time initialisation that both
:mod:`spurtle.application.bootstrap` (PyInstaller / MSIX) and
:mod:`spurtle.application.qt` (CLI / dev-script) perform
before the GUI event loop starts:

1. Seed user config from the build config (one-time propagation).
2. Import projects from Porringer's old directory cache (one-time).
3. Register the ``spurtle://`` URI protocol handler (no-op under MSIX).
4. Synchronise the Windows auto-startup state (no-op under MSIX).

Heavy dependencies (PySide6, porringer) are **not** imported here so
that the bootstrap path can call this before loading them.
"""

import logging
import sys

from spurtle.protocol import register_protocol
from spurtle.resolution import (
    import_legacy_projects,
    resolve_config,
    seed_user_config_from_build,
)
from spurtle.startup import sync_startup

logger = logging.getLogger(__name__)


class _PreambleState:
    """Module-level mutable state (avoids ``global`` statements)."""

    done: bool = False


def run_startup_preamble(exe_path: str | None = None) -> None:
    """Run the shared startup preamble for non-dev-mode launches.

    Both the frozen entry point
    (:mod:`~spurtle.application.bootstrap`) and the CLI entry
    point (:func:`~spurtle.application.qt.application`) call
    this unconditionally.  An internal guard ensures the work only
    executes once per process.

    Under MSIX packaging the protocol and startup registrations are
    declarative and the corresponding calls become no-ops.

    Args:
        exe_path: Absolute path to the application executable.  Defaults
            to ``sys.executable`` when not supplied.
    """
    if _PreambleState.done:
        return
    _PreambleState.done = True

    if exe_path is None:
        exe_path = sys.executable

    seed_user_config_from_build()
    import_legacy_projects()

    # Both calls are MSIX-aware and become no-ops when packaged.
    register_protocol(exe_path)

    config = resolve_config()
    sync_startup(exe_path, auto_start=config.auto_start)

    logger.info("Startup preamble complete (auto_start=%s)", config.auto_start)
