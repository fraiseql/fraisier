"""The confiture hooks fraisier registers connect through ``PgConn``.

``database.hooks.audit`` and ``database.hooks.backup`` hand confiture's
``AuditHook`` and ``BackupHook`` a ``database_url`` of their own. fraisier
passed it unread: ``AuditHook`` connected with it in process, so libpq filled
whatever it left out from the deploy's environment, and a URL ``PgConn``
refuses was only noticed when registration failed and the hook was skipped.

Now an enabled hook's URL is read before the migration and the deploy is
refused, naming the hook and the reason but never the URL; ``AuditHook`` gets
a connection factory that opens the pinned conninfo and ``BackupHook`` the
child environment, so neither reads an ambient ``PG*`` variable.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from confiture.core.hooks import HookContext, HookPhase
from confiture.core.hooks.builtin import (
    AuditConfig,
    AuditHook,
    BackupConfig,
    BackupHook,
)
from confiture.core.hooks.context import ExecutionContext
from psycopg.conninfo import conninfo_to_dict

from fraisier.config import AuditHookConfig, BackupHookConfig
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
    def test_the_audit_hook_connects_with_the_pinned_conninfo(self, monkeypatch):
        opened = []
        monkeypatch.setattr(psycopg, "connect", opened.append)

        AuditHook(_registered_config(_audit(_URL)))._connect()

        assert opened == [PgConn.parse(_URL).conninfo()]

    def test_an_envvar_audit_url_is_resolved_first(self, monkeypatch):
        monkeypatch.setenv("ACME_AUDIT_URL", _URL)

        config = _registered_config(_audit(LazyEnv("ACME_AUDIT_URL")))

        assert config.database_url == _URL

    def test_an_envvar_backup_url_is_resolved(self, monkeypatch):
        monkeypatch.setenv("ACME_BACKUP_URL", _URL)

        config = _registered_config(_backup(LazyEnv("ACME_BACKUP_URL")))

        assert config.database_url == _URL
        assert config.env == PgConn.parse(_URL).child_env()

    def test_registration_still_skips_a_hook_it_cannot_build(self, caplog):
        """``migrate_down`` registers hooks too: a rollback must not raise."""
        migrator = MagicMock()

        _register_migration_hooks(migrator, _audit(_REFUSED))

        migrator.register_hook.assert_not_called()
        assert "keepalives" in caplog.text
        assert "s3cret" not in caplog.text


_AMBIENT = {"PGHOSTADDR": "192.0.2.1", "PGOPTIONS": "-c search_path=acme_other"}


def _context(**metadata: object) -> HookContext[ExecutionContext]:
    return HookContext(
        phase=HookPhase.BEFORE_EXECUTE,
        data=ExecutionContext(metadata={"migration_name": "001_acme", **metadata}),
    )


class TestTheHooksDoNotReadTheDeployEnvironment:
    @pytest.fixture
    def pg_dump(self, tmp_path, monkeypatch):
        """A ``pg_dump`` on PATH that records its environment and argv."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        record = tmp_path / "pg_dump.record"
        stub = bin_dir / "pg_dump"
        stub.write_text(
            "#!/bin/bash\n"
            'if [ "$1" = --version ]; then echo "pg_dump (PostgreSQL) 18.0"; exit; fi\n'
            f'{{ env | grep "^PG"; echo "argv $*"; }} > "{record}"\n'
            'for a in "$@"; do [ "$prev" = -f ] && : > "$a"; prev="$a"; done\n'
        )
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
        return record

    def test_the_backup_pg_dump_gets_the_child_environment(
        self, pg_dump, tmp_path, monkeypatch
    ):
        for var, value in _AMBIENT.items():
            monkeypatch.setenv(var, value)
        hooks = _backup(_URL)
        hooks["backup"]["backup_dir"] = str(tmp_path / "backups")

        result = asyncio.run(BackupHook(_registered_config(hooks)).execute(_context()))

        assert result.success, result.error
        lines = pg_dump.read_text().splitlines()
        assert "PGHOST=/nonexistent/acme" in lines
        assert "PGPASSWORD=s3cret" in lines
        assert not [ln for ln in lines if ln.startswith(tuple(_AMBIENT))]
        assert "s3cret" not in next(ln for ln in lines if ln.startswith("argv "))

    def test_the_audit_connection_ignores_ambient_variables(self, monkeypatch):
        for var, value in _AMBIENT.items():
            monkeypatch.setenv(var, value)
        opened = []
        monkeypatch.setattr(psycopg, "connect", opened.append)

        AuditHook(_registered_config(_audit(_URL)))._connect()

        (conninfo,) = opened
        params = conninfo_to_dict(conninfo)
        assert params.get("hostaddr", "") == ""
        assert params.get("options", "") == ""

    def test_the_audit_connection_refuses_an_ambient_service(self, monkeypatch):
        hook = AuditHook(_registered_config(_audit(_URL)))
        monkeypatch.setenv("PGSERVICE", "acme")

        with pytest.raises(ValueError, match="PGSERVICE"):
            hook._connect()

    def test_a_failed_audit_connect_is_the_hooks_failure_not_the_migrations(self):
        """``AuditHook`` reports a ``psycopg.Error``; anything else escapes it."""
        hook = AuditHook(_registered_config(_audit(_URL)))

        result = asyncio.run(hook.execute(_context()))

        assert not result.success
        assert "s3cret" not in (result.error or "")

    def test_an_environment_the_audit_door_refuses_skips_it_at_registration(
        self, monkeypatch, caplog
    ):
        """Not a hook whose factory raises mid-migration."""
        monkeypatch.setenv("PGTZ", "Asia/Tokyo")
        migrator = MagicMock()

        _register_migration_hooks(migrator, _audit(_URL))

        migrator.register_hook.assert_not_called()
        assert "PGTZ" in caplog.text


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


def _fields(cls: type) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


class TestTheSchemaDescribesWhatConfitureTakes:
    """``BackupHookConfig``/``AuditHookConfig`` describe ``database.hooks``.

    Each block reaches confiture's config class as keyword arguments, so a
    field confiture does not take makes registration skip the hook.
    """

    @pytest.mark.parametrize(
        ("ours", "theirs", "supplied"),
        [
            # fraisier supplies `env`; `compress` is deprecated for `compression`.
            (BackupHookConfig, BackupConfig, {"env", "compress"}),
            (AuditHookConfig, AuditConfig, {"connection_factory"}),
        ],
    )
    def test_the_fields_are_confitures(self, ours, theirs, supplied):
        assert _fields(ours) - {"enabled"} == _fields(theirs) - supplied

    @pytest.mark.parametrize(
        ("key", "block"),
        [
            ("backup", BackupHookConfig(enabled=True, database_url=_URL)),
            (
                "audit",
                AuditHookConfig(enabled=True, database_url=_URL, signing_key="k"),
            ),
        ],
    )
    def test_a_block_written_to_the_schema_registers(self, key, block):
        migrator = MagicMock()

        _register_migration_hooks(migrator, {key: dataclasses.asdict(block)})

        migrator.register_hook.assert_called_once()
