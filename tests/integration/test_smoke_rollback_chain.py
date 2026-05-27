"""Integration test for the composed smoke-test → rollback chain (#204).

Each piece of the chain — ``smoke_tests.run`` → ``SmokeTestError(rollback=True)``
→ ``ApiDeployer.rollback()`` → ``_build_rollback_result`` — is exercised by
unit tests in isolation. This test composes them so that the full
contract holds end-to-end:

1. ``rollback()`` is invoked exactly once when a smoke test fails with
   the default ``on_failure: rollback`` policy.
2. The returned ``DeploymentResult.status`` is ``ROLLED_BACK``.
3. ``_complete_db_record`` is called with the rolled-back result so the
   deployment ledger reflects the post-rollback state, not the failed
   deploy.
"""

from __future__ import annotations

from unittest.mock import patch

from fraisier.deployers.api import APIDeployer
from fraisier.deployers.base import DeploymentResult, DeploymentStatus
from fraisier.smoke_tests import SmokeTestError


def _deployer_with_smoke(smoke):
    return APIDeployer(
        {
            "fraise_name": "myapi",
            "environment": "production",
            "app_path": "/srv/myapi",
            "clone_url": "git@github.com:org/myapi.git",
            "branch": "main",
            "systemd_service": "myapi.service",
            "health_check": {"url": "http://localhost:8000/health", "timeout": 5},
            "repos_base": "/tmp/repos",
            "smoke_tests": smoke,
        }
    )


def test_smoke_failure_with_rollback_policy_completes_chain():
    """End-to-end: failing smoke → rollback → ROLLED_BACK result recorded."""
    deployer = _deployer_with_smoke([{"name": "auth", "url": "/me", "assert": []}])

    rollback_inner = DeploymentResult(
        success=True,
        status=DeploymentStatus.SUCCESS,
        new_version="prev-sha",
    )

    with (
        patch("fraisier.deployers.mixins.clone_bare_repo"),
        patch(
            "fraisier.deployers.mixins.fetch_and_checkout",
            return_value=("prev-sha", "new-sha"),
        ),
        patch.object(deployer, "_restart_service"),
        patch.object(deployer, "_wait_for_health", return_value=True),
        patch(
            "fraisier.smoke_tests.run_smoke_tests",
            side_effect=SmokeTestError("auth check failed", rollback=True),
        ),
        patch.object(deployer, "rollback", return_value=rollback_inner) as mock_rb,
        patch.object(deployer, "_complete_db_record") as mock_complete,
    ):
        result = deployer.execute()

    mock_rb.assert_called_once()
    assert result.status is DeploymentStatus.ROLLED_BACK
    assert result.success is False

    assert mock_complete.called, "_complete_db_record was never invoked"
    recorded_result = mock_complete.call_args.args[1]
    assert recorded_result is result
    assert recorded_result.status is DeploymentStatus.ROLLED_BACK


def test_smoke_failure_with_halt_policy_skips_rollback_and_records_failed():
    """Sibling contract: halt policy records FAILED without touching rollback()."""
    deployer = _deployer_with_smoke(
        [{"name": "auth", "url": "/me", "on_failure": "halt", "assert": []}]
    )

    with (
        patch("fraisier.deployers.mixins.clone_bare_repo"),
        patch(
            "fraisier.deployers.mixins.fetch_and_checkout",
            return_value=("prev-sha", "new-sha"),
        ),
        patch.object(deployer, "_restart_service"),
        patch.object(deployer, "_wait_for_health", return_value=True),
        patch(
            "fraisier.smoke_tests.run_smoke_tests",
            side_effect=SmokeTestError("auth check failed", rollback=False),
        ),
        patch.object(deployer, "rollback") as mock_rb,
        patch.object(deployer, "_complete_db_record") as mock_complete,
    ):
        result = deployer.execute()

    mock_rb.assert_not_called()
    assert result.status is DeploymentStatus.FAILED
    assert result.success is False
    recorded_result = mock_complete.call_args.args[1]
    assert recorded_result.status is DeploymentStatus.FAILED
