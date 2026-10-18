"""Tests for the inventory-backed Tools view: grouping, row menus and click mapping."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import spurtle.application.screen.inventory_view as view_module
from packaging.version import Version
from porringer.schema import (
    ManagedPackageResult,
    ManagedToolReport,
    ManifestEntry,
    PackageSpec,
    RuntimeRecord,
)
from porringer.schema.plugin import PluginInfo, PluginKind
from spurtle.application.screen.inventory_row import (
    InventoryRow,
    RowAction,
    row_actions,
)
from spurtle.application.screen.inventory_view import InventoryToolsView
from spurtle.application.screen.plugin_row import PluginProviderHeader
from spurtle.operations.inventory import RowState, ToolInventory, ToolRow

from .conftest import make_config_store, make_mock_porringer

_PY314 = RuntimeRecord(
    provider="pim", kind="python", tag="3.14-64", executable=Path("C:/py314/python.exe")
)
_ENTRY = ManifestEntry(
    entry_id="e1",
    manifest_path=Path("C:/config/porringer.json"),
    kind=PluginKind.TOOL,
    ecosystem="python",
    spec=PackageSpec.model_validate("pdm>=2"),
)


def _rows() -> list[ToolRow]:
    return [
        ToolRow(
            plugin="pipx",
            name="pdm",
            kind=PluginKind.TOOL,
            ecosystem="python",
            version="2.30",
            latest_version="2.31",
            state=RowState.MANAGED,
            entry=_ENTRY,
        ),
        ToolRow(
            plugin="pipx",
            name="ruff",
            kind=PluginKind.TOOL,
            ecosystem="python",
            version="0.15",
            state=RowState.PROJECT,
            project_paths=["D:/periapsis"],
        ),
        ToolRow(
            plugin="pip",
            name="rich",
            kind=PluginKind.PACKAGE,
            ecosystem="python",
            version="13.0",
            runtime=_PY314,
        ),
    ]


def _plugin(name: str, kind: PluginKind) -> PluginInfo:
    return PluginInfo(
        name=name, kind=kind, version=Version("1.0"), installed=True, tool_version=None
    )


@pytest.fixture(name="view")
def fixture_view(monkeypatch: pytest.MonkeyPatch) -> InventoryToolsView:
    """A Tools view whose inventory is scripted."""
    inventory = ToolInventory(rows=_rows(), failures=["winget: broken"])
    monkeypatch.setattr(
        view_module, "gather_tool_inventory", AsyncMock(return_value=inventory)
    )
    view = InventoryToolsView(make_mock_porringer(), make_config_store())
    plugins = [_plugin("pipx", PluginKind.TOOL), _plugin("pip", PluginKind.PACKAGE)]
    monkeypatch.setattr(view, "_fetch_data", AsyncMock(return_value=(plugins, [])))
    view._build_widget_tree(asyncio.run(view._gather_refresh_data()))
    return view


def _row_widget(view: InventoryToolsView, name: str) -> InventoryRow:
    return next(
        w
        for w in view._section_widgets
        if isinstance(w, InventoryRow) and w.tool_row.name == name
    )


def test_rows_group_by_plugin_and_interpreter(view: InventoryToolsView) -> None:
    """Interpreter-bound rows get their own header with the runtime tag."""
    headers = [
        w._signal_key
        for w in view._section_widgets
        if isinstance(w, PluginProviderHeader)
    ]

    assert headers == ["pipx", "pip:3.14-64"]
    assert _row_widget(view, "rich")._signal_key == "pip:3.14-64"


def test_menus_follow_row_state() -> None:
    """Managed, project and unmanaged rows offer different actions."""
    managed, project, unmanaged = _rows()

    assert [a for a, _ in row_actions(managed)] == [
        RowAction.UPDATE,
        RowAction.SKIP_VERSION,
        RowAction.UPDATE_PAST_CONSTRAINT,
        RowAction.STOP_MANAGING,
        RowAction.UNINSTALL,
    ]
    assert [a for a, _ in row_actions(project)] == [None]
    assert [a for a, _ in row_actions(unmanaged)] == [
        RowAction.MANAGE,
        RowAction.UNINSTALL,
    ]


def test_project_rows_cannot_be_uninstalled_here(view: InventoryToolsView) -> None:
    """The remove button is disabled for rows only a project declares."""
    remove = _row_widget(view, "ruff")._remove_btn

    assert remove is not None
    assert not remove.isEnabled()


def _run_now(coro: Coroutine[Any, Any, Any]) -> MagicMock:
    asyncio.run(coro)
    return MagicMock()


def test_update_click_runs_the_upgrade(
    view: InventoryToolsView, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row's Update button upgrades that entry, then refreshes."""
    report = ManagedToolReport(
        operation="upgrade",
        results=(ManagedPackageResult(package="pdm", success=True),),
    )
    update = AsyncMock(return_value=report)
    monkeypatch.setattr(view_module, "update_row", update)
    monkeypatch.setattr(view_module.asyncio, "create_task", _run_now)
    monkeypatch.setattr(view, "refresh", MagicMock())

    row = _row_widget(view, "pdm")
    assert row._update_btn is not None
    row._update_btn.click()

    update.assert_awaited_once()
    assert update.await_args.args[1].entry is _ENTRY
    view.refresh.assert_called_once()


def test_entry_auto_update_toggle_is_stored_by_entry_id(
    view: InventoryToolsView,
) -> None:
    """Switching auto-update off for a managed row records its entry id."""
    view._on_entry_auto_update_toggled(_rows()[0], False)

    assert view._store.config.entry_auto_update == {"e1": False}
