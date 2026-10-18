"""Tool and package update operations.

Pure async functions for checking and running updates for the packages
and tools that registered projects declare.  Policy logic (auto-update
scope, manifest-aware defaults) lives here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from porringer.schema import (
    ActionCompletedEvent,
    ActionStartedEvent,
    InspectionMode,
    InspectionStatus,
    ProgressEvent,
    SetupActionResult,
    SetupParameters,
    SkipReason,
)
from porringer.schema.plugin import PluginKind

from spurtle.operations.project import registered_projects
from spurtle.operations.schema import DeclaredPackage, UpdateResult

if TYPE_CHECKING:
    from porringer.api import API
    from porringer.backend.command.core.discovery import DiscoveredPlugins

logger = logging.getLogger(__name__)


def _capture_versions(
    result: UpdateResult, pkg_name: str, action_result: SetupActionResult
) -> None:
    """Store before/after version info from *action_result* into *result*."""
    old_ver = action_result.installed_version or ""
    new_ver = action_result.available_version or ""
    if pkg_name and (old_ver or new_ver):
        result.version_map[pkg_name] = (old_ver, new_ver)


def log_update_result(result: UpdateResult) -> None:
    """Log a human-readable summary of an :class:`UpdateResult`.

    Called by both the GUI controller and the CLI after an update
    completes so that version transitions are recorded consistently.
    """
    logger.info(
        "Tool update completed: %d manifest(s), %d updated, %d already latest, %d failed",
        result.manifests_processed,
        result.updated,
        len(result.already_latest),
        result.failed,
    )
    for pkg_name, (old_ver, new_ver) in result.version_map.items():
        if old_ver and new_ver:
            logger.info("  %s: %s \u2192 %s", pkg_name, old_ver, new_ver)
        elif new_ver:
            logger.info("  %s: \u2192 %s", pkg_name, new_ver)


def _project_paths(directories: Sequence[str | Path] | None) -> list[Path]:
    """Return *directories*, or every registered project that still exists."""
    paths = (
        [Path(d) for d in directories]
        if directories is not None
        else registered_projects()
    )
    return [path for path in paths if path.is_dir()]


# ---------------------------------------------------------------------------
# Declared packages
# ---------------------------------------------------------------------------

_MANAGED_KINDS = {PluginKind.PACKAGE, PluginKind.TOOL, PluginKind.RUNTIME}


async def declared_packages(
    porringer: API,
    directories: Sequence[str | Path] | None = None,
    discovered: DiscoveredPlugins | None = None,
) -> list[DeclaredPackage]:
    """List the packages, tools, and runtimes registered manifests declare.

    One inspection covers every project, reporting installed and
    available versions.  A package declared by several projects through
    the same plugin appears once, with every declaring project listed.
    Actions whose installer is only chosen at run time are omitted.

    Args:
        porringer: The porringer API instance.
        directories: Project directories to scan.  ``None`` means every
            registered project.
        discovered: Pre-discovered plugins.
    """
    paths = _project_paths(directories)
    if not paths:
        return []

    report = await porringer.sync.inspect(
        SetupParameters(paths=paths, action_kinds=_MANAGED_KINDS, fail_fast=False),
        plugins=discovered,
    )
    merged: dict[tuple[str, str], DeclaredPackage] = {}
    for manifest in report.manifests:
        root = manifest.root_directory or (
            paths[manifest.index] if manifest.index < len(paths) else None
        )
        for inspection in manifest.actions:
            snapshot = inspection.action
            if snapshot.installer is None or snapshot.package_name is None:
                continue
            entry = merged.setdefault(
                (snapshot.installer, snapshot.package_name),
                DeclaredPackage(
                    plugin=snapshot.installer,
                    name=snapshot.package_name,
                    kind=snapshot.kind,
                ),
            )
            entry.installed_version = (
                entry.installed_version or inspection.installed_version
            )
            if inspection.status == InspectionStatus.UPDATE_AVAILABLE:
                entry.available_version = inspection.available_version
            if root is not None and str(root) not in entry.project_paths:
                entry.project_paths.append(str(root))
            entry.action_ids.add(inspection.action_id)
    return list(merged.values())


# ---------------------------------------------------------------------------
# Update-check
# ---------------------------------------------------------------------------


async def check_tool_updates(
    porringer: API,
    directories: Sequence[str | Path] | None = None,
    discovered: DiscoveredPlugins | None = None,
) -> dict[str, dict[str, str]]:
    """Detect available updates for packages declared by project manifests.

    Returns ``{plugin_name: {package_name: latest_version}}`` for
    packages that have a newer version available.

    Args:
        porringer: The porringer API instance.
        directories: Project directories to scan.  ``None`` means every
            registered project.
        discovered: Pre-discovered plugins to avoid redundant discovery.
    """
    paths = _project_paths(directories)
    if not paths:
        return {}

    report = await porringer.tool.check_updates(paths=paths, plugins=discovered)
    available: dict[str, dict[str, str]] = {}
    for item in report.results:
        if item.plugin and item.package:
            available.setdefault(item.plugin, {})[item.package] = (
                item.available_version or ""
            )
    return available


# ---------------------------------------------------------------------------
# Update execution
# ---------------------------------------------------------------------------


async def _resolve_action_ids(
    porringer: API,
    paths: list[Path],
    plugin_name: str,
    package_name: str,
    discovered: DiscoveredPlugins | None,
) -> set[str]:
    """Return the action ids declaring *package_name* through *plugin_name*.

    Actions whose installer is only chosen at run time are excluded, so a
    name match can never act through a different installer.
    """
    report = await porringer.sync.inspect(
        SetupParameters(
            paths=paths, inspection_mode=InspectionMode.FAST, fail_fast=False
        ),
        plugins=discovered,
    )
    return {
        action.action_id
        for manifest in report.manifests
        for action in manifest.actions
        if action.action.installer == plugin_name
        and action.action.package_name == package_name
    }


def _event_recorder(
    result: UpdateResult,
    on_package_starting: Callable[[str], None] | None,
    on_package_completed: Callable[[str, bool, bool], None] | None,
) -> Callable[[ProgressEvent], None]:
    """Build an ``on_event`` callback that records results into *result*."""

    def _on_event(event: ProgressEvent) -> None:
        if isinstance(event, ActionStartedEvent):
            pkg_name = str(event.action.package.name) if event.action.package else ""
            if pkg_name and on_package_starting is not None:
                on_package_starting(pkg_name)
        elif isinstance(event, ActionCompletedEvent):
            ar = event.result
            pkg_name = str(ar.action.package.name) if ar.action.package else ""
            _record_completed_event(result, ar, pkg_name, on_package_completed)

    return _on_event


async def update_tool(
    porringer: API,
    plugin_name: str,
    package_name: str | None = None,
    *,
    directories: Sequence[str | Path] | None = None,
    discovered: DiscoveredPlugins | None = None,
    on_package_starting: Callable[[str], None] | None = None,
    on_package_completed: Callable[[str, bool, bool], None] | None = None,
) -> UpdateResult:
    """Upgrade every package one plugin manages, or one specific package.

    Args:
        porringer: The porringer API instance.
        plugin_name: The installer plugin name.
        package_name: Optional specific package to upgrade.
        directories: Project directories to scan.  ``None`` means every
            registered project.
        discovered: Pre-discovered plugins.
        on_package_starting: Called with ``(package_name)`` before each
            package upgrade begins.
        on_package_completed: Called with ``(package_name, success, skipped)``
            after each package upgrade finishes.

    Returns:
        An :class:`UpdateResult` summarising the operation.
    """
    if package_name is None:
        result = await update_all_tools(
            porringer,
            plugins={plugin_name},
            directories=directories,
            discovered=discovered,
            on_package_starting=on_package_starting,
            on_package_completed=on_package_completed,
        )
        result.plugin = plugin_name
        return result

    result = UpdateResult(plugin=plugin_name)
    paths = _project_paths(directories)
    if not paths:
        return result
    action_ids = await _resolve_action_ids(
        porringer, paths, plugin_name, package_name, discovered
    )
    if not action_ids:
        logger.info(
            "No registered manifest declares %s via %s", package_name, plugin_name
        )
        return result

    report = await porringer.tool.upgrade_project(
        paths=paths,
        plugins=discovered,
        include_packages={package_name},
        action_ids=action_ids,
        on_event=_event_recorder(result, on_package_starting, on_package_completed),
    )
    result.manifests_processed = report.manifests_processed
    return result


async def remove_package(
    porringer: API,
    plugin_name: str,
    package_name: str,
    *,
    directories: Sequence[str | Path] | None = None,
    discovered: DiscoveredPlugins | None = None,
) -> bool:
    """Uninstall a package that a registered project declares.

    Args:
        porringer: The porringer API instance.
        plugin_name: The installer plugin name.
        package_name: The package to remove.
        directories: Project directories to scan.  ``None`` means every
            registered project.
        discovered: Pre-discovered plugins.

    Returns:
        ``True`` if the removal succeeded.
    """
    paths = _project_paths(directories)
    if not paths:
        return False
    action_ids = await _resolve_action_ids(
        porringer, paths, plugin_name, package_name, discovered
    )
    if not action_ids:
        logger.info(
            "No registered manifest declares %s via %s", package_name, plugin_name
        )
        return False

    report = await porringer.tool.uninstall(
        paths=paths,
        plugins=discovered,
        include_packages={package_name},
        action_ids=action_ids,
    )
    return report.success and any(
        item.success and not item.skipped for item in report.results
    )


def _record_completed_event(
    result: UpdateResult,
    ar: SetupActionResult,
    pkg_name: str,
    on_package_completed: Callable[[str, bool, bool], None] | None,
) -> None:
    """Record a single completed-event into *result* and fire the callback."""
    if ar.skipped:
        if ar.skip_reason in {SkipReason.ALREADY_LATEST, SkipReason.ALREADY_INSTALLED}:
            result.already_latest.append(pkg_name)
        if on_package_completed is not None and pkg_name:
            on_package_completed(pkg_name, False, True)
    elif ar.success:
        result.packages_updated.append(pkg_name)
        if pkg_name:
            result.updated_packages.add(pkg_name)
        _capture_versions(result, pkg_name, ar)
        if on_package_completed is not None and pkg_name:
            on_package_completed(pkg_name, True, False)
    else:
        result.packages_failed.append(pkg_name)
        if on_package_completed is not None and pkg_name:
            on_package_completed(pkg_name, False, False)


async def update_all_tools(
    porringer: API,
    plugins: set[str] | None = None,
    include_packages: set[str] | None = None,
    *,
    directories: Sequence[str | Path] | None = None,
    discovered: DiscoveredPlugins | None = None,
    on_package_starting: Callable[[str], None] | None = None,
    on_package_completed: Callable[[str, bool, bool], None] | None = None,
) -> UpdateResult:
    """Upgrade the packages registered project manifests declare.

    ``None`` filters select everything; an empty set selects nothing.

    Args:
        porringer: The porringer API instance.
        plugins: Optional include-set of plugin names.
        include_packages: Optional include-set of package names.
        directories: Project directories to update.  ``None`` means every
            registered project.
        discovered: Pre-discovered plugins.
        on_package_starting: Called with ``(package_name)`` before each
            package action begins.
        on_package_completed: Called with ``(package_name, success, skipped)``
            after each package action completes.

    Returns:
        An :class:`UpdateResult` summarising the full run.
    """
    result = UpdateResult(plugin="*")
    paths = _project_paths(directories)
    if not paths or plugins == set() or include_packages == set():
        return result

    report = await porringer.tool.upgrade_project(
        paths=paths,
        plugins=discovered,
        plugin_names=plugins,
        include_packages=include_packages,
        on_event=_event_recorder(result, on_package_starting, on_package_completed),
    )
    result.manifests_processed = report.manifests_processed
    return result


# ---------------------------------------------------------------------------
# Auto-update scope policy
# ---------------------------------------------------------------------------


def resolve_auto_update_scope(
    plugin_auto_update: dict[str, bool | dict[str, bool]] | None,
    all_plugin_names: list[str],
    manifest_packages: dict[str, set[str]] | None = None,
) -> tuple[set[str] | None, set[str] | None]:
    """Derive plugin and package include-lists for auto-update.

    Walks ``plugin_auto_update`` to determine which plugins and packages
    should participate in automatic updates.

    Args:
        plugin_auto_update: The per-plugin auto-update config mapping.
        all_plugin_names: Every known (installed) plugin name.
        manifest_packages: Mapping of ``plugin_name`` → set of package
            names declared in registered manifests.

    Returns:
        A ``(enabled_plugins, include_packages)`` tuple.  ``None`` means
        "no filtering"; an empty set means "nothing".
    """
    mapping = plugin_auto_update

    disabled_plugins: set[str] = set()
    per_package_entries: dict[str, dict[str, bool]] = {}

    if mapping:
        for name, value in mapping.items():
            if ":" in name:
                continue
            if value is False:
                disabled_plugins.add(name)
            elif isinstance(value, dict):
                per_package_entries[name] = value

    enabled_plugins: set[str] | None = None
    if disabled_plugins:
        enabled_plugins = {n for n in all_plugin_names if n not in disabled_plugins}

    include_packages = _build_include_packages(
        per_package_entries,
        manifest_packages,
        disabled_plugins,
    )

    return enabled_plugins, include_packages


def _build_include_packages(
    per_package_entries: dict[str, dict[str, bool]],
    manifest_packages: dict[str, set[str]] | None,
    disabled_plugins: set[str],
) -> set[str] | None:
    """Build the set of package names eligible for auto-update.

    Returns ``None`` only when there is nothing to filter by; an empty
    selection stays empty so it never widens to every package.
    """
    if not per_package_entries and not manifest_packages:
        return None

    pkg_set: set[str] = set()
    if manifest_packages:
        for plugin_name, pkgs in manifest_packages.items():
            if plugin_name not in disabled_plugins:
                pkg_set |= pkgs

    for plugin_name, pkg_map in per_package_entries.items():
        if plugin_name in disabled_plugins:
            continue
        for pkg_name, enabled in pkg_map.items():
            if enabled:
                pkg_set.add(pkg_name)
            else:
                pkg_set.discard(pkg_name)

    return pkg_set
