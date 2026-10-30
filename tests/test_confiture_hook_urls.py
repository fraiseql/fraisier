"""The confiture hooks fraisier registers connect through ``PgConn``.

``database.hooks.audit`` and ``database.hooks.backup`` hand confiture's
``AuditHook`` and ``BackupHook`` a ``database_url`` of their own. fraisier
passed it unread: ``AuditHook`` connected with it in process, so libpq filled
whatever it left out from the deploy's environment, and a URL ``PgConn``
refuses was only noticed when registration failed and the hook was skipped.

Now an enabled hook's URL is read before the migration and the deploy is
refused, naming the hook and the reason but never the URL; ``AuditHook`` gets
the pinned conninfo. ``BackupHook`` runs ``pg_dump`` with a copy of
``os.environ``, which confiture 1.43 gives no way to replace.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.config._lazy_env import LazyEnv
from fraisier.dbops._pgconn import PgConn
from fraisier.dbops.confiture import _register_migration_hooks, refuse_hook_urls
from fraisier.deployers.api import APIDeployer
from fraisier.errors import DeploymentError

_URL = "postgresql://acme:s3cret@/acme_db?host=/nonexistent/acme"
_REFUSED = f"{_URL}&keepalives=1"


def _audit(url: object) -> dict:
    return {"audit": {"enabled": True, "database_url": url, "signing_key": "k"}}


def _backup(url: object) -> dict:
    return {
        "backup": {"enabled": True, "database_url": url, "backup_dir": "/nonexistent"}
    }


def _registered_config(hooks: dict):
    migrator = MagicMock()
    _register_migration_hooks(migrator, hooks)
    (call,) = migrator.register_hook.call_args_list
    return call.args[1]._config


class TestRegistration:
    def test_the_audit_hook_connects_with_the_pinned_conninfo(self):
        config = _registered_config(_audit(_URL))

        assert config.database_url == PgConn.parse(_URL).conninfo()

    def test_an_envvar_audit_url_is_resolved_first(self, monkeypatch):
        monkeypatch.setenv("ACME_AUDIT_URL", _URL)

        config = _registered_config(_audit(LazyEnv("ACME_AUDIT_URL")))

        assert config.database_url == PgConn.parse(_URL).conninfo()

    def test_an_envvar_backup_url_is_resolved(self, monkeypatch):
        monkeypatch.setenv("ACME_BACKUP_URL", _URL)

        config = _registered_config(_backup(LazyEnv("ACME_BACKUP_URL")))

        assert config.database_url == _URL

    def test_registration_still_skips_a_hook_it_cannot_build(self, caplog):
        """``migrate_down`` registers hooks too: a rollback must not raise."""
        migrator = MagicMock()

        _register_migration_hooks(migrator, _audit(_REFUSED))

        migrator.register_hook.assert_not_called()
        assert "keepalives" in caplog.text
        assert "s3cret" not in caplog.text


class TestRefusal:
    @pytest.mark.parametrize(("hooks", "key"), [(_audit, "audit"), (_backup, "backup")])
    def test_a_url_pgconn_refuses_is_refused_naming_the_hook(self, hooks, key):
        with pytest.raises(ValueError, match="keepalives") as info:
            refuse_hook_urls(hooks(_REFUSED))

        assert str(info.value).startswith(f"database.hooks.{key}.database_url: ")

    @pytest.mark.parametrize("hooks", [_audit, _backup])
    def test_the_refusal_never_echoes_the_url(self, hooks):
        with pytest.raises(ValueError) as info:
            refuse_hook_urls(hooks(_REFUSED))

        assert "s3cret" not in str(info.value)
        assert "nonexistent" not in str(info.value)

    def test_an_ambient_service_is_refused(self, monkeypatch):
        monkeypatch.setenv("PGSERVICE", "acme")

        with pytest.raises(ValueError, match="PGSERVICE"):
            refuse_hook_urls(_backup(_URL))

    def test_the_audit_hook_is_held_to_the_in_process_door(self, monkeypatch):
        monkeypatch.setenv("PGTZ", "Asia/Tokyo")

        with pytest.raises(ValueError, match="PGTZ"):
            refuse_hook_urls(_audit(_URL))

    def test_an_unset_envvar_in_any_field_is_refused(self, monkeypatch):
        """Registration would skip the hook: the deploy must stop instead."""
        monkeypatch.delenv("ACME_SIGNING_KEY", raising=False)
        hooks = _audit(_URL)
        hooks["audit"]["signing_key"] = LazyEnv("ACME_SIGNING_KEY")

        with pytest.raises(ValueError, match="ACME_SIGNING_KEY") as info:
            refuse_hook_urls(hooks)

        assert str(info.value).startswith("database.hooks.audit: ")

    def test_a_disabled_hook_is_not_read(self):
        hooks = _audit(_REFUSED)
        hooks["audit"]["enabled"] = False

        refuse_hook_urls(hooks)

    def test_a_good_url_passes(self):
        refuse_hook_urls({**_audit(_URL), **_backup(_URL)})


class TestTheDeployRefusesBeforeTheStrategy:
    def test_a_refused_hook_url_stops_the_deploy_before_the_migration(
        self, tmp_path: Path
    ):
        deployer = APIDeployer(
            {"app_path": str(tmp_path), "database": {"hooks": _audit(_REFUSED)}}
        )
        strategy = MagicMock()

        with (
            patch.object(
                deployer,
                "_resolve_strategy",
                return_value=(strategy, Path("c.yaml"), Path("db/m"), None),
            ),
            pytest.raises(DeploymentError, match=r"database\.hooks\.audit"),
        ):
            deployer._run_strategy()

        strategy.execute.assert_not_called()

    def test_an_unset_envvar_url_stops_the_deploy_too(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.delenv("ACME_AUDIT_URL", raising=False)
        deployer = APIDeployer(
            {
                "app_path": str(tmp_path),
                "database": {"hooks": _audit(LazyEnv("ACME_AUDIT_URL"))},
            }
        )
        strategy = MagicMock()

        with (
            patch.object(
                deployer,
                "_resolve_strategy",
                return_value=(strategy, Path("c.yaml"), Path("db/m"), None),
            ),
            pytest.raises(DeploymentError, match="ACME_AUDIT_URL"),
        ):
            deployer._run_strategy()

        strategy.execute.assert_not_called()
