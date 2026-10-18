"""Tests for operations.tool module."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import spurtle.operations.tool as tool_module
from porringer.core.schema import PackageRef
from porringer.schema import (
    ActionCompletedEvent,
    ActionStartedEvent,
    InspectionMode,
    InspectionStatus,
    ProgressEvent,
    SetupAction,
    SetupActionResult,
    SkipReason,
)
from porringer.schema.plugin import PluginKind
from porringer.schema.tool import ManagedPackageResult, ManagedToolReport
from spurtle.operations.schema import DeclaredPackage, UpdateResult
from spurtle.operations.tool import (
    check_tool_updates,
    declared_packages,
    remove_package,
    update_all_tools,
    update_tool,
)

from ..porringer_factories import inspected, manifest, mock_inspect, report, spec_api

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _action(
    name: str,
    installer: str | None = "pip",
    *,
    kind: PluginKind = PluginKind.PACKAGE,
) -> SetupAction:
    """Build a real package ``SetupAction``."""
    return SetupAction(
        description=f"Install {name}",
        kind=kind,
        installer=installer,
        package=PackageRef(name=name),
    )


def _projects(tmp_path: Path, *names: str) -> list[Path]:
    """Create project directories under *tmp_path*."""
    paths = [tmp_path / name for name in names]
    for path in paths:
        path.mkdir()
    return paths


def _completed(action: SetupAction, **result: Any) -> ActionCompletedEvent:
    """Build an ``ActionCompletedEvent`` for *action*."""
    return ActionCompletedEvent(
        action=action, result=SetupActionResult(action=action, **result)
    )


def _emit(
    events: Sequence[ProgressEvent], *, manifests_processed: int = 1
) -> Callable[..., object]:
    """Build a ``tool.*`` side effect that delivers *events* via ``on_event``."""

    async def _side_effect(
        *, on_event: Callable[[ProgressEvent], object] | None = None, **_kw: object
    ) -> object:
        for event in events:
            if on_event is not None:
                on_event(event)
        return ManagedToolReport(
            operation="upgrade_project", manifests_processed=manifests_processed
        )

    return _side_effect


# ---------------------------------------------------------------------------
# declared_packages
# ---------------------------------------------------------------------------


class TestDeclaredPackages:
    """Tests for declared_packages()."""

    @staticmethod
    def test_merges_packages_across_projects(tmp_path: Path) -> None:
        """A package declared by two projects is one row listing both projects and action ids."""
        first, second = _projects(tmp_path, "a", "b")
        requests = _action("requests")
        ruff = _action("ruff", "pipx", kind=PluginKind.TOOL)
        deferred = _action("black", None)

        api = spec_api()
        calls = mock_inspect(
            api,
            report(
                manifest(
                    [
                        inspected(
                            requests,
                            index=0,
                            status=InspectionStatus.UPDATE_AVAILABLE,
                            skipped=True,
                            skip_reason=SkipReason.UPDATE_AVAILABLE,
                            installed_version="2.31.0",
                            available_version="2.32.0",
                        ),
                        inspected(
                            ruff,
                            index=1,
                            status=InspectionStatus.SATISFIED,
                            skipped=True,
                            skip_reason=SkipReason.ALREADY_LATEST,
                            installed_version="0.8.0",
                            available_version="0.8.0",
                        ),
                        inspected(deferred, index=2),
                    ],
                    index=0,
                    root_directory=first,
                ),
                manifest(
                    [
                        inspected(
                            requests,
                            index=0,
                            manifest_index=1,
                            status=InspectionStatus.SATISFIED,
                            skipped=True,
                            skip_reason=SkipReason.ALREADY_INSTALLED,
                            installed_version="2.31.0",
                        )
                    ],
                    index=1,
                    root_directory=second,
                ),
            ),
        )

        rows = asyncio.run(declared_packages(api, [first, second]))

        assert rows == [
            DeclaredPackage(
                plugin="pip",
                name="requests",
                kind="packages",
                installed_version="2.31.0",
                available_version="2.32.0",
                project_paths=[str(first), str(second)],
                action_ids={"0:0", "1:0"},
            ),
            DeclaredPackage(
                plugin="pipx",
                name="ruff",
                kind="tools",
                installed_version="0.8.0",
                available_version=None,
                project_paths=[str(first)],
                action_ids={"0:1"},
            ),
        ]
        assert calls[0].inspection_mode == InspectionMode.COMPLETE
        assert calls[0].paths == [first, second]
        assert calls[0].action_kinds == {
            PluginKind.PACKAGE,
            PluginKind.TOOL,
            PluginKind.RUNTIME,
        }

    @staticmethod
    def test_no_directories_skips_inspection() -> None:
        """No project directories → empty list and no inspection."""
        api = spec_api()
        assert asyncio.run(declared_packages(api, [])) == []
        api.sync.inspect.assert_not_awaited()

    @staticmethod
    def test_defaults_to_existing_registered_projects(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``directories=None`` inspects only registered projects that still exist."""
        (existing,) = _projects(tmp_path, "exists")
        monkeypatch.setattr(
            tool_module, "registered_projects", lambda: [existing, tmp_path / "gone"]
        )
        api = spec_api()
        calls = mock_inspect(api, report())

        asyncio.run(declared_packages(api))

        assert calls[0].paths == [existing]


# ---------------------------------------------------------------------------
# check_tool_updates
# ---------------------------------------------------------------------------


class TestCheckToolUpdates:
    """Tests for check_tool_updates()."""

    @staticmethod
    def test_empty_directories() -> None:
        """No directories → empty dict and no check."""
        api = spec_api()
        assert asyncio.run(check_tool_updates(api, [])) == {}
        api.tool.check_updates.assert_not_awaited()

    @staticmethod
    def test_maps_report_results(tmp_path: Path) -> None:
        """Report results become ``{plugin: {package: version}}``."""
        (project,) = _projects(tmp_path, "proj")
        api = spec_api()
        discovered = MagicMock()
        api.tool.check_updates.return_value = ManagedToolReport(
            operation="check_updates",
            results=(
                ManagedPackageResult(
                    plugin="pip",
                    package="requests",
                    success=True,
                    available_version="2.32.0",
                ),
                ManagedPackageResult(
                    plugin=None, package="orphan", success=True, available_version="1.0"
                ),
            ),
        )

        result = asyncio.run(check_tool_updates(api, [project], discovered))

        assert result == {"pip": {"requests": "2.32.0"}}
        api.tool.check_updates.assert_awaited_once_with(
            paths=[project], plugins=discovered
        )

    @staticmethod
    def test_skips_missing_directories(tmp_path: Path) -> None:
        """Directories that no longer exist are not checked."""
        api = spec_api()
        assert asyncio.run(check_tool_updates(api, [tmp_path / "missing"])) == {}
        api.tool.check_updates.assert_not_awaited()


# ---------------------------------------------------------------------------
# update_tool
# ---------------------------------------------------------------------------


class TestUpdateTool:
    """Tests for update_tool()."""

    @staticmethod
    def _api_declaring(projects: list[Path]) -> MagicMock:
        """Build an API whose FAST inspection declares ``requests`` in several ways."""
        api = spec_api()
        mock_inspect(
            api,
            report(
                manifest(
                    [
                        inspected(_action("requests"), index=0),
                        inspected(_action("requests", "uv"), index=1),
                        inspected(_action("requests", None), index=2),
                        inspected(_action("ruff"), index=3),
                    ],
                    index=0,
                    root_directory=projects[0],
                ),
                manifest(
                    [inspected(_action("requests"), index=0, manifest_index=1)], index=1
                ),
                mode=InspectionMode.FAST,
            ),
        )
        return api

    def test_single_package_passes_resolved_action_ids(self, tmp_path: Path) -> None:
        """Only actions that install the package through the plugin are selected."""
        projects = _projects(tmp_path, "a", "b")
        api = self._api_declaring(projects)
        discovered = MagicMock()
        action = _action("requests")
        api.tool.upgrade_project.side_effect = _emit(
            [
                ActionStartedEvent(action=action),
                _completed(
                    action,
                    success=True,
                    installed_version="2.31.0",
                    available_version="2.32.0",
                ),
            ],
            manifests_processed=len(projects),
        )
        started: list[str] = []

        result = asyncio.run(
            update_tool(
                api,
                "pip",
                "requests",
                directories=projects,
                discovered=discovered,
                on_package_starting=started.append,
            )
        )

        assert isinstance(result, UpdateResult)
        assert result.plugin == "pip"
        assert result.packages_updated == ["requests"]
        assert result.version_map == {"requests": ("2.31.0", "2.32.0")}
        assert result.manifests_processed == len(projects)
        assert started == ["requests"]
        assert (
            api.sync.inspect.await_args.args[0].inspection_mode == InspectionMode.FAST
        )
        kwargs = api.tool.upgrade_project.await_args.kwargs
        assert kwargs["action_ids"] == {"0:0", "1:0"}
        assert kwargs["include_packages"] == {"requests"}
        assert kwargs["paths"] == projects
        assert kwargs["plugins"] is discovered

    def test_no_matching_action_skips_upgrade(self, tmp_path: Path) -> None:
        """A package only declared with another or a deferred installer is not upgraded."""
        projects = _projects(tmp_path, "a", "b")
        api = self._api_declaring(projects)

        result = asyncio.run(update_tool(api, "pipx", "requests", directories=projects))

        assert result == UpdateResult(plugin="pipx")
        api.tool.upgrade_project.assert_not_awaited()

    def test_single_package_already_latest(self, tmp_path: Path) -> None:
        """An already-latest package is reported as such."""
        projects = _projects(tmp_path, "a", "b")
        api = self._api_declaring(projects)
        action = _action("requests")
        api.tool.upgrade_project.side_effect = _emit(
            [
                _completed(
                    action,
                    success=True,
                    skipped=True,
                    skip_reason=SkipReason.ALREADY_LATEST,
                )
            ]
        )
        completed: list[tuple[str, bool, bool]] = []

        result = asyncio.run(
            update_tool(
                api,
                "pip",
                "requests",
                directories=projects,
                on_package_completed=lambda name, ok, skip: completed.append(
                    (name, ok, skip)
                ),
            )
        )

        assert result.already_latest == ["requests"]
        assert result.version_map == {}
        assert completed == [("requests", False, True)]

    def test_single_package_failure(self, tmp_path: Path) -> None:
        """A failed upgrade is recorded as failed."""
        projects = _projects(tmp_path, "a", "b")
        api = self._api_declaring(projects)
        api.tool.upgrade_project.side_effect = _emit(
            [_completed(_action("requests"), success=False)]
        )

        result = asyncio.run(update_tool(api, "pip", "requests", directories=projects))

        assert result.packages_failed == ["requests"]
        assert result.version_map == {}

    def test_single_package_success_no_version_data(self, tmp_path: Path) -> None:
        """A success without version info records no version transition."""
        projects = _projects(tmp_path, "a", "b")
        api = self._api_declaring(projects)
        api.tool.upgrade_project.side_effect = _emit(
            [_completed(_action("requests"), success=True)]
        )

        result = asyncio.run(update_tool(api, "pip", "requests", directories=projects))

        assert result.packages_updated == ["requests"]
        assert result.version_map == {}

    @staticmethod
    def test_whole_plugin_upgrades_by_plugin_name(tmp_path: Path) -> None:
        """Without a package, the plugin's declared packages are upgraded by name."""
        projects = _projects(tmp_path, "a")
        api = spec_api()
        api.tool.upgrade_project.side_effect = _emit([])

        result = asyncio.run(update_tool(api, "pip", directories=projects))

        assert result.plugin == "pip"
        kwargs = api.tool.upgrade_project.await_args.kwargs
        assert kwargs["plugin_names"] == {"pip"}
        assert kwargs["include_packages"] is None
        api.sync.inspect.assert_not_awaited()


# ---------------------------------------------------------------------------
# update_all_tools
# ---------------------------------------------------------------------------


class TestUpdateAllTools:
    """Tests for update_all_tools()."""

    @staticmethod
    @pytest.mark.parametrize(
        ("plugins", "include_packages"),
        [({"pip"}, set()), (set(), None), (set(), set())],
        ids=["no-packages", "no-plugins", "neither"],
    )
    def test_empty_selection_makes_no_call(
        tmp_path: Path, plugins: set[str] | None, include_packages: set[str] | None
    ) -> None:
        """An empty include set selects nothing, so nothing runs."""
        projects = _projects(tmp_path, "a")
        api = spec_api()

        result = asyncio.run(
            update_all_tools(api, plugins, include_packages, directories=projects)
        )

        assert result.manifests_processed == 0
        api.tool.upgrade_project.assert_not_awaited()

    @staticmethod
    def test_no_directories_makes_no_call() -> None:
        """No project directories → no upgrade."""
        api = spec_api()
        asyncio.run(update_all_tools(api, directories=[]))
        api.tool.upgrade_project.assert_not_awaited()

    @staticmethod
    def test_forwards_filters(tmp_path: Path) -> None:
        """Plugin and package filters are forwarded unchanged."""
        projects = _projects(tmp_path, "a")
        api = spec_api()
        api.tool.upgrade_project.side_effect = _emit([], manifests_processed=1)

        result = asyncio.run(
            update_all_tools(api, {"pip"}, {"requests"}, directories=projects)
        )

        assert result.manifests_processed == 1
        kwargs = api.tool.upgrade_project.await_args.kwargs
        assert kwargs["plugin_names"] == {"pip"}
        assert kwargs["include_packages"] == {"requests"}
        assert kwargs["paths"] == projects

    @staticmethod
    def test_callbacks_fire_per_package(tmp_path: Path) -> None:
        """Start and completion callbacks fire for each package in order."""
        projects = _projects(tmp_path, "a")
        pdm = _action("pdm", "pipx", kind=PluginKind.TOOL)
        ruff = _action("ruff", "pipx", kind=PluginKind.TOOL)
        api = spec_api()
        api.tool.upgrade_project.side_effect = _emit(
            [
                ActionStartedEvent(action=pdm),
                _completed(pdm, success=True),
                ActionStartedEvent(action=ruff),
                _completed(
                    ruff,
                    success=True,
                    skipped=True,
                    skip_reason=SkipReason.ALREADY_LATEST,
                ),
            ]
        )
        started: list[str] = []
        completed: list[tuple[str, bool, bool]] = []

        result = asyncio.run(
            update_all_tools(
                api,
                directories=projects,
                on_package_starting=started.append,
                on_package_completed=lambda name, ok, skip: completed.append(
                    (name, ok, skip)
                ),
            )
        )

        assert started == ["pdm", "ruff"]
        assert completed == [("pdm", True, False), ("ruff", False, True)]
        assert result.updated_packages == {"pdm"}
        assert result.already_latest == ["ruff"]


# ---------------------------------------------------------------------------
# remove_package
# ---------------------------------------------------------------------------


class TestRemovePackage:
    """Tests for remove_package()."""

    @staticmethod
    def _api(projects: list[Path], *results: ManagedPackageResult) -> MagicMock:
        api = spec_api()
        mock_inspect(
            api,
            report(
                manifest(
                    [
                        inspected(_action("requests"), index=0),
                        inspected(_action("requests", None), index=1),
                    ]
                ),
                mode=InspectionMode.FAST,
            ),
        )
        api.tool.uninstall.return_value = ManagedToolReport(
            operation="uninstall", results=results
        )
        return api

    def test_success(self, tmp_path: Path) -> None:
        """Successful removal returns True and targets the resolved action ids only."""
        projects = _projects(tmp_path, "a")
        api = self._api(
            projects,
            ManagedPackageResult(plugin="pip", package="requests", success=True),
        )

        assert (
            asyncio.run(remove_package(api, "pip", "requests", directories=projects))
            is True
        )
        kwargs = api.tool.uninstall.await_args.kwargs
        assert kwargs["action_ids"] == {"0:0"}
        assert kwargs["include_packages"] == {"requests"}

    def test_failure(self, tmp_path: Path) -> None:
        """Failed removal returns False."""
        projects = _projects(tmp_path, "a")
        api = self._api(
            projects,
            ManagedPackageResult(plugin="pip", package="requests", success=False),
        )

        assert (
            asyncio.run(remove_package(api, "pip", "requests", directories=projects))
            is False
        )

    def test_not_installed_is_not_success(self, tmp_path: Path) -> None:
        """A skipped (not installed) removal returns False."""
        projects = _projects(tmp_path, "a")
        api = self._api(
            projects,
            ManagedPackageResult(
                plugin="pip", package="requests", success=True, skipped=True
            ),
        )

        assert (
            asyncio.run(remove_package(api, "pip", "requests", directories=projects))
            is False
        )

    def test_undeclared_package_makes_no_call(self, tmp_path: Path) -> None:
        """A package no manifest declares through the plugin is not uninstalled."""
        projects = _projects(tmp_path, "a")
        api = self._api(projects)

        assert (
            asyncio.run(remove_package(api, "uv", "requests", directories=projects))
            is False
        )
        api.tool.uninstall.assert_not_awaited()
