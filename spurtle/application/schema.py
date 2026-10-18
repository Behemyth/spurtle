"""Application-layer data models.

Contains data structures shared across application modules — the
``DataCoordinator`` snapshot and the tool-update result summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.schema import (
    PluginCapability,
    PluginInfo,
)

from spurtle.operations.schema import ProjectInfo


@dataclass(slots=True)
class Snapshot:
    """Immutable bundle of data produced by a single refresh cycle.

    All fields are populated by :meth:`DataCoordinator.refresh` and
    remain stable until the next refresh.
    """

    plugins: list[PluginInfo] = field(default_factory=list)
    """All discovered plugins with install status and version info."""

    directories: list[ProjectInfo] = field(default_factory=list)
    """Registered project directories with ``exists`` / ``has_manifest`` validation."""

    discovered: DiscoveredPlugins | None = None
    """Full plugin discovery result including runtime context."""

    plugin_capabilities: dict[str, frozenset[PluginCapability]] = field(
        default_factory=dict
    )
    """Protocol capabilities reported for each discovered plugin."""

    setup_manifests: list[SetupManifest] = field(default_factory=list)
    """Remote manifest URLs imported from setup profiles."""


@dataclass(frozen=True, slots=True)
class SetupManifest:
    """A remote manifest URL imported from a setup profile."""

    url: str
    """The HTTPS URL of the manifest."""

    name: str
    """Human-readable label derived from the profile or the URL."""

    profile_url: str
    """The profile URL that imported this manifest."""
