"""Tests for template metadata table operations."""

from datetime import UTC, datetime

import pytest

from fraisier.testing._metadata import (
    TemplateMeta,
    ensure_meta_table,
    read_meta,
    write_meta,
)

_TEST_URL = "postgresql://localhost/tpl"


class TestEnsureMetaTable:
    def test_executes_create_table_ddl(self, monkeypatch):
        captured: list[tuple] = []

        def fake_run_psql(sql, *, db_name, connection_url):
            captured.append((sql, db_name, connection_url))
            return (0, "", "")

        monkeypatch.setattr("fraisier.testing._metadata.run_psql", fake_run_psql)
        ensure_meta_table("tpl_test", connection_url=_TEST_URL)

        assert len(captured) == 1
        sql, db, url = captured[0]
        assert "CREATE TABLE IF NOT EXISTS" in sql
        assert "_fraisier_template_meta" in sql
        assert db == "tpl_test"
        assert url == _TEST_URL

    def test_raises_on_failure(self, monkeypatch):
        def fake_run_psql(sql, *, db_name, connection_url):
            return (1, "", "ERROR: connection refused")

        monkeypatch.setattr("fraisier.testing._metadata.run_psql", fake_run_psql)
        with pytest.raises(RuntimeError, match="Failed to create metadata table"):
            ensure_meta_table("tpl_test", connection_url=_TEST_URL)


class TestReadMeta:
    def test_returns_none_when_no_rows(self, monkeypatch):
        def fake_run_sql(sql, *, db_name, connection_url):
            return (0, "", "")

        monkeypatch.setattr("fraisier.testing._metadata.run_sql", fake_run_sql)
        result = read_meta("tpl_test", connection_url=_TEST_URL)
        assert result is None

    def test_parses_row(self, monkeypatch):
        row = "abc123|2026-03-30 10:00:00+00|0.8.17|1500"

        def fake_run_sql(sql, *, db_name, connection_url):
            return (0, row, "")

        monkeypatch.setattr("fraisier.testing._metadata.run_sql", fake_run_sql)
        result = read_meta("tpl_test", connection_url=_TEST_URL)
        assert result is not None
        assert result.schema_hash == "abc123"
        assert result.confiture_version == "0.8.17"
        assert result.build_duration_ms == 1500

    def test_returns_none_on_error(self, monkeypatch):
        def fake_run_sql(sql, *, db_name, connection_url):
            return (1, "", "ERROR")

        monkeypatch.setattr("fraisier.testing._metadata.run_sql", fake_run_sql)
        result = read_meta("tpl_test", connection_url=_TEST_URL)
        assert result is None


class TestWriteMeta:
    def test_truncates_then_inserts(self, monkeypatch):
        captured_sqls: list[str] = []

        def fake_run_psql(sql, *, db_name, connection_url):
            captured_sqls.append(sql)
            return (0, "", "")

        monkeypatch.setattr("fraisier.testing._metadata.run_psql", fake_run_psql)

        meta = TemplateMeta(
            schema_hash="abc123",
            built_at=datetime(2026, 3, 30, 10, 0, tzinfo=UTC),
            confiture_version="0.8.17",
            build_duration_ms=1500,
        )
        write_meta("tpl_test", meta, connection_url=_TEST_URL)

        assert len(captured_sqls) == 1
        sql = captured_sqls[0]
        assert "TRUNCATE" in sql
        assert "INSERT INTO" in sql
        assert "abc123" in sql

    def test_raises_on_failure(self, monkeypatch):
        def fake_run_psql(sql, *, db_name, connection_url):
            return (1, "", "ERROR")

        monkeypatch.setattr("fraisier.testing._metadata.run_psql", fake_run_psql)

        meta = TemplateMeta(
            schema_hash="abc",
            built_at=datetime.now(tz=UTC),
            confiture_version="0.8.17",
            build_duration_ms=100,
        )
        with pytest.raises(RuntimeError, match="Failed to write metadata"):
            write_meta("tpl_test", meta, connection_url=_TEST_URL)
