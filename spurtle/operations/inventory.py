"""Inventory-backed Tools list and the actions behind each row.

Rows come from Porringer's read-only inventory: every package installed
in each interpreter (pip, uv) and in each global store (pipx, winget),
plus the runtimes themselves. Each row is then marked:

* managed — recorded in the user's global manifest;
* project — declared by a registered project's manifest (Spurtle never
  edits project manifests, so those rows are read-only here);
* unmanaged — installed, but no manifest mentions it.

Every action is one Porringer call that changes what is installed and
updates the global manifest together.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from packaging.utils import canonicalize_name
from porringer.core.plugin_schema.runtime import runtime_tag_matches
from porringer.schema import (
    InventoryStatus,
    ManagedToolReport,
    ManifestEntry,
    PackageSpec,
    RangeStrategy,
    RuntimeRecord,
    RuntimeRemovalCheck,
)
from porringer.schema.plugin import PluginKind

from spurtle.operations.schema import UpdateResult
from spurtle.operations.tool import declared_packages

if TYPE_CHECKING:
    from porringer.api import API
    from porringer.backend.command.core.discovery import DiscoveredPlugins

logger = logging.getLogger(__name__)


class RowState(enum.Enum):
    """Who manages a Tools row."""

    MANAGED = "managed"
    PROJECT = "project"
    UNMANAGED = "unmanaged"


@dataclass(slots=True)
class ToolRow:
    """One package, tool or runtime on this machine."""

    plugin: str
    name: str
    kind: PluginKind
    ecosystem: str
    version: str = ""
    latest_version: str | None = None
    runtime: RuntimeRecord | None = None
    state: RowState = RowState.UNMANAGED
    entry: ManifestEntry | None = None
    """The global manifest entry, for managed rows."""
    project_paths: list[str] = field(default_factory=list)
    installed: bool = True

    @property
    def signal_key(self) -> str:
        """Key the Tools view groups and addresses rows by."""
        return (
            f"{self.plugin}:{self.runtime.tag}"
            if self.runtime is not None
            else self.plugin
        )


@dataclass(slots=True)
class ToolInventory:
    """Everything the Tools view shows."""

    rows: list[ToolRow] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def _same_name(left: str, right: str) -> bool:
    return canonicalize_name(left) == canonicalize_name(right)


def _entry_matches(entry: ManifestEntry, row: ToolRow) -> bool:
    """Whether a global manifest entry describes *row*."""
    if entry.kind is not row.kind or entry.ecosystem != row.ecosystem:
        return False
    if entry.spec.installer is not None and entry.spec.installer != row.plugin:
        return False
    if row.kind is PluginKind.RUNTIME:
        return runtime_tag_matches(row.name, entry.spec.name.name)
    if not _same_name(entry.spec.name.name, row.name):
        return False
    if entry.spec.runtime is None:
        return row.runtime is None or row.runtime.default
    return row.runtime is not None and runtime_tag_matches(
        row.runtime.tag, entry.spec.runtime
    )


def _plugin_shapes(discovered: DiscoveredPlugins) -> dict[str, tuple[PluginKind, str]]:
    shapes: dict[str, tuple[PluginKind, str]] = {}
    for name, env in (discovered.environments or {}).items():
        ecosystem = type(env).ecosystem()
        if ecosystem is not None:
            shapes[name] = (type(env).plugin_kind(), str(ecosystem))
    return shapes


async def gather_tool_inventory(
    porringer: API,
    project_paths: Sequence[str | Path] | None = None,
    discovered: DiscoveredPlugins | None = None,
    *,
    include_updates: bool = False,
) -> ToolInventory:
    """Build the Tools list from the inventory, the global manifest and project declarations."""
    if discovered is None:
        discovered = await porringer.discover_plugins(resolve_runtime=False)
    shapes = _plugin_shapes(discovered)
    report = await porringer.package.inventory(
        include_updates=include_updates, plugins=discovered
    )

    inventory = ToolInventory()
    inventory.failures.extend(
        f"{failure.provider}: {failure.error}" for failure in report.provider_failures
    )
    for record in report.runtimes:
        inventory.rows.append(
            ToolRow(
                plugin=record.provider,
                name=record.tag,
                kind=PluginKind.RUNTIME,
                ecosystem=record.kind,
                version=record.tag,
                runtime=None,
            )
        )
    for location in report.locations:
        if location.status is InventoryStatus.ERROR:
            where = f" ({location.runtime.tag})" if location.runtime else ""
            inventory.failures.append(f"{location.plugin}{where}: {location.error}")
            continue
        kind, ecosystem = shapes.get(location.plugin, (PluginKind.PACKAGE, ""))
        inventory.rows.extend(
            ToolRow(
                plugin=location.plugin,
                name=package.name,
                kind=kind,
                ecosystem=ecosystem,
                version=package.version or "",
                latest_version=package.latest_version,
                runtime=location.runtime,
            )
            for package in location.packages
        )

    for entry in porringer.tool.entries():
        if entry.kind not in {PluginKind.PACKAGE, PluginKind.TOOL, PluginKind.RUNTIME}:
            continue
        matched = [row for row in inventory.rows if _entry_matches(entry, row)]
        for row in matched:
            row.state, row.entry = RowState.MANAGED, entry
        if not matched:
            plugin = entry.spec.installer or next(
                (
                    name
                    for name, shape in shapes.items()
                    if shape == (entry.kind, entry.ecosystem)
                ),
                "",
            )
            inventory.rows.append(
                ToolRow(
                    plugin=plugin,
                    name=entry.spec.name.name,
                    kind=entry.kind,
                    ecosystem=entry.ecosystem,
                    state=RowState.MANAGED,
                    entry=entry,
                    installed=False,
                )
            )

    for declared in await declared_packages(porringer, project_paths, discovered):
        matched = [
            r
            for r in inventory.rows
            if r.plugin == declared.plugin and _same_name(r.name, declared.name)
        ]
        for row in matched:
            row.project_paths.extend(
                p for p in declared.project_paths if p not in row.project_paths
            )
            if row.state is RowState.UNMANAGED:
                row.state = RowState.PROJECT
        if not matched:
            kind, ecosystem = shapes.get(declared.plugin, (PluginKind.PACKAGE, ""))
            inventory.rows.append(
                ToolRow(
                    plugin=declared.plugin,
                    name=declared.name,
                    kind=kind,
                    ecosystem=ecosystem,
                    state=RowState.PROJECT,
                    project_paths=list(declared.project_paths),
                    installed=False,
                )
            )
    return inventory


# ---------------------------------------------------------------------------
# Row actions
# ---------------------------------------------------------------------------


class ProjectRowError(ValueError):
    """Raised for actions on rows only a project manifest declares; edit that manifest instead."""


def _new_entry_spec(row: ToolRow, constraint: str | None = None) -> PackageSpec:
    """The entry that records *row* in the global manifest."""
    name = f"{row.name}{constraint}" if constraint else row.name
    runtime = (
        row.runtime.tag if row.runtime is not None and not row.runtime.default else None
    )
    return PackageSpec.model_validate(
        {"name": name, "installer": row.plugin, "runtime": runtime}
    )


def _require_global(row: ToolRow) -> None:
    if row.state is RowState.PROJECT:
        raise ProjectRowError(
            f"{row.name} is declared by a project; change it in that project's manifest"
        )


def find_entry(porringer: API, row: ToolRow) -> ManifestEntry | None:
    """Return the global manifest entry that describes *row*, if any."""
    return next(
        (entry for entry in porringer.tool.entries() if _entry_matches(entry, row)),
        None,
    )


async def _adopt(porringer: API, row: ToolRow, *, install: bool) -> str:
    """Record *row* in the global manifest and return its entry id."""
    report = await porringer.tool.add(
        _new_entry_spec(row), kind=row.kind, ecosystem=row.ecosystem, install=install
    )
    if not report.success:
        raise RuntimeError(_first_error(report))
    entry = find_entry(porringer, row)
    if entry is None:
        raise RuntimeError(f"{row.name} was not recorded in the global manifest")
    return entry.entry_id


def _first_error(report: ManagedToolReport) -> str:
    for result in report.results:
        if not result.success:
            return result.message or f"{result.package} failed"
    for failed in report.failed_paths:
        return failed.error
    return "failed"


async def install_row(porringer: API, row: ToolRow) -> ManagedToolReport:
    """Install a row's package and record it (an uninstalled managed entry is just installed)."""
    _require_global(row)
    if row.entry is not None:
        return await porringer.tool.upgrade(
            {row.entry.entry_id}, range_strategy=RangeStrategy.IN_RANGE
        )
    return await porringer.tool.add(
        _new_entry_spec(row), kind=row.kind, ecosystem=row.ecosystem
    )


async def update_row(
    porringer: API, row: ToolRow, *, past_constraint: bool = False
) -> ManagedToolReport:
    """Update a row; unmanaged rows are recorded in the global manifest first."""
    _require_global(row)
    entry_id = (
        row.entry.entry_id
        if row.entry is not None
        else await _adopt(porringer, row, install=False)
    )
    strategy = RangeStrategy.REPLACE if past_constraint else RangeStrategy.IN_RANGE
    return await porringer.tool.upgrade({entry_id}, range_strategy=strategy)


async def uninstall_row(
    porringer: API, row: ToolRow, *, cascade: bool = False
) -> ManagedToolReport:
    """Uninstall a row and drop it from the global manifest."""
    _require_global(row)
    entry_id = (
        row.entry.entry_id
        if row.entry is not None
        else await _adopt(porringer, row, install=False)
    )
    return await porringer.tool.remove({entry_id}, cascade=cascade)


async def stop_managing_row(porringer: API, row: ToolRow) -> ManagedToolReport:
    """Drop a managed row from the global manifest, leaving it installed."""
    if row.entry is None:
        raise ValueError(f"{row.name} is not managed")
    return await porringer.tool.remove({row.entry.entry_id}, keep_installed=True)


async def manage_row(porringer: API, row: ToolRow) -> ManagedToolReport:
    """Record an installed, unmanaged row in the global manifest without changing it."""
    _require_global(row)
    return await porringer.tool.add(
        _new_entry_spec(row), kind=row.kind, ecosystem=row.ecosystem, install=False
    )


async def skip_version(porringer: API, row: ToolRow, version: str) -> ManagedToolReport:
    """Exclude *version* from the row's constraint so updates pass over it."""
    _require_global(row)
    if row.entry is None:
        entry_id = await _adopt(porringer, row, install=False)
        entry = next(e for e in porringer.tool.entries() if e.entry_id == entry_id)
    else:
        entry = row.entry
    current = entry.spec.name.constraint
    constraint = f"{current},!={version}" if current else f"!={version}"
    spec = entry.spec.model_copy(
        update={"name": entry.spec.name.model_copy(update={"constraint": constraint})}
    )
    return await porringer.tool.add(
        spec, kind=entry.kind, ecosystem=entry.ecosystem, install=False
    )


async def runtime_removal_check(porringer: API, row: ToolRow) -> RuntimeRemovalCheck:
    """Report what removing a managed runtime row would affect."""
    if row.entry is None:
        raise ValueError(f"{row.name} is not managed")
    (check,) = await porringer.tool.runtime_dependents({row.entry.entry_id})
    return check


async def update_global_entries(
    porringer: API, entry_auto_update: dict[str, bool] | None
) -> UpdateResult:
    """Upgrade the global manifest's packages and tools whose auto-update is on (the default)."""
    toggles = entry_auto_update or {}
    ids = {
        entry.entry_id
        for entry in porringer.tool.entries()
        if entry.kind in {PluginKind.PACKAGE, PluginKind.TOOL}
        and toggles.get(entry.entry_id, True)
    }
    result = UpdateResult()
    if not ids:
        return result
    report = await porringer.tool.upgrade(ids)
    result.manifests_processed = report.manifests_processed
    for item in report.results:
        name = item.package or ""
        if not item.success:
            result.packages_failed.append(name)
        elif item.skipped:
            result.already_latest.append(name)
        else:
            result.packages_updated.append(name)
            result.updated_packages.add(name)
            result.version_map[name] = (
                item.installed_version or "",
                item.available_version or "",
            )
    return result


def drop_runtime_keys(
    mapping: dict[str, bool | dict[str, bool]] | None,
) -> dict[str, bool | dict[str, bool]] | None:
    """Remove legacy ``plugin:tag`` auto-update keys; per-entry toggles replace them."""
    if not mapping:
        return mapping
    kept = {key: value for key, value in mapping.items() if ":" not in key}
    return kept or None
