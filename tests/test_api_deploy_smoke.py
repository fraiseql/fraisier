"""Tests for the authenticated smoke-test hook wired into the API deploy
pipeline (#204 PR B).

The smoke-test step runs *after* the unauthenticated ``/health`` poll
succeeds and *before* the success result is constructed. Three policies:

- ``rollback`` (default) — raise SmokeTestError, dispatch to
  ``_restore_previous_state``, return a rolled-back result.
- ``halt`` — raise SmokeTestError, do NOT invoke
  ``_restore_previous_state``; deploy aborts with status=failed.
- ``warn`` — log and continue; deploy ends in success.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fraisier.deployers.api import APIDeployer
from fraisier.smoke_tests import SmokeTestError


def _make_deployer(smoke_tests=None, **overrides):
    config = {
        "fraise_name": "myapi",
        "environment": "production",
        "app_path": "/srv/myapi",
        "clone_url": "git@github.com:org/myapi.git",
        "branch": "main",
        "systemd_service": "myapi.service",
        "health_check": {"url": "http://localhost:8000/health", "timeout": 5},
        "repos_base": "/tmp/repos",
    }
    if smoke_tests is not None:
        config["smoke_tests"] = smoke_tests
    config.update(overrides)
    return APIDeployer(config)


class TestSmokeTestsCallOrder:
    def test_smoke_tests_run_after_health_check_success(self):
        smoke = [{"name": "auth", "url": "/me", "assert": []}]
        deployer = _make_deployer(smoke_tests=smoke)
        call_order = []

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old", "new"),
            ),
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
            patch(
                "fraisier.smoke_tests.run_smoke_tests",
                side_effect=lambda *_a, **_kw: call_order.append("smoke"),
            ),
            patch.object(
                deployer,
                "_check_health_or_rollback",
                side_effect=lambda *_a, **_kw: (
                    call_order.append("health"),
                    None,
                )[-1],
            ),
        ):
            result = deployer.execute()

        assert result.success is True
        assert call_order == ["health", "smoke"]

    def test_no_smoke_tests_means_no_change_in_deploy_path(self):
        deployer = _make_deployer()
        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old", "new"),
            ),
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
            patch("fraisier.smoke_tests.run_smoke_tests") as mock_run,
        ):
            result = deployer.execute()
        assert result.success is True
        mock_run.assert_not_called()


class TestSmokeTestsFailureSemantics:
    def test_rollback_on_failure_invokes_restore_previous_state(self):
        smoke = [{"name": "auth", "url": "/me", "assert": []}]
        deployer = _make_deployer(smoke_tests=smoke)
        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old", "new"),
            ),
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
            patch(
                "fraisier.smoke_tests.run_smoke_tests",
                side_effect=SmokeTestError("auth check failed", rollback=True),
            ),
            patch.object(deployer, "rollback") as mock_rollback,
        ):
            mock_rollback.return_value = MagicMock(success=True)
            result = deployer.execute()

        assert result.success is False
        mock_rollback.assert_called_once()

    def test_halt_on_failure_does_not_invoke_restore_previous_state(self):
        smoke = [{"name": "auth", "url": "/me", "on_failure": "halt", "assert": []}]
        deployer = _make_deployer(smoke_tests=smoke)
        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old", "new"),
            ),
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
            patch(
                "fraisier.smoke_tests.run_smoke_tests",
                side_effect=SmokeTestError("auth check failed", rollback=False),
            ),
            patch.object(deployer, "rollback") as mock_rollback,
        ):
            result = deployer.execute()

        assert result.success is False
        mock_rollback.assert_not_called()
