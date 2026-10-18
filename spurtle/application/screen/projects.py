"""ProjectsView — widget for managing project directories and their manifests."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from porringer.api import API
from porringer.backend.command.core.discovery import DiscoveredPlugins
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from spurtle.application.data import DataCoordinator
from spurtle.application.package_state import PackageStateStore
from spurtle.application.screen.install import SetupPreviewWidget
from spurtle.application.screen.schema import PreviewPhase
from spurtle.application.screen.sidebar import ManifestSidebar
from spurtle.application.screen.spinner import LoadingIndicator
from spurtle.application.theme import COMPACT_MARGINS

if TYPE_CHECKING:
    from spurtle.application.config_store import ConfigStore

logger = logging.getLogger(__name__)


class ProjectsView(QWidget):
    """Widget for managing project directories and previewing their manifests.

    Displays a vertical sidebar of cached project directories on the
    left with a stacked widget on the right showing one
    :class:`SetupPreviewWidget` per manifest.  All manifests are loaded
    in parallel on first refresh; switching between them is instant.
    """

    navigate_to_tool_requested = Signal(str, str)
    """Emitted with ``(installer, package_name)`` when a child widget
    requests navigation to a tool in the Tools view."""

    def __init__(
        self,
        porringer: API,
        store: ConfigStore,
        parent: QWidget | None = None,
        *,
        coordinator: DataCoordinator | None = None,
        package_store: PackageStateStore | None = None,
    ) -> None:
        """Initialize the projects view.

        Args:
            porringer: The porringer API instance.
            store: The centralised :class:`ConfigStore`.
            parent: Optional parent widget.
            coordinator: Shared data coordinator for validated directory
                data.
            package_store: Shared package update state registry.
        """
        super().__init__(parent)
        self._porringer = porringer
        self._store = store
        self._coordinator = coordinator
        self._package_store = package_store
        self._refresh_in_progress = False
        self._pending_select: str | None = None
        self._widgets: dict[str, SetupPreviewWidget] = {}
        self._manifest_to_profile: dict[str, str] = {}
        self._batch_task: asyncio.Task[None] | None = None

        # Profile resolution cache: {profile_url: (SetupProfile, timestamp)}
        self._profile_cache: dict[str, tuple[object, float]] = {}
        self._profile_cache_ttl: float = 300.0  # 5 minutes

        self._init_ui()

    def _init_ui(self) -> None:
        """Build the sidebar + stacked widget layout."""
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Left — sidebar
        self._sidebar = ManifestSidebar()
        self._sidebar.add_requested.connect(self._on_add)
        self._sidebar.remove_requested.connect(self._on_remove)
        self._sidebar.selection_changed.connect(self._on_selection_changed)
        outer.addWidget(self._sidebar)

        # Right — stacked previews + empty placeholder
        right = QVBoxLayout()
        right.setContentsMargins(*COMPACT_MARGINS)
        right.setSpacing(0)

        # Install All button — visible when ≥1 widget is READY
        self._install_all_btn = QPushButton("Install All")
        self._install_all_btn.setToolTip("Install all ready manifests sequentially")
        self._install_all_btn.setVisible(False)
        self._install_all_btn.clicked.connect(self._on_install_all)
        right.addWidget(self._install_all_btn)

        self._stack = QStackedWidget()
        right.addWidget(self._stack, stretch=1)

        # Empty placeholder shown when there are no manifests
        self._empty_placeholder = QLabel(
            "No projects. Click + Add Project to get started."
        )
        self._empty_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_placeholder.setStyleSheet("color: grey; font-size: 13px;")
        self._stack.addWidget(self._empty_placeholder)

        self._loading_indicator = LoadingIndicator("Loading projects\u2026")
        self._stack.addWidget(self._loading_indicator)

        outer.addLayout(right, stretch=1)

    # --- Public API ---

    def refresh(self) -> None:
        """Schedule an asynchronous refresh of the cached directories."""
        if self._refresh_in_progress:
            return
        asyncio.create_task(self._async_refresh())

    async def _async_refresh(self) -> None:
        """Refresh the sidebar and stacked widgets from the porringer cache."""
        self._refresh_in_progress = True
        self._loading_indicator.start()
        self._stack.setCurrentWidget(self._loading_indicator)
        self._sidebar.set_enabled(False)

        try:
            previous = self._pending_select or self._sidebar.selected_key
            self._pending_select = None

            directories, discovered = await self._fetch_directories()

            current_keys = {key for key, _, _ in directories}

            # Remove widgets for directories no longer in cache
            self._remove_stale_widgets(current_keys)

            # Create new widgets for new directories
            self._create_directory_widgets(directories, discovered)

            # Rebuild sidebar
            self._sidebar.set_directories(directories)
            self._sidebar.select(previous)

            # Push latest discovered plugins to all existing widgets
            if discovered is not None:
                for w in self._widgets.values():
                    w._discovered_plugins = discovered

            # Load all stacked widgets in parallel
            self._load_widgets(directories)

        except Exception:
            logger.exception("Failed to refresh projects")
        finally:
            self._loading_indicator.stop()
            self._sidebar.set_enabled(True)
            self._refresh_in_progress = False

    async def _fetch_directories(
        self,
    ) -> tuple[list[tuple[str, str, bool]], DiscoveredPlugins | None]:
        """Fetch local directories and remote profile manifests.

        Returns:
            A tuple of ``(directories, discovered)`` where *directories*
            is a list of ``(key, name, valid)`` tuples and *discovered*
            is the plugin discovery result (or ``None``).
        """
        if self._coordinator is not None:
            snapshot = await self._coordinator.refresh()
            results = snapshot.directories
            discovered = snapshot.discovered
        else:
            from spurtle.operations.project import list_projects

            results = await asyncio.to_thread(list_projects, self._porringer)
            discovered = None

        directories: list[tuple[str, str, bool]] = []
        current_keys: set[str] = set()
        for result in results:
            valid = bool(result.exists and result.has_manifest)
            key = str(Path(result.path).resolve())
            directories.append((key, result.name or "", valid))
            current_keys.add(key)

        # Append remote manifest entries from setup profiles
        self._manifest_to_profile = await self._resolve_profiles(
            directories, current_keys
        )

        return directories, discovered

    async def _resolve_profiles(
        self,
        directories: list[tuple[str, str, bool]],
        current_keys: set[str],
    ) -> dict[str, str]:
        """Download setup profiles and append their manifests to *directories*.

        Uses a TTL cache to avoid re-downloading profiles on every refresh.

        Returns:
            A mapping from manifest URL to owning profile URL.
        """
        from spurtle.operations.install import open_profile
        from spurtle.operations.schema import SetupProfile

        now = time.monotonic()
        manifest_to_profile: dict[str, str] = {}

        # Evict cache entries for profiles no longer in config
        configured = set(self._store.config.setup_profiles)
        for url in list(self._profile_cache):
            if url not in configured:
                del self._profile_cache[url]

        for profile_url in self._store.config.setup_profiles:
            try:
                cached = self._profile_cache.get(profile_url)
                if cached is not None and (now - cached[1]) < self._profile_cache_ttl:
                    profile = cached[0]
                else:
                    async with open_profile(profile_url) as profile:
                        self._profile_cache[profile_url] = (profile, now)

                if not isinstance(profile, SetupProfile):
                    continue

                for manifest_url in profile.manifests:
                    manifest_to_profile[manifest_url] = profile_url
                    if manifest_url not in current_keys:
                        name = f"{profile.name}: {manifest_url.rsplit('/', 1)[-1]}"
                        directories.append((manifest_url, name, True))
                        current_keys.add(manifest_url)
            except Exception:
                logger.exception("Failed to resolve profile: %s", profile_url)
        return manifest_to_profile

    def _load_widgets(self, directories: list[tuple[str, str, bool]]) -> None:
        """Trigger :meth:`SetupPreviewWidget.load` for each valid directory."""
        for key, _name, valid in directories:
            widget = self._widgets.get(key)
            if widget is not None and valid:
                if key.startswith("https://"):
                    widget.load(key)
                else:
                    path = Path(key)
                    widget.load(
                        str(path),
                        project_directory=path if path.is_dir() else path.parent,
                    )

    # --- Event handlers ---

    def _remove_stale_widgets(self, current_keys: set[str]) -> None:
        """Remove stacked widgets for directories no longer in the cache."""
        for key in list(self._widgets):
            if key not in current_keys:
                widget = self._widgets.pop(key)
                self._stack.removeWidget(widget)
                widget.reset()
                widget.deleteLater()

    def _create_directory_widgets(
        self,
        directories: list[tuple[str, str, bool]],
        discovered: DiscoveredPlugins | None,
    ) -> None:
        """Create :class:`SetupPreviewWidget` instances for new valid directories."""
        for key, _name, valid in directories:
            if key not in self._widgets and valid:
                widget = SetupPreviewWidget(
                    self._porringer,
                    self,
                    show_close=False,
                    config=self._store.config,
                    package_store=self._package_store,
                )
                widget._discovered_plugins = discovered
                widget.install_finished.connect(self._on_install_finished)
                widget.navigate_to_tool_requested.connect(
                    self.navigate_to_tool_requested.emit
                )
                widget.phase_changed.connect(
                    lambda phase, k=key: self._on_widget_phase_changed(k, phase),
                )
                self._widgets[key] = widget
                self._stack.addWidget(widget)

    def _on_selection_changed(self, key: str) -> None:
        """Handle sidebar selection — switch the stacked widget."""
        widget = self._widgets.get(key)
        if widget is not None:
            self._stack.setCurrentWidget(widget)
        else:
            self._stack.setCurrentWidget(self._empty_placeholder)

    def _on_widget_phase_changed(self, key: str, phase: PreviewPhase) -> None:
        """Update the sidebar item's phase indicator and Install All visibility."""
        item = self._sidebar.get_item(key)
        if item is not None:
            item.set_phase(phase)
        self._update_install_all_visibility()

    def _on_add(self) -> None:
        """Show a choice dialog to add a local project or a remote profile URL."""
        dlg = QMessageBox(self)
        dlg.setWindowTitle("Add")
        dlg.setText("What would you like to add?")
        local_btn = dlg.addButton("Local Project", QMessageBox.ButtonRole.AcceptRole)
        profile_btn = dlg.addButton("Profile URL", QMessageBox.ButtonRole.ActionRole)
        dlg.addButton(QMessageBox.StandardButton.Cancel)
        dlg.exec()
        clicked = dlg.clickedButton()
        if clicked == local_btn:
            self._add_local_project()
        elif clicked == profile_btn:
            self._add_profile_url()

    def _add_local_project(self) -> None:
        """Open a file picker and immediately cache the chosen directory."""
        filenames = self._porringer.sync.manifest_filenames()
        filter_str = "Manifests (" + " ".join(filenames) + ");;All Files (*)"
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Select Manifest File",
            "",
            filter_str,
        )
        if not chosen:
            return

        selected = Path(chosen)
        directory = selected if selected.is_dir() else selected.parent

        try:
            from spurtle.operations.project import add_project

            add_project(self._porringer, str(directory))
            logger.info("Cached new project directory: %s", directory)
        except NotADirectoryError, ValueError:
            logger.debug("Directory already cached or invalid: %s", directory)

        if self._coordinator is not None:
            self._coordinator.invalidate()
        self._pending_select = str(directory.resolve())
        self.refresh()

    def _add_profile_url(self) -> None:
        """Prompt for a profile URL and add it to the config."""
        url, ok = QInputDialog.getText(
            self,
            "Add Profile URL",
            "Enter an HTTPS profile URL:",
        )
        if not ok or not url.strip():
            return
        url = url.strip()
        try:
            self.add_profile(url)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid URL", str(exc))

    def add_profile(self, url: str) -> None:
        """Add a setup profile URL to the config and refresh.

        Args:
            url: HTTPS URL of the profile JSON file.

        Raises:
            ValueError: If the URL is not HTTPS.
        """
        from spurtle.operations.install import validate_profile_url

        validate_profile_url(url)

        existing = list(self._store.config.setup_profiles)
        if url not in existing:
            existing.append(url)
            self._store.update(setup_profiles=existing)

        # Force re-fetch of the new profile on the next refresh
        self._profile_cache.pop(url, None)
        self._pending_select = url
        self.refresh()

    def _on_remove(self, key: str) -> None:
        """Remove a project directory or profile manifest."""
        if key.startswith("https://"):
            self._remove_profile_manifest(key)
        else:
            self._remove_local_project(key)

    def _remove_local_project(self, key: str) -> None:
        """Remove a local directory from the porringer cache."""
        from spurtle.operations.project import remove_project

        remove_project(self._porringer, key)
        logger.info("Removed project directory from cache: %s", key)

        # Tear down the widget immediately
        widget = self._widgets.pop(key, None)
        if widget is not None:
            self._stack.removeWidget(widget)
            widget.reset()
            widget.deleteLater()

        if self._coordinator is not None:
            self._coordinator.invalidate()
        self.refresh()

    def _remove_profile_manifest(self, manifest_url: str) -> None:
        """Remove the profile that owns *manifest_url* from config.

        Scans cached profile data to find the owning profile URL,
        removes it from stored profiles, then refreshes.  All manifests
        belonging to that profile are removed on the next refresh cycle.
        """
        # Find owning profile by scanning widget keys against stored profiles.
        # The _async_refresh populates widgets keyed by manifest URL, so we
        # need to map back to the profile that produced it.  We stored this
        # mapping during the last refresh cycle.
        owning_profile: str | None = self._manifest_to_profile.get(manifest_url)

        if owning_profile is not None:
            updated = [
                p for p in self._store.config.setup_profiles if p != owning_profile
            ]
            self._store.update(setup_profiles=updated)
            self._profile_cache.pop(owning_profile, None)
            logger.info("Removed setup profile: %s", owning_profile)
        else:
            logger.warning(
                "Could not find owning profile for manifest: %s", manifest_url
            )

        # Tear down the widget immediately
        widget = self._widgets.pop(manifest_url, None)
        if widget is not None:
            self._stack.removeWidget(widget)
            widget.reset()
            widget.deleteLater()
        self.refresh()

    def _on_install_finished(self, _results: object) -> None:
        """Refresh after a successful install."""
        if self._coordinator is not None:
            self._coordinator.invalidate()
        self.refresh()

    # --- Install All batch execution ---

    def _update_install_all_visibility(self) -> None:
        """Show the Install All button when at least one widget is READY."""
        has_ready = any(w.phase == PreviewPhase.READY for w in self._widgets.values())
        self._install_all_btn.setVisible(has_ready)

    def _on_install_all(self) -> None:
        """Start sequential batch installation of all READY widgets."""
        if self._batch_task is not None and not self._batch_task.done():
            return
        self._batch_task = asyncio.create_task(self._run_batch_install())

    async def _run_batch_install(self) -> None:
        """Run install on each READY widget sequentially."""
        self._install_all_btn.setEnabled(False)
        try:
            for widget in list(self._widgets.values()):
                if widget.phase != PreviewPhase.READY:
                    continue

                done_event = asyncio.Event()

                def slot(_result: object, event: asyncio.Event = done_event) -> None:
                    event.set()

                widget.install_finished.connect(slot)
                widget.start_install()
                await done_event.wait()
                widget.install_finished.disconnect(slot)
        except Exception:
            logger.exception("Batch install failed")
        finally:
            self._install_all_btn.setEnabled(True)
            self._update_install_all_visibility()
