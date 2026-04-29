"""Tests for PreflightConfig dataclass and preflight validation rules."""

from __future__ import annotations

import pytest

from fraisier.config._validation import _validate_environment
from fraisier.config.schema import PreflightConfig
from fraisier.errors import ValidationError

# ---------------------------------------------------------------------------
# PreflightConfig dataclass
# ---------------------------------------------------------------------------


class TestPreflightConfigDefaults:
    def test_enabled_default_is_true(self):
        cfg = PreflightConfig()
        assert cfg.enabled is True

    def test_timeout_seconds_default(self):
        cfg = PreflightConfig()
        assert cfg.timeout_seconds == 120


class TestPreflightConfigCustom:
    def test_enabled_can_be_disabled(self):
        cfg = PreflightConfig(enabled=False)
        assert cfg.enabled is False

    def test_timeout_seconds_settable(self):
        cfg = PreflightConfig(timeout_seconds=60)
        assert cfg.timeout_seconds == 60

    def test_all_custom_values(self):
        cfg = PreflightConfig(enabled=False, timeout_seconds=30)
        assert cfg.enabled is False
        assert cfg.timeout_seconds == 30


# ---------------------------------------------------------------------------
# Validation: preflight.enabled requires restore_migrate strategy
# ---------------------------------------------------------------------------


class TestPreflightValidationStrategy:
    def _db_with_preflight(self, strategy: str, preflight: dict, **extra_db) -> dict:
        return {
            "database": {
                "strategy": strategy,
                "database_url": "postgresql://localhost/mydb",
                "admin_url": "postgresql://admin@localhost/postgres",
                "restore": {"backup_dir": "/backups"},
                "name": "mydb",
                **extra_db,
                "preflight": preflight,
            }
        }

    def test_preflight_enabled_with_migrate_strategy_raises(self):
        env = {
            "database": {
                "strategy": "migrate",
                "database_url": "postgresql://localhost/mydb",
                "admin_url": "postgresql://admin@localhost/postgres",
                "preflight": {"enabled": True},
            }
        }
        with pytest.raises(ValidationError) as exc_info:
            _validate_environment("myfraise", env)
        msg = str(exc_info.value)
        assert "preflight" in msg.lower()
        assert "restore" in msg.lower()

    def test_preflight_enabled_with_restore_migrate_is_valid(self):
        env = self._db_with_preflight("restore_migrate", {"enabled": True})
        # Should not raise
        _validate_environment("myfraise", env)

    def test_preflight_disabled_with_migrate_strategy_is_valid(self):
        env = {
            "database": {
                "strategy": "migrate",
                "database_url": "postgresql://localhost/mydb",
                "preflight": {"enabled": False},
            }
        }
        # disabled preflight is fine with any strategy
        _validate_environment("myfraise", env)

    def test_no_preflight_block_is_always_valid(self):
        env = {
            "database": {
                "strategy": "migrate",
                "database_url": "postgresql://localhost/mydb",
            }
        }
        _validate_environment("myfraise", env)


# ---------------------------------------------------------------------------
# Validation: timeout_seconds must be positive
# ---------------------------------------------------------------------------


class TestPreflightValidationTimeout:
    def _restore_env_with_preflight(self, preflight: dict) -> dict:
        return {
            "database": {
                "strategy": "restore_migrate",
                "database_url": "postgresql://localhost/mydb",
                "admin_url": "postgresql://admin@localhost/postgres",
                "name": "mydb",
                "restore": {"backup_dir": "/backups"},
                "preflight": preflight,
            }
        }

    def test_negative_timeout_raises(self):
        env = self._restore_env_with_preflight({"timeout_seconds": -1})
        with pytest.raises(ValidationError) as exc_info:
            _validate_environment("myfraise", env)
        assert "timeout" in str(exc_info.value).lower()

    def test_zero_timeout_raises(self):
        env = self._restore_env_with_preflight({"timeout_seconds": 0})
        with pytest.raises(ValidationError) as exc_info:
            _validate_environment("myfraise", env)
        assert "timeout" in str(exc_info.value).lower()

    def test_positive_timeout_is_valid(self):
        env = self._restore_env_with_preflight({"timeout_seconds": 60})
        _validate_environment("myfraise", env)

    def test_default_timeout_not_validated_when_absent(self):
        env = self._restore_env_with_preflight({})
        _validate_environment("myfraise", env)


# ---------------------------------------------------------------------------
# PreflightConfig importable from fraisier.config (re-export)
# ---------------------------------------------------------------------------


class TestPreflightConfigImport:
    def test_importable_from_schema(self):
        from fraisier.config.schema import PreflightConfig as PC

        assert PC is not None

    def test_importable_from_config_package(self):
        from fraisier.config import PreflightConfig as PC

        assert PC is not None
