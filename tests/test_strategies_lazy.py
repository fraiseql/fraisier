"""Strategy entry-point LazyEnv resolution (#220 Phase 5 Cycle 5.5).

`database_url` and `admin_url` are resolved at strategy `.execute()`
entry — once, into a concrete ``str`` — so the ~70 downstream
``connection_url=`` propagation sites in ``dbops/`` and ``strategies/``
keep their ``str`` parameter contract. A LazyEnv reaching any of those
inner sites is a contract violation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fraisier.config._lazy_env import LazyEnv
from fraisier.dbops._url import resolve_db_url
from fraisier.errors import ConfigurationError


class TestResolveDbUrl:
    def test_none_passes_through(self):
        assert resolve_db_url(None) is None

    def test_str_passes_through(self):
        assert resolve_db_url("postgresql:///mydb") == "postgresql:///mydb"

    def test_lazyenv_resolves(self, monkeypatch):
        monkeypatch.setenv("DB_URL", "postgresql:///resolved")
        assert (
            resolve_db_url(LazyEnv("DB_URL", "fraises.api.prod.database_url"))
            == "postgresql:///resolved"
        )

    def test_unset_lazyenv_raises_with_path(self, monkeypatch):
        monkeypatch.delenv("DB_URL", raising=False)
        with pytest.raises(
            ConfigurationError,
            match=r"DB_URL.*fraises\.api\.prod\.database_url",
        ):
            resolve_db_url(LazyEnv("DB_URL", "fraises.api.prod.database_url"))

    def test_role_is_informational_only(self, monkeypatch):
        # role doesn't change resolution; it's there for grep
        # traceability.
        monkeypatch.setenv("DB_URL", "x")
        assert resolve_db_url(LazyEnv("DB_URL", "p"), role="admin_url") == "x"
        assert resolve_db_url(LazyEnv("DB_URL", "p"), role="database_url") == "x"


class TestMigrateStrategyResolvesEntry:
    def test_lazy_database_url_resolves_at_entry(self, monkeypatch, tmp_path):
        from fraisier.strategies._core import MigrateStrategy

        monkeypatch.setenv("DB_URL", "postgresql:///resolved")

        captured: dict = {}

        def fake_preflight(*args, **kwargs):
            captured["preflight_db_url"] = kwargs.get("database_url")

        def fake_migrate_up(*args, **kwargs):
            captured["migrate_db_url"] = kwargs.get("database_url")
            from fraisier.dbops.confiture import MigrationResult

            return MigrationResult(success=True, steps_applied=0, errors=[])

        with (
            patch("fraisier.strategies._core.preflight", side_effect=fake_preflight),
            patch(
                "fraisier.strategies._core.migrate_up",
                side_effect=fake_migrate_up,
            ),
        ):
            strategy = MigrateStrategy()
            strategy.execute(
                tmp_path / "confiture.yaml",
                database_url=LazyEnv("DB_URL", "fraises.api.prod.database_url"),
            )

        # Downstream functions receive the resolved string, not a
        # LazyEnv — the strategy entry is the boundary.
        assert captured["preflight_db_url"] == "postgresql:///resolved"
        assert captured["migrate_db_url"] == "postgresql:///resolved"
        assert not isinstance(captured["preflight_db_url"], LazyEnv)

    def test_unset_lazy_database_url_raises_at_entry_with_path(
        self, monkeypatch, tmp_path
    ):
        from fraisier.strategies._core import MigrateStrategy

        monkeypatch.delenv("DB_URL", raising=False)

        def must_not_call(*args, **kwargs):  # pragma: no cover
            raise AssertionError("downstream must not run when LazyEnv unset")

        with (
            patch("fraisier.strategies._core.preflight", side_effect=must_not_call),
            patch("fraisier.strategies._core.migrate_up", side_effect=must_not_call),
        ):
            strategy = MigrateStrategy()
            with pytest.raises(
                ConfigurationError,
                match=r"DB_URL.*fraises\.api\.prod\.database_url",
            ):
                strategy.execute(
                    tmp_path / "confiture.yaml",
                    database_url=LazyEnv("DB_URL", "fraises.api.prod.database_url"),
                )

    def test_rollback_resolves_at_entry(self, monkeypatch, tmp_path):
        from fraisier.strategies._core import MigrateStrategy

        monkeypatch.setenv("DB_URL", "postgresql:///resolved")
        captured: dict = {}

        def fake_migrate_down(*args, **kwargs):
            captured["db_url"] = kwargs.get("database_url")
            from fraisier.dbops.confiture import MigrationResult

            return MigrationResult(success=True, steps_applied=1, errors=[])

        with patch(
            "fraisier.strategies._core.migrate_down", side_effect=fake_migrate_down
        ):
            MigrateStrategy().rollback(
                Path(tmp_path / "confiture.yaml"),
                steps=1,
                database_url=LazyEnv("DB_URL", "fraises.api.prod.database_url"),
            )

        assert captured["db_url"] == "postgresql:///resolved"


class TestRebuildStrategyResolvesEntry:
    def test_lazy_admin_url_resolves_at_execute(self, monkeypatch):
        # admin_url passed at __init__ time is stored, then resolved
        # at execute() time (kept lazy so construction has no side
        # effects on the env).
        from fraisier.strategies._core import RebuildStrategy

        monkeypatch.setenv("ADMIN", "postgresql:///postgres")
        strategy = RebuildStrategy(
            admin_url=LazyEnv("ADMIN", "fraises.api.prod.admin_url"),
        )
        # Direct check: the stored value is still lazy.
        assert isinstance(strategy._admin_url, LazyEnv)
