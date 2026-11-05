"""A deployer built for a deploy reads the server's fraises.yaml (#506).

``build_deployer`` is how the CLI, the daemon and the webhook build every
deployer, and it gives none of them a typed config. Four deploy steps read
``self.config_object`` and did nothing without it: pgBackRest's helper socket,
the scheduled-unit auto-install, the install fallback's ownership check, and
the unit named by the sandbox check. They now read the file the deploy
installs, ``FRAISIER_CONFIG`` or ``/opt/fraisier/fraises.yaml``.

Every deployer here is built through ``build_deployer``; none is handed a
config, because no deploy hands one.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import Mock, patch

import pytest
import yaml

from fraisier.deployers.registry import build_deployer
from fraisier.runners import LocalRunner

if TYPE_CHECKING:
    from pathlib import Path

_PGBACKREST_DB = {
    "name": "app",
    "strategy": "restore_migrate",
    "admin_url": "postgresql://postgres@localhost:5433/postgres",
    "confiture_config": "confiture.yaml",
    "restore": {
        "source": "pgbackrest",
        "pgbackrest": {"stanza": "main", "repo": 1, "cluster": "18/staging"},
    },
}


def _server_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg: dict) -> None:
    """Install *cfg* as the server's fraises.yaml, where a deploy reads it."""
    path = tmp_path / "opt" / "fraises.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("FRAISIER_CONFIG", str(path))


def _built(fraise_type: str, config: dict[str, Any]) -> Any:
    deployer = build_deployer(fraise_type, config, runner=LocalRunner())
    assert deployer.config_object is None
    return deployer


class TestPgBackRestRestoreGetsItsHelperSocket:
    _ENV: ClassVar[dict[str, Any]] = {
        "fraise_name": "api",
        "environment": "staging",
        "app_path": "/var/www/api",
        "systemd_service": "api.staging.service",
        "database": _PGBACKREST_DB,
    }

    def test_the_socket_is_named_from_the_deployed_project(self, tmp_path, monkeypatch):
        _server_config(
            tmp_path,
            monkeypatch,
            {
                "name": "proj",
                "fraises": {
                    "api": {"type": "api", "environments": {"staging": self._ENV}}
                },
            },
        )

        strategy, *_ = _built("api", self._ENV)._resolve_strategy()

        assert strategy._config.pgbackrest_socket == (
            "/run/fraisier/pgbackrest-proj-api-staging.sock"
        )

    def test_without_a_deployed_config_it_refuses_naming_the_file(
        self, tmp_path, monkeypatch
    ):
        from fraisier.errors import DeploymentError

        missing = tmp_path / "opt" / "fraises.yaml"
        monkeypatch.setenv("FRAISIER_CONFIG", str(missing))

        with pytest.raises(DeploymentError, match=re.escape(str(missing))):
            _built("api", self._ENV)._resolve_strategy()


class TestScheduledUnitsAutoInstallRuns:
    def test_the_hook_reaches_the_installer_socket_check(
        self, tmp_path, monkeypatch, caplog
    ):
        _server_config(
            tmp_path,
            monkeypatch,
            {
                "name": "proj",
                "scaffold": {"deploy_user": "deployer"},
                "fraises": {
                    "nightly": {
                        "type": "scheduled",
                        "environments": {
                            "production": {
                                "app_path": "/var/www/nightly",
                                "systemd_service": "nightly.service",
                                "systemd_timer": "nightly.timer",
                                "script_path": "/usr/local/bin/nightly.sh",
                            }
                        },
                    }
                },
            },
        )
        deployer = _built(
            "scheduled", {"environment": "production", "fraise_name": "nightly"}
        )

        with caplog.at_level(logging.WARNING, logger="fraisier"):
            deployer._auto_install_scheduled_units_if_applicable()

        assert "unit-installer socket not present" in caplog.text


class TestInstallFallbackChecksOwnership:
    def test_the_manifest_is_verified_before_sudo(self, tmp_path, monkeypatch):
        install = {"command": ["/usr/local/bin/uv", "sync"], "user": "appuser"}
        _server_config(
            tmp_path,
            monkeypatch,
            {
                "name": "proj",
                "scaffold": {"deploy_user": "deployer"},
                "fraises": {
                    "my_api": {
                        "type": "api",
                        "install": install,
                        "environments": {"production": {"app_path": "/var/www/prod"}},
                    }
                },
            },
        )
        deployer = _built(
            "api",
            {
                "fraise_name": "my_api",
                "environment": "production",
                "app_path": "/var/www/prod",
                "deploy_user": "deployer",
                "install": install,
            },
        )
        deployer.runner = Mock()

        with patch(
            "fraisier.deployers.preflight_ownership._verify_manifest_ownership"
        ) as verify:
            deployer._install_dependencies()

        verify.assert_called_once()


class TestSandboxRefusalNamesTheUnit:
    def test_the_webhook_unit_is_named_by_project(self, tmp_path, monkeypatch):
        from fraisier.errors import DeploymentError

        _server_config(
            tmp_path,
            monkeypatch,
            {
                "name": "proj",
                "fraises": {
                    "api": {
                        "type": "api",
                        "environments": {"production": {"app_path": "/var/www/api"}},
                    }
                },
            },
        )
        read_only = tmp_path / "app"
        read_only.mkdir()
        read_only.chmod(0o500)
        deployer = _built(
            "api",
            {
                "fraise_name": "api",
                "environment": "production",
                "app_path": str(read_only),
            },
        )

        try:
            with pytest.raises(
                DeploymentError, match=re.escape("fraisier-proj-webhook.service")
            ):
                deployer._validate_sandbox_writes()
        finally:
            read_only.chmod(0o700)
