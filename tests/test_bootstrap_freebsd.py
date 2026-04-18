"""Tests for FreeBSD bootstrap script."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.bootstrap_freebsd import FreebsdBootstrapper


@pytest.fixture
def mock_runner():
    return MagicMock()


@pytest.fixture
def mock_config():
    """Mock FraisierConfig."""
    config = MagicMock()
    config.scaffold.deploy_user = "fraisier"
    config.project_name = "test_project"
    return config


def bootstrapper(mock_config, mock_runner, tmp_path):
    """Create a FreebsdBootstrapper with mocked runner."""
    return FreebsdBootstrapper(
        mock_config, "production", mock_runner, tmp_path / "fraises.yaml"
    )


class TestFreebsdBootstrapper:
    """Test FreeBSD bootstrapper."""

    def test_detects_freebsd(self, mock_config, mock_runner, tmp_path):
        """Should detect FreeBSD system."""
        bs = FreebsdBootstrapper(
            mock_config, "production", mock_runner, tmp_path / "fraises.yaml"
        )
        assert bs.os_name == "FreeBSD"

    def test_installs_python(self, mock_config, mock_runner, tmp_path):
        """Installs Python 3.11+ on FreeBSD."""
        bs = bootstrapper(mock_config, mock_runner, tmp_path)

        step = bs._install_python()

        assert step.name == "Install Python"
        mock_runner.run.assert_called_with(
            ["sudo", "pkg", "install", "-y", "python311"]
        )

    def test_installs_postgresql_client(self, mock_config, mock_runner, tmp_path):
        """Installs PostgreSQL client on FreeBSD."""
        bs = bootstrapper(mock_config, mock_runner, tmp_path)

        step = bs._install_postgres_client()

        assert step.name == "Install PostgreSQL client"
        mock_runner.run.assert_called_with(
            ["sudo", "pkg", "install", "-y", "postgresql15-client"]
        )

    def test_installs_git(self, mock_config, mock_runner, tmp_path):
        """Installs Git on FreeBSD."""
        bs = bootstrapper(mock_config, mock_runner, tmp_path)

        step = bs._install_git()

        assert step.name == "Install Git"
        mock_runner.run.assert_called_with(["sudo", "pkg", "install", "-y", "git"])

    def test_creates_deploy_user(self, mock_config, mock_runner, tmp_path):
        """Creates deploy user on FreeBSD."""
        bs = bootstrapper(mock_config, mock_runner, tmp_path)

        step = bs._create_deploy_user()

        assert step.name == "Create deploy user"
        mock_runner.run.assert_called_with(
            ["sudo", "pw", "useradd", "fraisier", "-m", "-s", "/bin/sh"]
        )

    def test_sets_up_rc_service_infrastructure(
        self, mock_config, mock_runner, tmp_path
    ):
        """Sets up rc.d service infrastructure."""
        bs = bootstrapper(mock_config, mock_runner, tmp_path)

        step = bs._setup_rc_infrastructure()

        assert step.name == "Setup rc.d infrastructure"
        # Would check for enabling rc scripts or something
        mock_runner.run.assert_called_with(["sudo", "sysrc", "fraisier_enable=YES"])
