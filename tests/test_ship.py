"""Tests for fraisier ship command."""

import json
from unittest.mock import MagicMock, patch

import yaml
from click.testing import CliRunner

from fraisier.cli import main


def _setup_project(tmp_path, version="1.0.0", webhook_secret=None):
    """Create version.json, pyproject.toml, and fraises.yaml."""
    vj = tmp_path / "version.json"
    vj.write_text(
        json.dumps(
            {
                "version": version,
                "commit": "abc123",
                "branch": "main",
                "timestamp": "2026-03-23T12:00:00Z",
                "schema_hash": "",
                "environment": "",
                "database_version": "",
            },
            indent=2,
        )
    )

    pp = tmp_path / "pyproject.toml"
    pp.write_text(f'[project]\nname = "myapp"\nversion = "{version}"\n')

    config = {
        "fraises": {
            "my_api": {
                "type": "api",
                "environments": {
                    "production": {
                        "name": "my-api",
                        "app_path": "/var/www/my-api",
                    },
                },
            },
        },
    }
    if webhook_secret:
        config["git"] = {
            "provider": "github",
            "github": {"webhook_secret": webhook_secret},
        }

    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(yaml.dump(config))
    return str(cfg)


class TestShipCommand:
    """Test fraisier ship bumps, commits, and pushes."""

    @patch("subprocess.run")
    def test_ship_patch_bumps_version(self, mock_run, tmp_path):
        """Test ship patch bumps version from 1.0.0 to 1.0.1."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code == 0
        assert "1.0.1" in result.output

    @patch("subprocess.run")
    def test_ship_creates_git_commit(self, mock_run, tmp_path):
        """Test ship creates a git commit with version message."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )

        # Should have called git add and git commit
        calls = mock_run.call_args_list
        commands = [c[0][0] for c in calls]
        assert any("git" in str(cmd) and "add" in str(cmd) for cmd in commands)
        assert any("git" in str(cmd) and "commit" in str(cmd) for cmd in commands)

    @patch("subprocess.run")
    def test_ship_pushes_to_remote(self, mock_run, tmp_path):
        """Test ship pushes to git remote."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )

        calls = mock_run.call_args_list
        commands = [c[0][0] for c in calls]
        assert any("git" in str(cmd) and "push" in str(cmd) for cmd in commands)


class TestShipDryRun:
    """Test fraisier ship --dry-run."""

    def test_dry_run_shows_plan(self, tmp_path):
        """Test --dry-run shows what would happen without executing."""
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--dry-run",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code == 0
        assert "1.0.1" in result.output
        assert "dry" in result.output.lower()

    def test_dry_run_does_not_modify_files(self, tmp_path):
        """Test --dry-run leaves version files unchanged."""
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--dry-run",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )

        # version.json should still be 1.0.0
        data = json.loads((tmp_path / "version.json").read_text())
        assert data["version"] == "1.0.0"

    @patch("subprocess.run")
    def test_dry_run_does_not_call_git(self, mock_run, tmp_path):
        """Test --dry-run does not invoke any git commands."""
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--dry-run",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        mock_run.assert_not_called()


class TestShipBumpTypes:
    """Test all bump types work."""

    @patch("subprocess.run")
    def test_ship_minor(self, mock_run, tmp_path):
        """Test ship minor bumps 1.0.0 -> 1.1.0."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "minor",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert "1.1.0" in result.output

    @patch("subprocess.run")
    def test_ship_major(self, mock_run, tmp_path):
        """Test ship major bumps 1.0.0 -> 2.0.0."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "major",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert "2.0.0" in result.output
