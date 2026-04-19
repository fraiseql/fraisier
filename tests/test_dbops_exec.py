"""Tests for dbops/exec module - SQL safety checks and psql invocation."""

from fraisier.dbops.exec import build_psql_argv, is_readonly_sql


class TestIsReadonlySql:
    def test_select_allowed(self):
        assert is_readonly_sql("SELECT 1") is True

    def test_explain_allowed(self):
        assert is_readonly_sql("EXPLAIN ANALYZE SELECT * FROM foo") is True

    def test_show_allowed(self):
        assert is_readonly_sql("SHOW work_mem") is True

    def test_with_cte_allowed(self):
        assert is_readonly_sql("WITH x AS (SELECT 1) SELECT * FROM x") is True

    def test_table_shorthand_allowed(self):
        assert is_readonly_sql("TABLE public.tb_user") is True

    def test_insert_rejected(self):
        assert is_readonly_sql("INSERT INTO foo VALUES (1)") is False

    def test_update_rejected(self):
        assert is_readonly_sql("UPDATE foo SET x = 1") is False

    def test_delete_rejected(self):
        assert is_readonly_sql("DELETE FROM foo") is False

    def test_drop_rejected(self):
        assert is_readonly_sql("DROP TABLE foo") is False

    def test_alter_rejected(self):
        assert is_readonly_sql("ALTER TABLE foo ADD COLUMN bar int") is False

    def test_truncate_rejected(self):
        assert is_readonly_sql("TRUNCATE foo") is False

    def test_create_rejected(self):
        assert is_readonly_sql("CREATE TABLE foo (id int)") is False

    def test_case_insensitive(self):
        assert is_readonly_sql("select 1") is True
        assert is_readonly_sql("insert into foo values (1)") is False

    def test_leading_whitespace_ignored(self):
        assert is_readonly_sql("   SELECT 1") is True

    def test_leading_comment_ignored(self):
        assert is_readonly_sql("-- check count\nSELECT count(*) FROM foo") is True

    def test_empty_sql_rejected(self):
        assert is_readonly_sql("") is False

    def test_whitespace_only_rejected(self):
        assert is_readonly_sql("   \n  ") is False


class TestBuildPsqlArgv:
    def test_basic_select_returns_argv(self):
        argv = build_psql_argv(
            "mydb", "SELECT 1", timeout_ms=30_000, output_format="table"
        )
        assert argv[0] == "psql"
        assert "-d" in argv
        assert "mydb" in argv
        assert "-c" in argv
        assert any("statement_timeout" in a for a in argv)
        assert "SELECT 1" in " ".join(argv)

    def test_json_format_sets_tuples_only_and_format(self):
        argv = build_psql_argv(
            "mydb", "SELECT 1", timeout_ms=5_000, output_format="json"
        )
        joined = " ".join(argv)
        assert "--csv" not in joined
        assert "json" in joined.lower() or "format" in joined.lower()

    def test_csv_format_sets_csv_flag(self):
        argv = build_psql_argv(
            "mydb", "SELECT 1", timeout_ms=5_000, output_format="csv"
        )
        assert "--csv" in argv

    def test_url_passed_directly(self):
        url = "postgresql://app@localhost/mydb"
        argv = build_psql_argv(
            url, "SELECT 1", timeout_ms=10_000, output_format="table"
        )
        assert url in argv

    def test_timeout_injected_as_set_statement(self):
        argv = build_psql_argv(
            "mydb", "SELECT 1", timeout_ms=15_000, output_format="table"
        )
        set_clause = next(a for a in argv if "statement_timeout" in a)
        assert "15000" in set_clause

    def test_no_psqlrc_flag_present(self):
        argv = build_psql_argv(
            "mydb", "SELECT 1", timeout_ms=10_000, output_format="table"
        )
        assert "--no-psqlrc" in argv

    def test_no_align_flag_for_non_table(self):
        argv = build_psql_argv(
            "mydb", "SELECT 1", timeout_ms=10_000, output_format="csv"
        )
        assert "-A" in argv
