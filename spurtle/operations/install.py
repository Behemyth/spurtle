"""Install / preview operations.

Pure async functions for previewing and executing manifest installs.
No Qt, no signals — progress is reported via streaming async iterators.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from porringer.schema import (
    ActionCompletedEvent,
    ActionInspectedEvent,
    ActionProgressEvent,
    ActionStartedEvent,
    DownloadParameters,
    InspectionMode,
    ManifestFailedEvent,
    ManifestLoadedEvent,
    PluginsDiscoveredEvent,
    ProgressEvent,
    SetupAction,
    SetupActionResult,
    SetupParameters,
    SetupResults,
    SyncPlan,
    SyncStrategy,
)

from spurtle.application.uri import resolve_local_path, safe_rmtree
from spurtle.operations.inspection import EventStream, run_events
from spurtle.operations.schema import (
    ActionInfo,
    PreviewActionChecked,
    PreviewEvent,
    PreviewManifestParsed,
    PreviewPlanned,
    PreviewPluginsQueried,
    PreviewReady,
    PreviewResult,
    SetupProfile,
    resolve_action_status,
)

if TYPE_CHECKING:
    from porringer.api import API
    from porringer.backend.command.core.discovery import DiscoveredPlugins

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


def validate_profile_url(url: str) -> None:
    """Validate that *url* is an HTTPS URL.

    Raises:
        ValueError: If the URL is not HTTPS.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https":
        msg = f"Only HTTPS URLs are allowed for profiles and manifests, got: {url}"
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# Manifest resolution
# ---------------------------------------------------------------------------


async def resolve_manifest_path(url: str) -> tuple[Path, str | None]:
    """Resolve *url* to a local manifest path, downloading if remote.

    Returns:
        ``(manifest_path, temp_dir)`` — *temp_dir* is ``None`` for local
        manifests and a temporary directory string for downloads.

    Raises:
        FileNotFoundError: If a local path does not exist.
        RuntimeError: If the download fails.
    """
    from porringer.api import API as _API

    local_path = resolve_local_path(url)

    if local_path is not None:
        if not local_path.exists():
            msg = f"Manifest not found:\n{local_path}"
            raise FileNotFoundError(msg)
        return local_path, None

    temp_dir = tempfile.mkdtemp(prefix="spurtle_install_")
    dest = Path(temp_dir) / "porringer.json"

    params = DownloadParameters(url=url, destination=dest, timeout=3)
    result = await _API.download(params)

    if not result.success:
        safe_rmtree(temp_dir)
        msg = f"Failed to download manifest:\n{result.message}"
        raise RuntimeError(msg)

    return dest, temp_dir


async def resolve_profile(url: str) -> tuple[SetupProfile, str | None]:
    """Download and parse a remote setup profile.

    Validates that *url* and all manifest URLs within the profile are
    HTTPS.

    Args:
        url: HTTPS URL of the profile JSON file.

    Returns:
        ``(profile, temp_dir)`` — *temp_dir* is the temporary directory
        used for the download.

    Raises:
        ValueError: If *url* or any manifest URL is not HTTPS.
        RuntimeError: If the download or parse fails.
    """
    import json

    from porringer.api import API as _API

    validate_profile_url(url)

    temp_dir = tempfile.mkdtemp(prefix="spurtle_profile_")
    dest = Path(temp_dir) / "profile.json"

    params = DownloadParameters(url=url, destination=dest, timeout=3)
    result = await _API.download(params)

    if not result.success:
        safe_rmtree(temp_dir)
        msg = f"Failed to download profile:\n{result.message}"
        raise RuntimeError(msg)

    try:
        data = json.loads(dest.read_text(encoding="utf-8"))
        profile = SetupProfile.model_validate(data)
    except Exception as exc:
        safe_rmtree(temp_dir)
        msg = f"Failed to parse profile from {url}"
        raise RuntimeError(msg) from exc

    for manifest_url in profile.manifests:
        validate_profile_url(manifest_url)

    return profile, temp_dir


# ---------------------------------------------------------------------------
# Context-manager wrappers for automatic temp-dir cleanup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def open_manifest(url: str) -> AsyncGenerator[Path]:
    """Resolve *url* to a local manifest path, cleaning up on exit.

    Usage::

        async with open_manifest(url) as manifest_path:
            ...
    """
    manifest_path, temp_dir = await resolve_manifest_path(url)
    try:
        yield manifest_path
    finally:
        if temp_dir is not None:
            safe_rmtree(temp_dir)


@asynccontextmanager
async def open_profile(url: str) -> AsyncGenerator[SetupProfile]:
    """Download, parse, and validate a remote setup profile.

    The temporary download directory is cleaned up automatically
    when the context manager exits.

    Usage::

        async with open_profile(url) as profile:
            for manifest_url in profile.manifests:
                ...
    """
    profile, temp_dir = await resolve_profile(url)
    try:
        yield profile
    finally:
        if temp_dir is not None:
            safe_rmtree(temp_dir)


# ---------------------------------------------------------------------------
# Streaming preview
# ---------------------------------------------------------------------------


async def preview_manifest_stream(
    porringer: API,
    url: str,
    *,
    project_directory: Path | None = None,
    prerelease_packages: set[str] | None = None,
    discovered: DiscoveredPlugins | None = None,
    resolve: bool = True,
) -> AsyncIterator[PreviewEvent]:
    """Stream preview events for a manifest.

    Resolves the manifest (downloading if remote), then plans it with
    Porringer: the actions are listed as soon as the manifest loads, and
    each action's status follows as its check finishes.  The final
    :class:`PreviewPlanned` carries the plan, which an install runs
    as previewed.

    Per-action results include a pre-resolved ``status`` string
    via :func:`resolve_action_status`.

    Args:
        porringer: The porringer API instance.
        url: Manifest URL or local path.
        project_directory: Optional project directory for the preview.
        prerelease_packages: Optional prerelease overrides.
        discovered: Pre-discovered plugins.
        resolve: Whether to resolve the URL to a local path first
            (downloading if remote).  When ``False`` the raw *url*
            is passed directly to porringer.

    Yields:
        :data:`PreviewEvent` instances as they arrive.
    """
    temp_dir: str | None = None
    manifest_path: Path | None
    if resolve:
        manifest_path, temp_dir = await resolve_manifest_path(url)
    else:
        manifest_path = resolve_local_path(url)

    manifest_path_str = str(manifest_path) if manifest_path is not None else url
    temp_dir_str = temp_dir or ""

    try:
        params = SetupParameters(
            paths=[manifest_path or url],
            project_directory=project_directory,
            prerelease_packages=prerelease_packages,
        )
        plugins = (
            discovered if discovered is not None else await porringer.discover_plugins()
        )
        stream = EventStream(
            lambda on_event: porringer.sync.plan(
                params, plugins=plugins, on_event=on_event
            )
        )

        failure: str | None = None
        manifest_index: int | None = None
        async for event in stream.events():
            if isinstance(event, PluginsDiscoveredEvent):
                yield PreviewPluginsQueried(
                    availability={
                        entry.name: entry.available
                        for entry in event.discovered_plugins
                    },
                    capabilities={
                        entry.name: entry.capabilities
                        for entry in event.discovered_plugins
                    },
                )
            elif isinstance(event, ManifestFailedEvent):
                failure = failure or event.failed_path[1]
            elif isinstance(event, ManifestLoadedEvent) and manifest_index is None:
                manifest_index = event.manifest_index
                loaded = event.manifest
                effective_path = (
                    str(loaded.manifest_path)
                    if loaded.manifest_path is not None
                    else manifest_path_str
                )
                yield PreviewManifestParsed(
                    manifest=loaded, manifest_path=effective_path, temp_dir=temp_dir_str
                )
                yield PreviewReady(
                    manifest=loaded, manifest_path=effective_path, temp_dir=temp_dir_str
                )
            elif (
                isinstance(event, ActionInspectedEvent)
                and event.inspection.manifest_index == manifest_index
            ):
                yield PreviewActionChecked(
                    index=event.inspection.index,
                    result=event.result,
                    status=resolve_action_status(event.result, event.action),
                )

        plan = stream.result
        if manifest_index is None or plan is None:
            if plan is not None:
                plan.discard()
            if failure is not None:
                raise RuntimeError(failure)
            return
        yield PreviewPlanned(plan=plan)

    except BaseException:
        if temp_dir:
            safe_rmtree(temp_dir)
        raise


# ---------------------------------------------------------------------------
# Batch preview (convenience wrapper)
# ---------------------------------------------------------------------------


async def preview_manifest(
    porringer: API,
    url: str,
    *,
    project_directory: Path | None = None,
    prerelease_packages: set[str] | None = None,
    discovered: DiscoveredPlugins | None = None,
) -> PreviewResult:
    """Perform a dry-run preview of a manifest.

    Consumes :func:`preview_manifest_stream` and returns a single
    :class:`PreviewResult` with all resolved actions.
    """
    actions: list[ActionInfo] = []
    project_name = ""
    description = ""
    manifest_key = url

    async for event in preview_manifest_stream(
        porringer,
        url,
        project_directory=project_directory,
        prerelease_packages=prerelease_packages,
        discovered=discovered,
        resolve=False,
    ):
        if isinstance(event, PreviewManifestParsed):
            meta = event.manifest.metadata
            if meta:
                project_name = meta.name or ""
                description = meta.description or ""
            manifest_key = str(event.manifest.manifest_path or url)

        elif isinstance(event, PreviewReady):
            for act in event.manifest.actions:
                actions.append(
                    ActionInfo(
                        description=act.description,
                        kind=act.kind.name if act.kind else None,
                        package=str(act.package.name) if act.package else None,
                        constraint=act.package.constraint if act.package else None,
                        installer=act.installer,
                    )
                )

        elif isinstance(event, PreviewPlanned):
            event.plan.discard()

    return PreviewResult(
        manifest_key=manifest_key,
        project_name=project_name,
        description=description,
        actions=actions,
    )


# ---------------------------------------------------------------------------
# Execute install
# ---------------------------------------------------------------------------


async def execute_install(
    porringer: API,
    manifest_path: Path | None = None,
    *,
    plan: SyncPlan | None = None,
    project_directory: Path | None = None,
    strategy: SyncStrategy = SyncStrategy.MINIMAL,
    prerelease_packages: set[str] | None = None,
    discovered: DiscoveredPlugins | None = None,
) -> AsyncIterator[tuple[str, ProgressEvent]]:
    """Execute setup actions and yield ``(stage, event)`` tuples.

    Yields ``("action_started", event)``, ``("sub_progress", event)``,
    ``("action_completed", event)``, etc. so callers can wire up
    progress tracking without coupling to porringer event kinds.

    Args:
        porringer: The porringer API instance.
        manifest_path: Path to the manifest file, loaded afresh.
        plan: A previewed plan to run instead of *manifest_path*; the
            remaining options are the plan's own.
        project_directory: Optional project directory override.
        strategy: Sync strategy (MINIMAL, LATEST, EXACT).
        prerelease_packages: Optional prerelease overrides.
        discovered: Pre-discovered plugins.
    """
    if plan is not None:
        events = run_events(porringer, plan=plan, plugins=discovered)
    else:
        params = SetupParameters(
            paths=[manifest_path] if manifest_path is not None else None,
            project_directory=project_directory,
            strategy=strategy,
            prerelease_packages=prerelease_packages,
        )
        events = run_events(porringer, params, plugins=discovered)

    async for event in events:
        if isinstance(event, ManifestLoadedEvent):
            yield ("manifest_loaded", event)
        elif isinstance(event, ActionStartedEvent):
            yield ("action_started", event)
        elif isinstance(event, ActionProgressEvent):
            yield ("sub_progress", event)
        elif isinstance(event, ActionCompletedEvent):
            yield ("action_completed", event)
        else:
            yield ("other", event)


# ---------------------------------------------------------------------------
# Batch collect helpers — consume a stream and return SetupResults
# ---------------------------------------------------------------------------

InstallProgressCallback = Callable[[str, ProgressEvent], None]


async def collect_install(
    porringer: API,
    manifest_path: Path | None = None,
    *,
    plan: SyncPlan | None = None,
    project_directory: Path | None = None,
    strategy: SyncStrategy = SyncStrategy.MINIMAL,
    prerelease_packages: set[str] | None = None,
    discovered: DiscoveredPlugins | None = None,
    on_progress: InstallProgressCallback | None = None,
) -> SetupResults:
    """Execute install actions and return collected results.

    Consumes :func:`execute_install` internally, collecting action
    results and optionally forwarding each ``(stage, event)`` to
    *on_progress* for UI updates.
    """
    actions: list[SetupAction] = []
    collected: list[SetupActionResult] = []
    manifest_result: SetupResults | None = None

    async for stage, event in execute_install(
        porringer,
        manifest_path,
        plan=plan,
        project_directory=project_directory,
        strategy=strategy,
        prerelease_packages=prerelease_packages,
        discovered=discovered,
    ):
        if stage == "manifest_loaded" and isinstance(event, ManifestLoadedEvent):
            manifest_result = event.manifest
            actions = list(event.manifest.actions)
        elif stage == "action_completed" and isinstance(event, ActionCompletedEvent):
            collected.append(event.result)

        if on_progress is not None:
            on_progress(stage, event)

    return SetupResults(
        actions=actions,
        results=collected,
        manifest_path=manifest_result.manifest_path if manifest_result else None,
        metadata=manifest_result.metadata if manifest_result else None,
    )


# ---------------------------------------------------------------------------
# Manifest action loading (lightweight, no dry-run checking)
# ---------------------------------------------------------------------------


async def load_manifest_actions(
    porringer: API,
    manifest_path: Path,
    *,
    project_directory: Path | None = None,
    discovered: DiscoveredPlugins | None = None,
) -> list[SetupAction]:
    """Load the action list from a manifest without presence checks.

    Args:
        porringer: The porringer API instance.
        manifest_path: Path to the manifest file.
        project_directory: Optional project directory override.
        discovered: Pre-discovered plugins.

    Returns:
        The list of :class:`SetupAction` entries from the manifest.
    """
    params = SetupParameters(
        paths=[manifest_path],
        project_directory=project_directory,
        inspection_mode=InspectionMode.FAST,
    )
    plan = await porringer.sync.plan(params, plugins=discovered)
    plan.discard()
    if not plan.manifests:
        return []
    return list(plan.manifests[0].actions)
