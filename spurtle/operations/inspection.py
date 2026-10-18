"""Async event streams over Porringer calls.

Porringer's ``sync.plan`` and ``sync.run`` report progress through an
``on_event`` callback.  The GUI and CLI consume async iterators, so
:class:`EventStream` bridges the two.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING

from porringer.schema import ProgressEvent, SetupParameters, SyncPlan

if TYPE_CHECKING:
    from porringer.api import API
    from porringer.backend.command.core.discovery import DiscoveredPlugins


class EventStream[T]:
    """The events of a Porringer call that reports through ``on_event``, as they happen.

    Iterate :meth:`events`; afterwards :attr:`result` holds the call's
    return value.  Closing the iterator cancels the call.  Errors raised
    by the call are re-raised after the events already received.
    """

    def __init__(
        self, call: Callable[[Callable[[ProgressEvent], None]], Awaitable[T]]
    ) -> None:
        """Wrap *call*, which is given the ``on_event`` callback to report through."""
        self._call = call
        self.result: T | None = None

    async def events(self) -> AsyncIterator[ProgressEvent]:
        """Run the call, yielding each event it reports."""
        queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()

        async def _run() -> None:
            try:
                self.result = await self._call(queue.put_nowait)
            finally:
                queue.put_nowait(None)

        task = asyncio.ensure_future(_run())
        try:
            while (event := await queue.get()) is not None:
                yield event
            await task
        finally:
            if not task.done():
                task.cancel()


async def run_events(
    porringer: API,
    parameters: SetupParameters | None = None,
    *,
    plan: SyncPlan | None = None,
    plugins: DiscoveredPlugins | None = None,
) -> AsyncIterator[ProgressEvent]:
    """Yield the progress events of ``porringer.sync.run`` for *parameters* or a *plan*."""
    if plan is not None:
        stream = EventStream(
            lambda on_event: porringer.sync.run(
                plan=plan, plugins=plugins, on_event=on_event
            )
        )
    else:
        stream = EventStream(
            lambda on_event: porringer.sync.run(
                parameters, plugins=plugins, on_event=on_event
            )
        )
    async for event in stream.events():
        yield event
