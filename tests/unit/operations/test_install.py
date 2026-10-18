"""Tests for operations.install module."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from porringer.core.schema import PackageRef
from porringer.schema import (
    ActionCompletedEvent,
    ActionProgress,
    ActionProgressEvent,
    ActionStartedEvent,
    InspectionMode,
    InspectionStatus,
    ManifestFailedEvent,
    ManifestLoadedEvent,
    PluginCapability,
    PluginsDiscoveredEvent,
    ProgressEvent,
    SetupAction,
    SetupActionResult,
    SetupResults,
    SkipReason,
    SyncPlan,
)
from porringer.schema.plugin import PluginKind
from spurtle.operations.install import (
    execute_install,
    load_manifest_actions,
    preview_manifest,
    preview_manifest_stream,
)
from spurtle.operations.schema import (
    ActionInfo,
    PreviewActionChecked,
    PreviewEvent,
    PreviewManifestParsed,
    PreviewPlanned,
    PreviewPluginsQueried,
    PreviewReady,
    PreviewResult,
)

from ..porringer_factories import (
    checked,
    loaded,
    mock_plan,
    mock_sync_run,
    plugin_entry,
)

_URL = "https://example.com/manifest.json"


def _package_action(
    name: str = "requests", *, constraint: str | None = None, installer: str = "pip"
) -> SetupAction:
    """Build a real package ``SetupAction``."""
    return SetupAction(
        description=f"Install {name}",
        kind=PluginKind.PACKAGE,
        installer=installer,
        package=PackageRef(name=name, constraint=constraint),
    )


def _collect_stages(
    stream: AsyncIterator[tuple[str, ProgressEvent]],
) -> list[tuple[str, ProgressEvent]]:
    """Drain an ``execute_*`` stream into a list."""

    async def _run() -> list[tuple[str, ProgressEvent]]:
        return [item async for item in stream]

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# preview_manifest
# ---------------------------------------------------------------------------


class TestPreviewManifest:
    """Tests for preview_manifest()."""

    @staticmethod
    def test_no_manifest_returns_empty_result() -> None:
        """A plan without manifests yields an empty PreviewResult."""
        api = MagicMock()
        mock_plan(api)

        result = asyncio.run(preview_manifest(api, _URL))
        assert isinstance(result, PreviewResult)
        assert result.actions == []
        assert not result.project_name

    @staticmethod
    def test_parses_metadata_and_actions() -> None:
        """Extracts project name, description, and actions from the loaded manifest."""
        action = _package_action(constraint=">=2.0")
        api = MagicMock()
        mock_plan(
            api,
            [
                loaded(
                    [action],
                    manifest_path=Path("/tmp/manifest.json"),
                    name="My Project",
                    description="A test project",
                ),
                checked(action),
            ],
        )

        result = asyncio.run(preview_manifest(api, _URL))
        assert result.project_name == "My Project"
        assert result.description == "A test project"
        assert result.manifest_key == str(Path("/tmp/manifest.json"))
        assert result.actions == [
            ActionInfo(
                description="Install requests",
                kind="PACKAGE",
                package="requests",
                constraint=">=2.0",
                installer="pip",
            )
        ]

    @staticmethod
    def test_passes_parameters_correctly() -> None:
        """Verifies SetupParameters are constructed with correct args."""
        api = MagicMock()
        calls = mock_plan(api, [loaded()])

        asyncio.run(
            preview_manifest(
                api,
                "https://example.com/m.json",
                project_directory=Path("/proj"),
                prerelease_packages={"alpha-pkg"},
            )
        )

        assert calls[0].paths == ["https://example.com/m.json"]
        assert calls[0].inspection_mode == InspectionMode.COMPLETE
        assert calls[0].project_directory == Path("/proj")
        assert calls[0].prerelease_packages == {"alpha-pkg"}


# ---------------------------------------------------------------------------
# preview_manifest_stream
# ---------------------------------------------------------------------------


def _stream_events(
    api: MagicMock, *, discovered: MagicMock | None = None
) -> list[PreviewEvent]:
    """Collect every event from ``preview_manifest_stream`` without resolving the URL."""

    async def _run() -> list[PreviewEvent]:
        return [
            event
            async for event in preview_manifest_stream(
                api, _URL, resolve=False, discovered=discovered
            )
        ]

    return asyncio.run(_run())


class TestPreviewManifestStream:
    """Tests for the plan-backed preview_manifest_stream()."""

    @staticmethod
    def test_yields_listing_then_checked_statuses_then_plan() -> None:
        """Plugins, then the listing, then each check as it finishes, then the plan."""
        needed = _package_action("requests")
        latest = _package_action("ruff")
        upgradable = _package_action("black")
        api = MagicMock()
        calls = mock_plan(
            api,
            [
                PluginsDiscoveredEvent(
                    discovered_plugins=(
                        plugin_entry(
                            "pip", capabilities=[PluginCapability.RUNTIME_CONSUMER]
                        ),
                    )
                ),
                loaded([needed, latest, upgradable]),
                checked(
                    upgradable,
                    index=2,
                    status=InspectionStatus.UPDATE_AVAILABLE,
                    skipped=True,
                    skip_reason=SkipReason.UPDATE_AVAILABLE,
                    installed_version="24.1",
                    available_version="25.1",
                ),
                checked(needed, index=0, status=InspectionStatus.NEEDED),
                checked(
                    latest,
                    index=1,
                    status=InspectionStatus.SATISFIED,
                    skipped=True,
                    skip_reason=SkipReason.ALREADY_LATEST,
                    installed_version="0.8.0",
                ),
            ],
        )

        events = _stream_events(api)

        assert [type(e) for e in events] == [
            PreviewPluginsQueried,
            PreviewManifestParsed,
            PreviewReady,
            PreviewActionChecked,
            PreviewActionChecked,
            PreviewActionChecked,
            PreviewPlanned,
        ]
        assert [c.inspection_mode for c in calls] == [InspectionMode.COMPLETE]

        queried = events[0]
        assert isinstance(queried, PreviewPluginsQueried)
        assert queried.availability == {"pip": True}
        assert queried.capabilities == {
            "pip": frozenset({PluginCapability.RUNTIME_CONSUMER})
        }

        ready = events[2]
        assert isinstance(ready, PreviewReady)
        assert ready.manifest.actions == [needed, latest, upgradable]

        results = [e for e in events if isinstance(e, PreviewActionChecked)]
        assert [(e.index, e.status) for e in results] == [
            (2, "Update available"),
            (0, "Needed"),
            (1, "Already latest"),
        ]
        assert results[0].result.action is upgradable
        assert results[0].result.available_version == "25.1"
        assert results[2].result.installed_version == "0.8.0"

        planned = events[-1]
        assert isinstance(planned, PreviewPlanned)
        assert planned.plan.manifests[0].actions == [needed, latest, upgradable]

    @staticmethod
    def test_discovers_plugins_once_when_not_provided() -> None:
        """Without pre-discovered plugins the stream discovers once and plans with them."""
        api = MagicMock()
        mock_plan(api, [loaded()])
        discovered = MagicMock()
        api.discover_plugins = AsyncMock(return_value=discovered)

        _stream_events(api)

        api.discover_plugins.assert_awaited_once()
        assert api.sync.plan.await_args.kwargs["plugins"] is discovered

    @staticmethod
    def test_uses_provided_plugins() -> None:
        """Pre-discovered plugins skip discovery."""
        api = MagicMock()
        mock_plan(api, [loaded()])
        discovered = MagicMock()

        _stream_events(api, discovered=discovered)

        api.discover_plugins.assert_not_awaited()
        assert api.sync.plan.await_args.kwargs["plugins"] is discovered

    @staticmethod
    def test_failed_path_raises() -> None:
        """A manifest that failed to load surfaces its error."""
        api = MagicMock()
        mock_plan(
            api,
            [
                ManifestFailedEvent(
                    failed_path=(Path("/x"), "bad manifest"), manifest_index=0
                )
            ],
        )

        with pytest.raises(RuntimeError, match="bad manifest"):
            _stream_events(api)


# ---------------------------------------------------------------------------
# execute_install
# ---------------------------------------------------------------------------


class TestExecuteInstall:
    """Tests for execute_install()."""

    @staticmethod
    def test_yields_stage_event_tuples() -> None:
        """Yields (stage, event) tuples for different event types."""
        action = _package_action()
        manifest_loaded = ManifestLoadedEvent(manifest=SetupResults(actions=[action]))
        started = ActionStartedEvent(action=action)
        progress = ActionProgressEvent(
            action=action,
            progress=ActionProgress(
                action=action, phase="installing", output="ok", channel="stdout"
            ),
        )
        completed = ActionCompletedEvent(
            action=action, result=SetupActionResult(action=action, success=True)
        )
        other = PluginsDiscoveredEvent(discovered_plugins=())

        api = MagicMock()
        mock_sync_run(api, [manifest_loaded, started, progress, completed, other])

        results = _collect_stages(execute_install(api, Path("/m.json")))
        assert results == [
            ("manifest_loaded", manifest_loaded),
            ("action_started", started),
            ("sub_progress", progress),
            ("action_completed", completed),
            ("other", other),
        ]

    @staticmethod
    def test_empty_run() -> None:
        """No events → no yields."""
        api = MagicMock()
        mock_sync_run(api)

        assert _collect_stages(execute_install(api, Path("/m.json"))) == []

    @staticmethod
    def test_passes_parameters() -> None:
        """Run parameters carry the manifest, strategy, and prerelease overrides."""
        api = MagicMock()
        calls = mock_sync_run(api)

        _collect_stages(
            execute_install(
                api,
                Path("/m.json"),
                project_directory=Path("/proj"),
                prerelease_packages={"x"},
            )
        )

        assert calls[0].paths == [Path("/m.json")]
        assert calls[0].project_directory == Path("/proj")
        assert calls[0].prerelease_packages == {"x"}

    @staticmethod
    def test_runs_a_plan() -> None:
        """A previewed plan is run as-is instead of loading a manifest."""
        api = MagicMock()
        calls = mock_sync_run(api)
        plan = MagicMock(spec=SyncPlan)

        _collect_stages(execute_install(api, plan=plan))

        assert calls == [plan]


# ---------------------------------------------------------------------------
# load_manifest_actions
# ---------------------------------------------------------------------------


class TestLoadManifestActions:
    """Tests for load_manifest_actions()."""

    @staticmethod
    def test_returns_actions_from_fast_plan() -> None:
        """Actions come from a FAST plan of the manifest, which is then released."""
        first = _package_action("requests")
        second = _package_action("ruff")
        api = MagicMock()
        calls = mock_plan(api, [loaded([first, second])])
        discovered = MagicMock()

        actions = asyncio.run(
            load_manifest_actions(
                api,
                Path("/tmp/manifest.json"),
                project_directory=Path("/proj"),
                discovered=discovered,
            ),
        )

        assert actions == [first, second]
        assert calls[0].inspection_mode == InspectionMode.FAST
        assert calls[0].paths == [Path("/tmp/manifest.json")]
        assert calls[0].project_directory == Path("/proj")
        assert api.sync.plan.await_args.kwargs["plugins"] is discovered

    @staticmethod
    def test_no_manifest_returns_empty_list() -> None:
        """No manifest in the plan → empty list."""
        api = MagicMock()
        mock_plan(api)

        assert asyncio.run(load_manifest_actions(api, Path("/tmp/manifest.json"))) == []
