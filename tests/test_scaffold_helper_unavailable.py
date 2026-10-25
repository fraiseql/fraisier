"""A deploy that cannot reach the scaffold-install helper fails at once (#486).

Before #433 the deploy fell back to ``scaffold-install --output-dir`` in a
subprocess. #433 made scaffold-install refuse ``--output-dir`` and refuse a
non-root caller, so that fallback could only ever fail, and with the
subprocess's refusal instead of the real cause. The only fix is the
operator's one-time step, and the deploy says so.
"""

import logging
from unittest.mock import MagicMock

import pytest

from fraisier.config_watcher import ConfigWatcher
from fraisier.deployers import base as base_mod
from fraisier.deployers.api import APIDeployer
from fraisier.errors import DeploymentError
from fraisier.scaffold_install_helper import OPERATOR_STEP


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "fraises.yaml"
    path.write_text("name: acme\nfraises: {}\n")
    return path


@pytest.fixture
def runner():
    return MagicMock()


@pytest.fixture
def deployer(runner):
    deployer = APIDeployer({"fraise_name": "api"})
    deployer.runner = runner
    return deployer


def _point_socket_at(monkeypatch, path):
    monkeypatch.setattr(base_mod, "_get_scaffold_socket_path", lambda _p: str(path))


class TestAnAbsentSocket:
    def test_fails_with_the_operator_step(
        self, deployer, config_path, tmp_path, monkeypatch
    ):
        _point_socket_at(monkeypatch, tmp_path / "missing.sock")

        with pytest.raises(DeploymentError) as exc:
            deployer._install_scaffold(config_path=config_path)

        assert OPERATOR_STEP in str(exc.value)
        assert str(tmp_path / "missing.sock") in str(exc.value)

    def test_runs_no_subprocess(
        self, deployer, runner, config_path, tmp_path, monkeypatch
    ):
        _point_socket_at(monkeypatch, tmp_path / "missing.sock")

        with pytest.raises(DeploymentError):
            deployer._install_scaffold(config_path=config_path)

        runner.run.assert_not_called()

    def test_says_the_socket_is_not_installed(
        self, deployer, config_path, tmp_path, monkeypatch
    ):
        _point_socket_at(monkeypatch, tmp_path / "missing.sock")

        with pytest.raises(DeploymentError) as exc:
            deployer._install_scaffold(config_path=config_path)

        assert "does not exist" in str(exc.value)

    def test_the_next_deploy_asks_again(
        self, deployer, config_path, tmp_path, monkeypatch
    ):
        """The hash is saved before the install, so it must be dropped here."""
        _point_socket_at(monkeypatch, tmp_path / "missing.sock")
        watcher = ConfigWatcher(config_path.parent)
        watcher.save_hash()

        with pytest.raises(DeploymentError):
            deployer._install_scaffold(config_path=config_path)

        assert watcher.get_previous_hash() is None


class TestAPresentButDeadSocket:
    def test_names_the_connection_error_and_the_operator_step(
        self, deployer, config_path, tmp_path, monkeypatch
    ):
        dead = tmp_path / "scaffold-install.sock"
        dead.write_text("")  # a plain file, not a listening socket
        _point_socket_at(monkeypatch, dead)

        with pytest.raises(DeploymentError) as exc:
            deployer._install_scaffold(config_path=config_path)

        assert "is present but did not answer" in str(exc.value)
        assert OPERATOR_STEP in str(exc.value)

    def test_does_not_log_a_fallback(
        self, deployer, config_path, tmp_path, monkeypatch, caplog
    ):
        dead = tmp_path / "scaffold-install.sock"
        dead.write_text("")
        _point_socket_at(monkeypatch, dead)

        with (
            caplog.at_level(logging.DEBUG, logger="fraisier"),
            pytest.raises(DeploymentError),
        ):
            deployer._install_scaffold(config_path=config_path)

        assert not any("falling back" in r.message.lower() for r in caplog.records)


class TestAnUnreadableConfig:
    def test_fails_with_the_operator_step(self, deployer, tmp_path, runner):
        broken = tmp_path / "fraises.yaml"
        broken.write_text("name: [unclosed\n")

        with pytest.raises(DeploymentError) as exc:
            deployer._install_scaffold(config_path=broken)

        assert OPERATOR_STEP in str(exc.value)
        runner.run.assert_not_called()

    def test_the_next_deploy_asks_again(self, deployer, tmp_path):
        broken = tmp_path / "fraises.yaml"
        broken.write_text("name: [unclosed\n")
        watcher = ConfigWatcher(tmp_path)
        watcher.save_hash()

        with pytest.raises(DeploymentError):
            deployer._install_scaffold(config_path=broken)

        assert watcher.get_previous_hash() is None
