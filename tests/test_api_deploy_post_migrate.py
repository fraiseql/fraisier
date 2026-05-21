"""Tests for the post-migrate SQL hook wired into the API deploy pipeline (#204).

The runner is invoked after ``_run_database_migrations`` succeeds and
before ``_restart_service``. A ``halt`` failure raises ``DeploymentError``
which aborts the deploy prior to the service restart; no rollback is
needed because no new code is yet serving. A ``warn`` failure logs and
the pipeline continues normally.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from fraisier.deployers.api import APIDeployer


def _make_deployer(post_migrate=None, **overrides):
    database = {
        "name": "mydb",
        "strategy": "migrate",
        "database_url": "postgresql:///mydb?host=/var/run/postgresql",
    }
    if post_migrate is not None:
        database["post_migrate"] = post_migrate
    config = {
        "fraise_name": "myapi",
        "environment": "production",
        "app_path": "/srv/myapi",
        "clone_url": "git@github.com:org/myapi.git",
        "branch": "main",
        "systemd_service": "myapi.service",
        "health_check": {"url": "http://localhost:8000/health", "timeout": 5},
        "repos_base": "/tmp/repos",
        "database": database,
    }
    config.update(overrides)
    return APIDeployer(config)


class TestPostMigrateOrdering:
    def test_runs_post_migrate_between_migrations_and_restart(self):
        call_order = []
        deployer = _make_deployer(
            post_migrate=[{"sql_file": "db/grant.sql"}],
        )

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old", "new"),
            ),
            patch.object(
                deployer,
                "_run_database_migrations",
                side_effect=lambda: call_order.append("migrate"),
            ),
            patch(
                "fraisier.post_migrate.run_post_migrate_steps",
                side_effect=lambda *_a, **_kw: call_order.append("post_migrate"),
            ),
            patch.object(
                deployer,
                "_restart_service",
                side_effect=lambda: call_order.append("restart"),
            ),
            patch.object(deployer, "_wait_for_health", return_value=True),
        ):
            result = deployer.execute()

        assert result.success is True
        assert call_order == ["migrate", "post_migrate", "restart"]

    def test_no_section_is_noop(self):
        # database has no post_migrate key — runner must not fire.
        deployer = _make_deployer()

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old", "new"),
            ),
            patch.object(deployer, "_run_database_migrations"),
            patch(
                "fraisier.post_migrate.run_post_migrate_steps",
            ) as mock_run,
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
        ):
            deployer.execute()

        mock_run.assert_not_called()


class TestPostMigrateFailureSemantics:
    def test_halt_failure_aborts_before_service_restart(self):
        from fraisier.errors import DeploymentError

        deployer = _make_deployer(
            post_migrate=[{"sql_file": "db/grant.sql", "on_error": "halt"}],
        )

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old", "new"),
            ),
            patch.object(deployer, "_run_database_migrations"),
            patch(
                "fraisier.post_migrate.run_post_migrate_steps",
                side_effect=DeploymentError("grant.sql failed"),
            ),
            patch.object(deployer, "_restart_service") as mock_restart,
            patch.object(deployer, "_wait_for_health", return_value=True),
        ):
            result = deployer.execute()

        # The deploy fails because the halt fired.
        assert result.success is False
        # And it did NOT restart the service — by design, the service is
        # left serving the previous version.
        mock_restart.assert_not_called()

    def test_warn_failure_proceeds_to_service_restart(self):
        # When the runner uses on_error=warn it does not raise; the
        # deploy continues normally. We assert by mocking the runner to
        # return None (no exception) and confirming restart happens.
        deployer = _make_deployer(
            post_migrate=[{"sql_file": "db/grant.sql", "on_error": "warn"}],
        )

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old", "new"),
            ),
            patch.object(deployer, "_run_database_migrations"),
            patch(
                "fraisier.post_migrate.run_post_migrate_steps",
                return_value=None,
            ) as mock_run,
            patch.object(deployer, "_restart_service") as mock_restart,
            patch.object(deployer, "_wait_for_health", return_value=True),
        ):
            result = deployer.execute()

        assert result.success is True
        mock_run.assert_called_once()
        mock_restart.assert_called_once()


@pytest.mark.parametrize(
    "missing_field", ["database_url"],
)
class TestPostMigrateGuards:
    def test_skips_when_database_url_missing(self, missing_field):
        # post_migrate is defined but database_url is missing — the
        # runner cannot connect to anything, so it must not fire.
        deployer = _make_deployer(post_migrate=[{"sql_file": "db/grant.sql"}])
        # Remove the database_url key.
        deployer.database_config = {
            k: v
            for k, v in deployer.database_config.items()
            if k != missing_field
        }

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old", "new"),
            ),
            patch.object(deployer, "_run_database_migrations"),
            patch(
                "fraisier.post_migrate.run_post_migrate_steps",
            ) as mock_run,
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
        ):
            deployer.execute()

        mock_run.assert_not_called()
