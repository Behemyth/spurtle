"""Tests that the preview widget installs exactly the plan it previewed."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from porringer.schema import (
    SetupParameters,
    SetupResults,
    SyncInspectionReport,
    SyncPlan,
)
from porringer.utility.exception import PlanStaleError
from PySide6.QtWidgets import QMessageBox
from spurtle.application.screen.install import SetupPreviewWidget
from spurtle.application.screen.schema import PreviewPhase
from spurtle.operations.schema import PreviewPlanned

_RUN_INSTALL = "spurtle.application.screen.install.run_install"
_KEY = "manifest-key"


def _plan() -> SyncPlan:
    return SyncPlan(parameters=SetupParameters(), report=SyncInspectionReport())


def _ready_widget(plan: SyncPlan) -> SetupPreviewWidget:
    """A widget whose preview finished with *plan*."""
    widget = SetupPreviewWidget(MagicMock())
    model = widget.model
    model.manifest_key = _KEY
    model.project_directory = model.previewed_project_directory = Path("project")
    model.install_plan = MagicMock(satisfied_indices=[])
    model.phase = PreviewPhase.READY
    widget._on_preview_event(PreviewPlanned(plan=plan))
    return widget


def _click_install(widget: SetupPreviewWidget) -> None:
    async def _run() -> None:
        widget._on_install()
        if widget._task is not None:
            await widget._task

    asyncio.run(_run())


def test_install_runs_the_previewed_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install hands the stored plan to the worker and lets go of it."""
    plan = _plan()
    widget = _ready_widget(plan)
    run = AsyncMock(return_value=SetupResults(actions=[]))
    monkeypatch.setattr(_RUN_INSTALL, run)

    _click_install(widget)

    assert run.await_args is not None
    assert run.await_args.args[1] is plan
    assert widget.model.plan is None


def test_stale_plan_reloads_the_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the manifest changed since the preview, the preview is shown again instead."""
    widget = _ready_widget(_plan())
    monkeypatch.setattr(
        _RUN_INSTALL, AsyncMock(side_effect=PlanStaleError([Path("porringer.json")]))
    )
    monkeypatch.setattr(QMessageBox, "information", MagicMock())
    load = MagicMock()
    monkeypatch.setattr(widget, "load", load)

    _click_install(widget)

    load.assert_called_once_with(_KEY, project_directory=Path("project"))


def test_changed_project_directory_previews_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A project directory changed after the preview is previewed before anything runs."""
    widget = _ready_widget(_plan())
    run = AsyncMock()
    monkeypatch.setattr(_RUN_INSTALL, run)
    load = MagicMock()
    monkeypatch.setattr(widget, "load", load)
    widget.set_project_directory(Path("elsewhere"))

    _click_install(widget)

    run.assert_not_awaited()
    load.assert_called_once_with(_KEY, project_directory=Path("elsewhere"))


def test_reset_discards_an_unused_plan() -> None:
    """Leaving a preview releases its plan."""
    plan = _plan()
    widget = _ready_widget(plan)

    widget.reset()

    assert plan.consumed
    assert widget.model.plan is None
