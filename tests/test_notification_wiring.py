"""Tests that notification dispatch is wired into the deployment lifecycle."""

from unittest.mock import MagicMock, patch

from fraisier.deployers.etl import ETLDeployer


def _make_etl_deployer(notifications_config=None):
    config = {
        "fraise_name": "pipeline",
        "environment": "prod",
        "app_path": "/tmp/test",
        "branch": "main",
    }
    if notifications_config:
        config["notifications"] = notifications_config
    runner = MagicMock()
    return ETLDeployer(config, runner=runner)


class TestLifecycleNotification:
    def test_success_triggers_notify(self, test_db):
        deployer = _make_etl_deployer(
            {"on_success": [{"type": "webhook", "url": "https://example.com"}]}
        )
        with (
            patch.object(deployer, "_git_pull", return_value=("aaa", "bbb")),
            patch.object(deployer, "_notify") as mock_notify,
        ):
            result = deployer.execute()

        assert result.success
        mock_notify.assert_called_once_with(result)

    def test_failure_triggers_notify(self, test_db):
        deployer = _make_etl_deployer(
            {"on_failure": [{"type": "webhook", "url": "https://example.com"}]}
        )
        with (
            patch.object(deployer, "_git_pull", side_effect=RuntimeError("fail")),
            patch.object(deployer, "_notify") as mock_notify,
        ):
            result = deployer.execute()

        assert not result.success
        mock_notify.assert_called_once_with(result)

    def test_no_notifications_config_does_not_error(self, test_db):
        deployer = _make_etl_deployer()
        with patch.object(deployer, "_git_pull", return_value=("aaa", "bbb")):
            result = deployer.execute()
        assert result.success

    def test_dispatcher_is_configured_from_config(self):
        deployer = _make_etl_deployer(
            {"on_failure": [{"type": "slack", "webhook_url": "https://x"}]}
        )
        assert deployer._dispatcher.is_configured
