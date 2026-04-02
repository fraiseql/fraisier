"""Unit tests for MigrationRunner without a live database."""

from unittest.mock import AsyncMock

import pytest

from fraisier.db.migrations import MigrationError, MigrationRunner, run_migrations


class TestMigrationRunnerInit:
    def test_missing_directory_raises(self):
        with pytest.raises(MigrationError, match="Migrations directory not found"):
            MigrationRunner("/nonexistent/path/migrations")

    def test_valid_directory_succeeds(self, tmp_path):
        runner = MigrationRunner(str(tmp_path))
        assert runner.migrations_dir == tmp_path


class TestGetDbMigrationsDir:
    def test_missing_db_specific_dir_raises(self, tmp_path):
        from fraisier.db.adapter import DatabaseType

        runner = MigrationRunner(str(tmp_path))
        with pytest.raises(MigrationError, match="No migrations found"):
            runner._get_db_migrations_dir(DatabaseType.POSTGRESQL)

    def test_existing_db_specific_dir_returns_path(self, tmp_path):
        from fraisier.db.adapter import DatabaseType

        pg_dir = tmp_path / "postgresql"
        pg_dir.mkdir()
        runner = MigrationRunner(str(tmp_path))
        result = runner._get_db_migrations_dir(DatabaseType.POSTGRESQL)
        assert result == pg_dir


class TestGetPendingMigrations:
    def test_empty_directory_returns_empty_list(self, tmp_path):
        runner = MigrationRunner(str(tmp_path))
        result = runner._get_pending_migrations(tmp_path)
        assert result == []

    def test_sql_files_returned_sorted(self, tmp_path):
        (tmp_path / "002_b.sql").write_text("SELECT 2;")
        (tmp_path / "001_a.sql").write_text("SELECT 1;")
        runner = MigrationRunner(str(tmp_path))
        result = runner._get_pending_migrations(tmp_path)
        assert [name for name, _ in result] == ["001_a.sql", "002_b.sql"]

    def test_non_sql_files_excluded(self, tmp_path):
        (tmp_path / "001_a.sql").write_text("SELECT 1;")
        (tmp_path / "README.md").write_text("docs")
        runner = MigrationRunner(str(tmp_path))
        result = runner._get_pending_migrations(tmp_path)
        assert len(result) == 1


class TestReadMigrationFile:
    def test_reads_file_content(self, tmp_path):
        f = tmp_path / "001_test.sql"
        f.write_text("CREATE TABLE t (id INT);")
        runner = MigrationRunner(str(tmp_path))
        result = runner._read_migration_file(str(f))
        assert result == "CREATE TABLE t (id INT);"

    def test_empty_file_raises(self, tmp_path):
        f = tmp_path / "001_empty.sql"
        f.write_text("")
        runner = MigrationRunner(str(tmp_path))
        with pytest.raises(MigrationError, match="empty"):
            runner._read_migration_file(str(f))


class TestRunDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_does_not_execute(self, tmp_path, capsys):
        from fraisier.db.adapter import DatabaseType

        pg_dir = tmp_path / "postgresql"
        pg_dir.mkdir()
        (pg_dir / "001_test.sql").write_text("CREATE TABLE t (id INT);")

        mock_adapter = AsyncMock()
        mock_adapter.database_type.return_value = DatabaseType.POSTGRESQL

        runner = MigrationRunner(str(tmp_path))
        await runner.run(mock_adapter, dry_run=True)

        mock_adapter.execute_query.assert_not_called()
        captured = capsys.readouterr()
        assert "[DRY RUN]" in captured.out

    @pytest.mark.asyncio
    async def test_run_empty_migrations_returns_skipped(self, tmp_path):
        from fraisier.db.adapter import DatabaseType

        pg_dir = tmp_path / "postgresql"
        pg_dir.mkdir()

        mock_adapter = AsyncMock()
        mock_adapter.database_type.return_value = DatabaseType.POSTGRESQL

        runner = MigrationRunner(str(tmp_path))
        result = await runner.run(mock_adapter)

        assert result["migrations_run"] == 0
        assert "skipped_reason" in result


class TestRunMigrationsConvenience:
    @pytest.mark.asyncio
    async def test_run_migrations_convenience(self, tmp_path):
        from fraisier.db.adapter import DatabaseType

        pg_dir = tmp_path / "postgresql"
        pg_dir.mkdir()

        mock_adapter = AsyncMock()
        mock_adapter.database_type.return_value = DatabaseType.POSTGRESQL

        result = await run_migrations(mock_adapter, migrations_dir=str(tmp_path))
        assert result["migrations_run"] == 0
