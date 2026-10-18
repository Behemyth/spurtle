"""Screen class for the Spurtle application."""

import asyncio
import logging
import traceback
from collections import OrderedDict
from pathlib import Path

from porringer.api import API
from porringer.schema import PluginInfo
from porringer.schema.plugin import PluginKind
from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QKeyEvent, QKeySequence, QShortcut, QShowEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from spurtle.application.config_store import ConfigStore
from spurtle.application.data import DataCoordinator
from spurtle.application.icon import app_icon
from spurtle.application.package_state import PackageStateStore
from spurtle.application.screen.plugin_row import (
    FilterChip,
    PluginKindHeader,
    PluginProviderHeader,
    PluginRow,
    RowPhase,
)
from spurtle.application.screen.projects import ProjectsView
from spurtle.application.screen.schema import (
    DisplayPackage,
    PackageEntry,
    PluginRowData,
    ProjectInstance,
    RefreshData,
)
from spurtle.application.screen.spinner import LoadingIndicator
from spurtle.application.screen.update_banner import UpdateBanner
from spurtle.application.theme import (
    COMPACT_MARGINS,
    FILTER_CHIP_SPACING,
    FILTER_PANEL_ANIMATION_MS,
    FILTER_TOGGLE_ACTIVE_STYLE,
    FILTER_TOGGLE_STYLE,
    MAIN_WINDOW_MIN_SIZE,
    PLUGIN_ROW_HIGHLIGHT_STYLE,
    PLUGIN_ROW_STATUS_AVAILABLE_STYLE,
    PLUGIN_ROW_STATUS_UP_TO_DATE_STYLE,
    PLUGIN_ROW_STYLE,
    PLUGIN_SECTION_SPACING,
    SEARCH_INPUT_STYLE,
    SETTINGS_GEAR_STYLE,
)
from spurtle.operations.schema import ProjectInfo
from spurtle.operations.tool import check_tool_updates, declared_packages

logger = logging.getLogger(__name__)

# Plugin kinds that support auto-update and per-plugin upgrade.
_UPDATABLE_KINDS = frozenset({PluginKind.TOOL, PluginKind.PACKAGE, PluginKind.RUNTIME})

# Preferred display ordering â€” Tools first, then alphabetical for the rest.
_KIND_DISPLAY_ORDER: dict[PluginKind, int] = {
    PluginKind.TOOL: 0,
    PluginKind.PACKAGE: 1,
    PluginKind.RUNTIME: 2,
    PluginKind.PROJECT: 3,
    PluginKind.SCM: 4,
}


class ToolsView(QWidget):
    """Central update hub for the packages and tools projects declare.

    Shows ``TOOL``, ``PACKAGE``, and ``RUNTIME`` entries declared by
    registered project manifests, grouped by the plugin that manages
    them.  Each tool has ``Auto`` / ``Update`` controls.
    """

    update_all_requested = Signal()
    """Emitted when the global *Update All* button is clicked."""

    plugin_check_requested = Signal(str)
    """Emitted with a plugin name when its manual check-for-updates button is clicked."""

    plugin_update_requested = Signal(str)
    """Emitted with a plugin name when its per-plugin *Update* button is clicked."""

    package_update_requested = Signal(str, str)
    """Emitted with ``(plugin_name, package_name)`` for a per-package update."""

    package_remove_requested = Signal(str, str)
    """Emitted with ``(plugin_name, package_name)`` for a per-package removal."""

    navigate_to_project_requested = Signal(str)
    """Emitted with a project path string to navigate to the Projects tab."""

    def __init__(
        self,
        porringer: API,
        store: ConfigStore,
        parent: QWidget | None = None,
        *,
        coordinator: DataCoordinator | None = None,
        package_store: PackageStateStore | None = None,
    ) -> None:
        """Initialize the tools view.

        Args:
            porringer: The porringer API instance.
            store: The centralised :class:`ConfigStore`.
            parent: Optional parent widget.
            coordinator: Shared data coordinator.  When provided, the
                view delegates plugin/directory fetching to the
                coordinator instead of calling porringer directly.
            package_store: Shared package update state registry.
        """
        super().__init__(parent)
        self._porringer = porringer
        self._store = store
        self._coordinator = coordinator
        self._package_store = package_store
        self._section_widgets: list[QWidget] = []
        self._filter_chips: dict[str, FilterChip] = {}
        self._deselected_plugins: set[str] = set()
        self._refresh_in_progress = False
        self._check_in_progress = False
        self._directories: list[ProjectInfo] = []
        self._timestamp_timer: QTimer | None = None
        self._init_ui()

        if self._package_store is not None:
            self._package_store.state_changed.connect(self._on_package_state_changed)

    def _init_ui(self) -> None:
        """Initialize the UI components."""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*COMPACT_MARGINS)

        outer.addLayout(self._build_toolbar())

        # Collapsible filter panel â€” search input + chip row
        self._filter_panel = QWidget()
        self._filter_panel.setMaximumHeight(0)
        self._filter_panel.setVisible(False)
        filter_layout = QVBoxLayout(self._filter_panel)
        filter_layout.setContentsMargins(0, 4, 0, 4)
        filter_layout.setSpacing(4)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search packages\u2026")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.setStyleSheet(SEARCH_INPUT_STYLE)
        self._search_input.textChanged.connect(self._apply_filter)
        self._search_input.installEventFilter(self)
        filter_layout.addWidget(self._search_input)

        # Filter chips row â€” auto-populated from discovered plugins
        chip_container = QWidget()
        self._chip_layout = QHBoxLayout(chip_container)
        self._chip_layout.setContentsMargins(0, 0, 0, 0)
        self._chip_layout.setSpacing(FILTER_CHIP_SPACING)
        self._chip_layout.addStretch()
        filter_layout.addWidget(chip_container)

        outer.addWidget(self._filter_panel)

        # Animation for filter panel slide-in / slide-out
        self._filter_anim = QPropertyAnimation(self._filter_panel, b"maximumHeight")
        self._filter_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._filter_anim.setDuration(FILTER_PANEL_ANIMATION_MS)
        self._filter_panel_open = False

        # Ctrl+F shortcut to toggle filter panel
        QShortcut(QKeySequence.StandardKey.Find, self, self._toggle_filter_panel)

        # Scroll area
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._container = QWidget()
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setSpacing(PLUGIN_SECTION_SPACING)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._container_layout.addStretch()

        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll)

        self._loading_indicator = LoadingIndicator("Loading tools\u2026")
        outer.addWidget(self._loading_indicator)

        # Periodic timer to refresh relative timestamps (every 60s)
        self._timestamp_timer = QTimer(self)
        self._timestamp_timer.setInterval(60_000)
        self._timestamp_timer.timeout.connect(self._refresh_timestamps)
        self._timestamp_timer.start()

    def _build_toolbar(self) -> QHBoxLayout:
        """Build the toolbar with filter toggle and action buttons."""
        toolbar = QHBoxLayout()

        self._filter_btn = QPushButton("\U0001f50d")
        self._filter_btn.setToolTip("Filter packages (Ctrl+F)")
        self._filter_btn.setFlat(True)
        self._filter_btn.setStyleSheet(FILTER_TOGGLE_STYLE)
        self._filter_btn.clicked.connect(self._toggle_filter_panel)
        toolbar.addWidget(self._filter_btn)

        toolbar.addStretch()

        check_btn = QPushButton("Check for Updates")
        check_btn.setToolTip("Scan all manifests for available package updates")
        check_btn.clicked.connect(self._on_check_for_updates)
        toolbar.addWidget(check_btn)
        self._check_btn = check_btn

        self._update_all_btn = QPushButton("Update All")
        self._update_all_btn.setToolTip("Upgrade all auto-update-enabled plugins now")
        self._update_all_btn.clicked.connect(self.update_all_requested.emit)
        toolbar.addWidget(self._update_all_btn)

        return toolbar

    # --- Public API ---

    def invalidate_update_data(self) -> None:
        """Clear cached update state so the next refresh re-checks."""
        if self._package_store is not None:
            self._package_store.clear()

    def record_updates_completed(
        self,
        signal_key: str,
        version_map: dict[str, tuple[str, str]],
    ) -> None:
        """Forward completed-update data to the shared store."""
        if self._package_store is not None:
            self._package_store.record_updates_completed(signal_key, version_map)

    def refresh(self) -> None:
        """Schedule an asynchronous rebuild of the tool list."""
        if self._refresh_in_progress:
            return
        logger.debug("ToolsView.refresh() called (visible=%s)", self.isVisible())
        asyncio.create_task(self._async_refresh())

    async def _async_refresh(self) -> None:
        """Rebuild the tool list from porringer data.

        One inspection of the registered manifests provides the rows,
        their installed versions, and their update badges.
        """
        self._refresh_in_progress = True
        self._scroll.hide()
        self._loading_indicator.start()
        self._check_btn.setEnabled(False)
        self._update_all_btn.setEnabled(False)

        try:
            data = await self._gather_refresh_data()
            self._store_check_results(data.available)
            self._build_widget_tree(data)
            self._apply_update_badges()
        except Exception:
            logger.exception("Failed to refresh tools")
        finally:
            self._loading_indicator.stop()
            self._scroll.show()
            self._check_btn.setEnabled(True)
            self._update_all_btn.setEnabled(True)
            self._refresh_in_progress = False

    # ------------------------------------------------------------------
    # _async_refresh helper methods
    # ------------------------------------------------------------------

    def _manifest_paths(self) -> list[str]:
        """Return the registered project directories that have a manifest."""
        return [d.path for d in self._directories if d.exists and d.has_manifest]

    async def _gather_refresh_data(self) -> RefreshData:
        """Fetch plugins and the packages registered manifests declare.

        Returns:
            A :class:`RefreshData` bundle containing all data needed
            to build the widget tree.
        """
        plugins, directories = await self._fetch_data()
        self._directories = directories
        discovered = self._coordinator.discovered_plugins if self._coordinator else None

        declared = await declared_packages(
            self._porringer, self._manifest_paths(), discovered
        )

        packages_map: dict[str, list[PackageEntry]] = {}
        manifest_packages: dict[str, set[str]] = {}
        available: dict[str, dict[str, str]] = {}
        for item in declared:
            version = item.installed_version or ""
            entries = packages_map.setdefault(item.plugin, [])
            entries.append(PackageEntry(name=item.name, version=version))
            entries.extend(
                PackageEntry(
                    name=item.name,
                    project_label=Path(project_path).name,
                    version=version,
                    project_path=project_path,
                )
                for project_path in item.project_paths
            )
            manifest_packages.setdefault(item.plugin, set()).add(item.name)
            if item.available_version is not None:
                available.setdefault(item.plugin, {})[item.name] = (
                    item.available_version
                )

        return RefreshData(
            plugins=plugins,
            packages_map=packages_map,
            manifest_packages=manifest_packages,
            available=available,
        )

    def _build_widget_tree(self, data: RefreshData) -> None:
        """Clear existing widgets and rebuild the tool/package tree."""
        self._clear_section_widgets()

        auto_update_map = self._store.config.plugin_auto_update or {}
        kind_buckets = self._bucket_by_kind(data.plugins, data.packages_map)

        sorted_kinds = sorted(
            kind_buckets,
            key=lambda k: _KIND_DISPLAY_ORDER.get(k, 99),
        )

        for kind in sorted_kinds:
            self._insert_section_widget(PluginKindHeader(kind, parent=self._container))
            for plugin in kind_buckets[kind]:
                self._build_plugin_section(plugin, data, auto_update_map)

        self._rebuild_chips()
        self._apply_filter()

    def _clear_section_widgets(self) -> None:
        """Remove and delete all current section widgets."""
        for widget in self._section_widgets:
            self._container_layout.removeWidget(widget)
            widget.deleteLater()
        self._section_widgets.clear()

    @staticmethod
    def _bucket_by_kind(
        plugins: list[PluginInfo],
        packages_map: dict[str, list[PackageEntry]],
    ) -> OrderedDict[PluginKind, list[PluginInfo]]:
        """Group updatable plugins by kind, filtering out empty entries."""
        buckets: OrderedDict[PluginKind, list[PluginInfo]] = OrderedDict()
        for plugin in plugins:
            if plugin.kind not in _UPDATABLE_KINDS:
                continue
            if plugin.tool_version is not None or bool(packages_map.get(plugin.name)):
                buckets.setdefault(plugin.kind, []).append(plugin)
        return buckets

    def _build_plugin_section(
        self,
        plugin: PluginInfo,
        data: RefreshData,
        auto_update_map: dict[str, bool | dict[str, bool]],
    ) -> None:
        """Build the provider header and package rows for a single plugin.

        For each package a :class:`PluginRow` is created with inline
        project-name tags for project-scoped occurrences.  Separate
        ``ProjectChildRow`` widgets are no longer used.
        """
        auto_val = auto_update_map.get(plugin.name, True)
        plugin_updates = self._get_plugin_updates(plugin.name)
        caps = (
            self._coordinator.snapshot.plugin_capabilities.get(plugin.name, frozenset())
            if self._coordinator is not None
            else frozenset()
        )

        provider = PluginProviderHeader(
            plugin,
            auto_val is not False,
            capabilities=caps,
            show_controls=True,
            has_updates=bool(plugin_updates),
            parent=self._container,
        )
        provider.auto_update_toggled.connect(self._on_auto_update_toggled)
        provider.check_requested.connect(self.plugin_check_requested.emit)
        provider.update_requested.connect(self.plugin_update_requested.emit)
        self._insert_section_widget(provider)

        plugin_manifest = data.manifest_packages.get(plugin.name, set())
        raw_packages = data.packages_map.get(plugin.name, [])
        display_packages = self._build_display_packages(raw_packages, plugin_manifest)
        tool_timestamps = self._store.config.last_tool_updates or {}

        if display_packages:
            for pkg in display_packages:
                pkg_auto = self._resolve_package_auto_update(
                    auto_val,
                    pkg.name,
                    pkg.is_global and pkg.name not in plugin_manifest,
                )
                ts_key = f"{plugin.name}/{pkg.name}"
                row = self._create_connected_row(
                    PluginRowData(
                        name=pkg.name,
                        version=pkg.global_version or "",
                        plugin_name=plugin.name,
                        auto_update=pkg_auto,
                        show_toggle=True,
                        has_update=pkg.name in plugin_updates,
                        is_global=pkg.is_global,
                        host_tool=pkg.host_tool,
                        project_instances=list(pkg.project_instances),
                        last_updated=tool_timestamps.get(ts_key, ""),
                    ),
                )
                self._insert_section_widget(row)
        else:
            version_text = (
                str(plugin.tool_version) if plugin.tool_version is not None else ""
            )
            row = PluginRow(
                PluginRowData(name=plugin.name, version=version_text),
                parent=self._container,
            )
            self._insert_section_widget(row)

    @staticmethod
    def _build_display_packages(
        raw_packages: list[PackageEntry],
        plugin_manifest: set[str],
    ) -> list[DisplayPackage]:
        """Build a two-tier display model from raw package entries.

        Each unique package name produces one :class:`DisplayPackage`.
        Global entries (no ``project_path``) set the global version;
        project-scoped entries become :class:`ProjectInstance` children.
        A project-scoped package is marked *transitive* when it is not
        declared in any manifest for this plugin.

        Returns:
            An ordered list of :class:`DisplayPackage` instances, with
            globals first, then project-only packages.
        """
        by_name: OrderedDict[str, DisplayPackage] = OrderedDict()
        for entry in raw_packages:
            from_project = bool(entry.project_path)

            if entry.name not in by_name:
                by_name[entry.name] = DisplayPackage(
                    name=entry.name,
                    host_tool=entry.host_tool,
                )

            dp = by_name[entry.name]

            if not from_project:
                # Global entry
                dp.is_global = True
                dp.global_version = entry.version
            else:
                # Project-scoped entry
                is_transitive = entry.name not in plugin_manifest
                # Deduplicate by project_path
                existing_paths = {pi.project_path for pi in dp.project_instances}
                if entry.project_path not in existing_paths:
                    dp.project_instances.append(
                        ProjectInstance(
                            project_label=entry.project_label,
                            project_path=entry.project_path,
                            version=entry.version,
                            is_transitive=is_transitive,
                        ),
                    )

        return list(by_name.values())

    @staticmethod
    def _resolve_package_auto_update(
        auto_val: bool | dict[str, bool],
        pkg_name: str,
        is_global: bool,
    ) -> bool:
        """Determine the effective auto-update setting for a single package."""
        if isinstance(auto_val, dict):
            return auto_val.get(pkg_name, not is_global)
        if auto_val is False:
            return False
        return not is_global

    # ------------------------------------------------------------------
    # Search & filter
    # ------------------------------------------------------------------

    def _rebuild_chips(self) -> None:
        """Rebuild the filter chip row from currently visible plugin providers.

        Preserves previous deselection state: if a chip was unchecked
        before a refresh, it stays unchecked (subtractive model).
        """
        # Remove old chips
        for chip in self._filter_chips.values():
            self._chip_layout.removeWidget(chip)
            chip.deleteLater()
        self._filter_chips.clear()

        # Collect unique plugin names in order
        seen: set[str] = set()
        plugin_names: list[str] = []
        for widget in self._section_widgets:
            if isinstance(widget, PluginProviderHeader):
                name = widget._plugin_name
                if name not in seen:
                    seen.add(name)
                    plugin_names.append(name)

        # Create chips â€” insert before the trailing stretch
        stretch_idx = self._chip_layout.count() - 1
        for name in plugin_names:
            chip = FilterChip(name, parent=self._chip_layout.parentWidget())
            chip.setChecked(name not in self._deselected_plugins)
            chip.toggled_with_name.connect(self._on_chip_toggled)
            self._chip_layout.insertWidget(stretch_idx, chip)
            self._filter_chips[name] = chip
            stretch_idx += 1

    def _on_chip_toggled(self, plugin_name: str, checked: bool) -> None:
        """Track deselected plugins and reapply the filter."""
        if checked:
            self._deselected_plugins.discard(plugin_name)
        else:
            self._deselected_plugins.add(plugin_name)
        self._apply_filter()

    # ------------------------------------------------------------------
    # Filter panel toggle & animation
    # ------------------------------------------------------------------

    @property
    def _has_active_filter(self) -> bool:
        """Return whether any search text or deselected chip is active."""
        return bool(self._search_input.text().strip()) or bool(self._deselected_plugins)

    def _toggle_filter_panel(self) -> None:
        """Slide the filter panel open or closed."""
        if self._filter_panel_open:
            self._close_filter_panel()
        else:
            self._open_filter_panel()

    def _open_filter_panel(self) -> None:
        """Slide the filter panel in and focus the search input."""
        if self._filter_panel_open:
            return
        self._filter_panel_open = True
        self._filter_panel.setVisible(True)
        self._filter_panel.adjustSize()
        target = self._filter_panel.sizeHint().height()
        self._filter_anim.stop()
        self._filter_anim.setStartValue(self._filter_panel.maximumHeight())
        self._filter_anim.setEndValue(target)
        self._filter_anim.start()
        self._search_input.setFocus()

    def _close_filter_panel(self) -> None:
        """Slide the filter panel out and return focus to the toggle button."""
        if not self._filter_panel_open:
            return
        self._filter_panel_open = False
        self._filter_anim.stop()
        self._filter_anim.setStartValue(self._filter_panel.maximumHeight())
        self._filter_anim.setEndValue(0)
        self._filter_anim.finished.connect(
            self._on_filter_panel_closed,
            type=Qt.ConnectionType.SingleShotConnection,
        )
        self._filter_anim.start()

    def _on_filter_panel_closed(self) -> None:
        """Hide the panel widget after slide-out completes."""
        if not self._filter_panel_open:
            self._filter_panel.setVisible(False)
        self._filter_btn.setFocus()

    def _update_filter_badge(self) -> None:
        """Swap the toggle-button style to indicate active filters."""
        style = (
            FILTER_TOGGLE_ACTIVE_STYLE
            if self._has_active_filter
            else FILTER_TOGGLE_STYLE
        )
        self._filter_btn.setStyleSheet(style)

    def _clear_active_filters(self) -> None:
        """Reset search text and re-check all deselected chips."""
        self._search_input.clear()
        for name in list(self._deselected_plugins):
            chip = self._filter_chips.get(name)
            if chip is not None:
                chip.setChecked(True)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """Handle Escape in the search input to clear filters / close panel."""
        if (
            obj is self._search_input
            and isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
        ):
            if self._has_active_filter:
                self._clear_active_filters()
            else:
                self._close_filter_panel()
            return True
        return super().eventFilter(obj, event)

    def _active_chip_plugins(self) -> set[str] | None:
        """Return the set of plugin names whose chips are checked.

        Returns ``None`` when no chips exist yet (initial state before
        any refresh), meaning all plugins should be shown.
        """
        if not self._filter_chips:
            return None
        return {name for name, chip in self._filter_chips.items() if chip.isChecked()}

    @staticmethod
    def _is_plugin_active(plugin_name: str, active: set[str] | None) -> bool:
        """Return whether *plugin_name* passes the chip filter."""
        return active is None or plugin_name in active

    @staticmethod
    def _finalise_provider(
        provider: PluginProviderHeader | None,
        has_visible: bool,
    ) -> bool:
        """Set provider visibility and return whether it had visible children."""
        if provider is not None:
            provider.setVisible(has_visible)
        return has_visible

    def _apply_filter(self, _text: str | None = None) -> None:
        """Show/hide section widgets based on search text and active chips.

        Delegates to :meth:`_is_plugin_active` for chip matching and
        :meth:`_finalise_provider` for provider visibility bookkeeping.
        """
        query = self._search_input.text().strip().lower()
        active = self._active_chip_plugins()

        current_kind_header: PluginKindHeader | None = None
        kind_has_visible = False
        current_provider: PluginProviderHeader | None = None
        provider_has_visible_child = False

        for widget in self._section_widgets:
            if isinstance(widget, PluginKindHeader):
                kind_has_visible |= self._finalise_provider(
                    current_provider, provider_has_visible_child
                )
                if current_kind_header is not None:
                    current_kind_header.setVisible(kind_has_visible)

                current_kind_header = widget
                kind_has_visible = False
                current_provider = None
                provider_has_visible_child = False

            elif isinstance(widget, PluginProviderHeader):
                kind_has_visible |= self._finalise_provider(
                    current_provider, provider_has_visible_child
                )

                current_provider = widget
                provider_has_visible_child = False
                if not self._is_plugin_active(widget._plugin_name, active):
                    widget.setVisible(False)

            elif isinstance(widget, PluginRow):
                if not self._is_plugin_active(widget._plugin_name, active):
                    widget.setVisible(False)
                    continue

                # Match search query against package name, plugin name, and project labels
                name_match = not query or (
                    query in widget._package_name.lower()
                    or query in widget._plugin_name.lower()
                    or any(query in lbl.lower() for lbl in widget._project_labels)
                )
                widget.setVisible(name_match)
                if name_match:
                    provider_has_visible_child = True

        # Finalise last provider and kind
        kind_has_visible |= self._finalise_provider(
            current_provider, provider_has_visible_child
        )
        if current_kind_header is not None:
            current_kind_header.setVisible(kind_has_visible)

        self._update_filter_badge()

    def _create_connected_row(self, data: PluginRowData) -> PluginRow:
        """Create a :class:`PluginRow` and wire all its signals."""
        row = PluginRow(data, parent=self._container)
        row.auto_update_toggled.connect(self._on_package_auto_update_toggled)
        row.update_requested.connect(self.package_update_requested.emit)
        row.remove_requested.connect(self.package_remove_requested.emit)
        row.navigate_to_project.connect(self.navigate_to_project_requested.emit)
        return row

    def _insert_section_widget(self, widget: QWidget) -> None:
        """Append a widget to the container layout above the stretch."""
        idx = self._container_layout.count() - 1
        self._container_layout.insertWidget(idx, widget)
        self._section_widgets.append(widget)

    async def _fetch_data(self) -> tuple[list[PluginInfo], list[ProjectInfo]]:
        """Fetch plugin list and directories via the coordinator (or direct fallback)."""
        if self._coordinator is not None:
            snapshot = await self._coordinator.refresh()
            return snapshot.plugins, snapshot.directories
        from spurtle.operations.project import list_projects

        plugins = await self._porringer.extension.list()
        directories = await asyncio.to_thread(list_projects, self._porringer)
        return plugins, directories

    # --- Callbacks ---

    def _on_auto_update_toggled(self, plugin_name: str, enabled: bool) -> None:
        """Persist the plugin-level auto-update toggle change to config."""
        mapping = dict(self._store.config.plugin_auto_update or {})

        if enabled:
            mapping.pop(plugin_name, None)
        else:
            mapping[plugin_name] = False

        new_value = mapping if mapping else None
        self._store.update(plugin_auto_update=new_value)
        logger.info("Auto-update for %s set to %s", plugin_name, enabled)

    def _on_package_auto_update_toggled(
        self,
        plugin_name: str,
        package_name: str,
        enabled: bool,
    ) -> None:
        """Persist a per-package auto-update override to the nested config dict."""
        mapping = dict(self._store.config.plugin_auto_update or {})
        current = mapping.get(plugin_name)

        if isinstance(current, dict):
            pkg_dict: dict[str, bool] = dict(current)
        else:
            pkg_dict = {}

        pkg_dict[package_name] = enabled

        if pkg_dict:
            mapping[plugin_name] = pkg_dict
        else:
            mapping.pop(plugin_name, None)

        new_value = mapping if mapping else None
        self._store.update(plugin_auto_update=new_value)
        logger.info(
            "Auto-update for %s/%s set to %s",
            plugin_name,
            package_name,
            enabled,
        )

    def _on_check_for_updates(self) -> None:
        """Start an inline update check with per-row spinners."""
        if self._check_in_progress or self._refresh_in_progress:
            return
        self._check_btn.setEnabled(False)
        self._check_btn.setText("Checking\u2026")
        self._set_all_checking(True)
        asyncio.create_task(self._run_inline_update_check())

    async def _run_inline_update_check(self) -> None:
        """Check for updates with inline spinners (no overlay / rebuild).

        Used by both the manual *Check for Updates* button and the
        automatic deferred check after initial refresh.
        """
        self._check_in_progress = True
        try:
            available = await self._check_for_updates()
            self._store_check_results(available)
            self._apply_update_badges()
        except Exception:
            logger.debug("Inline update check failed", exc_info=True)
        finally:
            self._set_all_checking(False)
            self._check_btn.setEnabled(True)
            self._check_btn.setText("Check for Updates")
            self._check_in_progress = False

    async def _check_for_updates(self) -> dict[str, dict[str, str]]:
        """Detect available updates for the packages registered manifests declare.

        Returns a mapping of ``{plugin_name: {package_name: latest_version}}``
        for packages that have a newer version available.
        """
        discovered = self._coordinator.discovered_plugins if self._coordinator else None
        return await check_tool_updates(
            self._porringer, self._manifest_paths(), discovered
        )

    def _get_plugin_updates(self, signal_key: str) -> dict[str, str]:
        """Return ``{package_name: latest_version}`` for *signal_key*.

        Reads from the shared :class:`PackageStateStore` when available,
        otherwise returns an empty dict.
        """
        if self._package_store is not None:
            return self._package_store.get_updates(signal_key)
        return {}

    def _store_check_results(self, available: dict[str, dict[str, str]]) -> None:
        """Push check results into the store."""
        if self._package_store is not None:
            self._package_store.set_check_results(available)

    @property
    def _has_update_data(self) -> bool:
        """Return whether update data has been fetched at least once."""
        return self._package_store is not None and self._package_store.has_data

    def _on_package_state_changed(self) -> None:
        """Re-apply badges when another view writes to the shared store."""
        if not self._check_in_progress and not self._refresh_in_progress:
            self._apply_update_badges()

    def _apply_update_badges(self) -> None:
        """Walk existing widgets and show/hide Update buttons + set inline status."""
        current_plugin: str = ""
        for widget in self._section_widgets:
            if isinstance(widget, PluginProviderHeader):
                current_plugin = widget._signal_key
                plugin_updates = self._get_plugin_updates(current_plugin)
                has = bool(plugin_updates)
                if widget._update_btn is not None:
                    widget._update_btn.setVisible(has)
            elif isinstance(widget, PluginRow) and widget._plugin_name:
                plugin_updates = self._get_plugin_updates(widget._signal_key)
                latest_version = plugin_updates.get(widget._package_name)
                has_update = latest_version is not None

                if widget._update_btn is not None:
                    widget._update_btn.setVisible(has_update)

                # Set inline status text
                if has_update:
                    version_text = (
                        f"v{latest_version} available"
                        if latest_version
                        else "Update available"
                    )
                    widget.set_update_status(
                        version_text, PLUGIN_ROW_STATUS_AVAILABLE_STYLE
                    )
                elif self._has_update_data:
                    widget.set_update_status(
                        "Up to date", PLUGIN_ROW_STATUS_UP_TO_DATE_STYLE
                    )

    def _set_all_checking(self, checking: bool) -> None:
        """Show or hide inline checking spinners on all plugin rows."""
        for widget in self._section_widgets:
            if isinstance(widget, (PluginProviderHeader, PluginRow)):
                widget.set_checking(checking)

    def _refresh_timestamps(self) -> None:
        """Refresh relative time labels on all plugin rows (called by timer)."""
        for widget in self._section_widgets:
            if isinstance(widget, PluginRow):
                widget.update_timestamp()

    def navigate_to_package(self, plugin_name: str, package_name: str) -> None:
        """Scroll to and briefly highlight a specific package row.

        Opens the filter panel, sets the search text to filter down to the
        target package, ensures the plugin chip is active, scrolls the row
        into view, and applies a transient amber highlight.
        """
        # Find the target row first
        target: PluginRow | None = None
        for widget in self._section_widgets:
            if (
                isinstance(widget, PluginRow)
                and widget._plugin_name == plugin_name
                and widget._package_name == package_name
            ):
                target = widget
                break

        if target is None:
            return

        # Ensure the plugin chip is checked so the row won't be hidden
        self._deselected_plugins.discard(plugin_name)
        chip = self._filter_chips.get(plugin_name)
        if chip is not None:
            chip.setChecked(True)

        # Set search text and apply the filter
        self._search_input.setText(package_name)
        self._apply_filter()

        # Open the filter panel if closed
        self._open_filter_panel()

        # Scroll the row into view
        self._scroll.ensureWidgetVisible(target)

        # Apply a brief amber highlight
        target.setStyleSheet(PLUGIN_ROW_HIGHLIGHT_STYLE)
        QTimer.singleShot(2000, lambda: target.setStyleSheet(PLUGIN_ROW_STYLE))

    def set_plugin_updating(self, plugin_name: str, updating: bool) -> None:
        """Toggle the *Updatingâ€¦* state on the header for *plugin_name*."""
        for widget in self._section_widgets:
            if (
                isinstance(widget, PluginProviderHeader)
                and widget._signal_key == plugin_name
            ):
                widget.set_updating(updating)
                break

    def set_package_updating(
        self,
        plugin_name: str,
        package_name: str,
        updating: bool,
    ) -> None:
        """Toggle the *Updatingâ€¦* state on a specific package row."""
        for widget in self._section_widgets:
            if (
                isinstance(widget, PluginRow)
                and widget._signal_key == plugin_name
                and widget._package_name == package_name
            ):
                widget.set_updating(updating)
                break

    def set_package_removing(
        self,
        plugin_name: str,
        package_name: str,
        removing: bool,
    ) -> None:
        """Toggle the *Removingâ€¦* state on a specific package row."""
        for widget in self._section_widgets:
            if (
                isinstance(widget, PluginRow)
                and widget._signal_key == plugin_name
                and widget._package_name == package_name
            ):
                widget.set_removing(removing)
                break

    def set_package_error(
        self,
        plugin_name: str,
        package_name: str,
        message: str,
    ) -> None:
        """Show a transient inline error on a specific package row."""
        for widget in self._section_widgets:
            if (
                isinstance(widget, PluginRow)
                and widget._signal_key == plugin_name
                and widget._package_name == package_name
            ):
                widget.set_error(message)
                break

    def set_plugin_error(self, plugin_name: str, message: str) -> None:
        """Show a transient inline error on the header for *plugin_name*."""
        for widget in self._section_widgets:
            if (
                isinstance(widget, PluginProviderHeader)
                and widget._signal_key == plugin_name
            ):
                widget.set_error(message)
                break

    # -- Per-package progress helpers for plugin-level updates --

    def get_plugin_update_packages(self, signal_key: str) -> set[str]:
        """Return the set of package names with known updates for *signal_key*."""
        return set(self._get_plugin_updates(signal_key).keys())

    def set_packages_pending(self, signal_key: str, package_names: set[str]) -> None:
        """Mark matching package rows as *Pending* for a plugin-level update."""
        for widget in self._section_widgets:
            if (
                isinstance(widget, PluginRow)
                and widget._signal_key == signal_key
                and widget._package_name in package_names
            ):
                widget.set_phase(RowPhase.PENDING)

    def set_package_active(self, signal_key: str, package_name: str) -> None:
        """Transition a package row from *Pending* to *Updating* (spinner)."""
        for widget in self._section_widgets:
            if (
                isinstance(widget, PluginRow)
                and widget._signal_key == signal_key
                and widget._package_name == package_name
            ):
                widget.set_phase(RowPhase.UPDATING)
                break

    def clear_plugin_row_states(self, signal_key: str) -> None:
        """Reset pending / updating state on all package rows for *signal_key*."""
        for widget in self._section_widgets:
            if isinstance(widget, PluginRow) and widget._signal_key == signal_key:
                widget.set_phase(RowPhase.IDLE)


class MainWindow(QMainWindow):
    """Main window for the application."""

    settings_requested = Signal()
    """Emitted when the user clicks the settings gear button."""

    tools_view_created = Signal(ToolsView)
    """Emitted once when the :class:`ToolsView` is lazily initialised."""

    _tabs: QTabWidget | None = None
    _tools_view: ToolsView | None = None
    _projects_view: ProjectsView | None = None

    def __init__(
        self,
        porringer: API | None = None,
        store: ConfigStore | None = None,
    ) -> None:
        """Initialize the main window.

        Args:
            porringer: Optional porringer API instance for manifest display.
            store: The centralised :class:`ConfigStore`.
        """
        super().__init__()
        self._porringer = porringer
        self._store = store
        self._coordinator: DataCoordinator | None = (
            DataCoordinator(porringer) if porringer is not None else None
        )
        self._package_store: PackageStateStore | None = (
            PackageStateStore(self) if porringer is not None else None
        )
        self.setWindowTitle("Spurtle")
        self.setMinimumSize(*MAIN_WINDOW_MIN_SIZE)
        self.setWindowIcon(app_icon())

        # Update banner â€” always available, starts hidden.
        self._update_banner = UpdateBanner(self)

    def showEvent(self, event: QShowEvent) -> None:
        """[DIAG] Log every show event with a stack trace."""
        geo = self.geometry()
        stack = "".join(traceback.format_stack(limit=10))
        logger.debug(
            "[DIAG] MainWindow.showEvent: geo=(%d,%d %dx%d) visible=%s\n%s",
            geo.x(),
            geo.y(),
            geo.width(),
            geo.height(),
            self.isVisible(),
            stack,
        )
        super().showEvent(event)

    @property
    def porringer(self) -> API | None:
        """Return the porringer API instance, if available."""
        return self._porringer

    @property
    def coordinator(self) -> DataCoordinator | None:
        """Return the shared data coordinator, if available."""
        return self._coordinator

    @property
    def tools_view(self) -> ToolsView | None:
        """Return the tools view, if initialised."""
        return self._tools_view

    @property
    def update_banner(self) -> UpdateBanner:
        """Return the update banner widget."""
        return self._update_banner

    def show(self) -> None:
        """Show the window, initializing UI lazily on first show."""
        if (
            self._tabs is None
            and self._porringer is not None
            and self._store is not None
        ):
            self._tabs = QTabWidget(self)

            self._projects_view = ProjectsView(
                self._porringer,
                self._store,
                self,
                coordinator=self._coordinator,
                package_store=self._package_store,
            )
            self._tabs.addTab(self._projects_view, "Projects")

            from spurtle.application.screen.inventory_view import InventoryToolsView

            self._tools_view = InventoryToolsView(
                self._porringer,
                self._store,
                self,
                coordinator=self._coordinator,
                package_store=self._package_store,
            )
            self._tabs.addTab(self._tools_view, "Tools")
            self.tools_view_created.emit(self._tools_view)

            # Navigate-to-project: switch to Projects tab and select directory
            self._tools_view.navigate_to_project_requested.connect(
                self._navigate_to_project
            )

            # Navigate-to-tool: switch to Tools tab and highlight the package
            self._projects_view.navigate_to_tool_requested.connect(
                self._navigate_to_tool
            )

            gear_btn = QPushButton("\u2699")
            gear_btn.setStyleSheet(SETTINGS_GEAR_STYLE)
            gear_btn.setToolTip("Settings")
            gear_btn.setFlat(True)
            gear_btn.clicked.connect(self.settings_requested.emit)
            self._tabs.setCornerWidget(gear_btn)

            # Container: banner above tabs
            container = QWidget(self)
            container_layout = QVBoxLayout(container)
            container_layout.setContentsMargins(0, 0, 0, 0)
            container_layout.setSpacing(0)
            container_layout.addWidget(self._update_banner)
            container_layout.addWidget(self._tabs)
            self.setCentralWidget(container)

        # Paint the window immediately, then refresh data asynchronously
        super().show()

        if self._tools_view is not None:
            self._tools_view.refresh()
        if self._projects_view is not None:
            self._projects_view.refresh()

    def _navigate_to_project(self, path_str: str) -> None:
        """Switch to the Projects tab and select the given directory."""
        if self._tabs is not None and self._projects_view is not None:
            self._tabs.setCurrentIndex(0)
            self._projects_view._sidebar.select(path_str)

    def _navigate_to_tool(self, plugin_name: str, package_name: str) -> None:
        """Switch to the Tools tab and highlight the given package."""
        if self._tabs is not None and self._tools_view is not None:
            self._tabs.setCurrentIndex(1)
            self._tools_view.navigate_to_package(plugin_name, package_name)

    def navigate_to_setup(self, profile_url: str) -> None:
        """Switch to the Projects tab and add a setup profile.

        Called from deep link handling to add a remote profile URL
        and select it in the sidebar.
        """
        self.show()
        if self._tabs is not None and self._projects_view is not None:
            self._tabs.setCurrentIndex(0)
            self._projects_view.add_profile(profile_url)


class Screen:
    """Screen class for the Spurtle application."""

    _window: MainWindow | None = None

    def __init__(
        self,
        porringer: API | None = None,
        store: ConfigStore | None = None,
    ) -> None:
        """Initialize the screen.

        Args:
            porringer: Optional porringer API instance.
            store: The centralised :class:`ConfigStore`.
        """
        self._porringer = porringer
        self._store = store

    @property
    def window(self) -> MainWindow:
        """Lazily create the main window on first access.

        Returns:
            The MainWindow instance.
        """
        if self._window is None:
            self._window = MainWindow(self._porringer, self._store)
        return self._window
