"""Builders for real Porringer inspection reports used by tests."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, create_autospec

from porringer.api import API
from porringer.backend.command.core.inspection import action_snapshot
from porringer.backend.command.package import PackageCommands
from porringer.backend.command.plugin import PluginCommands
from porringer.backend.command.sync import SyncCommands
from porringer.backend.command.tool import ToolCommands
from porringer.core.schema import PluginKind
from porringer.schema import (
    ActionInspectedEvent,
    ActionInspection,
    ActionRef,
    DiscoveredPluginEntry,
    DiscoveredPluginSnapshot,
    FailedPathInspection,
    InspectionMode,
    InspectionStatus,
    ManifestInspection,
    ManifestLoadedEvent,
    ManifestMetadata,
    ManifestMetadataSnapshot,
    PluginCapability,
    ProgressEvent,
    SetupAction,
    SetupActionResult,
    SetupParameters,
    SetupResults,
    SkipReason,
    SyncInspectionReport,
    SyncPlan,
)


def spec_api() -> MagicMock:
    """Build an ``API`` mock whose sub-command objects are autospecced."""
    api = create_autospec(API, instance=True)
    api.sync = create_autospec(SyncCommands, instance=True)
    api.tool = create_autospec(ToolCommands, instance=True)
    api.extension = create_autospec(PluginCommands, instance=True)
    api.package = create_autospec(PackageCommands, instance=True)
    return api


def inspected(
    action: SetupAction,
    *,
    index: int = 0,
    manifest_index: int = 0,
    status: InspectionStatus = InspectionStatus.NEEDED,
    success: bool = True,
    skipped: bool = False,
    skip_reason: SkipReason | None = None,
    installed_version: str | None = None,
    available_version: str | None = None,
    message: str | None = None,
) -> ActionInspection:
    """Build an ``ActionInspection`` for *action* at *index*."""
    ref = ActionRef.from_indices(manifest_index, index)
    return ActionInspection(
        index=index,
        ref=ref,
        action_id=ref.action_id,
        manifest_index=manifest_index,
        action_index=index,
        action=action_snapshot(action, index, ref=ref),
        status=status,
        success=success,
        skipped=skipped,
        skip_reason=skip_reason.name if skip_reason is not None else None,
        installed_version=installed_version,
        available_version=available_version,
        message=message,
    )


def manifest(
    actions: Iterable[ActionInspection] = (),
    *,
    index: int = 0,
    manifest_path: Path | None = None,
    root_directory: Path | None = None,
    name: str | None = None,
    description: str | None = None,
) -> ManifestInspection:
    """Build a ``ManifestInspection`` holding *actions*."""
    metadata = (
        ManifestMetadataSnapshot(name=name, description=description)
        if name or description
        else None
    )
    return ManifestInspection(
        index=index,
        manifest_path=manifest_path,
        root_directory=root_directory,
        metadata=metadata,
        actions=tuple(actions),
    )


def plugin(
    name: str,
    *,
    available: bool = True,
    capabilities: Iterable[PluginCapability] = (),
    kind: str = "packages",
) -> DiscoveredPluginSnapshot:
    """Build a ``DiscoveredPluginSnapshot``."""
    return DiscoveredPluginSnapshot(
        name=name,
        available=available,
        capabilities=tuple(sorted(cap.name.lower() for cap in capabilities)),
        kind=kind,
    )


def report(
    *manifests: ManifestInspection,
    plugins: Sequence[DiscoveredPluginSnapshot] = (),
    failed_paths: Sequence[FailedPathInspection] = (),
    mode: InspectionMode = InspectionMode.COMPLETE,
) -> SyncInspectionReport:
    """Build a ``SyncInspectionReport``."""
    return SyncInspectionReport(
        inspection_mode=mode,
        manifests=manifests,
        plugins=tuple(plugins),
        failed_paths=tuple(failed_paths),
    )


def mock_inspect(
    porringer: MagicMock,
    fast: SyncInspectionReport,
    complete: SyncInspectionReport | None = None,
) -> list[SetupParameters]:
    """Route ``sync.inspect`` by inspection mode and record the parameters.

    Also stubs ``discover_plugins`` so callers without pre-discovered
    plugins can await it.
    """
    calls: list[SetupParameters] = []

    async def _inspect(
        params: SetupParameters, **_kwargs: object
    ) -> SyncInspectionReport:
        calls.append(params)
        if params.inspection_mode == InspectionMode.FAST:
            return fast
        return complete if complete is not None else fast

    if isinstance(porringer.sync.inspect, AsyncMock):
        porringer.sync.inspect.side_effect = _inspect
    else:
        porringer.sync.inspect = AsyncMock(side_effect=_inspect)
    porringer.discover_plugins = AsyncMock(return_value=MagicMock())
    return calls


def mock_sync_run(
    porringer: MagicMock,
    events: Sequence[ProgressEvent] = (),
    *,
    error: BaseException | None = None,
) -> list[SetupParameters | SyncPlan]:
    """Make ``sync.run`` deliver *events* through ``on_event`` and record its parameters or plan."""
    calls: list[SetupParameters | SyncPlan] = []

    async def _run(
        params: SetupParameters | None = None,
        *,
        plan: SyncPlan | None = None,
        on_event: Callable[[ProgressEvent], None] | None = None,
        **_kwargs: object,
    ) -> None:
        recorded = plan if plan is not None else params
        assert recorded is not None
        calls.append(recorded)
        for event in events:
            if on_event is not None:
                on_event(event)
        if error is not None:
            raise error

    porringer.sync.run = _run
    return calls


def plugin_entry(
    name: str,
    *,
    available: bool = True,
    capabilities: Iterable[PluginCapability] = (),
    kind: PluginKind = PluginKind.PACKAGE,
) -> DiscoveredPluginEntry:
    """Build a ``DiscoveredPluginEntry``."""
    return DiscoveredPluginEntry(
        name=name, available=available, capabilities=frozenset(capabilities), kind=kind
    )


def loaded(
    actions: Sequence[SetupAction] = (),
    *,
    index: int = 0,
    manifest_path: Path | None = None,
    root_directory: Path | None = None,
    name: str | None = None,
    description: str | None = None,
) -> ManifestLoadedEvent:
    """Build the ``ManifestLoadedEvent`` for a manifest holding *actions*."""
    metadata = (
        ManifestMetadata(name=name, description=description)
        if name or description
        else None
    )
    preview = SetupResults(
        actions=list(actions),
        action_indices=list(range(len(actions))),
        manifest_index=index,
        manifest_path=manifest_path,
        root_directory=root_directory,
        metadata=metadata,
    )
    return ManifestLoadedEvent(manifest=preview, manifest_index=index)


def checked(
    action: SetupAction,
    *,
    index: int = 0,
    manifest_index: int = 0,
    status: InspectionStatus = InspectionStatus.NEEDED,
    success: bool = True,
    skipped: bool = False,
    skip_reason: SkipReason | None = None,
    installed_version: str | None = None,
    available_version: str | None = None,
) -> ActionInspectedEvent:
    """Build the ``ActionInspectedEvent`` for *action*'s finished check."""
    inspection = inspected(
        action,
        index=index,
        manifest_index=manifest_index,
        status=status,
        success=success,
        skipped=skipped,
        skip_reason=skip_reason,
        installed_version=installed_version,
        available_version=available_version,
    )
    result = SetupActionResult(
        action=action,
        success=success,
        skipped=skipped,
        skip_reason=skip_reason,
        installed_version=installed_version,
        available_version=available_version,
        ref=inspection.ref,
    )
    return ActionInspectedEvent(action=action, result=result, inspection=inspection)


def mock_plan(
    porringer: MagicMock, events: Sequence[ProgressEvent] = ()
) -> list[SetupParameters]:
    """Make ``sync.plan`` deliver *events* through ``on_event`` and record the parameters.

    The returned plan holds the manifests of the ``ManifestLoadedEvent``
    items.  Also stubs ``discover_plugins``.
    """
    calls: list[SetupParameters] = []

    async def _plan(
        params: SetupParameters,
        *,
        on_event: Callable[[ProgressEvent], None] | None = None,
        **_kwargs: object,
    ) -> SyncPlan:
        calls.append(params)
        for event in events:
            if on_event is not None:
                on_event(event)
        manifests = tuple(
            event.manifest for event in events if isinstance(event, ManifestLoadedEvent)
        )
        return SyncPlan(
            parameters=params, report=SyncInspectionReport(), manifests=manifests
        )

    if isinstance(porringer.sync.plan, AsyncMock):
        porringer.sync.plan.side_effect = _plan
    else:
        porringer.sync.plan = AsyncMock(side_effect=_plan)
    porringer.discover_plugins = AsyncMock(return_value=MagicMock())
    return calls
