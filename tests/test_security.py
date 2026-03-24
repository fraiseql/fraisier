"""Security tests for input validation across the codebase."""

import pytest

from fraisier.dbops._validation import (
    validate_file_path,
    validate_pg_identifier,
    validate_service_name,
)
from fraisier.dbops.operations import (
    check_db_exists,
    create_db,
    drop_db,
    terminate_backends,
)
from fraisier.health_check import ExecHealthChecker


class TestValidatePgIdentifier:
    """Tests for PostgreSQL identifier validation."""

    def test_valid_simple_name(self):
        assert validate_pg_identifier("my_database") == "my_database"

    def test_valid_underscore_prefix(self):
        assert validate_pg_identifier("_private") == "_private"

    def test_valid_mixed_case(self):
        assert validate_pg_identifier("MyDB_v2") == "MyDB_v2"

    def test_rejects_sql_injection(self):
        with pytest.raises(ValueError, match="Invalid"):
            validate_pg_identifier("'; DROP TABLE --")

    def test_rejects_command_injection(self):
        with pytest.raises(ValueError, match="Invalid"):
            validate_pg_identifier("$(whoami)")

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="Invalid"):
            validate_pg_identifier("")

    def test_rejects_starts_with_number(self):
        with pytest.raises(ValueError, match="Invalid"):
            validate_pg_identifier("1database")

    def test_rejects_spaces(self):
        with pytest.raises(ValueError, match="Invalid"):
            validate_pg_identifier("my database")

    def test_rejects_semicolons(self):
        with pytest.raises(ValueError, match="Invalid"):
            validate_pg_identifier("db;rm -rf")

    def test_rejects_backticks(self):
        with pytest.raises(ValueError, match="Invalid"):
            validate_pg_identifier("`whoami`")

    def test_rejects_too_long(self):
        with pytest.raises(ValueError, match="Invalid"):
            validate_pg_identifier("a" * 64)

    def test_accepts_max_length(self):
        assert validate_pg_identifier("a" * 63) == "a" * 63

    def test_custom_label_in_error(self):
        with pytest.raises(ValueError, match="database name"):
            validate_pg_identifier("bad;name", "database name")


class TestValidateServiceName:
    """Tests for systemd service name validation."""

    def test_valid_service(self):
        assert validate_service_name("nginx.service") == "nginx.service"

    def test_valid_instance(self):
        assert validate_service_name("app@1.service") == "app@1.service"

    def test_rejects_shell_injection(self):
        with pytest.raises(ValueError, match="Invalid service"):
            validate_service_name("my.service; rm -rf /")

    def test_rejects_pipe(self):
        with pytest.raises(ValueError, match="Invalid service"):
            validate_service_name("svc | cat /etc/passwd")

    def test_rejects_subshell(self):
        with pytest.raises(ValueError, match="Invalid service"):
            validate_service_name("$(evil)")


class TestValidateFilePath:
    """Tests for file path validation."""

    def test_valid_path(self):
        assert validate_file_path("/var/backups/db.dump") == "/var/backups/db.dump"

    def test_rejects_semicolons(self):
        with pytest.raises(ValueError, match="Invalid file"):
            validate_file_path("/tmp/a; rm -rf /")

    def test_rejects_backticks(self):
        with pytest.raises(ValueError, match="Invalid file"):
            validate_file_path("`cat /etc/passwd`")

    def test_rejects_subshell(self):
        with pytest.raises(ValueError, match="Invalid file"):
            validate_file_path("$(whoami)")


class TestDbopsValidation:
    """Tests that dbops functions reject malicious identifiers."""

    def test_check_db_exists_rejects_injection(self):
        with pytest.raises(ValueError, match="Invalid"):
            check_db_exists("'; DROP TABLE --")

    def test_terminate_backends_rejects_injection(self):
        with pytest.raises(ValueError, match="Invalid"):
            terminate_backends("$(whoami)")

    def test_drop_db_rejects_injection(self):
        with pytest.raises(ValueError, match="Invalid"):
            drop_db("db; rm -rf /")

    def test_create_db_rejects_bad_name(self):
        with pytest.raises(ValueError, match="Invalid"):
            create_db("bad;name")

    def test_create_db_rejects_bad_template(self):
        with pytest.raises(ValueError, match="Invalid"):
            create_db("good_db", template="bad;template")

    def test_create_db_rejects_bad_owner(self):
        with pytest.raises(ValueError, match="Invalid"):
            create_db("good_db", owner="$(whoami)")


class TestExecHealthChecker:
    """Tests for ExecHealthChecker shell safety."""

    def test_default_no_shell(self):
        checker = ExecHealthChecker("echo hello")
        assert not checker.use_shell

    def test_shell_false_runs_split_command(self):
        checker = ExecHealthChecker("echo hello", shell=False)
        result = checker.check(timeout=5.0)
        assert result.success
        assert result.check_type == "exec"

    def test_shell_true_runs_in_shell(self):
        checker = ExecHealthChecker("echo hello", shell=True)
        assert checker.use_shell
        result = checker.check(timeout=5.0)
        assert result.success

    def test_timeout_handling(self):
        checker = ExecHealthChecker("sleep 10")
        result = checker.check(timeout=0.1)
        assert not result.success
        assert "timeout" in result.message.lower()


class TestScaffoldValidation:
    """Tests for scaffold renderer name validation."""

    def test_rejects_bad_fraise_name(self, tmp_path):
        from unittest.mock import MagicMock

        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = MagicMock()
        config.scaffold.output_dir = str(tmp_path)
        config.list_fraises.return_value = ["bad;name"]
        config.get_fraise.return_value = {
            "type": "api",
            "environments": {"prod": {}},
        }

        renderer = ScaffoldRenderer(config)
        with pytest.raises(ValueError, match="Invalid fraise name"):
            renderer.render()

    def test_rejects_bad_environment_name(self, tmp_path):
        from unittest.mock import MagicMock

        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = MagicMock()
        config.scaffold.output_dir = str(tmp_path)
        config.list_fraises.return_value = ["good_api"]
        config.get_fraise.return_value = {
            "type": "api",
            "environments": {"bad;env": {}},
        }

        renderer = ScaffoldRenderer(config)
        with pytest.raises(ValueError, match="Invalid environment name"):
            renderer.render()
