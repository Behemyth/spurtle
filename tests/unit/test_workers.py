"""Tests for the UpdateResult dataclass."""

from __future__ import annotations

from spurtle.operations.schema import UpdateResult


class TestUpdateResult:
    """Tests for the UpdateResult dataclass."""

    @staticmethod
    def test_defaults() -> None:
        """Verify all fields start at zero / empty."""
        result = UpdateResult()
        assert result.manifests_processed == 0
        assert result.updated == 0
        assert len(result.already_latest) == 0
        assert result.failed == 0
        assert result.updated_packages == set()

    @staticmethod
    def test_fields_are_assignable() -> None:
        """Verify fields can be set via constructor."""
        expected_manifests = 3
        expected_updated = 2
        expected_packages = {"pdm", "ruff"}
        result = UpdateResult(
            manifests_processed=expected_manifests,
            packages_updated=["a", "b"],
            already_latest=["c"],
            packages_failed=[],
            updated_packages=expected_packages,
        )
        assert result.manifests_processed == expected_manifests
        assert result.updated == expected_updated
        assert len(result.already_latest) == 1
        assert result.failed == 0
        assert result.updated_packages == expected_packages

    @staticmethod
    def test_updated_packages_mutation() -> None:
        """Verify updated_packages is a mutable set."""
        result = UpdateResult()
        result.updated_packages.add("uv")
        assert "uv" in result.updated_packages

    @staticmethod
    def test_independent_set_per_instance() -> None:
        """Verify each instance gets its own set (field default_factory)."""
        a = UpdateResult()
        b = UpdateResult()
        a.updated_packages.add("foo")
        assert "foo" not in b.updated_packages
