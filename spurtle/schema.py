"""Core data models for the Spurtle.

Contains configuration schemas (Pydantic), update-lifecycle enums and
dataclasses, and the immutable runtime configuration snapshot.  These
types are intentionally decoupled from I/O, business logic, and UI so
that every layer can import them without circular dependencies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, StrEnum, auto
from typing import Any

from packaging.version import Version
from pydantic import BaseModel, ValidationError, model_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BuildConfig — read-only, lives next to the executable
# ---------------------------------------------------------------------------


class BuildConfig(BaseModel):
    """Read-only configuration embedded next to the executable.

    Written by the packaging script (e.g. ``pdm run package -- --local-source``).
    Only contains the two fields the build system needs to seed.
    """

    # URL pointing to the .appinstaller feed (or parent directory).
    update_source: str | None = None

    # Update channel: "stable" or "dev".
    update_channel: str | None = None


# ---------------------------------------------------------------------------
# UserConfig — read-write, lives in the OS data directory
# ---------------------------------------------------------------------------


class UserConfig(BaseModel):
    """User-scoped configuration persisted in the OS application data directory.

    On Windows: ``%LOCALAPPDATA%/Synodic/config.json``.

    Every field is always saved.  There are no sparse/unset semantics —
    the on-disk file is a complete snapshot of the user's preferences.
    """

    # URL pointing to the .appinstaller feed (or parent directory).
    # None means use the default GitHub release source.
    update_source: str | None = None

    # Update channel: "stable" or "dev".
    # None means auto-detect from sys.frozen.
    update_channel: str | None = None

    # Interval in minutes between automatic update checks.
    # 0 disables automatic checking.  None uses the default (30 minutes).
    auto_update_interval_minutes: int | None = None

    # Interval in minutes between tool update checks.
    # 0 disables automatic checking.  None uses the default (20 minutes).
    tool_update_interval_minutes: int | None = None

    # Per-plugin and per-package auto-update toggle.
    #
    # Maps plugin name to:
    #   - ``True``  — all packages under this plugin auto-update (default).
    #   - ``False`` — the entire plugin is disabled from auto-update.
    #   - ``dict[str, bool]`` — per-package overrides within this plugin.
    #     Packages with ``True`` auto-update; ``False`` are skipped.
    #     Packages not listed inherit the manifest-aware default (ON for
    #     manifest-referenced packages, OFF for global packages).
    #
    # ``None`` or absent means all plugins auto-update with manifest-aware defaults.
    plugin_auto_update: dict[str, bool | dict[str, bool]] | None = None

    # Per-entry auto-update for the global manifest, keyed by Porringer entry id.
    # Entries not listed auto-update.
    entry_auto_update: dict[str, bool] | None = None

    # Per-manifest pre-release overrides.  Outer key is a normalised
    # manifest path (or URL for remote manifests) produced by
    # ``normalize_manifest_key()``.  Inner value is a sorted list of
    # package names (case-insensitive) that should be checked for
    # pre-release updates even when the manifest does not set
    # ``include_prereleases: true`` on the package.  ``None`` means
    # no overrides anywhere.
    prerelease_packages: dict[str, list[str]] | None = None

    # Whether downloaded updates should be applied and restarted
    # automatically without user interaction.  None resolves to True.
    auto_apply: bool | None = None

    # Whether the application should start automatically with the OS.
    # None means use the default (enabled).  Explicitly False disables
    # auto-startup.
    auto_start: bool | None = None

    # Enable verbose DEBUG-level logging to the log file.
    # None resolves to False (INFO level).
    debug_logging: bool | None = None

    # ISO 8601 timestamp of the last successful client self-update.
    # None means no update has been recorded.
    last_client_update: str | None = None

    # Per-package timestamps of the last successful tool update.
    # Maps "plugin/package" → ISO 8601 timestamp.  None means no
    # tool updates have been recorded.
    last_tool_updates: dict[str, str] | None = None

    # List of setup profile URLs.
    # Each URL points to a remote JSON file describing a collection of
    # manifest URLs for machine provisioning.  None means no profiles.
    setup_profiles: list[str] | None = None

    # Absolute paths of registered project directories.  None means the
    # one-time import from Porringer's old directory cache has not run yet.
    projects: list[str] | None = None

    @model_validator(mode="wrap")
    @classmethod
    def _recover_invalid_fields(cls, data: Any, handler: Any) -> UserConfig:
        """Silently drop fields that fail validation instead of rejecting the entire config.

        When an on-disk ``config.json`` contains a value whose type no longer
        matches the schema (e.g. after a version upgrade renames or re-types a
        field), the normal behaviour is to raise ``ValidationError`` and lose
        *every* setting.  This wrap validator intercepts the error, removes
        only the offending fields (so their defaults kick in), and retries.
        """
        try:
            return handler(data)
        except ValidationError as exc:
            if not isinstance(data, dict):
                raise

            bad_fields = {str(e["loc"][0]) for e in exc.errors() if e.get("loc")}
            cleaned = {k: v for k, v in data.items() if k not in bad_fields}

            for name in bad_fields:
                logger.warning(
                    "Discarding invalid config field %r (using default)", name
                )

            return handler(cleaned)


# ---------------------------------------------------------------------------
# Update channel & state enums
# ---------------------------------------------------------------------------


class UpdateChannel(StrEnum):
    """Update channel selection."""

    STABLE = "stable"
    DEVELOPMENT = "development"


class UpdateState(Enum):
    """State of an update operation."""

    NO_UPDATE = auto()
    UPDATE_AVAILABLE = auto()
    DOWNLOADING = auto()
    DOWNLOADED = auto()
    APPLYING = auto()
    APPLIED = auto()
    FAILED = auto()


# ---------------------------------------------------------------------------
# Update dataclasses
# ---------------------------------------------------------------------------


@dataclass
class UpdateInfo:
    """Information about an available update."""

    available: bool
    current_version: Version
    latest_version: Version | None = None
    error: str | None = None


# Default interval for automatic update checks (minutes)
DEFAULT_AUTO_UPDATE_INTERVAL_MINUTES = 5

# Default interval for tool update checks (minutes)
DEFAULT_TOOL_UPDATE_INTERVAL_MINUTES = 5

# Default .appinstaller feed base URL.
GITHUB_REPO_URL = "https://github.com/synodic/spurtle"


@dataclass
class UpdateConfig:
    """Configuration for the updater."""

    # Base URL for the .appinstaller feed.
    repo_url: str = GITHUB_REPO_URL

    # Channel determines whether to use dev or stable releases.
    channel: UpdateChannel = UpdateChannel.STABLE

    # Interval in minutes between automatic update checks (0 = disabled)
    auto_update_interval_minutes: int = DEFAULT_AUTO_UPDATE_INTERVAL_MINUTES

    # Interval in minutes between tool update checks (0 = disabled)
    tool_update_interval_minutes: int = DEFAULT_TOOL_UPDATE_INTERVAL_MINUTES

    @classmethod
    def from_resolved(cls, config: ResolvedConfig) -> UpdateConfig:
        """Derive an ``UpdateConfig`` from resolved configuration values.

        Args:
            config: A resolved configuration snapshot.

        Returns:
            An ``UpdateConfig`` ready to initialise the updater.
        """
        channel = (
            UpdateChannel.DEVELOPMENT
            if config.update_channel == "dev"
            else UpdateChannel.STABLE
        )
        return cls(
            channel=channel,
            repo_url=config.update_source or GITHUB_REPO_URL,
            auto_update_interval_minutes=config.auto_update_interval_minutes,
            tool_update_interval_minutes=config.tool_update_interval_minutes,
        )

    @property
    def channel_name(self) -> str:
        """Return the channel name (``dev`` or ``stable``)."""
        return "dev" if self.channel == UpdateChannel.DEVELOPMENT else "stable"


# ---------------------------------------------------------------------------
# ResolvedConfig — immutable runtime snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedConfig:
    """Immutable runtime configuration snapshot.

    Constructed by :func:`~spurtle.resolution.resolve_config` from
    the merged ``BuildConfig`` + ``UserConfig`` layers.  Every field has a
    concrete, non-``None`` value (except ``update_source`` and
    ``prerelease_packages`` where ``None`` is a valid semantic value
    meaning "use default" / "no overrides").
    """

    update_source: str | None
    update_channel: str
    auto_update_interval_minutes: int
    tool_update_interval_minutes: int
    plugin_auto_update: dict[str, bool | dict[str, bool]] | None
    entry_auto_update: dict[str, bool] | None
    prerelease_packages: dict[str, list[str]] | None
    auto_apply: bool
    auto_start: bool
    debug_logging: bool
    last_client_update: str | None
    last_tool_updates: dict[str, str] | None
    setup_profiles: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
