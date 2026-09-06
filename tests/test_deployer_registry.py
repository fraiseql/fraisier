"""One dispatch table for fraise types (#379).

There were three: the CLI's, the daemon's and the webhook's. They disagreed —
the webhook knew nothing about ``scheduled`` or ``backup``, so a push to a
branch mapped to one was answered ``deployment_triggered`` and then dropped by
a background task that logged "Unknown fraise type" and returned. No status
write, no deployments row, no notification.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fraisier.deployers.api import APIDeployer
from fraisier.deployers.docker_compose import DockerComposeDeployer
from fraisier.deployers.etl import ETLDeployer
from fraisier.deployers.registry import (
    FRAISE_TYPES,
    UnknownFraiseTypeError,
    build_deployer,
)
from fraisier.deployers.scheduled import ScheduledDeployer
from fraisier.runners import LocalRunner

EXPECTED = {
    "api": APIDeployer,
    "etl": ETLDeployer,
    "scheduled": ScheduledDeployer,
    "backup": ScheduledDeployer,
    "docker_compose": DockerComposeDeployer,
}


@pytest.mark.parametrize("fraise_type", sorted(EXPECTED))
def test_build_deployer_returns_the_right_class(fraise_type: str):
    deployer = build_deployer(
        fraise_type,
        {"fraise_name": "f", "environment": "production", "app_path": "/tmp/f"},
        runner=LocalRunner(),
    )
    assert isinstance(deployer, EXPECTED[fraise_type])


def test_the_table_is_the_known_set():
    """A type that can be deployed is one every entry point can deploy."""
    assert set(EXPECTED) == FRAISE_TYPES


@pytest.mark.parametrize("fraise_type", ["bogus", "", None])
def test_an_unknown_type_raises_and_names_the_known_set(fraise_type: str | None):
    with pytest.raises(UnknownFraiseTypeError) as exc:
        build_deployer(
            fraise_type,
            {"fraise_name": "f", "environment": "production"},
            runner=LocalRunner(),
        )
    message = str(exc.value)
    assert repr(fraise_type) in message
    for known in FRAISE_TYPES:
        assert known in message


def test_a_scheduled_job_is_merged_into_the_config():
    """The CLI's per-job variant is part of the table, not a fourth copy."""
    deployer = build_deployer(
        "scheduled",
        {
            "fraise_name": "reports",
            "environment": "production",
            "app_path": "/tmp/reports",
            "jobs": {"nightly": {"systemd_timer": "reports-nightly.timer"}},
        },
        runner=LocalRunner(),
        job="nightly",
    )
    assert isinstance(deployer, ScheduledDeployer)
    assert deployer.config["job_name"] == "nightly"
    assert deployer.config["systemd_timer"] == "reports-nightly.timer"


@pytest.mark.parametrize("module", ["daemon.py", "webhook.py", "cli/_helpers.py"])
def test_no_entry_point_keeps_its_own_table(module: str):
    """A branch on ``fraise_type`` outside the registry is a fourth copy."""
    source = (Path(__file__).parent.parent / "fraisier" / module).read_text()
    stray = re.search(r"fraise_type\s*(==|in\s*\()", source)
    assert stray is None, (
        f"fraisier/{module} branches on the fraise type again — the table "
        "belongs to deployers/registry.py (#379)"
    )


class TestTheWebhookDispatchesEveryType:
    """A fraise the config accepts is a fraise both entry points deploy."""

    @staticmethod
    def _config(fraise_type: str) -> dict:
        return {
            "type": fraise_type,
            "fraise_name": "reports",
            "environment": "production",
            "app_path": "/tmp/reports",
            "systemd_timer": "reports.timer",
        }

    @pytest.mark.asyncio
    async def test_a_scheduled_fraise_is_deployed(self, tmp_path, monkeypatch):
        """It was answered `deployment_triggered` and then dropped (#379)."""
        from unittest.mock import MagicMock, patch

        from fraisier import webhook

        config = MagicMock()
        config.get_deploy_user.return_value = "deployer"
        monkeypatch.setattr(webhook, "get_config", lambda: config)

        with patch.object(ScheduledDeployer, "execute") as execute:
            execute.return_value = MagicMock(success=True, new_version="abc")
            await webhook._run_deployment(
                fraise_name="reports",
                environment="production",
                fraise_config=self._config("scheduled"),
                webhook_id=None,
                git_branch="main",
                git_commit="abc",
                db=MagicMock(),
            )

        execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_an_unknown_type_leaves_a_failed_record(self, tmp_path, monkeypatch):
        """Never answer "triggered" and then vanish without a trace."""
        from unittest.mock import MagicMock

        from fraisier import status as status_mod
        from fraisier import webhook

        config = MagicMock()
        config.get_deploy_user.return_value = "deployer"
        monkeypatch.setattr(webhook, "get_config", lambda: config)

        written: list = []
        monkeypatch.setattr(webhook, "write_status", written.append)

        await webhook._run_deployment(
            fraise_name="reports",
            environment="production",
            fraise_config=self._config("bogus"),
            webhook_id=None,
            git_branch="main",
            git_commit="abc",
            db=MagicMock(),
        )

        assert len(written) == 1
        assert written[0].state == "failed"
        assert written[0].state in status_mod.FAILURE_STATES
        assert "bogus" in (written[0].error_message or "")
