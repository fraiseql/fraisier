"""Tests for database and backup operations."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner


class TestRunPsql:
    """Test psql execution wrapper."""

    def test_run_psql_executes_command(self):
        from fraisier.dbops.operations import run_psql

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="result\n", stderr=""
            )
            code, stdout, _stderr = run_psql("SELECT 1", db_name="testdb")

        assert code == 0
        assert stdout == "result\n"
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "psql" in cmd
        assert "testdb" in cmd

    def test_run_psql_returns_nonzero_on_failure(self):
        from fraisier.dbops.operations import run_psql

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="ERROR: syntax error"
            )
            code, _stdout, stderr = run_psql("BAD SQL", db_name="testdb")

        assert code == 1
        assert "syntax error" in stderr

    def test_run_psql_uses_sudo_postgres_by_default(self):
        from fraisier.dbops.operations import run_psql

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            run_psql("SELECT 1", db_name="testdb")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "sudo"
        assert "-u" in cmd
        assert "postgres" in cmd


class TestRunSql:
    """Test SQL execution helper."""

    def test_run_sql_executes_inline_sql(self):
        from fraisier.dbops.operations import run_sql

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="1\n", stderr="")
            code, _stdout, _stderr = run_sql("SELECT 1", db_name="testdb")

        assert code == 0
        cmd = mock_run.call_args[0][0]
        assert "-c" in cmd

    def test_run_sql_passes_tuples_only_flag(self):
        from fraisier.dbops.operations import run_sql

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="42\n", stderr="")
            run_sql("SELECT count(*) FROM pg_tables", db_name="testdb")

        cmd = mock_run.call_args[0][0]
        assert "-t" in cmd


class TestCheckDbExists:
    """Test database existence check."""

    def test_check_db_exists_returns_true(self):
        from fraisier.dbops.operations import check_db_exists

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="1\n", stderr="")
            assert check_db_exists("mydb") is True

    def test_check_db_exists_returns_false(self):
        from fraisier.dbops.operations import check_db_exists

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="0\n", stderr="")
            assert check_db_exists("nonexistent") is False

    def test_check_db_exists_returns_false_on_error(self):
        from fraisier.dbops.operations import check_db_exists

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="connection refused"
            )
            assert check_db_exists("mydb") is False


class TestDropDb:
    """Test database drop helper."""

    def test_drop_db_calls_dropdb(self):
        from fraisier.dbops.operations import drop_db

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            code, _, _ = drop_db("testdb")

        assert code == 0
        cmd = mock_run.call_args[0][0]
        assert "dropdb" in cmd
        assert "testdb" in cmd

    def test_drop_db_force_disconnects(self):
        from fraisier.dbops.operations import drop_db

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            drop_db("testdb", force_disconnect=True)

        # First call should terminate backends, second should drop
        assert mock_run.call_count == 2


class TestCreateDb:
    """Test database creation helper."""

    def test_create_db_calls_createdb(self):
        from fraisier.dbops.operations import create_db

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            code, _, _ = create_db("newdb")

        assert code == 0
        cmd = mock_run.call_args[0][0]
        assert "createdb" in cmd
        assert "newdb" in cmd

    def test_create_db_with_template(self):
        from fraisier.dbops.operations import create_db

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            create_db("newdb", template="template_mydb")

        cmd = mock_run.call_args[0][0]
        assert "-T" in cmd
        assert "template_mydb" in cmd

    def test_create_db_with_owner(self):
        from fraisier.dbops.operations import create_db

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            create_db("newdb", owner="appuser")

        cmd = mock_run.call_args[0][0]
        assert "-O" in cmd
        assert "appuser" in cmd


class TestTerminateBackends:
    """Test backend termination helper."""

    def test_terminate_backends_runs_pg_terminate(self):
        from fraisier.dbops.operations import terminate_backends

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            terminate_backends("mydb")

        cmd = mock_run.call_args[0][0]
        # Should use psql to run pg_terminate_backend
        assert "psql" in cmd
        assert "pg_terminate_backend" in " ".join(cmd)


class TestCreateTemplate:
    """Test template creation from an existing database."""

    def test_create_template_drops_existing_and_creates(self):
        from fraisier.dbops.templates import create_template

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = create_template("mydb", prefix="template_")

        assert result.success is True
        assert result.template_name == "template_mydb"

    def test_create_template_terminates_backends(self):
        from fraisier.dbops.templates import create_template

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            create_template("mydb", prefix="template_")

        # Should call: terminate backends, dropdb template, terminate backends,
        # createdb with -T
        calls = mock_run.call_args_list
        cmds = [" ".join(c[0][0]) for c in calls]
        assert any("pg_terminate_backend" in c for c in cmds)
        assert any("createdb" in c and "-T" in c for c in cmds)

    def test_create_template_returns_failure_on_create_error(self):
        from fraisier.dbops.templates import create_template

        with patch("subprocess.run") as mock_run:
            # Succeed on terminate/drop, fail on createdb
            def side_effect(cmd, **kwargs):
                if "createdb" in cmd:
                    return MagicMock(returncode=1, stdout="", stderr="ERROR: no space")
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = side_effect
            result = create_template("mydb", prefix="template_")

        assert result.success is False
        assert "no space" in result.error


class TestResetFromTemplate:
    """Test database reset from template."""

    def test_reset_from_template_drops_and_creates(self):
        from fraisier.dbops.templates import reset_from_template

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = reset_from_template("mydb", prefix="template_")

        assert result.success is True
        calls = mock_run.call_args_list
        cmds = [" ".join(c[0][0]) for c in calls]
        assert any("dropdb" in c and "mydb" in c for c in cmds)
        assert any("createdb" in c and "-T" in c and "template_mydb" in c for c in cmds)

    def test_reset_force_disconnects_before_drop(self):
        from fraisier.dbops.templates import reset_from_template

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            reset_from_template("mydb", prefix="template_")

        calls = mock_run.call_args_list
        cmds = [" ".join(c[0][0]) for c in calls]
        # Terminate must come before dropdb
        terminate_idx = next(
            i for i, c in enumerate(cmds) if "pg_terminate_backend" in c
        )
        drop_idx = next(i for i, c in enumerate(cmds) if "dropdb" in c and "mydb" in c)
        assert terminate_idx < drop_idx

    def test_reset_from_template_returns_failure(self):
        from fraisier.dbops.templates import reset_from_template

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="template does not exist"
            )
            result = reset_from_template("mydb", prefix="template_")

        assert result.success is False


class TestConfitureBuild:
    """Test confiture build wrapper."""

    def test_confiture_build_runs_command(self):
        """confiture_build() without rebuild delegates to confiture migrate up."""
        from fraisier.dbops.confiture import confiture_build

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Applied 3 migrations\n",
                stderr="",
            )
            result = confiture_build(config_path="confiture.yaml", cwd="/var/app")

        assert result.success is True
        assert result.migration_count == 3
        cmd = mock_run.call_args[0][0]
        assert "confiture" in cmd
        assert "migrate" in cmd
        assert "up" in cmd

    def test_confiture_build_rebuild_mode(self):
        """confiture_build(rebuild=True) delegates to confiture migrate rebuild."""
        from fraisier.dbops.confiture import confiture_build

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="Rebuilt\n", stderr=""
            )
            confiture_build(
                config_path="confiture.yaml",
                cwd="/var/app",
                rebuild=True,
            )

        cmd = mock_run.call_args[0][0]
        assert "migrate" in cmd
        assert "rebuild" in cmd
        assert "--drop-schemas" in cmd
        assert "-y" in cmd

    def test_confiture_build_returns_failure(self):
        from fraisier.dbops.confiture import confiture_build

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="ERROR: relation already exists",
            )
            result = confiture_build(config_path="confiture.yaml", cwd="/var/app")

        assert result.success is False
        assert "already exists" in result.error


class TestViewHelpersForwarding:
    """Test that migrate_up/dry_run_execute forward view_helpers: auto."""

    def _mock_env(self, view_helpers="manual"):
        """Create a mock Environment with the given view_helpers setting."""
        env = MagicMock()
        env.migration.view_helpers = view_helpers
        env.migration.tracking_table = "tb_confiture"
        env.database_url = "postgresql:///testdb"
        return env

    @patch("fraisier.dbops.confiture.Migrator")
    @patch("fraisier.dbops.confiture._load_env")
    def test_migrate_up_installs_helpers_when_auto(self, mock_load_env, mock_migrator):
        from fraisier.dbops.confiture import migrate_up

        env = self._mock_env(view_helpers="auto")
        mock_load_env.return_value = env

        mock_session = MagicMock()
        mock_up_result = MagicMock()
        mock_up_result.has_errors = False
        mock_up_result.migrations_applied = []
        mock_session.up.return_value = mock_up_result
        mock_migrator.from_config.return_value.__enter__ = MagicMock(
            return_value=mock_session
        )
        mock_migrator.from_config.return_value.__exit__ = MagicMock(return_value=False)

        mock_vm = MagicMock()
        mock_vm.helpers_installed.return_value = False

        with patch(
            "confiture.core.view_manager.ViewManager", return_value=mock_vm
        ) as mock_vm_cls:
            migrate_up("confiture.yaml")

        mock_vm_cls.assert_called_once_with(mock_session._conn)
        mock_vm.helpers_installed.assert_called_once()
        mock_vm.install_helpers.assert_called_once()

    @patch("fraisier.dbops.confiture.Migrator")
    @patch("fraisier.dbops.confiture._load_env")
    def test_migrate_up_skips_helpers_when_already_installed(
        self, mock_load_env, mock_migrator
    ):
        from fraisier.dbops.confiture import migrate_up

        env = self._mock_env(view_helpers="auto")
        mock_load_env.return_value = env

        mock_session = MagicMock()
        mock_up_result = MagicMock()
        mock_up_result.has_errors = False
        mock_up_result.migrations_applied = []
        mock_session.up.return_value = mock_up_result
        mock_migrator.from_config.return_value.__enter__ = MagicMock(
            return_value=mock_session
        )
        mock_migrator.from_config.return_value.__exit__ = MagicMock(return_value=False)

        mock_vm = MagicMock()
        mock_vm.helpers_installed.return_value = True

        with patch("confiture.core.view_manager.ViewManager", return_value=mock_vm):
            migrate_up("confiture.yaml")

        mock_vm.install_helpers.assert_not_called()

    @patch("fraisier.dbops.confiture.Migrator")
    @patch("fraisier.dbops.confiture._load_env")
    def test_migrate_up_skips_helpers_when_manual(self, mock_load_env, mock_migrator):
        from fraisier.dbops.confiture import migrate_up

        env = self._mock_env(view_helpers="manual")
        mock_load_env.return_value = env

        mock_session = MagicMock()
        mock_up_result = MagicMock()
        mock_up_result.has_errors = False
        mock_up_result.migrations_applied = []
        mock_session.up.return_value = mock_up_result
        mock_migrator.from_config.return_value.__enter__ = MagicMock(
            return_value=mock_session
        )
        mock_migrator.from_config.return_value.__exit__ = MagicMock(return_value=False)

        with patch("confiture.core.view_manager.ViewManager") as mock_vm_cls:
            migrate_up("confiture.yaml")

        mock_vm_cls.assert_not_called()

    @patch("fraisier.dbops.confiture.Migrator")
    @patch("fraisier.dbops.confiture._load_env")
    def test_dry_run_execute_installs_helpers_when_auto(
        self, mock_load_env, mock_migrator
    ):
        from fraisier.dbops.confiture import dry_run_execute

        env = self._mock_env(view_helpers="auto")
        mock_load_env.return_value = env

        mock_session = MagicMock()
        mock_up_result = MagicMock()
        mock_up_result.has_errors = False
        mock_session.up.return_value = mock_up_result
        mock_migrator.from_config.return_value.__enter__ = MagicMock(
            return_value=mock_session
        )
        mock_migrator.from_config.return_value.__exit__ = MagicMock(return_value=False)

        mock_vm = MagicMock()
        mock_vm.helpers_installed.return_value = False

        with patch(
            "confiture.core.view_manager.ViewManager", return_value=mock_vm
        ) as mock_vm_cls:
            dry_run_execute("confiture.yaml")

        mock_vm_cls.assert_called_once_with(mock_session._conn)
        mock_vm.install_helpers.assert_called_once()


class TestConfitureMigrate:
    """Test confiture migrate wrapper."""

    def test_confiture_migrate_up(self):
        from fraisier.dbops.confiture import confiture_migrate

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Applied 2 migrations\n",
                stderr="",
            )
            result = confiture_migrate(
                config_path="confiture.yaml",
                cwd="/var/app",
                direction="up",
            )

        assert result.success is True
        assert result.migration_count == 2
        cmd = mock_run.call_args[0][0]
        assert "migrate" in cmd
        assert "up" in cmd

    def test_confiture_migrate_down(self):
        from fraisier.dbops.confiture import confiture_migrate

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="Rolled back 1 migration\n", stderr=""
            )
            result = confiture_migrate(
                config_path="confiture.yaml",
                cwd="/var/app",
                direction="down",
            )

        assert result.success is True
        cmd = mock_run.call_args[0][0]
        assert "down" in cmd

    def test_confiture_migrate_failure_classified(self):
        from fraisier.dbops.confiture import confiture_migrate

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="ERROR: column does not exist",
            )
            result = confiture_migrate(
                config_path="confiture.yaml",
                cwd="/var/app",
                direction="up",
            )

        assert result.success is False
        assert result.error_type == "schema_error"


class TestParseConfitureOutput:
    """Test output parsing helpers."""

    def test_parse_migration_count_from_stdout(self):
        from fraisier.dbops.confiture import parse_migration_count

        assert parse_migration_count("Applied 5 migrations\n") == 5
        assert parse_migration_count("Applied 1 migration\n") == 1
        assert parse_migration_count("Nothing to do\n") == 0
        assert parse_migration_count("Rolled back 3 migrations\n") == 3

    def test_classify_confiture_error(self):
        from fraisier.dbops.confiture import classify_error

        assert classify_error("ERROR: relation already exists") == "schema_error"
        assert classify_error("ERROR: column does not exist") == "schema_error"
        assert classify_error("connection refused") == "connection_error"
        assert classify_error("something unknown") == "unknown"


class TestSchemaHash:
    """Test schema hash computation."""

    def test_hash_schema_from_directory(self, tmp_path):
        from fraisier.dbops.schema import hash_schema

        # Create some migration files
        (tmp_path / "001_init.sql").write_text("CREATE TABLE foo (id int);")
        (tmp_path / "002_add_bar.sql").write_text("ALTER TABLE foo ADD bar text;")

        h = hash_schema(tmp_path)
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex

    def test_hash_schema_deterministic(self, tmp_path):
        from fraisier.dbops.schema import hash_schema

        (tmp_path / "001.sql").write_text("CREATE TABLE x (id int);")
        h1 = hash_schema(tmp_path)
        h2 = hash_schema(tmp_path)
        assert h1 == h2

    def test_hash_schema_changes_with_content(self, tmp_path):
        from fraisier.dbops.schema import hash_schema

        (tmp_path / "001.sql").write_text("CREATE TABLE x (id int);")
        h1 = hash_schema(tmp_path)

        (tmp_path / "001.sql").write_text("CREATE TABLE x (id bigint);")
        h2 = hash_schema(tmp_path)
        assert h1 != h2

    def test_hash_schema_changes_with_new_file(self, tmp_path):
        from fraisier.dbops.schema import hash_schema

        (tmp_path / "001.sql").write_text("CREATE TABLE x (id int);")
        h1 = hash_schema(tmp_path)

        (tmp_path / "002.sql").write_text("ALTER TABLE x ADD col text;")
        h2 = hash_schema(tmp_path)
        assert h1 != h2

    def test_hash_schema_ignores_non_sql_files(self, tmp_path):
        from fraisier.dbops.schema import hash_schema

        (tmp_path / "001.sql").write_text("CREATE TABLE x (id int);")
        h1 = hash_schema(tmp_path)

        (tmp_path / "README.md").write_text("notes")
        h2 = hash_schema(tmp_path)
        assert h1 == h2


class TestCompareWithTemplate:
    """Test schema hash comparison with template."""

    def test_compare_matches_when_hash_same(self, tmp_path):
        from fraisier.dbops.schema import compare_with_template

        schema_dir = tmp_path / "sql"
        schema_dir.mkdir()
        (schema_dir / "001.sql").write_text("CREATE TABLE x (id int);")

        hash_file = tmp_path / "template_hash"

        # First call: no hash file yet
        result = compare_with_template(schema_dir, hash_file)
        assert result.needs_rebuild is True

        # Save the hash
        result.save()

        # Second call: hash matches
        result = compare_with_template(schema_dir, hash_file)
        assert result.needs_rebuild is False

    def test_compare_detects_schema_change(self, tmp_path):
        from fraisier.dbops.schema import compare_with_template

        schema_dir = tmp_path / "sql"
        schema_dir.mkdir()
        (schema_dir / "001.sql").write_text("CREATE TABLE x (id int);")

        hash_file = tmp_path / "template_hash"

        result = compare_with_template(schema_dir, hash_file)
        result.save()

        # Modify schema
        (schema_dir / "002.sql").write_text("ALTER TABLE x ADD y text;")
        result = compare_with_template(schema_dir, hash_file)
        assert result.needs_rebuild is True


class TestBackupRunner:
    """Test pg_dump backup with compression and modes."""

    def test_run_backup_full_mode(self):
        from fraisier.dbops.backup import run_backup

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_backup(
                db_name="mydb",
                output_dir="/backup/myproject",
                compression="zstd:9",
                mode="full",
            )

        assert result.success is True
        assert result.backup_path.endswith(".dump")
        cmd = mock_run.call_args[0][0]
        assert "pg_dump" in cmd
        assert "mydb" in cmd

    def test_run_backup_slim_mode_excludes_tables(self):
        from fraisier.dbops.backup import run_backup

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_backup(
                db_name="mydb",
                output_dir="/backup/myproject",
                compression="zstd:9",
                mode="slim",
                excluded_tables=["big_logs", "audit_trail"],
            )

        assert result.success is True
        cmd = mock_run.call_args[0][0]
        assert any("big_logs" in arg for arg in cmd)
        assert any("audit_trail" in arg for arg in cmd)

    def test_run_backup_failure(self):
        from fraisier.dbops.backup import run_backup

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="pg_dump: error: no space"
            )
            result = run_backup(
                db_name="mydb",
                output_dir="/backup/myproject",
                compression="zstd:9",
                mode="full",
            )

        assert result.success is False
        assert "no space" in result.error

    def test_run_backup_uses_custom_format(self):
        from fraisier.dbops.backup import run_backup

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            run_backup(
                db_name="mydb",
                output_dir="/backup",
                compression="zstd:9",
                mode="full",
            )

        cmd = mock_run.call_args[0][0]
        assert "-Fc" in cmd


class TestDiskSpaceCheck:
    """Test disk space pre-check."""

    def test_check_disk_space_sufficient(self):
        from fraisier.dbops.backup import check_disk_space

        with patch("shutil.disk_usage") as mock_usage:
            mock_usage.return_value = MagicMock(
                free=10 * 1024**3  # 10 GB
            )
            assert check_disk_space("/backup", required_gb=2) is True

    def test_check_disk_space_insufficient(self):
        from fraisier.dbops.backup import check_disk_space

        with patch("shutil.disk_usage") as mock_usage:
            mock_usage.return_value = MagicMock(
                free=1 * 1024**3  # 1 GB
            )
            assert check_disk_space("/backup", required_gb=2) is False


class TestRetentionCleanup:
    """Test backup retention cleanup."""

    def test_cleanup_old_backups(self, tmp_path):
        import os
        import time

        from fraisier.dbops.backup import cleanup_old_backups

        # Create backup files with different ages
        old_file = tmp_path / "mydb_20260101_0000.dump"
        old_file.write_text("old")
        # Set mtime to 48 hours ago
        old_mtime = time.time() - 48 * 3600
        os.utime(old_file, (old_mtime, old_mtime))

        new_file = tmp_path / "mydb_20260322_0000.dump"
        new_file.write_text("new")

        removed = cleanup_old_backups(tmp_path, retention_hours=24)

        assert len(removed) == 1
        assert old_file.name in removed[0]
        assert new_file.exists()

    def test_cleanup_keeps_all_when_within_retention(self, tmp_path):
        from fraisier.dbops.backup import cleanup_old_backups

        f = tmp_path / "mydb_recent.dump"
        f.write_text("recent")

        removed = cleanup_old_backups(tmp_path, retention_hours=24)
        assert len(removed) == 0
        assert f.exists()


class TestBackupSchedule:
    """Test per-destination schedule matching."""

    def test_schedule_matches_current_time(self):
        from fraisier.dbops.backup import should_run_now

        # "00:30" schedule at 00:30 should match
        assert should_run_now("00:30", hour=0, minute=30) is True

    def test_schedule_does_not_match(self):
        from fraisier.dbops.backup import should_run_now

        assert should_run_now("00:30", hour=1, minute=0) is False

    def test_cron_style_schedule(self):
        from fraisier.dbops.backup import should_run_now

        # "*/6 *" means every 6th hour at minute 0
        assert should_run_now("*/6 *", hour=6, minute=0) is True
        assert should_run_now("*/6 *", hour=3, minute=0) is False
        assert should_run_now("*/6 *", hour=12, minute=0) is True


class TestStagingRestore:
    """Test restore from backup with table count validation."""

    def test_restore_runs_pg_restore(self):
        from fraisier.dbops.restore import restore_backup

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = restore_backup(
                backup_path="/backup/prod/latest.dump",
                db_name="staging_db",
                db_owner="appuser",
            )

        assert result.success is True
        calls = mock_run.call_args_list
        cmds = [" ".join(c[0][0]) for c in calls]
        assert any("pg_restore" in c for c in cmds)

    def test_restore_fixes_ownership(self):
        from fraisier.dbops.restore import restore_backup

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            restore_backup(
                backup_path="/backup/prod/latest.dump",
                db_name="staging_db",
                db_owner="appuser",
            )

        calls = mock_run.call_args_list
        cmds = [" ".join(c[0][0]) for c in calls]
        assert any("REASSIGN" in c and "appuser" in c for c in cmds)

    def test_restore_failure(self):
        from fraisier.dbops.restore import restore_backup

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="file not found"
            )
            result = restore_backup(
                backup_path="/nonexistent.dump",
                db_name="staging_db",
            )

        assert result.success is False
        assert "not found" in result.error


class TestValidateTableCount:
    """Test post-restore table count validation."""

    def test_validate_above_threshold(self):
        from fraisier.dbops.restore import validate_table_count

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="75\n", stderr="")
            ok, count = validate_table_count("staging_db", min_threshold=50)

        assert ok is True
        assert count == 75

    def test_validate_below_threshold(self):
        from fraisier.dbops.restore import validate_table_count

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="10\n", stderr="")
            ok, count = validate_table_count("staging_db", min_threshold=50)

        assert ok is False
        assert count == 10

    def test_validate_on_query_error(self):
        from fraisier.dbops.restore import validate_table_count

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            ok, count = validate_table_count("staging_db", min_threshold=50)

        assert ok is False
        assert count == 0


class TestFindLatestBackup:
    """Test finding most recent backup in source directory."""

    def test_find_latest_backup(self, tmp_path):
        import os
        import time

        from fraisier.dbops.restore import find_latest_backup

        old = tmp_path / "db_full_20260101.dump"
        old.write_text("old")
        os.utime(old, (time.time() - 3600, time.time() - 3600))

        new = tmp_path / "db_full_20260322.dump"
        new.write_text("new")

        result = find_latest_backup(tmp_path)
        assert result is not None
        assert result.name == "db_full_20260322.dump"

    def test_find_latest_backup_empty_dir(self, tmp_path):
        from fraisier.dbops.restore import find_latest_backup

        result = find_latest_backup(tmp_path)
        assert result is None


class TestExternalDbGuard:
    """Test that external_db: true fraises are skipped for DB ops."""

    def test_is_external_db_true(self):
        from fraisier.dbops.guard import is_external_db

        fraise_config = {"external_db": True, "type": "api"}
        assert is_external_db(fraise_config) is True

    def test_is_external_db_false_by_default(self):
        from fraisier.dbops.guard import is_external_db

        fraise_config = {"type": "api"}
        assert is_external_db(fraise_config) is False

    def test_is_external_db_explicit_false(self):
        from fraisier.dbops.guard import is_external_db

        fraise_config = {"external_db": False, "type": "api"}
        assert is_external_db(fraise_config) is False

    def test_filter_db_fraises_excludes_external(self):
        from fraisier.dbops.guard import filter_db_fraises

        fraises = {
            "internal_api": {"type": "api", "external_db": False},
            "external_api": {"type": "api", "external_db": True},
            "simple_api": {"type": "api"},
        }

        result = filter_db_fraises(fraises)
        assert "internal_api" in result
        assert "simple_api" in result
        assert "external_api" not in result

    def test_filter_db_fraises_returns_skipped(self):
        from fraisier.dbops.guard import filter_db_fraises

        fraises = {
            "ok": {"type": "api"},
            "ext": {"type": "api", "external_db": True},
        }

        _, skipped = filter_db_fraises(fraises, return_skipped=True)
        assert "ext" in skipped
        assert "ok" not in skipped


@pytest.fixture
def db_config_file(tmp_path):
    """Create a fraises.yaml with database config for CLI tests."""
    config_file = tmp_path / "fraises.yaml"
    config_file.write_text(
        """
git:
  provider: github

fraises:
  management:
    type: api
    external_db: false
    environments:
      development:
        app_path: /var/app/management
        systemd_service: management-dev.service
        database:
          name: my_project_management
          strategy: rebuild
          confiture_config: confiture.yaml
          template_prefix: template_
      production:
        app_path: /var/app/management
        systemd_service: management-prod.service
        database:
          name: my_project_management
          strategy: migrate
  external_svc:
    type: api
    external_db: true
    environments:
      production:
        app_path: /var/app/external
        systemd_service: external.service

backup:
  compression: "zstd:9"
  disk_space_required_gb: 2
  destinations:
    - name: local
      path: /backup/my-project
      retention_hours: 24
      mode: full
      schedule: "00:30"
"""
    )
    return str(config_file)


class TestDbResetCommand:
    """Test fraisier db reset CLI command."""

    def test_db_reset_invokes_reset(self, db_config_file):
        from fraisier.cli import main

        runner = CliRunner()
        with patch("fraisier.dbops.templates.reset_from_template") as mock_reset:
            from fraisier.dbops.templates import TemplateResult

            mock_reset.return_value = TemplateResult(
                success=True, template_name="template_my_project_management"
            )
            result = runner.invoke(
                main,
                [
                    "-c",
                    db_config_file,
                    "db",
                    "reset",
                    "management",
                    "-e",
                    "development",
                ],
            )

        assert result.exit_code == 0, result.output
        mock_reset.assert_called_once()

    def test_db_reset_skips_external_db(self, db_config_file):
        from fraisier.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["-c", db_config_file, "db", "reset", "external_svc", "-e", "production"],
        )

        assert result.exit_code == 0
        assert "external" in result.output.lower() or "skip" in result.output.lower()


class TestDbMigrateCommand:
    """Test fraisier db migrate CLI command."""

    def test_db_migrate_invokes_confiture(self, db_config_file):
        from fraisier.cli import main

        runner = CliRunner()
        with patch("fraisier.dbops.confiture.confiture_migrate") as mock_mig:
            from fraisier.dbops.confiture import ConfitureResult

            mock_mig.return_value = ConfitureResult(success=True, migration_count=2)
            result = runner.invoke(
                main,
                [
                    "-c",
                    db_config_file,
                    "db",
                    "migrate",
                    "management",
                    "-e",
                    "production",
                ],
            )

        assert result.exit_code == 0, result.output
        mock_mig.assert_called_once()


class TestDbBuildCommand:
    """Test fraisier db build CLI command."""

    def test_db_build_invokes_confiture_build(self, db_config_file):
        from fraisier.cli import main

        runner = CliRunner()
        with patch("fraisier.dbops.confiture.confiture_build") as mock_build:
            from fraisier.dbops.confiture import ConfitureResult

            mock_build.return_value = ConfitureResult(success=True, migration_count=5)
            result = runner.invoke(
                main,
                [
                    "-c",
                    db_config_file,
                    "db",
                    "build",
                    "management",
                    "-e",
                    "development",
                ],
            )

        assert result.exit_code == 0, result.output
        mock_build.assert_called_once()


class TestBackupCommand:
    """Test fraisier backup CLI command."""

    def test_backup_runs_for_fraise(self, db_config_file):
        from fraisier.cli import main

        runner = CliRunner()
        with (
            patch("fraisier.dbops.backup.run_backup") as mock_backup,
            patch("fraisier.dbops.backup.check_disk_space", return_value=True),
        ):
            from fraisier.dbops.backup import BackupResult

            mock_backup.return_value = BackupResult(
                success=True, backup_path="/backup/my-project/dump.dump"
            )
            result = runner.invoke(
                main,
                ["-c", db_config_file, "backup", "management", "-e", "production"],
            )

        assert result.exit_code == 0, result.output
        mock_backup.assert_called_once()
