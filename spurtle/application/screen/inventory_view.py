"""Tools view backed by Porringer's inventory: everything installed, and who manages it."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from packaging.version import Version
from porringer.schema import ManagedToolReport, PluginInfo
from porringer.schema.plugin import PluginKind
from PySide6.QtWidgets import QCheckBox, QLabel, QMessageBox

from spurtle.application.screen.inventory_row import InventoryRow, RowAction
from spurtle.application.screen.plugin_row import PluginKindHeader, PluginProviderHeader
from spurtle.application.screen.schema import (
    PluginRowData,
    ProjectInstance,
    RefreshData,
)
from spurtle.application.screen.screen import _KIND_DISPLAY_ORDER, ToolsView
from spurtle.application.theme import PLUGIN_ROW_ERROR_STYLE
from spurtle.operations.inventory import (
    RowState,
    ToolInventory,
    ToolRow,
    drop_runtime_keys,
    find_entry,
    gather_tool_inventory,
    install_row,
    manage_row,
    runtime_removal_check,
    skip_version,
    stop_managing_row,
    uninstall_row,
    update_row,
)

logger = logging.getLogger(__name__)


def available_updates(inventory: ToolInventory) -> dict[str, dict[str, str]]:
    """Map each row group to ``{package: latest_version}`` for rows with a newer version."""
    available: dict[str, dict[str, str]] = {}
    for row in inventory.rows:
        if row.latest_version:
            available.setdefault(row.signal_key, {})[row.name] = row.latest_version
    return available


def _runtime_label(row: ToolRow) -> str:
    assert row.runtime is not None
    return f"{row.runtime.tag} (default)" if row.runtime.default else row.runtime.tag


class InventoryToolsView(ToolsView):
    """The Tools view, listing the machine's inventory instead of project declarations only."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Initialise, then drop auto-update keys from before per-entry toggles."""
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        legacy = self._store.config.plugin_auto_update
        cleaned = drop_runtime_keys(legacy)
        if cleaned != legacy:
            self._store.update(plugin_auto_update=cleaned)

    async def _gather_refresh_data(self) -> RefreshData:
        """Fetch plugins and the inventory with each row's manifest state."""
        plugins, directories = await self._fetch_data()
        self._directories = directories
        discovered = self._coordinator.discovered_plugins if self._coordinator else None
        inventory = await gather_tool_inventory(
            self._porringer, self._manifest_paths(), discovered
        )
        return RefreshData(
            plugins=plugins,
            packages_map={},
            manifest_packages={},
            available=available_updates(inventory),
            inventory=inventory,
        )

    async def _check_for_updates(self) -> dict[str, dict[str, str]]:
        """Look up newer versions for everything listed."""
        discovered = self._coordinator.discovered_plugins if self._coordinator else None
        inventory = await gather_tool_inventory(
            self._porringer, self._manifest_paths(), discovered, include_updates=True
        )
        return available_updates(inventory)

    def _build_widget_tree(self, data: RefreshData) -> None:
        """Group rows by kind, then by plugin and interpreter."""
        if data.inventory is None:
            super()._build_widget_tree(data)
            return
        self._clear_section_widgets()

        for failure in data.inventory.failures:
            label = QLabel(f"Couldn't list {failure}", parent=self._container)
            label.setStyleSheet(PLUGIN_ROW_ERROR_STYLE)
            self._insert_section_widget(label)

        plugins = {plugin.name: plugin for plugin in data.plugins}
        groups: dict[PluginKind, OrderedDict[str, list[ToolRow]]] = {}
        for row in data.inventory.rows:
            groups.setdefault(row.kind, OrderedDict()).setdefault(
                row.signal_key, []
            ).append(row)

        for kind in sorted(groups, key=lambda k: _KIND_DISPLAY_ORDER.get(k, 99)):
            self._insert_section_widget(PluginKindHeader(kind, parent=self._container))
            for _key, rows in sorted(groups[kind].items()):
                first = rows[0]
                info = plugins.get(first.plugin) or PluginInfo(
                    name=first.plugin or "unresolved",
                    kind=kind,
                    version=Version("0"),
                    installed=False,
                    tool_version=None,
                )
                header = PluginProviderHeader(info, parent=self._container)
                if first.runtime is not None:
                    header.set_runtime(first.runtime.tag, _runtime_label(first))
                self._insert_section_widget(header)
                for row in sorted(rows, key=lambda r: r.name.lower()):
                    self._insert_section_widget(self._make_inventory_row(row))

        self._rebuild_chips()
        self._apply_filter()

    def _make_inventory_row(self, row: ToolRow) -> InventoryRow:
        """Create a row widget for *row* and wire its actions."""
        entry_auto = self._store.config.entry_auto_update or {}
        managed = row.state is RowState.MANAGED and row.entry is not None
        tool_timestamps = self._store.config.last_tool_updates or {}
        data = PluginRowData(
            name=row.name,
            version=row.version if row.installed else "not installed",
            plugin_name=row.plugin,
            auto_update=managed
            and entry_auto.get(row.entry.entry_id if row.entry else "", True),
            show_toggle=managed,
            has_update=row.latest_version is not None,
            runtime_tag=row.runtime.tag if row.runtime is not None else "",
            project_instances=[
                ProjectInstance(
                    project_label=p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
                    project_path=p,
                )
                for p in row.project_paths
            ],
            last_updated=tool_timestamps.get(f"{row.plugin}/{row.name}", ""),
        )
        widget = InventoryRow(data, row, parent=self._container)
        widget.auto_update_toggled.connect(
            lambda _key, _name, on, r=row: self._on_entry_auto_update_toggled(r, on)
        )
        widget.row_action_requested.connect(
            lambda action, r=row: self._on_row_action(r, RowAction(action))
        )
        widget.navigate_to_project.connect(self.navigate_to_project_requested.emit)
        return widget

    def _on_entry_auto_update_toggled(self, row: ToolRow, enabled: bool) -> None:
        """Persist a per-entry auto-update toggle; entries auto-update unless switched off."""
        if row.entry is None:
            return
        mapping = dict(self._store.config.entry_auto_update or {})
        if enabled:
            mapping.pop(row.entry.entry_id, None)
        else:
            mapping[row.entry.entry_id] = False
        self._store.update(entry_auto_update=mapping or None)

    # --- Row actions ---

    def _on_row_action(self, row: ToolRow, action: RowAction) -> None:
        """Confirm destructive actions, then run the matching Porringer call."""
        if action is RowAction.UNINSTALL and row.kind is PluginKind.RUNTIME:
            asyncio.create_task(self._remove_runtime(row))
            return
        if action is RowAction.UNINSTALL and not self._confirm(
            f"Uninstall {row.name}?", self._uninstall_text(row)
        ):
            return
        calls: dict[RowAction, Callable[[], Awaitable[ManagedToolReport]]] = {
            RowAction.INSTALL: lambda: install_row(self._porringer, row),
            RowAction.UPDATE: lambda: update_row(self._porringer, row),
            RowAction.UPDATE_PAST_CONSTRAINT: lambda: update_row(
                self._porringer, row, past_constraint=True
            ),
            RowAction.SKIP_VERSION: lambda: skip_version(
                self._porringer, row, row.latest_version or ""
            ),
            RowAction.MANAGE: lambda: manage_row(self._porringer, row),
            RowAction.STOP_MANAGING: lambda: stop_managing_row(self._porringer, row),
            RowAction.UNINSTALL: lambda: uninstall_row(self._porringer, row),
        }
        asyncio.create_task(self._run_row_call(row, calls[action]))

    @staticmethod
    def _uninstall_text(row: ToolRow) -> str:
        where = f" from Python {row.runtime.tag}" if row.runtime is not None else ""
        managed = (
            " and remove it from your global manifest"
            if row.state is RowState.MANAGED
            else ""
        )
        return f"This uninstalls {row.name}{where} using {row.plugin}{managed}."

    def _confirm(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(
            self,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    async def _run_row_call(
        self, row: ToolRow, call: Callable[[], Awaitable[ManagedToolReport]]
    ) -> None:
        """Run one row action, show its outcome on the row and refresh the list."""
        self.set_package_updating(row.signal_key, row.name, True)
        try:
            report = await call()
        except Exception as exc:
            logger.exception("Action on %s failed", row.name)
            self.set_package_updating(row.signal_key, row.name, False)
            self.set_package_error(row.signal_key, row.name, str(exc))
            return
        self.set_package_updating(row.signal_key, row.name, False)
        if not report.success:
            failure = next(
                (r.message for r in report.results if not r.success and r.message),
                "failed",
            )
            self.set_package_error(row.signal_key, row.name, failure)
            return
        if self._coordinator is not None:
            self._coordinator.invalidate()
        self.invalidate_update_data()
        self.refresh()

    async def _remove_runtime(self, row: ToolRow) -> None:
        """Show what depends on a runtime, then remove it (and optionally its dependents)."""
        adopted = row.entry is None
        try:
            if adopted:
                await manage_row(self._porringer, row)
                row.entry = find_entry(self._porringer, row)
            check = await runtime_removal_check(self._porringer, row)
        except Exception as exc:
            logger.exception("Could not check what depends on %s", row.name)
            self.set_package_error(row.signal_key, row.name, str(exc))
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(f"Remove Python {row.name}?")
        lines = [f"This removes {check.executable or row.name} using {row.plugin}."]
        if check.default:
            lines.append(
                "It is the default interpreter; py and python will use another one."
            )
        if check.installed_packages:
            lines.append(
                "Removed along with it: " + ", ".join(check.installed_packages)
            )
        if check.dependents:
            lines.append(
                "Still using it:\n"
                + "\n".join(f"  {d.description}" for d in check.dependents)
            )
        box.setText("\n\n".join(lines))
        cascade_box: QCheckBox | None = None
        if check.dependents:
            cascade_box = QCheckBox("Remove these too")
            box.setCheckBox(cascade_box)
        remove = box.addButton("Remove", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()

        cascade = cascade_box is not None and cascade_box.isChecked()
        if box.clickedButton() is not remove or (check.dependents and not cascade):
            if adopted:
                await stop_managing_row(self._porringer, row)
            return
        await self._run_row_call(
            row, lambda: uninstall_row(self._porringer, row, cascade=cascade)
        )
