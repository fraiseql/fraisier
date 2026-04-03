"""Tests for lifecycle hooks: base, backup, audit, dispatcher, and config."""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from fraisier.hooks.audit import AuditHook
from fraisier.hooks.backup import BackupHook
from fraisier.hooks.base import (
    Hook,
    HookAbortError,
    HookContext,
    HookPhase,
    HookResult,
    HookRunner,
)
from fraisier.hooks.dispatcher import build_hook_runner


def _context(phase=HookPhase.BEFORE_DEPLOY, **kwargs):
    defaults = {
        "fraise_name": "api",
        "environment": "prod",
        "phase": phase,
    }
    defaults.update(kwargs)
    return HookContext(**defaults)


class _StubHook:
    def __init__(self, name="stub", success=True, error=None):
        self._name = name
        self._success = success
        self._error = error
        self.calls = []

    @property
    def name(self):
        return self._name

    def execute(self, context):
        self.calls.append(context)
        return HookResult(
            success=self._success,
            hook_name=self._name,
            error=self._error,
        )


class TestHookProtocol:
    def test_stub_satisfies_protocol(self):
        hook = _StubHook()
        assert isinstance(hook, Hook)


class TestHookRunner:
    def test_empty_runner_not_configured(self):
        runner = HookRunner()
        assert not runner.is_configured

    def test_register_makes_configured(self):
        runner = HookRunner()
        runner.register(HookPhase.BEFORE_DEPLOY, _StubHook())
        assert runner.is_configured

    def test_run_calls_hooks_in_order(self):
        runner = HookRunner()
        h1 = _StubHook("first")
        h2 = _StubHook("second")
        runner.register(HookPhase.AFTER_DEPLOY, h1)
        runner.register(HookPhase.AFTER_DEPLOY, h2)

        ctx = _context(HookPhase.AFTER_DEPLOY)
        results = runner.run(HookPhase.AFTER_DEPLOY, ctx)

        assert len(results) == 2
        assert results[0].hook_name == "first"
        assert results[1].hook_name == "second"
        assert len(h1.calls) == 1
        assert len(h2.calls) == 1

    def test_before_deploy_failure_raises(self):
        runner = HookRunner()
        runner.register(
            HookPhase.BEFORE_DEPLOY,
            _StubHook("bad", success=False, error="disk full"),
        )

        with pytest.raises(HookAbortError, match="disk full"):
            runner.run(HookPhase.BEFORE_DEPLOY, _context())

    def test_before_deploy_exception_raises(self):
        runner = HookRunner()
        hook = MagicMock()
        hook.name = "boom"
        hook.execute.side_effect = RuntimeError("crash")

        runner.register(HookPhase.BEFORE_DEPLOY, hook)

        with pytest.raises(HookAbortError, match="crash"):
            runner.run(HookPhase.BEFORE_DEPLOY, _context())

    def test_after_deploy_failure_swallowed(self):
        runner = HookRunner()
        runner.register(
            HookPhase.AFTER_DEPLOY,
            _StubHook("bad", success=False, error="oops"),
        )
        good = _StubHook("good")
        runner.register(HookPhase.AFTER_DEPLOY, good)

        results = runner.run(
            HookPhase.AFTER_DEPLOY,
            _context(HookPhase.AFTER_DEPLOY),
        )
        assert len(results) == 2
        assert not results[0].success
        assert results[1].success
        assert len(good.calls) == 1

    def test_on_failure_exception_swallowed(self):
        runner = HookRunner()
        hook = MagicMock()
        hook.name = "crashy"
        hook.execute.side_effect = RuntimeError("boom")
        runner.register(HookPhase.ON_FAILURE, hook)

        results = runner.run(
            HookPhase.ON_FAILURE,
            _context(HookPhase.ON_FAILURE),
        )
        assert len(results) == 1
        assert not results[0].success

    def test_no_hooks_returns_empty(self):
        runner = HookRunner()
        results = runner.run(HookPhase.BEFORE_DEPLOY, _context())
        assert results == []


class TestBackupHook:
    @patch("fraisier.hooks.backup.subprocess.run")
    def test_creates_compressed_backup(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(
            stdout=b"CREATE TABLE foo;",
            returncode=0,
        )

        hook = BackupHook(
            backup_dir=tmp_path,
            database_url="postgresql://localhost/mydb",
        )
        result = hook.execute(_context())

        assert result.success
        backups = list(tmp_path.glob("*.sql.gz"))
        assert len(backups) == 1
        assert "api_prod_" in backups[0].name

    @patch("fraisier.hooks.backup.subprocess.run")
    def test_creates_uncompressed_backup(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(
            stdout=b"CREATE TABLE foo;",
            returncode=0,
        )

        hook = BackupHook(
            backup_dir=tmp_path,
            database_url="postgresql://localhost/mydb",
            compress=False,
        )
        result = hook.execute(_context())

        assert result.success
        backups = list(tmp_path.glob("*.sql"))
        assert len(backups) == 1

    @patch("fraisier.hooks.backup.subprocess.run")
    def test_pg_dump_failure(self, mock_run, tmp_path):
        import subprocess

        mock_run.side_effect = subprocess.CalledProcessError(
            1, "pg_dump", stderr=b"connection refused"
        )

        hook = BackupHook(
            backup_dir=tmp_path,
            database_url="postgresql://localhost/mydb",
        )
        result = hook.execute(_context())

        assert not result.success
        assert "pg_dump failed" in result.error

    @patch("fraisier.hooks.backup.subprocess.run")
    def test_retention_policy(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(
            stdout=b"data",
            returncode=0,
        )

        hook = BackupHook(
            backup_dir=tmp_path,
            database_url="postgresql://localhost/mydb",
            max_backups=2,
        )

        # Create 3 backups
        for i in range(3):
            # Create pre-existing files with different timestamps
            (tmp_path / f"old_{i}.sql.gz").write_bytes(b"old")

        hook.execute(_context())

        # Should keep max_backups (2) + the new one = but only 2 total
        backups = list(tmp_path.glob("*.sql.gz"))
        assert len(backups) == 2


class TestAuditHook:
    def test_writes_audit_record(self, tmp_path):
        db_path = str(tmp_path / "audit.db")
        hook = AuditHook(
            database_path=db_path,
            signing_key="test-secret",
        )

        ctx = _context(
            HookPhase.AFTER_DEPLOY,
            old_version="abc123",
            new_version="def456",
        )
        result = hook.execute(ctx)

        assert result.success

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT fraise, environment, phase, old_version, "
            "new_version, signature FROM fraisier_audit_log"
        ).fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0][0] == "api"
        assert rows[0][1] == "prod"
        assert rows[0][2] == "after_deploy"
        assert rows[0][3] == "abc123"
        assert rows[0][4] == "def456"
        assert rows[0][5]  # signature is non-empty

    def test_writes_failure_record(self, tmp_path):
        db_path = str(tmp_path / "audit.db")
        hook = AuditHook(
            database_path=db_path,
            signing_key="test-secret",
        )

        ctx = _context(
            HookPhase.ON_FAILURE,
            error_message="Health check failed",
        )
        result = hook.execute(ctx)

        assert result.success

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT phase, error_message FROM fraisier_audit_log"
        ).fetchone()
        conn.close()

        assert row[0] == "on_failure"
        assert row[1] == "Health check failed"

    def test_signature_verification(self, tmp_path):
        db_path = str(tmp_path / "audit.db")
        key = "my-secret-key"
        hook = AuditHook(database_path=db_path, signing_key=key)

        ctx = _context(HookPhase.AFTER_DEPLOY, new_version="abc123")
        hook.execute(ctx)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT fraise, environment, phase, old_version, "
            "new_version, error_message, executed_at, signature "
            "FROM fraisier_audit_log"
        ).fetchone()
        conn.close()

        record = {
            "fraise": row[0],
            "environment": row[1],
            "phase": row[2],
            "old_version": row[3],
            "new_version": row[4],
            "error_message": row[5],
            "executed_at": row[6],
        }
        assert AuditHook.verify_signature(record, row[7], key)
        assert not AuditHook.verify_signature(record, row[7], "wrong-key")

    def test_tampered_record_fails_verification(self, tmp_path):
        db_path = str(tmp_path / "audit.db")
        key = "my-secret-key"
        hook = AuditHook(database_path=db_path, signing_key=key)

        ctx = _context(HookPhase.AFTER_DEPLOY)
        hook.execute(ctx)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT fraise, environment, phase, old_version, "
            "new_version, error_message, executed_at, signature "
            "FROM fraisier_audit_log"
        ).fetchone()
        conn.close()

        record = {
            "fraise": "tampered",
            "environment": row[1],
            "phase": row[2],
            "old_version": row[3],
            "new_version": row[4],
            "error_message": row[5],
            "executed_at": row[6],
        }
        assert not AuditHook.verify_signature(record, row[7], key)


class TestHookDispatcher:
    def test_empty_config(self):
        runner = build_hook_runner({})
        assert not runner.is_configured

    def test_backup_hook_registered(self):
        config = {
            "hooks": {
                "before_deploy": [
                    {
                        "type": "backup",
                        "backup_dir": "/tmp/backups",
                        "database_url": "postgresql://localhost/db",
                    }
                ]
            }
        }
        runner = build_hook_runner(config)
        assert runner.is_configured
        assert len(runner._hooks[HookPhase.BEFORE_DEPLOY]) == 1
        assert isinstance(runner._hooks[HookPhase.BEFORE_DEPLOY][0], BackupHook)

    def test_audit_hook_registered(self):
        config = {
            "hooks": {
                "after_deploy": [
                    {
                        "type": "audit",
                        "database_path": "/tmp/audit.db",
                        "signing_key": "secret",
                    }
                ]
            }
        }
        runner = build_hook_runner(config)
        assert len(runner._hooks[HookPhase.AFTER_DEPLOY]) == 1
        assert isinstance(runner._hooks[HookPhase.AFTER_DEPLOY][0], AuditHook)

    def test_multiple_phases(self):
        config = {
            "hooks": {
                "before_deploy": [
                    {
                        "type": "backup",
                        "backup_dir": "/tmp/backups",
                        "database_url": "postgresql://localhost/db",
                    }
                ],
                "after_deploy": [
                    {
                        "type": "audit",
                        "database_path": "/tmp/audit.db",
                        "signing_key": "secret",
                    }
                ],
                "on_failure": [
                    {
                        "type": "audit",
                        "database_path": "/tmp/audit.db",
                        "signing_key": "secret",
                    }
                ],
            }
        }
        runner = build_hook_runner(config)
        assert len(runner._hooks[HookPhase.BEFORE_DEPLOY]) == 1
        assert len(runner._hooks[HookPhase.AFTER_DEPLOY]) == 1
        assert len(runner._hooks[HookPhase.ON_FAILURE]) == 1

    def test_unknown_hook_type_raises(self):
        config = {"hooks": {"before_deploy": [{"type": "unknown_hook"}]}}
        with pytest.raises(ValueError, match="unknown_hook"):
            build_hook_runner(config)

    def test_env_var_expansion(self, monkeypatch):
        monkeypatch.setenv("DB_URL", "postgresql://real-host/db")
        config = {
            "hooks": {
                "before_deploy": [
                    {
                        "type": "backup",
                        "backup_dir": "/tmp/backups",
                        "database_url": "${DB_URL}",
                    }
                ]
            }
        }
        runner = build_hook_runner(config)
        hook = runner._hooks[HookPhase.BEFORE_DEPLOY][0]
        assert hook.database_url == "postgresql://real-host/db"


class TestHookConfigValidation:
    def test_valid_backup_hook(self, tmp_path):
        from fraisier.config import FraisierConfig

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
git:
  provider: github
hooks:
  before_deploy:
    - type: backup
      backup_dir: /var/backups
      database_url: postgresql://localhost/mydb
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""")
        config = FraisierConfig(str(cfg))
        assert config._config.get("hooks") is not None

    def test_valid_audit_hook(self, tmp_path):
        from fraisier.config import FraisierConfig

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
git:
  provider: github
hooks:
  after_deploy:
    - type: audit
      database_path: /var/lib/fraisier/audit.db
      signing_key: my-secret
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""")
        config = FraisierConfig(str(cfg))
        assert config._config.get("hooks") is not None

    def test_invalid_hook_type(self, tmp_path):
        from fraisier.config import FraisierConfig
        from fraisier.errors import ValidationError

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
git:
  provider: github
hooks:
  before_deploy:
    - type: fax_machine
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""")
        with pytest.raises(ValidationError, match="fax_machine"):
            FraisierConfig(str(cfg))

    def test_invalid_hook_phase(self, tmp_path):
        from fraisier.config import FraisierConfig
        from fraisier.errors import ValidationError

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
git:
  provider: github
hooks:
  during_deploy:
    - type: backup
      backup_dir: /tmp
      database_url: postgresql://localhost/db
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""")
        with pytest.raises(ValidationError, match="during_deploy"):
            FraisierConfig(str(cfg))

    def test_backup_missing_required_fields(self, tmp_path):
        from fraisier.config import FraisierConfig
        from fraisier.errors import ValidationError

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
git:
  provider: github
hooks:
  before_deploy:
    - type: backup
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""")
        with pytest.raises(ValidationError, match="backup_dir"):
            FraisierConfig(str(cfg))

    def test_audit_missing_signing_key(self, tmp_path):
        from fraisier.config import FraisierConfig
        from fraisier.errors import ValidationError

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
git:
  provider: github
hooks:
  after_deploy:
    - type: audit
      database_path: /tmp/audit.db
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""")
        with pytest.raises(ValidationError, match="signing_key"):
            FraisierConfig(str(cfg))

    def test_no_hooks_section_is_valid(self, tmp_path):
        from fraisier.config import FraisierConfig

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
git:
  provider: github
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""")
        FraisierConfig(str(cfg))
