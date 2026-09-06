"""An invalid fraises.yaml never reaches /opt, and a rollback puts the old one back (#383).

The deploy used to copy the checkout's ``fraises.yaml`` over the server copy
*before* anything had loaded it. Validation happened next, indirectly, when
``fraisier scaffold`` ran against the copied file; the refusal aborted the
deploy and rolled back **git only**. The invalid file stayed at
``/opt/fraisier/fraises.yaml``, outliving the deploy that installed it.

Nothing looked wrong at first: the running webhook keeps its cached config.
The next webhook restart — a self-upgrade, a reboot, a ``scaffold-install`` —
died in ``lifespan`` on ``get_config()``, and so did ``trigger-deploy``. The
operator had been told to "fix the underlying error and redeploy", and no
deploy route worked until someone repaired the ``/opt`` file by hand.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

from fraisier.deployers.api import APIDeployer
from fraisier.errors import DeploymentError

if TYPE_CHECKING:
    from pathlib import Path


VALID = """\
fraises:
  my_api:
    type: api
    description: Test API service
    environments:
      production:
        app_path: /var/www/api
        systemd_service: my_api.service
"""

#: Two logical servers claiming the same machine — ``validate_servers`` refuses
#: this at load time, so it never reaches the per-environment validator.
INVALID_SERVERS = """\
servers:
  primary:
    machine_hostnames: [host-a]
  secondary:
    machine_hostnames: [host-a]
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
"""

#: Valid YAML, valid at load time, refused by the per-environment validator —
#: which only runs when someone asks for this fraise and environment.
INVALID_ENVIRONMENT = """\
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        timeout: "ten minutes"
"""


@pytest.fixture
def opt_config(tmp_path, monkeypatch):
    """A writable stand-in for /opt/fraisier/fraises.yaml, already populated."""
    path = tmp_path / "opt" / "fraises.yaml"
    path.parent.mkdir()
    path.write_text(VALID)
    monkeypatch.setenv("FRAISIER_CONFIG", str(path))
    return path


@pytest.fixture
def checkout(tmp_path):
    """The git checkout the deploy syncs from."""
    path = tmp_path / "app"
    path.mkdir()
    return path


def _deployer(checkout: Path, monkeypatch, **extra) -> APIDeployer:
    deployer = APIDeployer(
        {
            "fraise_name": "my_api",
            "environment": "production",
            "app_path": str(checkout),
            "deploy_user": "deployer",
            **extra,
        }
    )
    # The scaffold is a privileged, host-shaped step; this phase is about what
    # reaches /opt before it runs.
    monkeypatch.setattr(deployer, "_regenerate_scaffold", lambda **_: None)
    monkeypatch.setattr(deployer, "_install_scaffold", lambda **_: None)
    return deployer


class TestValidationHappensBeforeTheCopy:
    def test_an_unloadable_config_never_reaches_opt(
        self, opt_config, checkout, monkeypatch
    ):
        (checkout / "fraises.yaml").write_text(INVALID_SERVERS)
        deployer = _deployer(checkout, monkeypatch)

        with pytest.raises(DeploymentError):
            deployer._sync_config_if_needed()

        assert opt_config.read_text() == VALID, (
            "the invalid config was installed and would outlive the rollback"
        )

    def test_an_invalid_environment_never_reaches_opt(
        self, opt_config, checkout, monkeypatch
    ):
        (checkout / "fraises.yaml").write_text(INVALID_ENVIRONMENT)
        deployer = _deployer(checkout, monkeypatch)

        with pytest.raises(DeploymentError):
            deployer._sync_config_if_needed()

        assert opt_config.read_text() == VALID

    def test_the_error_names_the_file_and_the_validator_message(
        self, opt_config, checkout, monkeypatch
    ):
        (checkout / "fraises.yaml").write_text(INVALID_SERVERS)
        deployer = _deployer(checkout, monkeypatch)

        with pytest.raises(DeploymentError) as excinfo:
            deployer._sync_config_if_needed()

        message = str(excinfo.value)
        assert str(checkout / "fraises.yaml") in message
        assert "host-a" in message, "the validator's own words are what fix it"

    def test_a_valid_config_still_syncs(self, opt_config, checkout, monkeypatch):
        new = VALID.replace("my_api.service", "my_api_renamed.service")
        (checkout / "fraises.yaml").write_text(new)
        deployer = _deployer(checkout, monkeypatch)

        deployer._sync_config_if_needed()

        assert opt_config.read_text() == new

    def test_validation_does_not_rebind_the_global_config(
        self, opt_config, checkout, monkeypatch
    ):
        """The candidate is loaded on its own, not through the singleton.

        ``get_config(path)`` rebuilds the process-wide config. Validating the
        checkout's file through it would leave a long-running webhook pointing
        at ``<app_path>/fraises.yaml`` after an aborted deploy.
        """
        from fraisier.config import get_config, reset_config

        reset_config()
        get_config(opt_config)
        (checkout / "fraises.yaml").write_text(INVALID_SERVERS)
        deployer = _deployer(checkout, monkeypatch)

        with pytest.raises(DeploymentError):
            deployer._sync_config_if_needed()

        assert get_config().config_path == opt_config


class TestThePreviousConfigComesBack:
    def test_a_replaced_config_is_kept_next_to_the_live_one(
        self, opt_config, checkout, monkeypatch
    ):
        (checkout / "fraises.yaml").write_text(
            VALID.replace("my_api.service", "renamed.service")
        )
        deployer = _deployer(checkout, monkeypatch)

        deployer._sync_config_if_needed()

        prev = opt_config.with_name(opt_config.name + ".prev")
        assert prev.exists(), "nothing to put back if the deploy fails"
        assert prev.read_text() == VALID
        assert prev.parent == opt_config.parent, (
            "the restore is a rename; it must not cross a filesystem"
        )

    def test_restore_puts_the_previous_bytes_back(
        self, opt_config, checkout, monkeypatch
    ):
        new = VALID.replace("my_api.service", "renamed.service")
        (checkout / "fraises.yaml").write_text(new)
        deployer = _deployer(checkout, monkeypatch)
        deployer._sync_config_if_needed()
        assert opt_config.read_text() == new

        assert deployer._restore_synced_config() is True

        assert opt_config.read_text() == VALID
        assert not opt_config.with_name(opt_config.name + ".prev").exists()

    def test_restore_is_idempotent(self, opt_config, checkout, monkeypatch):
        (checkout / "fraises.yaml").write_text(
            VALID.replace("my_api.service", "renamed.service")
        )
        deployer = _deployer(checkout, monkeypatch)
        deployer._sync_config_if_needed()

        assert deployer._restore_synced_config() is True
        assert deployer._restore_synced_config() is False
        assert opt_config.read_text() == VALID

    def test_restore_without_a_sync_does_nothing(
        self, opt_config, checkout, monkeypatch
    ):
        deployer = _deployer(checkout, monkeypatch)
        assert deployer._restore_synced_config() is False
        assert opt_config.read_text() == VALID

    def test_two_syncs_in_one_deploy_keep_the_pre_deploy_bytes(
        self, opt_config, checkout, monkeypatch
    ):
        """``execute()`` syncs twice — once pre-pull, once after the checkout.

        The second must not overwrite the first's ``.prev``, or a rollback
        restores the config the deploy itself installed minutes earlier.
        """
        deployer = _deployer(checkout, monkeypatch)
        (checkout / "fraises.yaml").write_text(
            VALID.replace("my_api.service", "pre_pull.service")
        )
        deployer._sync_config_if_needed()
        (checkout / "fraises.yaml").write_text(
            VALID.replace("my_api.service", "post_pull.service")
        )
        deployer._sync_config_if_needed()

        deployer._restore_synced_config()

        assert opt_config.read_text() == VALID

    def test_a_first_ever_sync_records_nothing_to_restore(
        self, tmp_path, checkout, monkeypatch
    ):
        """No previous file means no previous config; a rollback leaves the
        new one, which the next deploy regenerates from."""
        import os

        dest = tmp_path / "opt" / "fraises.yaml"
        dest.parent.mkdir()
        os.environ["FRAISIER_CONFIG"] = str(dest)
        try:
            (checkout / "fraises.yaml").write_text(VALID)
            deployer = _deployer(checkout, monkeypatch)
            deployer._sync_config_if_needed()

            assert dest.exists()
            assert not dest.with_name(dest.name + ".prev").exists()
            assert deployer._restore_synced_config() is False
        finally:
            del os.environ["FRAISIER_CONFIG"]

    def test_discard_removes_the_prev_file(self, opt_config, checkout, monkeypatch):
        (checkout / "fraises.yaml").write_text(
            VALID.replace("my_api.service", "renamed.service")
        )
        deployer = _deployer(checkout, monkeypatch)
        deployer._sync_config_if_needed()

        deployer._discard_replaced_config()

        assert not opt_config.with_name(opt_config.name + ".prev").exists()
        assert opt_config.read_text() != VALID, "a success keeps the new config"
        assert deployer._restore_synced_config() is False


class TestTheDeployWiresTheRestoreUp:
    def test_restore_previous_state_puts_the_config_back(
        self, opt_config, checkout, monkeypatch
    ):
        new = VALID.replace("my_api.service", "renamed.service")
        (checkout / "fraises.yaml").write_text(new)
        deployer = _deployer(checkout, monkeypatch)
        deployer._sync_config_if_needed()
        deployer._previous_sha = "abc1234"
        monkeypatch.setattr(deployer, "_git_rollback", lambda _sha: None)
        monkeypatch.setattr(deployer, "_restart_service", lambda: None)

        outcome = deployer._restore_previous_state()

        assert outcome.git_reverted is True
        assert opt_config.read_text() == VALID, (
            "the tree went back a commit; the config it describes must too"
        )

    def test_rollback_puts_the_config_back(self, opt_config, checkout, monkeypatch):
        new = VALID.replace("my_api.service", "renamed.service")
        (checkout / "fraises.yaml").write_text(new)
        deployer = _deployer(checkout, monkeypatch)
        deployer._sync_config_if_needed()
        monkeypatch.setattr(deployer, "_git_rollback", lambda _sha: None)
        monkeypatch.setattr(
            deployer, "_finalize_rollback", lambda *_a, **_k: _rolled_back()
        )

        deployer.rollback(to_version="abc1234")

        assert opt_config.read_text() == VALID

    def test_a_database_rollback_failure_leaves_the_new_config(
        self, opt_config, checkout, monkeypatch
    ):
        """The tree is deliberately left at the new commit when the database
        rollback fails; the config must describe the same commit."""
        from fraisier.deployers.base import DeploymentResult, DeploymentStatus

        new = VALID.replace("my_api.service", "renamed.service")
        (checkout / "fraises.yaml").write_text(new)
        deployer = _deployer(
            checkout, monkeypatch, database={"strategy": "migrate", "name": "db"}
        )
        deployer._sync_config_if_needed()
        deployer._previous_sha = "abc1234"
        deployer._migrations_applied = 1
        monkeypatch.setattr(
            deployer,
            "_rollback_database",
            lambda *_a: DeploymentResult(
                success=False,
                status=DeploymentStatus.ROLLBACK_FAILED,
                error_message="down failed",
            ),
        )

        outcome = deployer._restore_previous_state()

        assert outcome.git_reverted is False
        assert opt_config.read_text() == new

    def test_a_successful_deploy_leaves_no_prev_file(
        self, opt_config, checkout, monkeypatch
    ):
        from fraisier.deployers.base import DeploymentResult, DeploymentStatus

        (checkout / "fraises.yaml").write_text(
            VALID.replace("my_api.service", "renamed.service")
        )
        deployer = _deployer(checkout, monkeypatch)
        deployer._sync_config_if_needed()

        from unittest.mock import patch

        with patch("fraisier.deployers.mixins.write_status"):
            deployer._record_outcome(
                DeploymentResult(success=True, status=DeploymentStatus.SUCCESS)
            )

        assert not opt_config.with_name(opt_config.name + ".prev").exists()


def _rolled_back():
    from fraisier.deployers.base import DeploymentResult, DeploymentStatus

    return DeploymentResult(success=True, status=DeploymentStatus.ROLLED_BACK)


class TestTheDeadRollbackPathIsGone:
    def test_rollback_config_no_longer_exists(self):
        """``_rollback_config`` was written for this job and never called.

        ``install.sh.j2`` described the behaviour it never had. Three tests
        asserted only that the method existed, was callable, and had a
        "Returns:" line in its docstring — none of them ran it.
        """
        from fraisier.deployers.base import BaseDeployer

        assert not hasattr(BaseDeployer, "_rollback_config")


class TestTheWebhookStartsWithAnUnloadableConfig:
    """A webhook that crash-loops cannot be repaired by the redeploy the
    operator was told to run; a webhook that starts and refuses can.

    Owner's call (#383): the lifespan starts, and every request that needs the
    configuration answers a structured error naming it. This is a behaviour
    change — before, `lifespan` raised out of `get_config()` and the unit went
    into `Restart=on-failure`.
    """

    @pytest.fixture
    def broken_config(self, tmp_path, monkeypatch):
        from fraisier.config import reset_config

        path = tmp_path / "fraises.yaml"
        path.write_text(INVALID_SERVERS)
        monkeypatch.setenv("FRAISIER_CONFIG", str(path))
        reset_config()
        return path

    def test_lifespan_starts(self, broken_config):
        from fastapi.testclient import TestClient

        from fraisier.webhook import app as webhook_app

        with TestClient(webhook_app) as client:
            assert client.get("/health").status_code == 200

    def test_the_startup_error_names_the_config(self, broken_config, caplog):
        import logging

        from fastapi.testclient import TestClient

        from fraisier.webhook import app as webhook_app

        with caplog.at_level(logging.ERROR), TestClient(webhook_app):
            pass

        assert any(
            str(broken_config) in record.getMessage() for record in caplog.records
        ), "an operator reading the journal must be told which file is broken"

    def test_a_delivery_gets_a_structured_error(self, broken_config):
        from fastapi.testclient import TestClient

        from fraisier.webhook import app as webhook_app

        with TestClient(webhook_app, raise_server_exceptions=False) as client:
            response = client.post(
                "/webhook",
                json={"ref": "refs/heads/main"},
                headers={"X-GitHub-Event": "push", "X-GitHub-Delivery": "d1"},
            )

        assert response.status_code == 500
        body = response.json()
        assert body.get("error_type") == "configuration_error", body
