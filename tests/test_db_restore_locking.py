"""`db restore` must hold the deployment lock (#310).

`fraisier.locking` gives per-fraise mutual exclusion and the webhook wraps every
deployment in ``deployment_lock(fraise)``. The `db restore` CLI never acquired
it, so a timer-, cron- or hand-driven restore ran completely unsynchronised
with deploys of the same fraise.

Incident (printoptim.dev, 2026-07-30 00:00 UTC): the staging-restore timer fired
mid-deploy, stopped the API service and terminated every connection to the
staging database — killing the in-flight deploy's pg_restore
(``FATAL: terminating connection due to administrator command``) and leaving
staging half-restored. The timer firing at midnight at all was #311.
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
def restore_config():
    config = MagicMock()
    config.get_fraise.return_value = {"type": "api", "description": "Test API"}
    config.get_fraise_environment.return_value = {
        "type": "api",
        "app_path": "/var/www/api",
        "systemd_service": "api.staging.service",
        "database": {
            "name": "mydb_staging",
            "strategy": "restore_migrate",
            "admin_url": _ADMIN_URL,
            "confiture_config": "confiture.yaml",
            "restore": {
                "backup_dir": "/backup/production",
                "backup_pattern": "*.dump",
                "max_age_hours": 48.0,
                "create_template": True,
                "min_tables": 100,
            },
        },
    }
    config._config = {"backup": {}}
    config.deployment = MagicMock()
    config.deployment.get_strategy.return_value = "restore_migrate"
    config.list_fraises_detailed.return_value = []
    with patch("fraisier.cli.main.get_config", return_value=config):
        yield config


def _invoke(runner, args, *, lock_side_effect=None):
    """Run `db restore`, stubbing everything below the lock."""
    result_mock = MagicMock(
        success=True,
        migrations_applied=0,
        total_duration_seconds=0.0,
        restore_duration_seconds=0.0,
        migration_duration_seconds=0.0,
    )
    lock = MagicMock()
    if lock_side_effect is not None:
        lock.side_effect = lock_side_effect

    with (
        patch("fraisier.locking.deployment_lock", lock),
        patch("fraisier.dbops.guard.is_external_db", return_value=False),
        patch(
            "fraisier.strategies.RestoreMigrateStrategy.execute",
            return_value=result_mock,
        ) as mock_execute,
        patch("fraisier.systemd.SystemdServiceManager"),
        patch("fraisier.dbops.restore.find_latest_backup", return_value="/b/x.dump"),
        patch("fraisier.dbops.restore.validate_backup_age"),
        patch("fraisier.post_migrate.run_configured_post_migrate", return_value=None),
    ):
        res = runner.invoke(main, args)
    return res, lock, mock_execute


class TestRestoreTakesTheDeploymentLock:
    def test_lock_is_acquired_for_the_fraise(self, runner, restore_config):
        res, lock, _ = _invoke(runner, ["db", "restore", "api", "staging"])

        assert lock.called, f"deployment_lock never acquired (exit {res.exit_code})"
        assert lock.call_args.args[0] == "api"

    def test_restore_runs_while_holding_it(self, runner, restore_config):
        """The lock must wrap the mutation, not merely be taken and dropped."""
        _, lock, mock_execute = _invoke(runner, ["db", "restore", "api", "staging"])

        assert lock.return_value.__enter__.called
        assert mock_execute.called


class TestLockedBehaviour:
    def test_errors_when_a_deploy_holds_the_lock(self, runner, restore_config):
        """Default: refuse loudly rather than interleave with a deploy."""
        res, _, mock_execute = _invoke(
            runner,
            ["db", "restore", "api", "staging"],
            lock_side_effect=DeploymentLockError("Deploy already running for api"),
        )

        assert res.exit_code != 0
        assert not mock_execute.called, "restore ran despite the lock being held"

    def test_skip_if_locked_exits_zero_without_restoring(self, runner, restore_config):
        """For timer units: a skipped nightly restore is a non-event.

        A concurrent staging deploy is itself restoring from production, so
        failing the timer would page someone over a no-op.
        """
        res, _, mock_execute = _invoke(
            runner,
            ["db", "restore", "api", "staging", "--skip-if-locked"],
            lock_side_effect=DeploymentLockError("Deploy already running for api"),
        )

        assert res.exit_code == 0, res.output
        assert not mock_execute.called
        assert "skip" in res.output.lower()

    def test_skip_if_locked_does_not_mask_an_unlocked_failure(
        self, runner, restore_config
    ):
        """The flag suppresses lock contention only, never a real error."""
        res, _, _ = _invoke(
            runner,
            ["db", "restore", "api", "staging", "--skip-if-locked"],
            lock_side_effect=RuntimeError("disk on fire"),
        )

        assert res.exit_code != 0


class TestDryRunIsNotBlocked:
    def test_dry_run_does_not_take_the_lock(self, runner, restore_config):
        """A dry run mutates nothing, so a running deploy must not veto it."""
        res, lock, mock_execute = _invoke(
            runner, ["db", "restore", "api", "staging", "--dry-run"]
        )

        assert res.exit_code == 0, res.output
        assert not lock.called
        assert not mock_execute.called


class TestTimerUnitPassesTheFlag:
    """The generated unit must opt into the skip, or the timer just fails nightly.

    Without `--skip-if-locked` the new lock turns a harmless collision into a
    failed systemd unit and a paged operator — the fix would trade a silent
    corruption for a noisy false alarm.
    """

    def _service_text(self, tmp_path) -> str:
        from fraisier.config import FraisierConfig
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(f"""
name: myproj
scaffold:
  deploy_user: fraisier
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      staging:
        app_path: /var/www/staging
        database:
          name: myapp_staging
          strategy: restore_migrate
""")
        ScaffoldRenderer(FraisierConfig(p)).render()
        return (tmp_path / "output" / "systemd" / "restore-staging.service").read_text()

    def test_execstart_passes_skip_if_locked(self, tmp_path):
        exec_lines = [
            ln
            for ln in self._service_text(tmp_path).splitlines()
            if ln.startswith("ExecStart=")
        ]
        assert len(exec_lines) == 1
        assert "--skip-if-locked" in exec_lines[0]

    def test_execstart_still_invokes_db_restore(self, tmp_path):
        """The flag must be added to the restore call, not replace it."""
        exec_lines = [
            ln
            for ln in self._service_text(tmp_path).splitlines()
            if ln.startswith("ExecStart=")
        ]
        assert "db restore" in exec_lines[0]


class TestUnusableLockIsNotSilentlySkipped:
    """A lock that cannot be taken at all is an error, never a skip.

    `/run/fraisier` is tmpfs, created by the webhook unit's
    `RuntimeDirectory=fraisier`. If it is absent, acquiring raises OSError
    rather than DeploymentLockError — that means "I could not tell whether a
    deploy is running", which must never be treated as "no deploy is running".
    """

    def test_oserror_fails_even_with_skip_if_locked(self, runner, restore_config):
        res, _, mock_execute = _invoke(
            runner,
            ["db", "restore", "api", "staging", "--skip-if-locked"],
            lock_side_effect=PermissionError(13, "Permission denied", "/run/fraisier"),
        )

        assert res.exit_code != 0, "an unusable lock must not read as 'nothing running'"
        assert not mock_execute.called

    def test_oserror_message_names_the_lock_directory(self, runner, restore_config):
        res, _, _ = _invoke(
            runner,
            ["db", "restore", "api", "staging"],
            lock_side_effect=PermissionError(13, "Permission denied", "/run/fraisier"),
        )

        assert "/run/fraisier" in res.output
