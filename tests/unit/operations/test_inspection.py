"""Tests for operations.inspection module."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import aclosing
from unittest.mock import MagicMock

import pytest
from porringer.core.schema import PackageRef
from porringer.schema import (
    ActionStartedEvent,
    ProgressEvent,
    SetupAction,
    SetupParameters,
)
from porringer.schema.plugin import PluginKind
from spurtle.operations.inspection import EventStream, run_events

from ..porringer_factories import spec_api


def _action(name: str = "requests") -> SetupAction:
    """Build a plain package action."""
    return SetupAction(
        description=f"Install {name}",
        kind=PluginKind.PACKAGE,
        installer="pip",
        package=PackageRef(name=name),
    )


class TestEventStream:
    """Tests for EventStream."""

    @staticmethod
    def test_result_is_available_after_the_events() -> None:
        """The call's return value is kept once its events are consumed."""
        event = ActionStartedEvent(action=_action())

        async def _call(on_event: Callable[[ProgressEvent], None]) -> str:
            on_event(event)
            return "done"

        stream = EventStream(_call)

        async def _collect() -> list[ProgressEvent]:
            return [item async for item in stream.events()]

        assert asyncio.run(_collect()) == [event]
        assert stream.result == "done"


class TestRunEvents:
    """Tests for run_events()."""

    @staticmethod
    def _events(count: int) -> list[ProgressEvent]:
        return [ActionStartedEvent(action=_action(f"pkg{i}")) for i in range(count)]

    def test_yields_events_in_order(self) -> None:
        """Events delivered through ``on_event`` are yielded in order."""
        events = self._events(3)
        api = spec_api()

        async def _run(
            _params: SetupParameters,
            *,
            on_event: Callable[[ProgressEvent], None],
            **_kw: object,
        ) -> None:
            for event in events:
                on_event(event)
                await asyncio.sleep(0)

        api.sync.run.side_effect = _run

        async def _collect() -> list[ProgressEvent]:
            return [event async for event in run_events(api, SetupParameters(paths=[]))]

        assert asyncio.run(_collect()) == events

    @staticmethod
    def test_forwards_parameters_and_plugins() -> None:
        """The parameters and plugins reach ``sync.run``."""
        api = spec_api()
        params = SetupParameters(paths=[])
        plugins = MagicMock()

        async def _collect() -> None:
            async for _event in run_events(api, params, plugins=plugins):
                pass

        asyncio.run(_collect())

        assert api.sync.run.await_args.args == (params,)
        assert api.sync.run.await_args.kwargs["plugins"] is plugins

    @staticmethod
    def test_runs_a_plan() -> None:
        """A plan is passed to ``sync.run`` in place of parameters."""
        api = spec_api()
        plan = MagicMock()

        async def _collect() -> None:
            async for _event in run_events(api, plan=plan):
                pass

        asyncio.run(_collect())

        assert api.sync.run.await_args.args == ()
        assert api.sync.run.await_args.kwargs["plan"] is plan

    def test_reraises_run_error_after_events(self) -> None:
        """A run error is raised after the events already delivered."""
        events = self._events(2)
        api = spec_api()

        async def _run(
            _params: SetupParameters,
            *,
            on_event: Callable[[ProgressEvent], None],
            **_kw: object,
        ) -> None:
            for event in events:
                on_event(event)
            raise RuntimeError("run failed")

        api.sync.run.side_effect = _run
        received: list[ProgressEvent] = []

        async def _collect() -> None:
            async for event in run_events(api, SetupParameters(paths=[])):
                received.append(event)

        with pytest.raises(RuntimeError, match="run failed"):
            asyncio.run(_collect())
        assert received == events

    def test_closing_early_cancels_run(self) -> None:
        """Stopping consumption cancels the in-flight run."""
        api = spec_api()
        cancelled = False

        async def _run(
            _params: SetupParameters,
            *,
            on_event: Callable[[ProgressEvent], None],
            **_kw: object,
        ) -> None:
            nonlocal cancelled
            on_event(self._events(1)[0])
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled = True
                raise

        api.sync.run.side_effect = _run

        async def _consume_one() -> None:
            async with aclosing(run_events(api, SetupParameters(paths=[]))) as stream:
                async for _event in stream:
                    break
            await asyncio.sleep(0)

        asyncio.run(_consume_one())

        assert cancelled
