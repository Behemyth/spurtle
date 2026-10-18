"""Tests for ToolsView refresh data, display model, and filtering."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from packaging.version import Version
from porringer.schema.plugin import PluginInfo, PluginKind
from PySide6.QtWidgets import QLabel, QPushButton

import spurtle.application.screen.screen as screen_module
from spurtle.application.screen.plugin_row import (
    FilterChip,
    PluginKindHeader,
    PluginProviderHeader,
    PluginRow,
    ProjectChildRow,
)
from spurtle.application.screen.schema import PackageEntry, PluginRowData, ProjectInstance
from spurtle.application.screen.screen import ToolsView
from spurtle.application.theme import FILTER_TOGGLE_ACTIVE_STYLE, FILTER_TOGGLE_STYLE
from spurtle.operations.schema import DeclaredPackage, ProjectInfo

from .conftest import make_config_store, make_mock_porringer

# Named constants for expected counts (avoids PLR2004)
_EXPECTED_PROJECT_INSTANCES = 2
_EXPECTED_VISIBLE_ROWS_ALL = 3
_EXPECTED_VISIBLE_ROWS_PIPX = 2


def _plugin(name: str, kind: PluginKind, tool_version: str | None = None) -> PluginInfo:
    """Build a ``PluginInfo``."""
    return PluginInfo(
        name=name,
        kind=kind,
        version=Version('0.1.0'),
        installed=True,
        tool_version=Version(tool_version) if tool_version is not None else None,
    )


# ---------------------------------------------------------------------------
# _gather_refresh_data
# ---------------------------------------------------------------------------


class TestGatherRefreshData:
    """Verify the refresh data is built from manifest-declared packages only."""

    @staticmethod
    def test_builds_rows_from_declared_packages(monkeypatch: pytest.MonkeyPatch) -> None:
        """Declared packages become rows with one instance per declaring project."""
        plugins = [_plugin('pipx', PluginKind.TOOL)]
        directories = [
            ProjectInfo(path='/projects/a', name='a', exists=True, has_manifest=True),
            ProjectInfo(path='/projects/b', name='b', exists=True, has_manifest=True),
            ProjectInfo(path='/projects/gone', name='gone', exists=False, has_manifest=False),
            ProjectInfo(path='/projects/bare', name='bare', exists=True, has_manifest=False),
        ]
        declared = [
            DeclaredPackage(
                plugin='pipx',
                name='ruff',
                kind='tools',
                installed_version='0.8.0',
                available_version='0.9.0',
                project_paths=['/projects/a', '/projects/b'],
                action_ids={'0:0', '1:0'},
            ),
            DeclaredPackage(plugin='pipx', name='pdm', kind='tools', project_paths=['/projects/a']),
        ]
        declared_mock = AsyncMock(return_value=declared)
        monkeypatch.setattr(screen_module, 'declared_packages', declared_mock)

        view = ToolsView(make_mock_porringer(), make_config_store())
        monkeypatch.setattr(view, '_fetch_data', AsyncMock(return_value=(plugins, directories)))

        data = asyncio.run(view._gather_refresh_data())

        assert declared_mock.await_args.args[1] == ['/projects/a', '/projects/b']
        assert data.plugins == plugins
        assert data.manifest_packages == {'pipx': {'ruff', 'pdm'}}
        assert data.available == {'pipx': {'ruff': '0.9.0'}}
        assert data.packages_map['pipx'] == [
            PackageEntry(name='ruff', version='0.8.0'),
            PackageEntry(name='ruff', project_label='a', version='0.8.0', project_path='/projects/a'),
            PackageEntry(name='ruff', project_label='b', version='0.8.0', project_path='/projects/b'),
            PackageEntry(name='pdm', version=''),
            PackageEntry(name='pdm', project_label='a', version='', project_path='/projects/a'),
        ]

    @staticmethod
    def test_no_declared_packages(monkeypatch: pytest.MonkeyPatch) -> None:
        """No declared packages yields empty maps."""
        monkeypatch.setattr(screen_module, 'declared_packages', AsyncMock(return_value=[]))
        view = ToolsView(make_mock_porringer(), make_config_store())
        monkeypatch.setattr(view, '_fetch_data', AsyncMock(return_value=([], [])))

        data = asyncio.run(view._gather_refresh_data())

        assert data.packages_map == {}
        assert data.manifest_packages == {}
        assert data.available == {}


# ---------------------------------------------------------------------------
# _bucket_by_kind
# ---------------------------------------------------------------------------


class TestBucketByKind:
    """Verify plugin grouping by kind."""

    @staticmethod
    def test_runtime_with_packages_included() -> None:
        """RUNTIME plugins with declared packages appear in the buckets."""
        plugins = [_plugin('pim', PluginKind.RUNTIME)]
        buckets = ToolsView._bucket_by_kind(plugins, {'pim': [PackageEntry(name='3.15', version='3.15.0')]})
        assert [p.name for p in buckets[PluginKind.RUNTIME]] == ['pim']

    @staticmethod
    def test_plugin_without_content_excluded() -> None:
        """Plugins without packages or a tool version are excluded."""
        plugins = [_plugin('pim', PluginKind.RUNTIME)]
        assert PluginKind.RUNTIME not in ToolsView._bucket_by_kind(plugins, {'pim': []})

    @staticmethod
    def test_tool_version_alone_includes_plugin() -> None:
        """An installed tool without declared packages still appears."""
        plugins = [_plugin('uv', PluginKind.TOOL, tool_version='0.5.0')]
        assert [p.name for p in ToolsView._bucket_by_kind(plugins, {})[PluginKind.TOOL]] == ['uv']

    @staticmethod
    def test_non_updatable_kinds_excluded() -> None:
        """PROJECT and SCM plugins never appear."""
        plugins = [
            _plugin('pdm', PluginKind.PROJECT, tool_version='2.0.0'),
            _plugin('git', PluginKind.SCM, tool_version='2.45.0'),
        ]
        assert not ToolsView._bucket_by_kind(plugins, {'pdm': [PackageEntry(name='x', version='1')]})


# ---------------------------------------------------------------------------
# _build_display_packages
# ---------------------------------------------------------------------------


class TestBuildDisplayPackages:
    """Verify the two-tier display model built from raw PackageEntry lists."""

    @staticmethod
    def test_global_only_package() -> None:
        """A package without a project path has is_global=True and no project instances."""
        entries = [PackageEntry(name='ruff', version='0.8.0')]
        result = ToolsView._build_display_packages(entries, set())

        assert len(result) == 1
        pkg = result[0]
        assert pkg.name == 'ruff'
        assert pkg.is_global is True
        assert pkg.global_version == '0.8.0'
        assert pkg.project_instances == []

    @staticmethod
    def test_project_only_package() -> None:
        """A package found only in a project is not global."""
        entries = [
            PackageEntry(
                name='cppython',
                version='0.9.15.dev3',
                project_label='myproject',
                project_path='/projects/myproject',
            ),
        ]
        result = ToolsView._build_display_packages(entries, set())

        assert len(result) == 1
        pkg = result[0]
        assert pkg.name == 'cppython'
        assert pkg.is_global is False
        assert pkg.global_version is None
        assert len(pkg.project_instances) == 1
        assert pkg.project_instances[0].project_label == 'myproject'
        assert pkg.project_instances[0].project_path == '/projects/myproject'

    @staticmethod
    def test_both_global_and_project() -> None:
        """A package found globally AND in a project has both fields set."""
        entries = [
            PackageEntry(name='ruff', version='0.8.0'),
            PackageEntry(
                name='ruff',
                version='0.7.0',
                project_label='myproject',
                project_path='/projects/myproject',
            ),
        ]
        result = ToolsView._build_display_packages(entries, set())

        assert len(result) == 1
        pkg = result[0]
        assert pkg.is_global is True
        assert pkg.global_version == '0.8.0'
        assert len(pkg.project_instances) == 1
        assert pkg.project_instances[0].version == '0.7.0'

    @staticmethod
    def test_transitive_dependency_marked() -> None:
        """Project-scoped packages not in the manifest are marked transitive."""
        entries = [
            PackageEntry(
                name='cppython',
                version='0.9.15.dev3',
                project_label='myproject',
                project_path='/projects/myproject',
            ),
        ]
        result = ToolsView._build_display_packages(entries, set())
        assert result[0].project_instances[0].is_transitive is True

    @staticmethod
    def test_manifest_declared_not_transitive() -> None:
        """Project-scoped packages in the manifest are NOT marked transitive."""
        entries = [
            PackageEntry(
                name='cppython',
                version='0.9.15.dev3',
                project_label='myproject',
                project_path='/projects/myproject',
            ),
        ]
        result = ToolsView._build_display_packages(entries, {'cppython'})
        assert result[0].project_instances[0].is_transitive is False

    @staticmethod
    def test_multiple_projects_same_package() -> None:
        """Same package in multiple projects creates multiple ProjectInstances."""
        entries = [
            PackageEntry(
                name='requests',
                version='2.31.0',
                project_label='project-a',
                project_path='/projects/a',
            ),
            PackageEntry(
                name='requests',
                version='2.30.0',
                project_label='project-b',
                project_path='/projects/b',
            ),
        ]
        result = ToolsView._build_display_packages(entries, set())

        assert len(result) == 1
        pkg = result[0]
        assert len(pkg.project_instances) == _EXPECTED_PROJECT_INSTANCES
        labels = {pi.project_label for pi in pkg.project_instances}
        assert labels == {'project-a', 'project-b'}

    @staticmethod
    def test_deduplicates_same_project_path() -> None:
        """Duplicate entries for the same project_path produce one instance."""
        entries = [
            PackageEntry(
                name='ruff',
                version='0.8.0',
                project_label='myproject',
                project_path='/projects/myproject',
            ),
            PackageEntry(
                name='ruff',
                version='0.8.0',
                project_label='myproject',
                project_path='/projects/myproject',
            ),
        ]
        result = ToolsView._build_display_packages(entries, set())
        assert len(result[0].project_instances) == 1

    @staticmethod
    def test_host_tool_preserved() -> None:
        """The host_tool from the first entry is carried through."""
        entries = [
            PackageEntry(name='cppython', version='0.5.0', host_tool='pdm'),
        ]
        result = ToolsView._build_display_packages(entries, set())
        assert result[0].host_tool == 'pdm'

    @staticmethod
    def test_empty_input() -> None:
        """Empty input returns empty list."""
        result = ToolsView._build_display_packages([], set())
        assert result == []


# ---------------------------------------------------------------------------
# ProjectChildRow
# ---------------------------------------------------------------------------


class TestProjectChildRow:
    """Verify the ProjectChildRow widget renders and emits signals."""

    @staticmethod
    def _make_instance(*, transitive: bool = False) -> ProjectChildRow:
        return ProjectChildRow(
            ProjectInstance(
                project_label='myproject',
                project_path='/projects/myproject',
                version='0.9.15.dev3',
                is_transitive=transitive,
            ),
            package_name='cppython',
        )

    def test_navigate_signal_emitted(self) -> None:
        """Clicking the navigate button emits the project path."""
        row = self._make_instance()
        spy = MagicMock()
        row.navigate_to_project.connect(spy)

        nav_btns = [w for w in row.findChildren(QPushButton) if w.text() == '\u2192']
        assert len(nav_btns) == 1
        nav_btns[0].click()
        spy.assert_called_once_with('/projects/myproject')

    def test_transitive_label_shown(self) -> None:
        """Transitive instances show a (transitive) label."""
        row = self._make_instance(transitive=True)
        labels = [w for w in row.findChildren(QLabel) if w.text() == '(transitive)']
        assert len(labels) == 1

    def test_transitive_label_hidden_when_not_transitive(self) -> None:
        """Non-transitive instances do not show a (transitive) label."""
        row = self._make_instance(transitive=False)
        labels = [w for w in row.findChildren(QLabel) if w.text() == '(transitive)']
        assert len(labels) == 0


# ---------------------------------------------------------------------------
# FilterChip
# ---------------------------------------------------------------------------


class TestFilterChip:
    """Verify the FilterChip widget renders and emits toggled signals."""

    @staticmethod
    def test_chip_starts_checked() -> None:
        """Filter chips start in the checked (active) state."""
        chip = FilterChip('pipx')
        assert chip.isChecked()
        assert chip.text() == 'pipx'

    @staticmethod
    def test_toggling_emits_signal() -> None:
        """Toggling a chip emits the plugin name and new state."""
        chip = FilterChip('uv')
        spy = MagicMock()
        chip.toggled_with_name.connect(spy)

        chip.setChecked(False)
        spy.assert_called_once_with('uv', False)

    @staticmethod
    def test_recheck_emits_true() -> None:
        """Re-checking a chip emits True."""
        chip = FilterChip('pip')
        spy = MagicMock()
        chip.setChecked(False)
        chip.toggled_with_name.connect(spy)
        chip.setChecked(True)
        spy.assert_called_once_with('pip', True)


# ---------------------------------------------------------------------------
# Search & filter integration (ToolsView._apply_filter)
# ---------------------------------------------------------------------------


class TestSearchFilter:
    """Verify the search and filter logic on ToolsView."""

    @staticmethod
    def _make_view() -> ToolsView:
        """Build a ToolsView with a mock porringer."""
        return ToolsView(make_mock_porringer(), make_config_store())

    @staticmethod
    def _populate_section_widgets(view: ToolsView) -> None:
        """Manually inject section widgets simulating a two-plugin tree.

        Structure:
          KindHeader(TOOL)
            ProviderHeader(pipx)
              PluginRow(ruff, plugin=pipx)
              PluginRow(pdm, plugin=pipx)
            ProviderHeader(uv)
              PluginRow(mypy, plugin=uv)
        """
        widgets = [
            PluginKindHeader(PluginKind.TOOL),
            PluginProviderHeader(_plugin('pipx', PluginKind.TOOL, tool_version='1.0.0'), True),
            PluginRow(PluginRowData(name='ruff', plugin_name='pipx')),
            PluginRow(PluginRowData(name='pdm', plugin_name='pipx')),
            PluginProviderHeader(_plugin('uv', PluginKind.TOOL, tool_version='2.0.0'), True),
            PluginRow(PluginRowData(name='mypy', plugin_name='uv')),
        ]
        for position, widget in enumerate(widgets):
            view._section_widgets.append(widget)
            view._container_layout.insertWidget(position, widget)

        view._rebuild_chips()

    def test_search_hides_non_matching_rows(self) -> None:
        """Typing a search term hides rows whose names don't match."""
        view = self._make_view()
        self._populate_section_widgets(view)

        view._search_input.setText('ruff')

        visible_rows = [w for w in view._section_widgets if isinstance(w, PluginRow) and not w.isHidden()]
        assert len(visible_rows) == 1
        assert visible_rows[0]._package_name == 'ruff'

    def test_empty_search_shows_all(self) -> None:
        """Clearing the search bar makes all rows visible again."""
        view = self._make_view()
        self._populate_section_widgets(view)

        view._search_input.setText('ruff')
        view._search_input.setText('')

        visible_rows = [w for w in view._section_widgets if isinstance(w, PluginRow) and not w.isHidden()]
        assert len(visible_rows) == _EXPECTED_VISIBLE_ROWS_ALL

    def test_chip_deselection_hides_plugin(self) -> None:
        """Deselecting a chip hides all rows from that plugin."""
        view = self._make_view()
        self._populate_section_widgets(view)

        view._filter_chips['pipx'].setChecked(False)

        visible_rows = [w for w in view._section_widgets if isinstance(w, PluginRow) and not w.isHidden()]
        assert len(visible_rows) == 1
        assert visible_rows[0]._package_name == 'mypy'

    def test_chip_reselection_restores(self) -> None:
        """Re-checking a chip restores the plugin's rows."""
        view = self._make_view()
        self._populate_section_widgets(view)

        view._filter_chips['pipx'].setChecked(False)
        view._filter_chips['pipx'].setChecked(True)

        visible_rows = [w for w in view._section_widgets if isinstance(w, PluginRow) and not w.isHidden()]
        assert len(visible_rows) == _EXPECTED_VISIBLE_ROWS_ALL

    def test_search_plus_chip_filter(self) -> None:
        """Search and chip filtering compose - only matching rows in active plugins survive."""
        view = self._make_view()
        self._populate_section_widgets(view)

        view._filter_chips['uv'].setChecked(False)
        view._search_input.setText('pdm')

        visible_rows = [w for w in view._section_widgets if isinstance(w, PluginRow) and not w.isHidden()]
        assert len(visible_rows) == 1
        assert visible_rows[0]._package_name == 'pdm'

    def test_kind_header_hidden_when_no_children_visible(self) -> None:
        """Kind headers hide when all their children are filtered out."""
        view = self._make_view()
        self._populate_section_widgets(view)

        view._filter_chips['pipx'].setChecked(False)
        view._filter_chips['uv'].setChecked(False)

        hidden_kinds = [w for w in view._section_widgets if isinstance(w, PluginKindHeader) and w.isHidden()]
        assert len(hidden_kinds) == 1

    def test_provider_hidden_when_search_matches_nothing(self) -> None:
        """A provider header hides when no child rows match the search."""
        view = self._make_view()
        self._populate_section_widgets(view)

        view._search_input.setText('mypy')

        visible_providers = [
            w for w in view._section_widgets if isinstance(w, PluginProviderHeader) and not w.isHidden()
        ]
        assert len(visible_providers) == 1
        assert visible_providers[0]._plugin_name == 'uv'

    def test_search_matches_plugin_name(self) -> None:
        """Searching by plugin name shows all rows from that plugin."""
        view = self._make_view()
        self._populate_section_widgets(view)

        view._search_input.setText('pipx')

        visible_rows = [w for w in view._section_widgets if isinstance(w, PluginRow) and not w.isHidden()]
        assert len(visible_rows) == _EXPECTED_VISIBLE_ROWS_PIPX
        names = {w._package_name for w in visible_rows}
        assert names == {'ruff', 'pdm'}

    def test_deselected_chips_preserved_across_rebuild(self) -> None:
        """_rebuild_chips preserves deselected state from self._deselected_plugins."""
        view = self._make_view()
        self._populate_section_widgets(view)

        view._filter_chips['pipx'].setChecked(False)
        assert 'pipx' in view._deselected_plugins

        view._rebuild_chips()
        assert not view._filter_chips['pipx'].isChecked()
        assert view._filter_chips['uv'].isChecked()


# ---------------------------------------------------------------------------
# Filter panel toggle (ToolsView collapsible filter)
# ---------------------------------------------------------------------------


class TestFilterPanel:
    """Verify the collapsible filter panel toggle behaviour."""

    @staticmethod
    def _make_view() -> ToolsView:
        """Build a ToolsView with a mock porringer."""
        return ToolsView(make_mock_porringer(), make_config_store())

    def test_panel_starts_collapsed(self) -> None:
        """The filter panel is hidden and has zero max-height on init."""
        view = self._make_view()
        assert not view._filter_panel.isVisible()
        assert view._filter_panel.maximumHeight() == 0
        assert not view._filter_panel_open

    def test_toggle_opens_panel(self) -> None:
        """Calling _toggle_filter_panel opens a collapsed panel."""
        view = self._make_view()
        view._toggle_filter_panel()
        assert view._filter_panel_open
        assert not view._filter_panel.isHidden()

    def test_toggle_closes_open_panel(self) -> None:
        """Calling _toggle_filter_panel on an open panel closes it."""
        view = self._make_view()
        view._open_filter_panel()
        view._toggle_filter_panel()
        assert not view._filter_panel_open

    def test_badge_inactive_by_default(self) -> None:
        """The filter badge is inactive when no filters are set."""
        view = self._make_view()
        assert view._filter_btn.styleSheet() == FILTER_TOGGLE_STYLE

    def test_badge_active_with_search_text(self) -> None:
        """The filter badge activates when search text is entered."""
        view = self._make_view()
        view._search_input.setText('ruff')
        assert view._filter_btn.styleSheet() == FILTER_TOGGLE_ACTIVE_STYLE

    def test_badge_active_with_deselected_chip(self) -> None:
        """The filter badge activates when a chip is deselected."""
        view = self._make_view()
        view._deselected_plugins.add('test-plugin')
        view._update_filter_badge()
        assert view._filter_btn.styleSheet() == FILTER_TOGGLE_ACTIVE_STYLE

    def test_badge_clears_when_filters_removed(self) -> None:
        """The filter badge deactivates when all filters are cleared."""
        view = self._make_view()
        view._search_input.setText('ruff')
        view._search_input.clear()
        assert view._filter_btn.styleSheet() == FILTER_TOGGLE_STYLE

    def test_clear_active_filters_resets_search(self) -> None:
        """_clear_active_filters clears search text and rechecks chips."""
        view = self._make_view()
        view._search_input.setText('some query')
        view._deselected_plugins.add('fake')
        view._clear_active_filters()
        assert not view._search_input.text()

    def test_has_active_filter_false_by_default(self) -> None:
        """_has_active_filter is False when no filters are set."""
        view = self._make_view()
        assert not view._has_active_filter

    def test_has_active_filter_true_with_text(self) -> None:
        """_has_active_filter is True when search text is non-empty."""
        view = self._make_view()
        view._search_input.setText('x')
        assert view._has_active_filter

    def test_has_active_filter_true_with_deselected(self) -> None:
        """_has_active_filter is True when plugins are deselected."""
        view = self._make_view()
        view._deselected_plugins.add('p')
        assert view._has_active_filter
