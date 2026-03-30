"""Tests for the fraisier test-db CLI commands."""

from datetime import UTC, datetime
from unittest.mock import patch

from click.testing import CliRunner

from fraisier.cli.main import main
from fraisier.testing._manager import TemplateInfo, TemplateStatus
from fraisier.testing._timing import TimingReport


class TestTestDbStatus:
    def test_status_command_exists(self):
        runner = CliRunner()
        result = runner.invoke(main, ["test-db", "status", "--help"])
        assert result.exit_code == 0
        assert "Show template database status" in result.output

    @patch("fraisier.testing._manager.TemplateManager", autospec=True)
    def test_status_displays_info(self, mock_manager_cls, tmp_path):
        mock_manager = mock_manager_cls.return_value
        mock_manager.status.return_value = TemplateStatus(
            template_name="tpl_test_test",
            template_exists=True,
            current_hash="abcdef1234567890" * 4,
            stored_hash="abcdef1234567890" * 4,
            needs_rebuild=False,
            built_at=datetime(2026, 3, 30, tzinfo=UTC),
            build_duration_ms=1500,
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "test-db",
                "status",
                "--env",
                "test",
                "-d",
                str(tmp_path),
                "--connection-url",
                "postgresql://localhost/test",
            ],
        )
        assert result.exit_code == 0
        assert "tpl_test_test" in result.output
        assert "up to date" in result.output

    @patch("fraisier.testing._manager.TemplateManager", autospec=True)
    def test_status_shows_needs_rebuild(self, mock_manager_cls, tmp_path):
        mock_manager = mock_manager_cls.return_value
        mock_manager.status.return_value = TemplateStatus(
            template_name="tpl_test_test",
            template_exists=True,
            current_hash="new_hash_" + "0" * 56,
            stored_hash="old_hash_" + "0" * 56,
            needs_rebuild=True,
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["test-db", "status", "-d", str(tmp_path)],
        )
        assert result.exit_code == 0
        assert "needs rebuild" in result.output


class TestTestDbRebuild:
    def test_rebuild_command_exists(self):
        runner = CliRunner()
        result = runner.invoke(main, ["test-db", "rebuild", "--help"])
        assert result.exit_code == 0
        assert "Force rebuild" in result.output

    @patch("fraisier.testing._manager.TemplateManager", autospec=True)
    def test_rebuild_calls_build_template(self, mock_manager_cls, tmp_path):
        report = TimingReport()
        report.record("build", 3000)

        mock_manager = mock_manager_cls.return_value
        mock_manager.build_template.return_value = TemplateInfo(
            template_name="tpl_test_test",
            schema_hash="abc123" + "0" * 58,
            from_cache=False,
            timing=report,
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "test-db",
                "rebuild",
                "-d",
                str(tmp_path),
                "--connection-url",
                "postgresql://localhost/test",
            ],
        )
        assert result.exit_code == 0
        assert "rebuilt" in result.output
        mock_manager.build_template.assert_called_once()


class TestTestDbClean:
    def test_clean_command_exists(self):
        runner = CliRunner()
        result = runner.invoke(main, ["test-db", "clean", "--help"])
        assert result.exit_code == 0
        assert "Drop test database templates" in result.output

    @patch("fraisier.testing._manager.TemplateManager", autospec=True)
    def test_clean_drops_templates(self, mock_manager_cls, tmp_path):
        mock_manager = mock_manager_cls.return_value
        mock_manager.cleanup.return_value = 2

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["test-db", "clean", "-d", str(tmp_path)],
        )
        assert result.exit_code == 0
        assert "Dropped 2 template(s)" in result.output

    @patch("fraisier.testing._manager.TemplateManager", autospec=True)
    def test_clean_no_templates(self, mock_manager_cls, tmp_path):
        mock_manager = mock_manager_cls.return_value
        mock_manager.cleanup.return_value = 0

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["test-db", "clean", "-d", str(tmp_path)],
        )
        assert result.exit_code == 0
        assert "No templates to clean up" in result.output
