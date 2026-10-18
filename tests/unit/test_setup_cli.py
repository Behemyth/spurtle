"""Tests for the CLI setup subcommands."""

from __future__ import annotations

from typing import TypedDict, Unpack
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 requires system Qt libraries")

from spurtle.cli import app
from spurtle.schema import ResolvedConfig
from typer.testing import CliRunner

runner = CliRunner()


class _ResolvedConfigOverrides(TypedDict, total=False):
    update_source: str | None
    update_channel: str
    auto_update_interval_minutes: int
    tool_update_interval_minutes: int
    plugin_auto_update: dict[str, bool | dict[str, bool]] | None
    prerelease_packages: dict[str, list[str]] | None
    auto_apply: bool
    auto_start: bool
    debug_logging: bool
    last_client_update: str | None
    last_tool_updates: dict[str, str] | None
    setup_profiles: list[str]


def _make_config(**overrides: Unpack[_ResolvedConfigOverrides]) -> ResolvedConfig:
    from spurtle.schema import (
        DEFAULT_AUTO_UPDATE_INTERVAL_MINUTES,
        DEFAULT_TOOL_UPDATE_INTERVAL_MINUTES,
    )

    defaults: _ResolvedConfigOverrides = {
        "update_source": None,
        "update_channel": "stable",
        "auto_update_interval_minutes": DEFAULT_AUTO_UPDATE_INTERVAL_MINUTES,
        "tool_update_interval_minutes": DEFAULT_TOOL_UPDATE_INTERVAL_MINUTES,
        "plugin_auto_update": None,
        "prerelease_packages": None,
        "auto_apply": True,
        "auto_start": True,
        "debug_logging": False,
        "last_client_update": None,
        "last_tool_updates": None,
        "setup_profiles": [],
    }
    defaults.update(overrides)
    return ResolvedConfig(
        update_source=defaults["update_source"],
        update_channel=defaults["update_channel"],
        auto_update_interval_minutes=defaults["auto_update_interval_minutes"],
        tool_update_interval_minutes=defaults["tool_update_interval_minutes"],
        plugin_auto_update=defaults["plugin_auto_update"],
        entry_auto_update=None,
        prerelease_packages=defaults["prerelease_packages"],
        auto_apply=defaults["auto_apply"],
        auto_start=defaults["auto_start"],
        debug_logging=defaults["debug_logging"],
        last_client_update=defaults["last_client_update"],
        last_tool_updates=defaults["last_tool_updates"],
        setup_profiles=defaults["setup_profiles"],
    )


# ---------------------------------------------------------------------------
# setup list
# ---------------------------------------------------------------------------


class TestSetupList:
    """Tests for ``sprt setup list``."""

    @staticmethod
    def test_empty() -> None:
        """No profiles prints a message."""
        config = _make_config(setup_profiles=[])
        with patch(
            "spurtle.cli.context.get_services", return_value=(None, MagicMock(), config)
        ):
            result = runner.invoke(app, ["setup", "list"])
            assert result.exit_code == 0
            assert "No setup profiles" in result.output

    @staticmethod
    def test_lists_urls() -> None:
        """Configured profiles are printed one per line."""
        config = _make_config(
            setup_profiles=["https://a.com/p.json", "https://b.com/p.json"]
        )
        with patch(
            "spurtle.cli.context.get_services", return_value=(None, MagicMock(), config)
        ):
            result = runner.invoke(app, ["setup", "list"])
            assert result.exit_code == 0
            assert "https://a.com/p.json" in result.output
            assert "https://b.com/p.json" in result.output


# ---------------------------------------------------------------------------
# setup add
# ---------------------------------------------------------------------------


class TestSetupAdd:
    """Tests for ``sprt setup add``."""

    @staticmethod
    def test_rejects_http() -> None:
        """Non-HTTPS URLs are rejected."""
        result = runner.invoke(app, ["setup", "add", "http://bad.com/p.json"])
        assert result.exit_code == 1

    @staticmethod
    def test_adds_url() -> None:
        """A valid HTTPS URL is appended to the config."""
        mock_user = MagicMock()
        mock_user.setup_profiles = []
        with (
            patch("spurtle.config.load_user_config", return_value=mock_user),
            patch("spurtle.resolution.update_user_config") as mock_update,
        ):
            result = runner.invoke(app, ["setup", "add", "https://example.com/p.json"])
            assert result.exit_code == 0
            assert "Added" in result.output
            mock_update.assert_called_once_with(
                setup_profiles=["https://example.com/p.json"]
            )

    @staticmethod
    def test_duplicate_url() -> None:
        """Adding a duplicate URL prints a message and does not update."""
        mock_user = MagicMock()
        mock_user.setup_profiles = ["https://example.com/p.json"]
        with (
            patch("spurtle.config.load_user_config", return_value=mock_user),
            patch("spurtle.resolution.update_user_config") as mock_update,
        ):
            result = runner.invoke(app, ["setup", "add", "https://example.com/p.json"])
            assert result.exit_code == 0
            assert "already configured" in result.output
            mock_update.assert_not_called()


# ---------------------------------------------------------------------------
# setup remove
# ---------------------------------------------------------------------------


class TestSetupRemove:
    """Tests for ``sprt setup remove``."""

    @staticmethod
    def test_removes_url() -> None:
        """An existing URL is removed from the config."""
        mock_user = MagicMock()
        mock_user.setup_profiles = ["https://example.com/p.json"]
        with (
            patch("spurtle.config.load_user_config", return_value=mock_user),
            patch("spurtle.resolution.update_user_config") as mock_update,
        ):
            result = runner.invoke(
                app, ["setup", "remove", "https://example.com/p.json"]
            )
            assert result.exit_code == 0
            assert "Removed" in result.output
            mock_update.assert_called_once_with(setup_profiles=[])

    @staticmethod
    def test_remove_not_found() -> None:
        """Removing a URL that isn't in the config exits with code 1."""
        mock_user = MagicMock()
        mock_user.setup_profiles = []
        with patch("spurtle.config.load_user_config", return_value=mock_user):
            result = runner.invoke(
                app, ["setup", "remove", "https://example.com/missing.json"]
            )
            assert result.exit_code == 1
            assert "not found" in result.output


# ---------------------------------------------------------------------------
# setup run
# ---------------------------------------------------------------------------


class TestSetupRun:
    """Tests for ``sprt setup run``."""

    @staticmethod
    def test_rejects_http() -> None:
        """Non-HTTPS profile URLs are rejected."""
        result = runner.invoke(app, ["setup", "run", "http://bad.com/p.json"])
        assert result.exit_code == 1
