"""Tests for validate-deployment command."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path  # noqa: TC003
from unittest.mock import MagicMock, patch

import yaml
from click.testing import CliRunner

from fraisier.cli.main import main


def _make_config(tmp_path: Path, config_dict: dict) -> str:
    """Write config dict to tmp file and return path."""
    config_file = tmp_path / "fraises.yaml"
    config_file.write_text(yaml.dump(config_dict))
    return str(config_file)


def _minimal_config() -> dict:
    """Return minimal valid fraises config."""
    return {
        "fraises": {
            "my_api": {
                "type": "api",
                "environments": {
                    "production": {
                        "app_path": "/var/www/app",
                    }
                },
            }
        }
    }


def _with_wrappers(tmp_path: Path) -> dict:
    """Create wrapper files and return env dict pointing to them."""
    systemctl_wrapper = tmp_path / "systemctl-wrapper"
    pg_wrapper = tmp_path / "pg-wrapper"
    systemctl_wrapper.touch(mode=0o755)
    pg_wrapper.touch(mode=0o755)
    return {
        "FRAISIER_SYSTEMCTL_WRAPPER": str(systemctl_wrapper),
        "FRAISIER_PG_WRAPPER": str(pg_wrapper),
    }


class TestConfigCheck:
    """Test config_accessible check."""

    def test_missing_fraise_exits_with_error(self, tmp_path):
        """Missing fraise arg shows usage error."""
        config_file = _make_config(tmp_path, _minimal_config())
        runner = CliRunner()
        result = runner.invoke(main, ["-c", config_file, "validate-deployment"])
        assert result.exit_code != 0

    def test_missing_environment_exits_with_error(self, tmp_path):
        """Missing environment arg shows usage error."""
        config_file = _make_config(tmp_path, _minimal_config())
        runner = CliRunner()
        result = runner.invoke(
            main, ["-c", config_file, "validate-deployment", "my_api"]
        )
        assert result.exit_code != 0

    def test_unknown_fraise_exits_error(self, tmp_path):
        """Unknown fraise shows error."""
        config_file = _make_config(tmp_path, _minimal_config())
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["-c", config_file, "validate-deployment", "unknown_fraise", "production"],
        )
        assert result.exit_code == 1
        output_lower = result.output.lower()
        assert "not found" in output_lower or "unknown" in output_lower

    def test_unknown_environment_exits_error(self, tmp_path):
        """Unknown environment shows error."""
        config_file = _make_config(tmp_path, _minimal_config())
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["-c", config_file, "validate-deployment", "my_api", "unknown_env"],
        )
        assert result.exit_code == 1


class TestGitRepoCheck:
    """Test git_repo_accessible check."""

    def test_clone_url_reachable(self, tmp_path):
        """git ls-remote succeeds on valid clone_url."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config["fraises"]["my_api"]["environments"]["production"]["clone_url"] = (
            "https://github.com/example/repo.git"
        )
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        wrapper_env = _with_wrappers(tmp_path)
        with patch("subprocess.run") as mock_run, patch.dict(os.environ, wrapper_env):
            mock_run.return_value = MagicMock(returncode=0)
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 0

    def test_clone_url_unreachable(self, tmp_path):
        """git ls-remote fails on invalid clone_url."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config["fraises"]["my_api"]["environments"]["production"]["clone_url"] = (
            "https://github.com/nonexistent/repo.git"
        )
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        wrapper_env = _with_wrappers(tmp_path)
        with patch("subprocess.run") as mock_run, patch.dict(os.environ, wrapper_env):
            mock_run.side_effect = subprocess.CalledProcessError(128, "git")
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 1

    def test_bare_git_repo_exists(self, tmp_path):
        """Bare git repo path check succeeds if directory exists."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        git_repo = tmp_path / "my_api.git"
        git_repo.mkdir()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config["fraises"]["my_api"]["environments"]["production"]["git_repo"] = str(
            git_repo
        )
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        with patch.dict(os.environ, _with_wrappers(tmp_path)):
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 0

    def test_bare_git_repo_missing(self, tmp_path):
        """Bare git repo missing fails."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config["fraises"]["my_api"]["environments"]["production"]["git_repo"] = str(
            tmp_path / "nonexistent.git"
        )
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        with patch.dict(os.environ, _with_wrappers(tmp_path)):
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 1


class TestAppPathCheck:
    """Test app_path_writable check."""

    def test_app_path_exists_writable(self, tmp_path):
        """app_path exists and writable passes."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        with patch.dict(os.environ, _with_wrappers(tmp_path)):
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 0

    def test_app_path_not_exists(self, tmp_path):
        """app_path missing fails."""
        config = _minimal_config()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            tmp_path / "nonexistent"
        )
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        with patch.dict(os.environ, _with_wrappers(tmp_path)):
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 1
            assert "app_path" in result.output.lower()


class TestDatabaseConfigCheck:
    """Test database_config_complete check."""

    def test_no_database_skipped(self, tmp_path):
        """Config without database section is skipped."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        with patch.dict(os.environ, _with_wrappers(tmp_path)):
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 0
            assert "skipped" in result.output.lower()

    def test_incomplete_database_config_fails(self, tmp_path):
        """Database config missing required fields fails."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config["fraises"]["my_api"]["environments"]["production"]["database"] = {
            "host": "localhost",
        }
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        with patch.dict(os.environ, _with_wrappers(tmp_path)):
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 1

    def test_complete_database_config_passes(self, tmp_path):
        """Complete database config passes."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config["fraises"]["my_api"]["environments"]["production"]["database"] = {
            "host": "localhost",
            "dbname": "my_db",
            "user": "my_user",
            "strategy": "rebuild",
        }
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        with patch.dict(os.environ, _with_wrappers(tmp_path)):
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 0


class TestSystemdCheck:
    """Test systemd_service_exists check."""

    def test_systemd_service_active(self, tmp_path):
        """systemctl is-active returns 'active'."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config["fraises"]["my_api"]["environments"]["production"]["systemd_service"] = (
            "my-api"
        )
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        wrapper_env = _with_wrappers(tmp_path)
        with patch("subprocess.run") as mock_run, patch.dict(os.environ, wrapper_env):
            mock_run.return_value = MagicMock(returncode=0, stdout=b"active")
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 0

    def test_systemd_service_inactive(self, tmp_path):
        """systemctl is-active returns 'inactive' fails."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config["fraises"]["my_api"]["environments"]["production"]["systemd_service"] = (
            "my-api"
        )
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        wrapper_env = _with_wrappers(tmp_path)
        with patch("subprocess.run") as mock_run, patch.dict(os.environ, wrapper_env):
            mock_run.return_value = MagicMock(returncode=0, stdout=b"inactive")
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 1


class TestWrapperScriptsCheck:
    """Test wrapper_scripts_valid check."""

    def test_wrapper_scripts_present(self, tmp_path):
        """Both wrappers present and executable."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        # Create actual wrapper scripts
        systemctl_wrapper = tmp_path / "systemctl-wrapper"
        pg_wrapper = tmp_path / "pg-wrapper"
        systemctl_wrapper.touch(mode=0o755)
        pg_wrapper.touch(mode=0o755)
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        with patch.dict(
            os.environ,
            {
                "FRAISIER_SYSTEMCTL_WRAPPER": str(systemctl_wrapper),
                "FRAISIER_PG_WRAPPER": str(pg_wrapper),
            },
        ):
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 0

    def test_systemctl_wrapper_missing(self, tmp_path):
        """Missing FRAISIER_SYSTEMCTL_WRAPPER env var fails."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        with patch.dict(os.environ, {}, clear=True):
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 1
            assert "wrapper" in result.output.lower()


class TestSudoersCheck:
    """Test sudoers_installed check."""

    def test_sudoers_missing_warning(self, tmp_path):
        """sudoers missing is a warning (doesn't fail)."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        systemctl_wrapper = tmp_path / "systemctl-wrapper"
        pg_wrapper = tmp_path / "pg-wrapper"
        systemctl_wrapper.touch(mode=0o755)
        pg_wrapper.touch(mode=0o755)
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        # Don't create sudoers file, mock Path.exists to return False for it
        def mock_exists_impl(path_self):
            path_str = str(path_self)
            if "sudoers" in path_str:
                return False
            return path_self.exists.__wrapped__(path_self)

        with patch.dict(
            os.environ,
            {
                "FRAISIER_SYSTEMCTL_WRAPPER": str(systemctl_wrapper),
                "FRAISIER_PG_WRAPPER": str(pg_wrapper),
            },
        ):
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            # Warning doesn't cause exit 1
            assert result.exit_code == 0
            assert "sudoers" in result.output.lower()


class TestHealthEndpointCheck:
    """Test health_check_reachable check."""

    def test_health_check_passes(self, tmp_path):
        """Health endpoint responds with 200."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config["fraises"]["my_api"]["environments"]["production"]["health_check"] = {
            "url": "http://localhost:8000/health",
        }
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        with (
            patch("urllib.request.urlopen") as mock_urlopen,
            patch.dict(os.environ, _with_wrappers(tmp_path)),
        ):
            mock_response = MagicMock()
            mock_response.status = 200
            mock_urlopen.return_value = mock_response
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 0

    def test_health_check_fails(self, tmp_path):
        """Health endpoint unreachable fails."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config["fraises"]["my_api"]["environments"]["production"]["health_check"] = {
            "url": "http://localhost:8000/health",
        }
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        with (
            patch("urllib.request.urlopen") as mock_urlopen,
            patch.dict(os.environ, _with_wrappers(tmp_path)),
        ):
            import urllib.error

            mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 1


class TestInstallCommandCheck:
    """Test install_command_available check."""

    def test_install_command_found(self, tmp_path):
        """Install command binary found via which."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config["fraises"]["my_api"]["environments"]["production"]["install"] = {
            "command": "npm install",
        }
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        with (
            patch("shutil.which", return_value="/usr/bin/npm"),
            patch.dict(os.environ, _with_wrappers(tmp_path)),
        ):
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 0

    def test_install_command_not_found(self, tmp_path):
        """Install command binary not found fails."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config["fraises"]["my_api"]["environments"]["production"]["install"] = {
            "command": "nonexistent-tool install",
        }
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        with (
            patch("shutil.which", return_value=None),
            patch.dict(os.environ, _with_wrappers(tmp_path)),
        ):
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 1


class TestJSONOutput:
    """Test JSON output format."""

    def test_json_output_structure(self, tmp_path):
        """JSON output has top-level passed and checks keys."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        with patch.dict(os.environ, _with_wrappers(tmp_path)):
            result = runner.invoke(
                main,
                [
                    "-c",
                    config_file,
                    "validate-deployment",
                    "my_api",
                    "production",
                    "--json",
                ],
            )
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "passed" in data
            assert "checks" in data
            assert isinstance(data["checks"], list)

    def test_json_check_structure(self, tmp_path):
        """Each check in JSON has name and passed fields."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        with patch.dict(os.environ, _with_wrappers(tmp_path)):
            result = runner.invoke(
                main,
                [
                    "-c",
                    config_file,
                    "validate-deployment",
                    "my_api",
                    "production",
                    "--json",
                ],
            )
            data = json.loads(result.output)
            for check in data["checks"]:
                assert "name" in check
                assert "passed" in check


class TestExitCode:
    """Test exit code behavior."""

    def test_all_pass_exits_zero(self, tmp_path):
        """All checks passing exits with 0."""
        config = _minimal_config()
        app_path = tmp_path / "app"
        app_path.mkdir()
        systemctl_wrapper = tmp_path / "systemctl-wrapper"
        pg_wrapper = tmp_path / "pg-wrapper"
        systemctl_wrapper.touch(mode=0o755)
        pg_wrapper.touch(mode=0o755)
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            app_path
        )
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        with patch.dict(
            os.environ,
            {
                "FRAISIER_SYSTEMCTL_WRAPPER": str(systemctl_wrapper),
                "FRAISIER_PG_WRAPPER": str(pg_wrapper),
            },
        ):
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 0

    def test_error_check_fails_exits_one(self, tmp_path):
        """Any error-severity check failing exits with 1."""
        config = _minimal_config()
        config["fraises"]["my_api"]["environments"]["production"]["app_path"] = str(
            tmp_path / "nonexistent"
        )
        config_file = _make_config(tmp_path, config)
        runner = CliRunner()

        # Create wrapper files to avoid wrapper check failure
        systemctl_wrapper = tmp_path / "systemctl-wrapper"
        pg_wrapper = tmp_path / "pg-wrapper"
        systemctl_wrapper.touch(mode=0o755)
        pg_wrapper.touch(mode=0o755)

        with patch.dict(
            os.environ,
            {
                "FRAISIER_SYSTEMCTL_WRAPPER": str(systemctl_wrapper),
                "FRAISIER_PG_WRAPPER": str(pg_wrapper),
            },
        ):
            result = runner.invoke(
                main, ["-c", config_file, "validate-deployment", "my_api", "production"]
            )
            assert result.exit_code == 1
