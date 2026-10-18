"""Tests for operations.project module."""

import json
from pathlib import Path
from unittest.mock import MagicMock, create_autospec

import pytest
from porringer import API

import spurtle.config as config_module
from spurtle.config import load_user_config, save_user_config
from spurtle.operations.project import add_project, list_projects, remove_project
from spurtle.operations.schema import ProjectInfo
from spurtle.resolution import import_legacy_projects
from spurtle.schema import UserConfig


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the user config at a temporary directory."""
    config_root = tmp_path / 'config'
    monkeypatch.setattr(config_module, 'config_dir', lambda: config_root)
    monkeypatch.setattr(config_module, '_legacy_config_dir', lambda: config_root)
    return config_root


def _api(*, has_manifest: bool = False) -> MagicMock:
    """Create an API mock whose manifest check returns *has_manifest*."""
    api = create_autospec(API, instance=True)
    api.sync = MagicMock()
    api.sync.has_manifest.return_value = has_manifest
    return api


class TestListProjects:
    """Tests for list_projects()."""

    @staticmethod
    def test_empty() -> None:
        """No registered directories returns empty list."""
        assert list_projects(_api()) == []

    @staticmethod
    def test_returns_project_info(tmp_path: Path) -> None:
        """Each registered directory maps to a ProjectInfo."""
        existing = tmp_path / 'foo'
        existing.mkdir()
        missing = tmp_path / 'bar'
        save_user_config(UserConfig(projects=[str(existing), str(missing)]))

        result = list_projects(_api(has_manifest=True))

        assert result == [
            ProjectInfo(path=str(existing), name='foo', exists=True, has_manifest=True),
            ProjectInfo(path=str(missing), name='bar', exists=False, has_manifest=False),
        ]


class TestAddProject:
    """Tests for add_project()."""

    @staticmethod
    def test_not_a_directory(tmp_path: Path) -> None:
        """Raises NotADirectoryError for a missing directory."""
        with pytest.raises(NotADirectoryError, match='Not a directory'):
            add_project(_api(), tmp_path / 'nonexistent')

    @staticmethod
    def test_adds_and_returns_info(tmp_path: Path) -> None:
        """Adds the directory to the registry and returns ProjectInfo."""
        info = add_project(_api(), tmp_path)

        assert info.path == str(tmp_path.resolve())
        assert load_user_config().projects == [str(tmp_path.resolve())]

    @staticmethod
    def test_duplicate_raises(tmp_path: Path) -> None:
        """Adding the same directory twice raises ValueError."""
        add_project(_api(), tmp_path)
        with pytest.raises(ValueError, match='already registered'):
            add_project(_api(), tmp_path)


class TestRemoveProject:
    """Tests for remove_project()."""

    @staticmethod
    def test_removes_from_registry(tmp_path: Path) -> None:
        """remove_project drops the directory from the registry."""
        add_project(_api(), tmp_path)
        remove_project(_api(), tmp_path.resolve())
        assert load_user_config().projects == []


class TestImportLegacyProjects:
    """Tests for the one-time import from Porringer's old directory cache."""

    @staticmethod
    def test_imports_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Paths are imported when projects is unset, then never again."""
        legacy = tmp_path / 'directories.json'
        legacy.write_text(json.dumps({'version': '1', 'directories': [{'path': 'C:/a'}, {'path': 'C:/a'}]}))
        monkeypatch.setattr('spurtle.resolution._legacy_projects_file', lambda: legacy)

        import_legacy_projects()
        assert load_user_config().projects == ['C:/a']

        legacy.write_text(json.dumps({'directories': [{'path': 'C:/b'}]}))
        import_legacy_projects()
        assert load_user_config().projects == ['C:/a']

    @staticmethod
    def test_missing_or_corrupt_file_marks_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing or corrupt legacy file results in an empty, saved registry."""
        legacy = tmp_path / 'directories.json'
        legacy.write_text('{not json')
        monkeypatch.setattr('spurtle.resolution._legacy_projects_file', lambda: legacy)

        import_legacy_projects()

        assert load_user_config().projects == []
