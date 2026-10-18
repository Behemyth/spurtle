"""Shared data coordinator for the Spurtle application.

Centralises porringer API calls so that plugin discovery, directory
listing, and runtime context resolution happen once per refresh cycle
and the results are reused by every consumer (ToolsView, ProjectsView,
TrayScreen, install workers).

The coordinator follows an *invalidate-on-mutation* strategy: callers
that modify state (install, uninstall, add/remove directory) call
:meth:`invalidate` to force the next :meth:`refresh` to re-fetch.
"""

from __future__ import annotations

import asyncio
import logging

from porringer.api import API
from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.schema import (
    CheckParameters,
    CheckResult,
)

from spurtle.application.schema import Snapshot
from spurtle.operations.project import list_projects

logger = logging.getLogger(__name__)


class DataCoordinator:
    """Single source of truth for porringer data across the application.

    Usage::

        coordinator = DataCoordinator(porringer)
        snapshot = await coordinator.refresh()  # first load
        # … later, after an install …
        coordinator.invalidate()
        snapshot = await coordinator.refresh()  # re-fetches everything

    The coordinator caches the most recent :class:`Snapshot` so that
    synchronous property access (``coordinator.snapshot``) is available
    between refresh cycles.
    """

    def __init__(self, porringer: API) -> None:
        """Initialize the coordinator with a porringer API instance."""
        self._porringer = porringer
        self._snapshot: Snapshot = Snapshot()
        self._stale = True
        self._refresh_lock = asyncio.Lock()

    # -- Public API --------------------------------------------------------

    @property
    def snapshot(self) -> Snapshot:
        """Return the most recent snapshot (may be empty before first refresh)."""
        return self._snapshot

    @property
    def discovered_plugins(self) -> DiscoveredPlugins | None:
        """Shortcut to the current ``DiscoveredPlugins`` instance."""
        return self._snapshot.discovered

    @property
    def is_stale(self) -> bool:
        """Whether the cached data needs refreshing."""
        return self._stale

    def invalidate(self) -> None:
        """Mark the cached data as stale.

        The next call to :meth:`refresh` will re-fetch everything from
        porringer.  This is a lightweight O(1) flag-flip.
        """
        self._stale = True

    async def refresh(self, *, force: bool = False) -> Snapshot:
        """Fetch fresh data from porringer if stale (or *force* is set).

        Multiple concurrent callers are coalesced via an ``asyncio.Lock``
        so that only one discovery + listing round-trip runs at a time.

        Returns:
            The populated :class:`Snapshot`.
        """
        if not self._stale and not force:
            return self._snapshot

        async with self._refresh_lock:
            # Double-check after acquiring the lock — another coroutine
            # may have already refreshed while we were waiting.
            if not self._stale and not force:
                return self._snapshot

            self._snapshot = await self._fetch()
            self._stale = False
            return self._snapshot

    async def check_updates(
        self,
        plugins: list[str] | None = None,
    ) -> list[CheckResult]:
        """Run update detection using the cached ``DiscoveredPlugins``.

        Args:
            plugins: Optional include-set of plugin names.  ``None``
                means all plugins.

        Returns:
            A list of :class:`CheckResult` per plugin.
        """
        params = CheckParameters(
            plugin_names=set(plugins) if plugins is not None else None
        )
        return await self._porringer.package.check_updates(
            params,
            plugins=self._snapshot.discovered,
        )

    # -- Internals ---------------------------------------------------------

    async def _fetch(self) -> Snapshot:
        """Run the full discovery + listing pipeline.

        1. ``API.discover_plugins()`` — plugin entry-points + runtime
           context in one shot.
        2. ``api.extension.list()`` — installed status + versions,
           passing the already-discovered ``DiscoveredPlugins``.
        3. Registered project directories with validation baked in.

        All blocking calls are dispatched via ``asyncio.to_thread``.
        """
        discovered = await API.discover_plugins()

        plugins_task = asyncio.create_task(
            self._porringer.extension.list(plugins=discovered),
        )
        directories = await asyncio.to_thread(list_projects, self._porringer)
        plugins = await plugins_task

        capabilities: dict[str, frozenset] = {
            plugin.name: frozenset(discovered.capabilities(plugin.name))
            for plugin in plugins
        }

        logger.info(
            "Discovery complete: %d plugin(s), %d directory(ies)",
            len(plugins),
            len(directories),
        )

        return Snapshot(
            plugins=plugins,
            directories=directories,
            discovered=discovered,
            plugin_capabilities=capabilities,
        )
