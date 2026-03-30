"""Tests for TemplateManager lifecycle orchestration."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from fraisier.testing._manager import TemplateInfo, TemplateManager
from fraisier.testing._metadata import TemplateMeta


def _make_manager(**kwargs):
    defaults = {
        "env": "test",
        "project_dir": Path("/tmp/project"),
        "confiture_config": Path("/tmp/project/confiture.yaml"),
        "connection_url": "postgresql://localhost/testdb",
    }
    defaults.update(kwargs)
    return TemplateManager(**defaults)


class TestEnsureTemplate:
    """Test the hash-check -> clone-or-rebuild state machine."""

    @patch("fraisier.testing._manager.check_db_exists")
    @patch("fraisier.testing._manager.read_meta")
    def test_hash_match_skips_rebuild(self, mock_read_meta, mock_db_exists):
        mock_db_exists.return_value = True
        mock_read_meta.return_value = TemplateMeta(
            schema_hash="abc123",
            built_at=datetime.now(tz=UTC),
            confiture_version="0.8.17",
            build_duration_ms=100,
        )

        manager = _make_manager()
        with patch.object(manager, "_compute_hash", return_value="abc123"):
            info = manager.ensure_template()

        assert info.from_cache is True
        assert info.schema_hash == "abc123"

    @patch("fraisier.testing._manager.check_db_exists")
    @patch("fraisier.testing._manager.read_meta")
    def test_hash_mismatch_triggers_rebuild(self, mock_read_meta, mock_db_exists):
        mock_db_exists.return_value = True
        mock_read_meta.return_value = TemplateMeta(
            schema_hash="old_hash",
            built_at=datetime.now(tz=UTC),
            confiture_version="0.8.17",
            build_duration_ms=100,
        )

        manager = _make_manager()
        with (
            patch.object(manager, "_compute_hash", return_value="new_hash"),
            patch.object(manager, "build_template") as mock_build,
        ):
            mock_build.return_value = TemplateInfo(
                template_name="tpl_test_test",
                schema_hash="new_hash",
                from_cache=False,
            )
            info = manager.ensure_template()

        assert info.from_cache is False
        mock_build.assert_called_once()

    @patch("fraisier.testing._manager.check_db_exists")
    def test_no_template_triggers_rebuild(self, mock_db_exists):
        mock_db_exists.return_value = False

        manager = _make_manager()
        with (
            patch.object(manager, "_compute_hash", return_value="abc123"),
            patch.object(manager, "build_template") as mock_build,
        ):
            mock_build.return_value = TemplateInfo(
                template_name="tpl_test_test",
                schema_hash="abc123",
                from_cache=False,
            )
            info = manager.ensure_template()

        assert info.from_cache is False
        mock_build.assert_called_once()


class TestClone:
    @patch("fraisier.testing._manager.terminate_backends")
    @patch("fraisier.testing._manager.create_db")
    def test_clone_creates_db_from_template(self, mock_create, mock_terminate):
        mock_create.return_value = (0, "", "")
        mock_terminate.return_value = (0, "", "")

        manager = _make_manager(
            connection_url="postgresql://user:pass@localhost:5432/testdb"
        )
        url = manager.clone("test_session_001")

        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args
        assert call_kwargs[1]["template"] == "tpl_test_test"
        assert "test_session_001" in url

    @patch("fraisier.testing._manager.terminate_backends")
    @patch("fraisier.testing._manager.create_db")
    def test_clone_raises_on_failure(self, mock_create, mock_terminate):
        mock_create.return_value = (1, "", "ERROR: template not found")
        mock_terminate.return_value = (0, "", "")

        manager = _make_manager()
        with pytest.raises(RuntimeError, match="Failed to clone"):
            manager.clone("test_session_001")


class TestCleanup:
    @patch("fraisier.testing._manager.cleanup_templates")
    def test_cleanup_delegates_to_dbops(self, mock_cleanup):
        mock_cleanup.return_value = 2

        manager = _make_manager()
        dropped = manager.cleanup()

        assert dropped == 2
        mock_cleanup.assert_called_once()


class TestStatus:
    @patch("fraisier.testing._manager.check_db_exists")
    @patch("fraisier.testing._manager.read_meta")
    def test_status_shows_needs_rebuild(self, mock_read_meta, mock_db_exists):
        mock_db_exists.return_value = True
        mock_read_meta.return_value = TemplateMeta(
            schema_hash="old_hash",
            built_at=datetime.now(tz=UTC),
            confiture_version="0.8.17",
            build_duration_ms=100,
        )

        manager = _make_manager()
        with patch.object(manager, "_compute_hash", return_value="new_hash"):
            status = manager.status()

        assert status.needs_rebuild is True
        assert status.template_exists is True
        assert status.current_hash == "new_hash"
        assert status.stored_hash == "old_hash"

    @patch("fraisier.testing._manager.check_db_exists")
    def test_status_no_template(self, mock_db_exists):
        mock_db_exists.return_value = False

        manager = _make_manager()
        with patch.object(manager, "_compute_hash", return_value="abc123"):
            status = manager.status()

        assert status.needs_rebuild is True
        assert status.template_exists is False
        assert status.stored_hash is None
