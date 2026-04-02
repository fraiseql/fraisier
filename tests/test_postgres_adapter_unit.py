"""Unit tests for PostgresAdapter without a live database."""

from unittest.mock import AsyncMock, MagicMock

import pytest

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")

from fraisier.db.postgres_adapter import PostgresAdapter  # noqa: E402


class TestConstructorValidation:
    def test_empty_connection_string_raises(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            PostgresAdapter("")

    def test_valid_connection_string_stores_it(self):
        adapter = PostgresAdapter("postgresql://localhost/test")
        assert adapter.connection_string == "postgresql://localhost/test"

    def test_default_pool_sizes(self):
        adapter = PostgresAdapter("postgresql://localhost/test")
        assert adapter.pool_min_size == 1
        assert adapter.pool_max_size == 10

    def test_custom_pool_sizes(self):
        adapter = PostgresAdapter(
            "postgresql://localhost/test", pool_min_size=2, pool_max_size=5
        )
        assert adapter.pool_min_size == 2
        assert adapter.pool_max_size == 5


class TestConvertPlaceholders:
    def test_no_question_marks_returns_unchanged(self):
        query = "SELECT * FROM table WHERE id = $1"
        assert PostgresAdapter._convert_placeholders(query) == query

    def test_single_question_mark(self):
        result = PostgresAdapter._convert_placeholders("SELECT * FROM t WHERE id = ?")
        assert result == "SELECT * FROM t WHERE id = $1"

    def test_multiple_question_marks(self):
        result = PostgresAdapter._convert_placeholders(
            "INSERT INTO t (a, b) VALUES (?, ?)"
        )
        assert result == "INSERT INTO t (a, b) VALUES ($1, $2)"

    def test_three_placeholders(self):
        result = PostgresAdapter._convert_placeholders(
            "UPDATE t SET a=?, b=? WHERE id=?"
        )
        assert result == "UPDATE t SET a=$1, b=$2 WHERE id=$3"

    def test_empty_query(self):
        assert PostgresAdapter._convert_placeholders("") == ""


class TestDatabaseType:
    def test_returns_postgresql(self):
        from fraisier.db.adapter import DatabaseType

        adapter = PostgresAdapter("postgresql://localhost/test")
        assert adapter.database_type() == DatabaseType.POSTGRESQL


class TestProperties:
    def test_last_insert_id_initially_none(self):
        adapter = PostgresAdapter("postgresql://localhost/test")
        assert adapter.last_insert_id is None

    def test_is_connected_false_when_no_pool(self):
        adapter = PostgresAdapter("postgresql://localhost/test")
        assert adapter.is_connected is False

    def test_is_connected_true_when_pool_set(self):
        adapter = PostgresAdapter("postgresql://localhost/test")
        adapter._pool = MagicMock()
        assert adapter.is_connected is True


class TestNotConnectedErrors:
    @pytest.mark.asyncio
    async def test_execute_query_raises_when_not_connected(self):
        adapter = PostgresAdapter("postgresql://localhost/test")
        with pytest.raises(RuntimeError, match="Not connected"):
            await adapter.execute_query("SELECT 1")

    @pytest.mark.asyncio
    async def test_execute_update_raises_when_not_connected(self):
        adapter = PostgresAdapter("postgresql://localhost/test")
        with pytest.raises(RuntimeError, match="Not connected"):
            await adapter.execute_update("UPDATE t SET x=1")

    @pytest.mark.asyncio
    async def test_insert_raises_when_not_connected(self):
        adapter = PostgresAdapter("postgresql://localhost/test")
        with pytest.raises(RuntimeError, match="Not connected"):
            await adapter.insert("t", {"x": 1})

    @pytest.mark.asyncio
    async def test_health_check_returns_false_when_no_pool(self):
        adapter = PostgresAdapter("postgresql://localhost/test")
        result = await adapter.health_check()
        assert result is False


class TestInsertValidation:
    @pytest.mark.asyncio
    async def test_insert_empty_data_raises(self):
        adapter = PostgresAdapter("postgresql://localhost/test")
        adapter._pool = MagicMock()
        with pytest.raises(ValueError, match="No data to insert"):
            await adapter.insert("t", {})


class TestUpdateAndDelete:
    @pytest.mark.asyncio
    async def test_update_empty_data_returns_false(self):
        adapter = PostgresAdapter("postgresql://localhost/test")
        result = await adapter.update("t", 1, {})
        assert result is False

    @pytest.mark.asyncio
    async def test_insert_with_mock_pool(self):
        adapter = PostgresAdapter("postgresql://localhost/test")

        mock_cursor = AsyncMock()
        mock_cursor.fetchone.return_value = {"id": 42}

        mock_cursor_ctx = MagicMock()
        mock_cursor_ctx.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor_ctx

        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_pool = MagicMock()
        mock_pool.connection.return_value = mock_conn_ctx
        adapter._pool = mock_pool

        result = await adapter.insert(
            "users", {"name": "Alice", "email": "alice@example.com"}
        )
        assert result == 42
        assert adapter.last_insert_id == 42

    @pytest.mark.asyncio
    async def test_execute_update_returning_tuple(self):
        """execute_update handles tuple result from RETURNING clause."""
        adapter = PostgresAdapter("postgresql://localhost/test")

        mock_cursor = AsyncMock()
        mock_cursor.rowcount = 1
        mock_cursor.fetchone.return_value = (99,)

        mock_cursor_ctx = MagicMock()
        mock_cursor_ctx.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor_ctx

        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_pool = MagicMock()
        mock_pool.connection.return_value = mock_conn_ctx
        adapter._pool = mock_pool

        rows = await adapter.execute_update(
            "INSERT INTO t (x) VALUES ($1) RETURNING id", [1]
        )
        assert rows == 1
        assert adapter.last_insert_id == 99

    @pytest.mark.asyncio
    async def test_execute_update_returning_dict(self):
        """execute_update handles dict result from RETURNING clause."""
        adapter = PostgresAdapter("postgresql://localhost/test")

        mock_cursor = AsyncMock()
        mock_cursor.rowcount = 1
        mock_cursor.fetchone.return_value = {"id": 77}

        mock_cursor_ctx = MagicMock()
        mock_cursor_ctx.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor_ctx

        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_pool = MagicMock()
        mock_pool.connection.return_value = mock_conn_ctx
        adapter._pool = mock_pool

        rows = await adapter.execute_update(
            "INSERT INTO t (x) VALUES ($1) RETURNING id", [1]
        )
        assert rows == 1
        assert adapter.last_insert_id == 77
