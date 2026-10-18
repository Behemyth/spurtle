"""Tests for the inventory-backed Tools rows and their actions."""

import asyncio
import functools
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from porringer.schema import (
    InventoryLocation,
    InventoryPackage,
    InventoryReport,
    InventoryStatus,
    ManagedPackageResult,
    ManagedToolReport,
    ManifestEntry,
    PackageSpec,
    RangeStrategy,
    RuntimeRecord,
)
from porringer.schema.plugin import PluginKind
from spurtle.operations.inventory import (
    RowState,
    ToolRow,
    drop_runtime_keys,
    gather_tool_inventory,
    install_row,
    skip_version,
    stop_managing_row,
    uninstall_row,
    update_global_entries,
    update_row,
)
from spurtle.operations.schema import DeclaredPackage

from tests.unit.porringer_factories import spec_api

_GLOBAL = Path("C:/config/porringer.json")
_PY314 = RuntimeRecord(
    provider="pim", kind="python", tag="3.14-64", executable=Path("C:/py314/python.exe")
)
_PY315 = RuntimeRecord(
    provider="pim",
    kind="python",
    tag="3.15-64",
    executable=Path("C:/py315/python.exe"),
    default=True,
)


def _entry(
    spec: str | dict, kind: PluginKind = PluginKind.TOOL, entry_id: str = "e1"
) -> ManifestEntry:
    return ManifestEntry(
        entry_id=entry_id,
        manifest_path=_GLOBAL,
        kind=kind,
        ecosystem="python",
        spec=PackageSpec.model_validate(spec),
    )


def _discovered() -> MagicMock:
    def env(kind: PluginKind) -> MagicMock:
        plugin = MagicMock()
        type(plugin).ecosystem = MagicMock(return_value="python")
        type(plugin).plugin_kind = MagicMock(return_value=kind)
        return plugin

    discovered = MagicMock()
    discovered.environments = {
        "pip": env(PluginKind.PACKAGE),
        "pipx": env(PluginKind.TOOL),
        "pim": env(PluginKind.RUNTIME),
    }
    return discovered


def _report(**_: Any) -> InventoryReport:
    return InventoryReport(
        runtimes=(_PY315, _PY314),
        locations=(
            InventoryLocation(
                plugin="pip",
                runtime=_PY315,
                packages=(InventoryPackage(name="rich", version="14.0"),),
            ),
            InventoryLocation(
                plugin="pip",
                runtime=_PY314,
                packages=(InventoryPackage(name="rich", version="13.0"),),
            ),
            InventoryLocation(
                plugin="pipx",
                packages=(
                    InventoryPackage(name="pdm", version="2.30", latest_version="2.31"),
                    InventoryPackage(name="ruff", version="0.15"),
                ),
            ),
            InventoryLocation(
                plugin="winget", status=InventoryStatus.ERROR, error="winget is broken"
            ),
        ),
    )


@pytest.fixture(name="api")
def fixture_api() -> MagicMock:
    """An API whose inventory, global manifest and project declarations are scripted."""
    api = spec_api()
    api.package.inventory = AsyncMock(side_effect=_report)
    api.tool.entries.return_value = (
        _entry("pdm>=2"),
        _entry("black", entry_id="e2"),
        _entry("3.14", PluginKind.RUNTIME, "e3"),
    )
    return api


def _sync[**P, R](test: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, R]:
    """Run an async test body with ``asyncio.run``."""

    @functools.wraps(test)
    def run(*args: P.args, **kwargs: P.kwargs) -> R:
        return asyncio.run(test(*args, **kwargs))

    return run


@_sync
async def test_rows_are_classified_by_who_manages_them(api: MagicMock) -> None:
    """Global entries are managed, project declarations project, the rest unmanaged."""
    declared = [
        DeclaredPackage(plugin="pipx", name="ruff", project_paths=["D:/periapsis"])
    ]
    with patch(
        "spurtle.operations.inventory.declared_packages",
        AsyncMock(return_value=declared),
    ):
        inventory = await gather_tool_inventory(api, [], _discovered())

    summary = {
        (row.plugin, row.runtime.tag if row.runtime else None, row.name): (
            row.state,
            row.installed,
        )
        for row in inventory.rows
    }
    assert summary == {
        ("pim", None, "3.15-64"): (RowState.UNMANAGED, True),
        ("pim", None, "3.14-64"): (RowState.MANAGED, True),
        ("pip", "3.15-64", "rich"): (RowState.UNMANAGED, True),
        ("pip", "3.14-64", "rich"): (RowState.UNMANAGED, True),
        ("pipx", None, "pdm"): (RowState.MANAGED, True),
        ("pipx", None, "ruff"): (RowState.PROJECT, True),
        ("pipx", None, "black"): (RowState.MANAGED, False),
    }
    assert inventory.failures == ["winget: winget is broken"]


def _ok() -> ManagedToolReport:
    return ManagedToolReport(
        operation="x", results=(ManagedPackageResult(package="pdm", success=True),)
    )


def _row(**overrides: Any) -> ToolRow:
    values: dict[str, Any] = {
        "plugin": "pipx",
        "name": "pdm",
        "kind": PluginKind.TOOL,
        "ecosystem": "python",
    }
    values.update(overrides)
    return ToolRow(**values)


@_sync
async def test_managed_update_and_past_constraint_use_upgrade(api: MagicMock) -> None:
    """Managed rows upgrade by entry id, in range or past the constraint."""
    api.tool.upgrade = AsyncMock(return_value=_ok())
    row = _row(state=RowState.MANAGED, entry=_entry("pdm>=2"))

    await update_row(api, row)
    await update_row(api, row, past_constraint=True)

    assert [
        call.kwargs["range_strategy"] for call in api.tool.upgrade.await_args_list
    ] == [
        RangeStrategy.IN_RANGE,
        RangeStrategy.REPLACE,
    ]
    assert all(call.args == ({"e1"},) for call in api.tool.upgrade.await_args_list)


@_sync
async def test_unmanaged_update_records_then_upgrades(api: MagicMock) -> None:
    """An unmanaged row is added edit-only, pinned to its installer and runtime, then upgraded."""
    api.tool.add = AsyncMock(return_value=_ok())
    api.tool.upgrade = AsyncMock(return_value=_ok())
    api.tool.entries.return_value = (
        _entry(
            {"name": "rich", "installer": "pip", "runtime": "3.14-64"},
            PluginKind.PACKAGE,
            "e9",
        ),
    )
    row = _row(plugin="pip", name="rich", kind=PluginKind.PACKAGE, runtime=_PY314)

    await update_row(api, row)

    spec = api.tool.add.await_args.args[0]
    assert (spec.installer, spec.runtime) == ("pip", "3.14-64")
    assert api.tool.add.await_args.kwargs["install"] is False
    api.tool.upgrade.assert_awaited_once_with(
        {"e9"}, range_strategy=RangeStrategy.IN_RANGE
    )


@_sync
async def test_install_uninstall_and_stop_managing(api: MagicMock) -> None:
    """Missing managed entries install via upgrade; uninstall removes; stop managing keeps installed."""
    api.tool.upgrade = AsyncMock(return_value=_ok())
    api.tool.remove = AsyncMock(return_value=_ok())
    row = _row(
        state=RowState.MANAGED, entry=_entry("black", entry_id="e2"), installed=False
    )

    await install_row(api, row)
    await uninstall_row(api, row)
    await stop_managing_row(api, row)

    api.tool.upgrade.assert_awaited_once_with(
        {"e2"}, range_strategy=RangeStrategy.IN_RANGE
    )
    assert [call.kwargs for call in api.tool.remove.await_args_list] == [
        {"cascade": False},
        {"keep_installed": True},
    ]


@_sync
async def test_skip_version_adds_an_exclusion_to_the_existing_constraint(
    api: MagicMock,
) -> None:
    """Skipping a version keeps the entry's identity and constraint, edit-only."""
    api.tool.add = AsyncMock(return_value=_ok())
    row = _row(state=RowState.MANAGED, entry=_entry("pdm>=2"))

    await skip_version(api, row, "2.31")

    spec = api.tool.add.await_args.args[0]
    assert spec.name.constraint == ">=2,!=2.31"
    assert spec.installer is None
    assert api.tool.add.await_args.kwargs["install"] is False


@_sync
async def test_project_rows_are_never_edited(api: MagicMock) -> None:
    """Rows only a project declares raise instead of writing to any manifest."""
    with pytest.raises(ValueError, match="project's manifest"):
        await update_row(api, _row(state=RowState.PROJECT))
    api.tool.add.assert_not_called()


@_sync
async def test_auto_update_covers_enabled_global_packages_and_tools(
    api: MagicMock,
) -> None:
    """Runtimes and switched-off entries are left alone; an empty selection makes no call."""
    api.tool.upgrade = AsyncMock(return_value=_ok())

    result = await update_global_entries(api, {"e2": False})
    api.tool.entries.return_value = ()
    empty = await update_global_entries(api, None)

    api.tool.upgrade.assert_awaited_once_with({"e1"})
    assert result.packages_updated == ["pdm"]
    assert empty.updated == 0


def test_legacy_runtime_keys_are_dropped() -> None:
    """``plugin:tag`` toggles are removed; plugin-level ones stay."""
    assert drop_runtime_keys({"pip:3.14": False, "pipx": {"pdm": False}}) == {
        "pipx": {"pdm": False}
    }
    assert drop_runtime_keys({"pip:3.14": False}) is None
