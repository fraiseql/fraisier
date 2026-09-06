"""`db reset` must hold the deployment lock (#389).

#310 gave `db restore` the per-fraise lock the webhook takes around every
deploy, after a restore timer fired mid-deploy and killed an in-flight
`pg_restore`. `db reset` was left out, and it is the more destructive of the
two: `reset_from_template` force-disconnects every client, **drops** the
database, and recreates it from its template. Run against a fraise that is
mid-deploy, it takes the schema out from under a running migration.

Of the `db` subcommands only `restore` took the lock. `reset` is the one that
destroys, so it is the one that gets it next.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fraisier.cli.main import main
from fraisier.errors import DeploymentLockError

_ADMIN_URL = "postgresql:///postgres?host=/var/run/postgresql"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def reset_config():
    config = MagicMock()
    config.get_fraise.return_value = {"type": "api", "description": "Test API"}
    config.get_fraise_environment.return_value = {
        "type": "api",
        "app_path": "/var/www/api",
        "database": {
            "name": "mydb_dev",
            "admin_url": _ADMIN_URL,
            "template_prefix": "template_",
        },
    }
    config._config = {}
    config.list_fraises_detailed.return_value = []
    with patch("fraisier.cli.main.get_config", return_value=config):
        yield config


def _invoke(runner, args, *, lock_side_effect=None):
    """Run `db reset`, stubbing everything below the lock."""
    lock = MagicMock()
    if lock_side_effect is not None:
        lock.side_effect = lock_side_effect

    with (
        patch("fraisier.locking.deployment_lock", lock),
        patch("fraisier.dbops.guard.is_external_db", return_value=False),
        patch(
            "fraisier.dbops.templates.reset_from_template",
            return_value=MagicMock(success=True, template_name="template_mydb_dev"),
        ) as mock_reset,
    ):
        res = runner.invoke(main, args)
    return res, lock, mock_reset


class TestResetTakesTheDeploymentLock:
    def test_lock_is_acquired_for_the_fraise(self, runner, reset_config):
        res, lock, _ = _invoke(runner, ["db", "reset", "api", "-e", "development"])

        assert lock.called, f"deployment_lock never acquired (exit {res.exit_code})"
        assert lock.call_args.args[0] == "api"

    def test_the_drop_happens_while_holding_it(self, runner, reset_config):
        """The lock must wrap the mutation, not merely be taken and dropped."""
        _, lock, mock_reset = _invoke(
            runner, ["db", "reset", "api", "-e", "development"]
        )

        assert lock.return_value.__enter__.called
        assert mock_reset.called


class TestLockedBehaviour:
    def test_errors_when_a_deploy_holds_the_lock(self, runner, reset_config):
        """Refuse loudly. There is no benign interleaving here: the next
        statement drops the database the deploy is migrating."""
        res, _, mock_reset = _invoke(
            runner,
            ["db", "reset", "api", "-e", "development"],
            lock_side_effect=DeploymentLockError("Deploy already running for api"),
        )

        assert res.exit_code != 0
        assert not mock_reset.called, "the database was dropped despite the lock"

    def test_the_message_says_what_to_do(self, runner, reset_config):
        res, _, _ = _invoke(
            runner,
            ["db", "reset", "api", "-e", "development"],
            lock_side_effect=DeploymentLockError("Deploy already running for api"),
        )

        assert "deploy is in progress" in res.output.lower(), res.output


class TestTheGuardsStillComeFirst:
    def test_an_external_db_is_skipped_without_taking_the_lock(
        self, runner, reset_config
    ):
        """Nothing to serialise against if nothing is going to be dropped."""
        lock = MagicMock()
        with (
            patch("fraisier.locking.deployment_lock", lock),
            patch("fraisier.dbops.guard.is_external_db", return_value=True),
            patch("fraisier.dbops.templates.reset_from_template") as mock_reset,
        ):
            res = runner.invoke(main, ["db", "reset", "api", "-e", "development"])

        assert res.exit_code == 0
        assert not mock_reset.called
        assert not lock.called

    def test_a_missing_admin_url_fails_before_the_lock(self, runner, reset_config):
        reset_config.get_fraise_environment.return_value = {
            "type": "api",
            "database": {"name": "mydb_dev"},
        }
        lock = MagicMock()
        with (
            patch("fraisier.locking.deployment_lock", lock),
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch("fraisier.dbops.templates.reset_from_template") as mock_reset,
        ):
            res = runner.invoke(main, ["db", "reset", "api", "-e", "development"])

        assert res.exit_code != 0
        assert not mock_reset.called
        assert not lock.called
