"""Tests for PostgresAdapter pool configuration and transaction support."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from psycopg.rows import dict_row

from fraisier.db.postgres_adapter import PostgresAdapter


class TestPoolCreation:
    """Pool must be created with correct row_factory kwarg."""

    @pytest.mark.asyncio
    async def test_connect_passes_row_factory_via_kwargs(self):
        """AsyncConnectionPool must receive kwargs={'row_factory': dict_row}."""
        adapter = PostgresAdapter("postgresql://localhost/test")

        with patch(
            "fraisier.db.postgres_adapter.AsyncConnectionPool"
        ) as MockPool:
            mock_pool = AsyncMock()
            MockPool.return_value = mock_pool

            await adapter.connect()

            MockPool.assert_called_once()
            call_kwargs = MockPool.call_args
            assert "rows_factory" not in (call_kwargs.kwargs or {}), (
                "rows_factory is not a valid AsyncConnectionPool parameter"
            )
            assert call_kwargs.kwargs.get("kwargs") == {
                "row_factory": dict_row,
            }


class TestPoolMetrics:
    """Pool metrics must use public get_stats() API."""

    def test_pool_metrics_uses_get_stats(self):
        """pool_metrics() uses pool.get_stats() instead of private attributes."""
        adapter = PostgresAdapter("postgresql://localhost/test")
        mock_pool = MagicMock()
        mock_pool.get_stats.return_value = {
            "pool_size": 10,
            "pool_available": 6,
            "pool_min": 2,
            "pool_max": 10,
        }
        adapter._pool = mock_pool

        metrics = adapter.pool_metrics()

        mock_pool.get_stats.assert_called_once()
        assert metrics.total_connections == 10
        assert metrics.idle_connections == 6
        assert metrics.active_connections == 4
        assert metrics.waiting_requests == 0

    def test_pool_metrics_none_pool(self):
        """pool_metrics() returns empty metrics when pool is None."""
        adapter = PostgresAdapter("postgresql://localhost/test")
        metrics = adapter.pool_metrics()
        assert metrics.total_connections == 0


class TestTransactionMethods:
    """Transaction methods must actually manage a connection and transaction."""

    @pytest.mark.asyncio
    async def test_begin_transaction_acquires_connection(self):
        """begin_transaction must acquire a dedicated connection from pool."""
        adapter = PostgresAdapter("postgresql://localhost/test")

        mock_conn = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool.getconn.return_value = mock_conn
        adapter._pool = mock_pool

        await adapter.begin_transaction()

        assert adapter._tx_conn is not None
        mock_pool.getconn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_commit_transaction_commits_and_returns_connection(self):
        """commit_transaction must commit and return conn to pool."""
        adapter = PostgresAdapter("postgresql://localhost/test")

        mock_conn = AsyncMock()
        mock_pool = AsyncMock()
        adapter._pool = mock_pool
        adapter._tx_conn = mock_conn

        await adapter.commit_transaction()

        mock_conn.commit.assert_awaited_once()
        mock_pool.putconn.assert_awaited_once_with(mock_conn)
        assert adapter._tx_conn is None

    @pytest.mark.asyncio
    async def test_rollback_transaction_rolls_back_and_returns_connection(self):
        """rollback_transaction must rollback and return conn to pool."""
        adapter = PostgresAdapter("postgresql://localhost/test")

        mock_conn = AsyncMock()
        mock_pool = AsyncMock()
        adapter._pool = mock_pool
        adapter._tx_conn = mock_conn

        await adapter.rollback_transaction()

        mock_conn.rollback.assert_awaited_once()
        mock_pool.putconn.assert_awaited_once_with(mock_conn)
        assert adapter._tx_conn is None

    @pytest.mark.asyncio
    async def test_transaction_methods_raise_when_not_connected(self):
        """Transaction methods must raise RuntimeError when pool is None."""
        adapter = PostgresAdapter("postgresql://localhost/test")

        with pytest.raises(RuntimeError, match="Not connected"):
            await adapter.begin_transaction()

    @pytest.mark.asyncio
    async def test_commit_without_begin_raises(self):
        """commit_transaction without begin must raise RuntimeError."""
        adapter = PostgresAdapter("postgresql://localhost/test")
        adapter._pool = AsyncMock()

        with pytest.raises(RuntimeError, match="No active transaction"):
            await adapter.commit_transaction()

    @pytest.mark.asyncio
    async def test_rollback_without_begin_raises(self):
        """rollback_transaction without begin must raise RuntimeError."""
        adapter = PostgresAdapter("postgresql://localhost/test")
        adapter._pool = AsyncMock()

        with pytest.raises(RuntimeError, match="No active transaction"):
            await adapter.rollback_transaction()
