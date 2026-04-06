"""Tests for PeeweeMigrateStrategy."""

from unittest.mock import patch

from fraisier.strategies import PeeweeMigrateStrategy


class TestPeeweeMigrateStrategyValidateSetup:
    """Test Peewee migration setup validation."""

    def test_validate_setup_missing_migrations_dir(self, tmp_path):
        """validate_setup fails when migrations dir is missing."""
        strategy = PeeweeMigrateStrategy("models", "migrations")
        result = strategy.validate_setup(tmp_path)
        assert not result.valid
        assert any("migrations directory" in e for e in result.errors)

    def test_validate_setup_valid(self, tmp_path):
        """validate_setup succeeds when migrations dir exists and dependencies OK."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()

        strategy = PeeweeMigrateStrategy("models", migrations_dir)

        # Mock the imports and peewee check
        with patch("importlib.util.find_spec") as mock_find:
            mock_find.return_value = True  # peewee is "installed"
            with patch("builtins.__import__"):  # models module "imports"
                result = strategy.validate_setup(tmp_path)

        assert result.valid


class TestPeeweeMigrateStrategyFrameworkName:
    """Test framework name property."""

    def test_framework_name(self):
        """framework_name returns 'peewee'."""
        strategy = PeeweeMigrateStrategy("models", "migrations")
        assert strategy.framework_name == "peewee"


class TestPeeweeMigrateStrategyGetLatestVersion:
    """Test Peewee get latest version."""

    def test_get_latest_version_returns_last_file(self, tmp_path):
        """get_latest_version returns last migration file version."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "0001_initial.py").write_text("")
        (migrations_dir / "0002_add_users.py").write_text("")

        strategy = PeeweeMigrateStrategy("models", migrations_dir)
        result = strategy.get_latest_version(tmp_path)

        assert result == "0002"

    def test_get_latest_version_returns_none_for_empty_dir(self, tmp_path):
        """get_latest_version returns None for empty migrations dir."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()

        strategy = PeeweeMigrateStrategy("models", migrations_dir)
        result = strategy.get_latest_version(tmp_path)

        assert result is None


class TestPeeweeMigrateStrategyGetMigrationHistory:
    """Test Peewee get migration history."""

    def test_get_migration_history_parses_filenames(self, tmp_path):
        """get_migration_history parses migration file names."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "0001_initial.py").write_text("")
        (migrations_dir / "0002_add_users.py").write_text("")

        strategy = PeeweeMigrateStrategy("models", migrations_dir)
        history = strategy.get_migration_history(tmp_path)

        assert len(history) == 2
        assert history[0]["version"] == "0001"
        assert history[1]["version"] == "0002"
        assert "Add Users" in history[1]["description"]


class TestPeeweeMigrateStrategyMigrateUp:
    """Test Peewee migrate up."""

    def test_migrate_up_not_implemented(self, tmp_path):
        """migrate_up returns failure with stub implementation."""
        strategy = PeeweeMigrateStrategy("models", "migrations")

        # Mock the models import so we reach the "not yet implemented" message
        with patch("builtins.__import__"):
            result = strategy.migrate_up(tmp_path)

        assert result.success is False
        assert len(result.errors) > 0
        assert "not yet implemented" in result.errors[0].lower()


class TestPeeweeMigrateStrategyMigrateDown:
    """Test Peewee migrate down."""

    def test_migrate_down_not_implemented(self, tmp_path):
        """migrate_down returns failure with stub implementation."""
        strategy = PeeweeMigrateStrategy("models", "migrations")
        result = strategy.migrate_down(tmp_path, "0001")

        assert result.success is False
        assert len(result.errors) > 0
        assert "not yet implemented" in result.errors[0].lower()
