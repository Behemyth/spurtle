"""Tests for the install preview window and URI-based install flow."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from porringer.core.schema import PackageRef
from porringer.schema import (
    ActionCompletedEvent,
    DownloadResult,
    InspectionMode,
    InspectionStatus,
    ManifestLoadedEvent,
    PluginCapability,
    PluginsDiscoveredEvent,
    SetupAction,
    SetupActionResult,
    SetupParameters,
    SetupResults,
    SkipReason,
    SyncInspectionReport,
    SyncPlan,
)
from porringer.schema.plugin import PluginKind
from spurtle.application.screen import (
    ACTION_KIND_LABELS,
    SKIP_REASON_LABELS,
    format_cli_command,
    skip_reason_label,
)
from spurtle.application.screen.install_workers import run_install, run_preview
from spurtle.application.screen.schema import PreviewConfig
from spurtle.application.uri import normalize_manifest_key, resolve_local_path
from spurtle.operations.schema import (
    PreviewActionChecked,
    PreviewEvent,
    PreviewManifestParsed,
    PreviewPlanned,
    PreviewPluginsQueried,
    PreviewReady,
)

from ..porringer_factories import (
    checked,
    loaded,
    mock_plan,
    mock_sync_run,
    plugin_entry,
)

_DOWNLOAD_PATCH = "spurtle.application.screen.install_workers.API.download"
_EXPECTED_CHECKED_COUNT = 2


def _package_action(
    name: str = "requests",
    *,
    kind: PluginKind = PluginKind.PACKAGE,
    installer: str = "pip",
) -> SetupAction:
    """Build a real package ``SetupAction``."""
    return SetupAction(
        description=f"Install {name}",
        kind=kind,
        installer=installer,
        package=PackageRef(name=name),
    )


def _scm_action(description: str = "Clone repo") -> SetupAction:
    """Build a real SCM ``SetupAction``."""
    return SetupAction(description=description, kind=PluginKind.SCM, installer="git")


def _collect(porringer: MagicMock, url: str, **kwargs: Any) -> list[PreviewEvent]:
    """Run ``run_preview`` and return every emitted event."""
    events: list[PreviewEvent] = []
    asyncio.run(run_preview(porringer, url, on_event=events.append, **kwargs))
    return events


class TestInstallPreviewWindow:
    """Tests for InstallPreviewWindow table population logic."""

    @staticmethod
    def test_action_kind_labels() -> None:
        """Verify action kind label mapping covers all kinds plus None."""
        for plugin_kind in PluginKind:
            assert plugin_kind in ACTION_KIND_LABELS
        assert None in ACTION_KIND_LABELS

    @staticmethod
    def test_skip_reason_labels() -> None:
        """Verify skip reason label mapping covers all reasons."""
        for reason in SkipReason:
            assert reason in SKIP_REASON_LABELS

    @staticmethod
    def test_skip_reason_label_human_readable() -> None:
        """Verify skip reason labels are human-readable, not raw enum names."""
        assert skip_reason_label(SkipReason.ALREADY_INSTALLED) == "Already installed"
        assert skip_reason_label(SkipReason.ALREADY_LATEST) == "Already latest"
        assert skip_reason_label(SkipReason.UPDATE_AVAILABLE) == "Update available"
        assert skip_reason_label(None) == "Skipped"


class TestFormatCliCommand:
    """Tests for format_cli_command helper."""

    @staticmethod
    def _make_action(**overrides: Any) -> MagicMock:
        """Create a mock SetupAction with optional attribute overrides."""
        defaults: dict[str, Any] = {
            "kind": "PACKAGE",
            "description": "Install test",
            "installer": "pip",
            "package": "requests",
            "command": None,
        }
        defaults.update(overrides)
        action = MagicMock()
        kind = defaults["kind"]
        action.kind = getattr(PluginKind, kind) if isinstance(kind, str) else kind
        action.description = defaults["description"]
        action.installer = defaults["installer"]
        action.package = defaults["package"]
        action.command = defaults["command"]
        return action

    def test_prefers_cli_command_from_result(self) -> None:
        """Verify result cli_command takes precedence over command and fallback."""
        action = self._make_action()
        result = MagicMock()
        result.cli_command = ("uv", "pip", "install", "requests")
        assert format_cli_command(action, result=result) == "uv pip install requests"

    def test_falls_back_to_command(self) -> None:
        """Verify command is used when cli_command is absent."""
        action = self._make_action(
            kind="TOOL",
            command=("echo", "hello"),
        )
        assert format_cli_command(action) == "echo hello"

    def test_synthesises_package_command(self) -> None:
        """Verify package actions synthesise installer + package."""
        action = self._make_action(installer="pip", package="ruff")
        assert format_cli_command(action) == "pip install ruff"

    def test_synthesises_default_installer(self) -> None:
        """Verify pip is used as default installer for package actions."""
        action = self._make_action(installer=None, package="ruff")
        assert format_cli_command(action) == "pip install ruff"

    def test_description_fallback(self) -> None:
        """Verify description is returned when nothing else is available."""
        action = self._make_action(
            kind="TOOL",
            description="Custom step",
            package=None,
        )
        assert format_cli_command(action) == "Custom step"


class TestInstallWorker:
    """Tests for run_install coroutine."""

    @staticmethod
    def _plan() -> SyncPlan:
        return SyncPlan(parameters=SetupParameters(), report=SyncInspectionReport())

    def test_worker_returns_results_on_success(self) -> None:
        """Verify coroutine returns the loaded actions and completed results."""
        porringer = MagicMock()
        action = _package_action()
        manifest = SetupResults(actions=[action])
        result = SetupActionResult(action=action, success=True)
        mock_sync_run(
            porringer,
            [
                ManifestLoadedEvent(manifest=manifest),
                ActionCompletedEvent(action=action, result=result, action_index=0),
            ],
        )

        results = asyncio.run(run_install(porringer, self._plan()))

        assert results.actions == [action]
        assert results.results == [result]

    def test_worker_raises_on_failure(self) -> None:
        """Verify coroutine re-raises a run error."""
        porringer = MagicMock()
        mock_sync_run(porringer, error=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(run_install(porringer, self._plan()))

    def test_worker_runs_the_previewed_plan(self) -> None:
        """Verify the plan itself is run, not a reloaded manifest."""
        porringer = MagicMock()
        calls = mock_sync_run(
            porringer, [ManifestLoadedEvent(manifest=SetupResults(actions=[]))]
        )
        plan = self._plan()

        asyncio.run(run_install(porringer, plan))

        assert calls == [plan]


class TestResolveLocalPath:
    """Tests for _resolve_local_path helper."""

    @staticmethod
    def test_http_url_returns_none() -> None:
        """HTTP URLs should not resolve to a local path."""
        assert resolve_local_path("https://example.com/porringer.json") is None

    @staticmethod
    def test_absolute_path_returns_path(tmp_path: Path) -> None:
        """Absolute OS paths should resolve."""
        path = str(tmp_path / "porringer.json")
        result = resolve_local_path(path)
        assert result is not None
        assert result == Path(path)

    @staticmethod
    def test_file_uri_returns_path() -> None:
        """file:// URIs should resolve to a local path."""
        result = resolve_local_path("file:///C:/Users/test/porringer.json")
        assert result is not None
        assert "porringer.json" in str(result)

    @staticmethod
    def test_existing_relative_path(tmp_path: Path) -> None:
        """Relative paths that exist on disk should resolve."""
        manifest_file = tmp_path / "porringer.json"
        manifest_file.write_text("{}")
        result = resolve_local_path(str(manifest_file))
        assert result is not None


class TestPreviewWorkerLocal:
    """Tests for run_preview with local manifest files."""

    @staticmethod
    def test_local_manifest_skips_download(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify run_preview skips download for local files."""
        manifest_file = tmp_path / "porringer.json"
        manifest_file.write_text("{}")
        download = AsyncMock()
        monkeypatch.setattr(_DOWNLOAD_PATCH, download)

        porringer = MagicMock()
        mock_plan(porringer, [loaded(manifest_path=manifest_file)])

        events = _collect(porringer, str(manifest_file))

        ready = [e for e in events if isinstance(e, PreviewReady)]
        assert len(ready) == 1
        assert ready[0].manifest.actions == []
        assert ready[0].manifest_path == str(manifest_file)
        assert not ready[0].temp_dir
        download.assert_not_called()

    @staticmethod
    def test_local_manifest_not_found(tmp_path: Path) -> None:
        """Verify run_preview raises error for missing local file."""
        path = str(tmp_path / "nonexistent" / "porringer.json")
        porringer = MagicMock()

        with pytest.raises(FileNotFoundError, match="not found"):
            asyncio.run(run_preview(porringer, path))


class TestPreviewWorker:
    """Tests for run_preview download and preview flow."""

    @staticmethod
    def test_emits_error_on_download_failure(monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify run_preview raises when download fails."""
        porringer = MagicMock()

        async def _mock_download(
            params: Any, progress_callback: Any = None
        ) -> DownloadResult:
            return DownloadResult(
                success=False,
                path=None,
                verified=False,
                size=0,
                message="Network error",
            )

        monkeypatch.setattr(_DOWNLOAD_PATCH, _mock_download)

        with pytest.raises(RuntimeError, match="Network error"):
            asyncio.run(run_preview(porringer, "https://example.com/bad.json"))

    @staticmethod
    def test_emits_preview_ready_on_success(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Verify run_preview inspects the downloaded manifest and emits PreviewReady."""
        porringer = MagicMock()

        async def _mock_download(
            params: Any, progress_callback: Any = None
        ) -> DownloadResult:
            params.destination.write_text("{}")
            return DownloadResult(
                success=True,
                path=params.destination,
                verified=True,
                size=100,
                message="OK",
            )

        monkeypatch.setattr(_DOWNLOAD_PATCH, _mock_download)
        action = _package_action()
        calls = mock_plan(porringer, [loaded([action])])

        events = _collect(porringer, "https://example.com/good.json")

        ready = [e for e in events if isinstance(e, PreviewReady)]
        assert len(ready) == 1
        assert ready[0].manifest.actions == [action]
        assert ready[0].temp_dir
        assert calls[0].paths[0] == Path(ready[0].temp_dir) / "porringer.json"


class TestPreviewWorkerSignals:
    """Tests for run_preview event emission from the plan's inspection events."""

    @staticmethod
    def test_emits_preview_ready_and_action_checked(tmp_path: Path) -> None:
        """Verify the loaded manifest lists actions and each check reports status."""
        manifest_file = tmp_path / "porringer.json"
        manifest_file.write_text("{}")
        action = _package_action()

        porringer = MagicMock()
        mock_plan(
            porringer,
            [loaded([action]), checked(action, status=InspectionStatus.NEEDED)],
        )

        events = _collect(porringer, str(manifest_file))

        ready = [e for e in events if isinstance(e, PreviewReady)]
        results = [e for e in events if isinstance(e, PreviewActionChecked)]
        assert len(ready) == 1
        assert ready[0].manifest.actions == [action]
        assert len(results) == 1
        assert results[0].index == 0
        assert results[0].result.action is action
        assert results[0].result.success is True
        assert results[0].status == "Needed"

    @staticmethod
    def test_completes_for_empty_actions(tmp_path: Path) -> None:
        """Verify coroutine completes normally with no actions."""
        manifest_file = tmp_path / "porringer.json"
        manifest_file.write_text("{}")

        porringer = MagicMock()
        mock_plan(porringer, [loaded()])

        events = _collect(porringer, str(manifest_file))

        assert not [e for e in events if isinstance(e, PreviewActionChecked)]
        assert [e for e in events if isinstance(e, PreviewReady)]

    @staticmethod
    def test_action_checked_maps_rows_by_action_index(tmp_path: Path) -> None:
        """Verify each checked result carries its own action's row index, in completion order."""
        manifest_file = tmp_path / "porringer.json"
        manifest_file.write_text("{}")
        action_a = _package_action("python", kind=PluginKind.RUNTIME, installer="pim")
        action_b = _package_action("requests")

        porringer = MagicMock()
        mock_plan(
            porringer,
            [
                loaded([action_a, action_b]),
                checked(
                    action_b,
                    index=1,
                    status=InspectionStatus.SATISFIED,
                    skipped=True,
                    skip_reason=SkipReason.ALREADY_INSTALLED,
                ),
                checked(action_a, index=0, status=InspectionStatus.NEEDED),
            ],
        )

        results = [
            e
            for e in _collect(porringer, str(manifest_file))
            if isinstance(e, PreviewActionChecked)
        ]

        assert len(results) == _EXPECTED_CHECKED_COUNT
        assert [(e.index, e.result.action, e.status) for e in results] == [
            (1, action_b, "Already installed"),
            (0, action_a, "Needed"),
        ]

    @staticmethod
    def test_raises_when_planning_fails(tmp_path: Path) -> None:
        """Verify coroutine raises when planning fails."""
        manifest_file = tmp_path / "porringer.json"
        manifest_file.write_text("{}")

        porringer = MagicMock()
        porringer.discover_plugins = AsyncMock(return_value=MagicMock())
        porringer.sync.plan = AsyncMock(side_effect=RuntimeError("plan boom"))

        with pytest.raises(RuntimeError, match="plan boom"):
            asyncio.run(run_preview(porringer, str(manifest_file)))

    @staticmethod
    def test_emits_plugins_queried(tmp_path: Path) -> None:
        """Verify plugin availability and capabilities come from the plugins event."""
        manifest_file = tmp_path / "porringer.json"
        manifest_file.write_text("{}")

        porringer = MagicMock()
        mock_plan(
            porringer,
            [
                PluginsDiscoveredEvent(
                    discovered_plugins=(
                        plugin_entry(
                            "pip", capabilities=[PluginCapability.RUNTIME_CONSUMER]
                        ),
                        plugin_entry("uv", available=False),
                    )
                ),
                loaded(),
            ],
        )

        queried = [
            e
            for e in _collect(porringer, str(manifest_file))
            if isinstance(e, PreviewPluginsQueried)
        ]

        assert len(queried) == 1
        assert queried[0].availability == {"pip": True, "uv": False}
        assert queried[0].capabilities == {
            "pip": frozenset({PluginCapability.RUNTIME_CONSUMER}),
            "uv": frozenset(),
        }

    @staticmethod
    def test_emits_manifest_parsed_with_metadata(tmp_path: Path) -> None:
        """Verify PreviewManifestParsed carries the manifest metadata."""
        manifest_file = tmp_path / "porringer.json"
        manifest_file.write_text("{}")

        porringer = MagicMock()
        mock_plan(
            porringer,
            [loaded(manifest_path=manifest_file, name="Demo", description="A demo")],
        )

        parsed = [
            e
            for e in _collect(porringer, str(manifest_file))
            if isinstance(e, PreviewManifestParsed)
        ]

        assert len(parsed) == 1
        assert parsed[0].manifest.metadata is not None
        assert parsed[0].manifest.metadata.name == "Demo"
        assert parsed[0].manifest_path == str(manifest_file)

    @staticmethod
    def test_event_order(tmp_path: Path) -> None:
        """Verify the event order: plugins, parsed, ready, checked, then the plan."""
        manifest_file = tmp_path / "porringer.json"
        manifest_file.write_text("{}")
        action = _package_action()

        porringer = MagicMock()
        mock_plan(
            porringer,
            [
                PluginsDiscoveredEvent(discovered_plugins=(plugin_entry("pip"),)),
                loaded([action]),
                checked(action),
            ],
        )

        order = [type(e).__name__ for e in _collect(porringer, str(manifest_file))]

        assert order == [
            "PreviewPluginsQueried",
            "PreviewManifestParsed",
            "PreviewReady",
            "PreviewActionChecked",
            "PreviewPlanned",
        ]

    @staticmethod
    def test_plans_once_with_complete_inspection(tmp_path: Path) -> None:
        """Verify the preview is a single complete plan, handed over at the end."""
        manifest_file = tmp_path / "porringer.json"
        manifest_file.write_text("{}")
        action = _package_action()

        porringer = MagicMock()
        calls = mock_plan(porringer, [loaded([action])])

        events = _collect(porringer, str(manifest_file))

        assert [c.inspection_mode for c in calls] == [InspectionMode.COMPLETE]
        planned = events[-1]
        assert isinstance(planned, PreviewPlanned)
        assert planned.plan.manifests[0].actions == [action]


class TestPreviewWorkerPrerelease:
    """Tests for run_preview passing prerelease config to porringer."""

    @staticmethod
    def test_passes_prerelease_packages(tmp_path: Path) -> None:
        """Verify prerelease_packages are forwarded to SetupParameters."""
        manifest_file = tmp_path / "porringer.json"
        manifest_file.write_text("{}")

        porringer = MagicMock()
        calls = mock_plan(porringer, [loaded()])

        asyncio.run(
            run_preview(
                porringer,
                str(manifest_file),
                config=PreviewConfig(prerelease_packages={"some-pkg"}),
            ),
        )

        assert calls
        assert all(c.prerelease_packages == {"some-pkg"} for c in calls)

    @staticmethod
    def test_defaults_prerelease_none(tmp_path: Path) -> None:
        """Verify prerelease_packages defaults to None."""
        manifest_file = tmp_path / "porringer.json"
        manifest_file.write_text("{}")

        porringer = MagicMock()
        calls = mock_plan(porringer, [loaded()])

        asyncio.run(run_preview(porringer, str(manifest_file)))

        assert calls
        assert all(c.prerelease_packages is None for c in calls)


class TestNormalizeManifestKey:
    """Tests for normalize_manifest_key helper."""

    @staticmethod
    def test_http_url_passthrough() -> None:
        """HTTP URLs are returned unchanged."""
        url = "https://example.com/porringer.json"
        assert normalize_manifest_key(url) == url

    @staticmethod
    def test_local_path_resolved(tmp_path: Path) -> None:
        """Local paths are resolved to absolute form."""
        result = normalize_manifest_key(str(tmp_path / "manifest"))
        assert Path(result).is_absolute()

    @staticmethod
    def test_relative_path_resolved() -> None:
        """Relative paths are resolved relative to cwd."""
        result = normalize_manifest_key("some/relative/path")
        assert Path(result).is_absolute()


class TestPreviewWorkerProjectDirectory:
    """Tests for project_directory forwarding to the inspection."""

    @staticmethod
    def test_file_path_forwards_parent_as_project_directory(tmp_path: Path) -> None:
        """When a manifest *file* is selected, its parent dir is forwarded."""
        manifest_file = tmp_path / "porringer.json"
        manifest_file.write_text("{}")

        porringer = MagicMock()
        calls = mock_plan(porringer, [loaded()])

        asyncio.run(
            run_preview(
                porringer,
                str(manifest_file),
                config=PreviewConfig(project_directory=manifest_file.parent),
            ),
        )

        assert calls
        assert all(c.project_directory == tmp_path for c in calls)

    @staticmethod
    def test_directory_path_forwarded_directly(tmp_path: Path) -> None:
        """When a directory is selected its path is forwarded as-is."""
        manifest_file = tmp_path / "porringer.json"
        manifest_file.write_text("{}")

        porringer = MagicMock()
        calls = mock_plan(porringer, [loaded()])

        asyncio.run(
            run_preview(
                porringer,
                str(tmp_path),
                config=PreviewConfig(project_directory=tmp_path),
            ),
        )

        assert calls
        assert all(c.project_directory == tmp_path for c in calls)


class TestSCMPreviewActions:
    """Tests for SCM (git clone) actions in the preview inspection flow."""

    @staticmethod
    def test_scm_already_installed_emits_correct_result(tmp_path: Path) -> None:
        """An SCM action with ALREADY_INSTALLED is surfaced as checked."""
        manifest_file = tmp_path / "porringer.json"
        manifest_file.write_text("{}")
        action = _scm_action()

        porringer = MagicMock()
        mock_plan(
            porringer,
            [
                loaded([action]),
                checked(
                    action,
                    status=InspectionStatus.SATISFIED,
                    skipped=True,
                    skip_reason=SkipReason.ALREADY_INSTALLED,
                ),
            ],
        )

        results = [
            e
            for e in _collect(
                porringer,
                str(manifest_file),
                config=PreviewConfig(project_directory=tmp_path),
            )
            if isinstance(e, PreviewActionChecked)
        ]

        assert len(results) == 1
        assert results[0].index == 0
        assert results[0].result.action == action
        assert results[0].result.skipped is True
        assert results[0].result.skip_reason == SkipReason.ALREADY_INSTALLED

    @staticmethod
    def test_scm_needed_emits_correct_result(tmp_path: Path) -> None:
        """An SCM action that is *not* installed is surfaced as Needed."""
        manifest_file = tmp_path / "porringer.json"
        manifest_file.write_text("{}")
        action = _scm_action()

        porringer = MagicMock()
        mock_plan(
            porringer,
            [loaded([action]), checked(action, status=InspectionStatus.NEEDED)],
        )

        results = [
            e
            for e in _collect(
                porringer,
                str(manifest_file),
                config=PreviewConfig(project_directory=tmp_path),
            )
            if isinstance(e, PreviewActionChecked)
        ]

        assert len(results) == 1
        assert results[0].result.skipped is False
        assert results[0].result.skip_reason is None
        assert results[0].status == "Needed"
