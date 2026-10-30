"""A restore refuses a connection it would fail on before anything is dropped.

The dump source stops the service, drops and recreates the database, then
restores and migrates. A URL that only fails at the restore (a multi-host
``admin_url``) or at the migration (a refusal of the in-process connection)
used to fail there, with the target empty and its service stopped.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fraisier.config._validation import _validate_restore_migrate
from fraisier.errors import DatabaseError
from fraisier.strategies._restore import RestoreConfig, RestoreMigrateStrategy

MULTI_HOST = "postgresql://postgres@h1,h2/postgres"


#: A socket directory that does not exist: a step that slips past a refusal
#: fails to connect instead of reaching a real cluster.
NOWHERE = "postgresql:///postgres?host=/nonexistent/acme"


def _strategy(admin_url: str = NOWHERE, **config):
    source = MagicMock()
    service_manager = MagicMock()
    strategy = RestoreMigrateStrategy(
        RestoreConfig(db_name="staging", backup_dir=Path("/b"), **config),
        admin_url=admin_url,
        service_manager=service_manager,
        service_name="api",
        source=source,
    )
    return strategy, source, service_manager


class TestAMultiHostAdminUrl:
    def test_is_refused_before_anything_runs(self):
        with pytest.raises(ValueError, match="more than one host"):
            _strategy(admin_url=MULTI_HOST)

    def test_is_reported_by_validate(self):
        db = {
            "name": "staging",
            "admin_url": MULTI_HOST,
            "restore": {"backup_dir": "/b"},
        }

        errors = _validate_restore_migrate("api", db)

        assert any("admin_url" in e and "more than one host" in e for e in errors)


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "staging.yaml"
    path.write_text(
        "name: staging\ndatabase_url: postgresql:///staging?host=/nonexistent/acme\n"
    )
    return path


class TestTheMigrationConnection:
    """``migrate up`` runs after the drop; its connection is checked first."""

    def test_a_refused_database_url(self, config, monkeypatch):
        strategy, source, service_manager = _strategy()
        monkeypatch.setenv("PGSSLMODE", "verify-full")

        with pytest.raises(DatabaseError, match="sslmode=verify-full"):
            strategy.execute(config, database_url="postgresql://db.acme.test/staging")
        source.prepare.assert_not_called()
        assert service_manager.method_calls == []

    def test_a_refused_config_url_when_the_deploy_names_none(self, config):
        config.write_text("name: staging\ndatabase_url: postgresql:///s?keepalives=1\n")
        strategy, source, _ = _strategy()

        with pytest.raises(DatabaseError, match="keepalives"):
            strategy.execute(config)
        source.prepare.assert_not_called()

    def test_a_startup_setting_libpq_would_send(self, config, monkeypatch):
        strategy, source, _ = _strategy()
        monkeypatch.setenv("PGDATESTYLE", "SQL, DMY")

        with pytest.raises(DatabaseError, match="PGDATESTYLE"):
            strategy.execute(
                config, database_url="postgresql:///staging?host=/nonexistent/acme"
            )
        source.prepare.assert_not_called()

    def test_the_step_8_child_when_the_deploy_names_no_url(self, config):
        """``template: auto`` runs ``migrate preflight`` on the config's URLs."""
        config.write_text(
            "name: staging\ndatabase_url: postgresql:///staging?host=/nonexistent/acme\n"
            "scratch_url: postgresql:///scratch?keepalives=1\n"
        )
        strategy, source, _ = _strategy(template="auto")

        with pytest.raises(DatabaseError, match=r"scratch_url.*keepalives"):
            strategy.execute(config)
        source.prepare.assert_not_called()
