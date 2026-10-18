"""Tools rows built from Porringer's inventory, with the actions their state allows."""

from __future__ import annotations

from enum import Enum

from porringer.schema.plugin import PluginKind
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QMenu, QToolButton

from spurtle.application.screen.plugin_row import PluginRow
from spurtle.application.screen.schema import PluginRowData
from spurtle.operations.inventory import RowState, ToolRow


class RowAction(Enum):
    """Actions an inventory row can request."""

    INSTALL = "install"
    UPDATE = "update"
    UPDATE_PAST_CONSTRAINT = "update_past_constraint"
    SKIP_VERSION = "skip_version"
    MANAGE = "manage"
    STOP_MANAGING = "stop_managing"
    UNINSTALL = "uninstall"


def row_actions(row: ToolRow) -> list[tuple[RowAction | None, str]]:
    """Menu entries for *row*; ``None`` marks an informational, disabled entry."""
    if row.state is RowState.PROJECT:
        return [
            (None, "Declared by a project \u2014 change it in that project's manifest")
        ]
    actions: list[tuple[RowAction | None, str]] = []
    if not row.installed:
        actions.append((RowAction.INSTALL, "Install"))
    elif row.latest_version:
        actions.append((RowAction.UPDATE, f"Update to {row.latest_version}"))
        actions.append((RowAction.SKIP_VERSION, f"Skip version {row.latest_version}"))
    if (
        row.state is RowState.MANAGED
        and row.installed
        and row.kind is not PluginKind.RUNTIME
    ):
        actions.append((RowAction.UPDATE_PAST_CONSTRAINT, "Update past its constraint"))
    if row.state is RowState.UNMANAGED:
        actions.append((RowAction.MANAGE, "Manage (add to your global manifest)"))
    else:
        actions.append((RowAction.STOP_MANAGING, "Stop managing (keep installed)"))
    if row.installed:
        actions.append((RowAction.UNINSTALL, "Uninstall"))
    return actions


class InventoryRow(PluginRow):
    """A :class:`PluginRow` whose buttons and menu act on its inventory row."""

    row_action_requested = Signal(str)
    """Emitted with a :class:`RowAction` value."""

    def __init__(
        self, data: PluginRowData, tool_row: ToolRow, **kwargs: object
    ) -> None:
        """Build the row for *tool_row*."""
        self.tool_row = tool_row
        data.is_global = tool_row.state is not RowState.PROJECT and tool_row.installed
        super().__init__(data, **kwargs)  # type: ignore[arg-type]

    def _build_update_button(self, layout: QHBoxLayout, data: PluginRowData) -> None:
        super()._build_update_button(layout, data)
        if self._update_btn is not None:
            self._update_btn.clicked.disconnect()
            self._update_btn.clicked.connect(
                lambda: self.row_action_requested.emit(RowAction.UPDATE.value)
            )

    def _build_remove_button(self, layout: QHBoxLayout, data: PluginRowData) -> None:
        self._build_action_menu(layout)
        super()._build_remove_button(layout, data)
        if self._remove_btn is not None and data.is_global:
            self._remove_btn.clicked.disconnect()
            self._remove_btn.setToolTip(f"Uninstall {data.name}")
            self._remove_btn.clicked.connect(
                lambda: self.row_action_requested.emit(RowAction.UNINSTALL.value)
            )

    def _build_action_menu(self, layout: QHBoxLayout) -> None:
        """Add a menu button with the actions this row's state allows."""
        button = QToolButton()
        button.setText("\u22ef")
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setToolTip("More actions")
        menu = QMenu(button)
        for action, label in row_actions(self.tool_row):
            item = menu.addAction(label)
            if action is None:
                item.setEnabled(False)
            else:
                item.triggered.connect(
                    lambda _checked=False, a=action: self.row_action_requested.emit(
                        a.value
                    )
                )
        button.setMenu(menu)
        self.action_menu = menu
        layout.addWidget(button)
