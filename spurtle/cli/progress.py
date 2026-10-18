"""Shared progress reporting for CLI commands.

Provides factory functions that build ``on_progress`` callbacks for
:func:`~spurtle.operations.install.collect_install`.
"""

from __future__ import annotations

from collections.abc import Callable

import typer


def make_install_progress(
    *,
    json_output: bool,
    on_manifest_loaded: Callable[[int], None] | None = None,
) -> Callable[[str, object], None]:
    """Return a progress callback for install streaming.

    Handles ``action_started``, ``action_completed``, and
    ``manifest_loaded`` events, printing human-readable output unless
    *json_output* is ``True``.

    Args:
        json_output: Suppress human-readable output when ``True``.
        on_manifest_loaded: Optional callback invoked with the action
            count when the manifest is loaded.
    """
    from porringer.schema import (
        ActionCompletedEvent,
        ActionStartedEvent,
        ManifestLoadedEvent,
    )

    def _on_progress(stage: str, event: object) -> None:
        if stage == "manifest_loaded" and isinstance(event, ManifestLoadedEvent):
            action_count = len(event.manifest.actions)
            if on_manifest_loaded is not None:
                on_manifest_loaded(action_count)
            if not json_output:
                typer.echo(f"Manifest loaded: {action_count} action(s)")
        elif stage == "action_started" and isinstance(event, ActionStartedEvent):
            if not json_output:
                typer.echo(f"  Starting: {event.action.description}")
        elif stage == "action_completed" and isinstance(event, ActionCompletedEvent):
            if not json_output:
                status = "OK" if event.result.success else "FAILED"
                if event.result.skipped:
                    status = "SKIPPED"
                typer.echo(f"  {status}: {event.action.description}")

    return _on_progress
