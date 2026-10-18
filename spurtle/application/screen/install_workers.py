"""Async worker coroutines for install and preview operations.

Contains ``run_install``, ``run_preview``, and
supporting helpers that stream porringer events back to the GUI via
callbacks.  All execution delegates to the operations layer to
avoid direct porringer API coupling.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from porringer.api import API
from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.schema import (
    ActionCompletedEvent,
    ActionProgressEvent,
    ActionStartedEvent,
    SetupResults,
    SyncPlan,
)

from spurtle.application.screen.schema import (
    InstallCallbacks,
    PreviewConfig,
)
from spurtle.application.uri import safe_rmtree
from spurtle.operations.install import collect_install, preview_manifest_stream
from spurtle.operations.schema import (
    PreviewEvent,
    PreviewManifestParsed,
)

logger = logging.getLogger(__name__)


def _progress_router(cb: InstallCallbacks) -> Callable[[str, object], None]:
    """Route operations-layer ``(stage, event)`` pairs to GUI callbacks."""

    def _on_progress(stage: str, event: object) -> None:
        if (
            stage == "action_started"
            and isinstance(event, ActionStartedEvent)
            and cb.on_action_started is not None
        ):
            cb.on_action_started(event.action)
        elif (
            stage == "sub_progress"
            and isinstance(event, ActionProgressEvent)
            and cb.on_sub_progress is not None
        ):
            cb.on_sub_progress(event.action, event.progress)
        elif (
            stage == "action_completed"
            and isinstance(event, ActionCompletedEvent)
            and cb.on_progress is not None
        ):
            cb.on_progress(event.action, event.result)

    return _on_progress


# ---------------------------------------------------------------------------
# run_install — execute setup actions via operations layer
# ---------------------------------------------------------------------------


async def run_install(
    porringer: API,
    plan: SyncPlan,
    callbacks: InstallCallbacks | None = None,
    *,
    plugins: DiscoveredPlugins | None = None,
) -> SetupResults:
    """Run a previewed *plan* via the operations layer and stream progress.

    Delegates to :func:`~spurtle.operations.install.collect_install`
    and routes progress events to GUI callbacks.

    Raises:
        PlanStaleError: If the manifest changed since the preview.
    """
    cb = callbacks or InstallCallbacks()

    return await collect_install(
        porringer,
        plan=plan,
        discovered=plugins,
        on_progress=_progress_router(cb),
    )


# ---------------------------------------------------------------------------
# run_preview — dry-run preview of a manifest
# ---------------------------------------------------------------------------


async def run_preview(
    porringer: API,
    url: str,
    *,
    config: PreviewConfig | None = None,
    on_event: Callable[[PreviewEvent], object] | None = None,
    plugins: DiscoveredPlugins | None = None,
) -> None:
    """Download a manifest and perform a dry-run preview.

    Delegates to :func:`preview_manifest_stream` in the operations
    layer, then yields each :data:`PreviewEvent` to *on_event*.
    """
    logger.info("run_preview starting for: %s", url)
    cfg = config or PreviewConfig()
    temp_dir: str | None = None
    try:
        async for event in preview_manifest_stream(
            porringer,
            url,
            project_directory=cfg.project_directory,
            prerelease_packages=cfg.prerelease_packages,
            discovered=plugins,
        ):
            if isinstance(event, PreviewManifestParsed):
                temp_dir = event.temp_dir or None

            if on_event is not None:
                on_event(event)

    except asyncio.CancelledError:
        if temp_dir:
            safe_rmtree(temp_dir)
        raise
    except Exception:
        if temp_dir:
            safe_rmtree(temp_dir)
        raise
