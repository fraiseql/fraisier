"""Tests for config values being properly wired to runtime behaviour."""

import warnings
from unittest.mock import MagicMock, patch

from fraisier.deployers.api import APIDeployer


def _make_deployer(tmp_path, **overrides):
    config = {
        "fraise_name": "myapi",
        "environment": "production",
        "app_path": "/srv/myapi",
        "clone_url": "git@github.com:org/myapi.git",
        "branch": "main",
        "systemd_service": "myapi.service",
        "health_check": {
            "url": "http://localhost:8000/health",
            "timeout": 15,
            "retries": 3,
        },
        "repos_base": str(tmp_path / "repos"),
        "status_dir": str(tmp_path / "status"),
        **overrides,
    }
    return APIDeployer(config)


class TestHealthCheckRetriesWired:
    """health_check.retries config is used (not hardcoded)."""

    def test_retries_from_config_used(self, tmp_path):
        """Setting retries: 3 results in exactly 3 health check retries."""
        deployer = _make_deployer(tmp_path)

        with patch("fraisier.deployers.api.HealthCheckManager") as MockMgr:
            mock_result = MagicMock(success=True)
            MockMgr.return_value.check_with_retries.return_value = mock_result
            deployer._wait_for_health()

        call_kwargs = MockMgr.return_value.check_with_retries.call_args
        assert call_kwargs.kwargs["max_retries"] == 3

    def test_retries_default_when_not_configured(self, tmp_path):
        """Without retries in config, sensible default (5) is used."""
        deployer = _make_deployer(
            tmp_path,
            health_check={"url": "http://localhost:8000/health"},
        )

        with patch("fraisier.deployers.api.HealthCheckManager") as MockMgr:
            mock_result = MagicMock(success=True)
            MockMgr.return_value.check_with_retries.return_value = mock_result
            deployer._wait_for_health()

        call_kwargs = MockMgr.return_value.check_with_retries.call_args
        assert call_kwargs.kwargs["max_retries"] == 5


class TestHealthCheckTimeoutWired:
    """health_check.timeout config is passed to health checker."""

    def test_timeout_from_config_used(self, tmp_path):
        """Setting timeout: 15 passes 15s to the health checker."""
        deployer = _make_deployer(tmp_path)

        with patch("fraisier.deployers.api.HealthCheckManager") as MockMgr:
            mock_result = MagicMock(success=True)
            MockMgr.return_value.check_with_retries.return_value = mock_result
            deployer._wait_for_health()

        call_kwargs = MockMgr.return_value.check_with_retries.call_args
        assert call_kwargs.kwargs["timeout"] == 15


class TestDeprecationWarnings:
    """Dead config keys emit deprecation warnings during validation."""

    def test_webhook_secret_env_emits_warning(self, tmp_path):
        """Config with deployment.webhook_secret_env emits warning."""
        from fraisier.config import FraisierConfig

        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """\
deployment:
  webhook_secret_env: MY_SECRET
fraises: {}
"""
        )
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            cfg = FraisierConfig(str(config_file))
            _ = cfg.deployment  # triggers property access

        deprecation_msgs = [
            str(x.message) for x in w if issubclass(x.category, DeprecationWarning)
        ]
        assert any("webhook_secret_env" in m for m in deprecation_msgs)
