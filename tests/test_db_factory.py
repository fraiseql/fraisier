"""Unit tests for database factory and config validation."""

import pytest

from fraisier.db.factory import (
    DatabaseConfig,
    create_adapter_from_url,
    get_database_adapter,
    set_default_adapter,
)


class TestDatabaseConfig:
    def test_default_type_is_sqlite(self, monkeypatch):
        monkeypatch.delenv("FRAISIER_DB_TYPE", raising=False)
        config = DatabaseConfig()
        assert config.db_type == "sqlite"

    def test_explicit_type_override(self):
        config = DatabaseConfig(db_type="sqlite")
        assert config.db_type == "sqlite"

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError, match="Invalid FRAISIER_DB_TYPE"):
            DatabaseConfig(db_type="oracle")

    def test_pool_max_less_than_min_raises(self):
        with pytest.raises(ValueError, match="FRAISIER_DB_POOL_MAX"):
            DatabaseConfig(db_type="sqlite", pool_min_size=5, pool_max_size=2)

    def test_explicit_url_stored(self):
        config = DatabaseConfig(db_url="postgresql://localhost/test")
        assert config.db_url == "postgresql://localhost/test"

    def test_sqlite_default_path_is_memory(self, monkeypatch):
        monkeypatch.delenv("FRAISIER_DB_PATH", raising=False)
        monkeypatch.delenv("FRAISIER_DB_URL", raising=False)
        config = DatabaseConfig()
        assert config.db_path == ":memory:"


class TestCreateAdapterFromUrl:
    @pytest.mark.asyncio
    async def test_invalid_url_no_scheme_raises(self):
        with pytest.raises(ValueError, match="Invalid connection URL"):
            await create_adapter_from_url("no-scheme-here")

    @pytest.mark.asyncio
    async def test_unsupported_scheme_raises(self):
        with pytest.raises(ValueError, match="Unsupported database scheme"):
            await create_adapter_from_url("mysql://localhost/test")


class TestGetDatabaseAdapter:
    @pytest.mark.asyncio
    async def test_non_sqlite_without_url_raises(self):
        config = DatabaseConfig.__new__(DatabaseConfig)
        config.db_type = "postgresql"
        config.db_url = None
        config.db_path = None
        config.pool_min_size = 1
        config.pool_max_size = 10

        with pytest.raises(ValueError, match="No connection URL"):
            await get_database_adapter(config)


class TestSetDefaultAdapter:
    @pytest.mark.asyncio
    async def test_set_and_clear_default_adapter(self):
        mock_adapter = object()
        await set_default_adapter(mock_adapter)

        import fraisier.db.factory as factory_module

        assert factory_module._default_adapter is mock_adapter

        await set_default_adapter(None)
        assert factory_module._default_adapter is None
