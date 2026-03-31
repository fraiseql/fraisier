"""Tests for fraisier.dbops._url — PostgreSQL URL rewriting."""

from fraisier.dbops._url import replace_db_name


class TestReplaceDbName:
    """replace_db_name preserves URL structure for all connection styles."""

    def test_tcp_url(self):
        url = "postgresql://user:pass@localhost:5432/original_db"
        assert (
            replace_db_name(url, "new_db")
            == "postgresql://user:pass@localhost:5432/new_db"
        )

    def test_tcp_url_to_postgres(self):
        url = "postgresql://user:pass@localhost:5432/myapp"
        assert (
            replace_db_name(url, "postgres")
            == "postgresql://user:pass@localhost:5432/postgres"
        )

    def test_unix_socket_url(self):
        url = "postgresql:///postgres?host=/var/run/postgresql"
        assert (
            replace_db_name(url, "myapp_db")
            == "postgresql:///myapp_db?host=/var/run/postgresql"
        )

    def test_unix_socket_url_to_postgres(self):
        url = "postgresql:///myapp_db?host=/var/run/postgresql"
        assert (
            replace_db_name(url, "postgres")
            == "postgresql:///postgres?host=/var/run/postgresql"
        )

    def test_unix_socket_no_query(self):
        url = "postgresql:///mydb"
        assert replace_db_name(url, "other_db") == "postgresql:///other_db"

    def test_tcp_url_with_query(self):
        url = "postgresql://user@host/db?sslmode=require"
        assert (
            replace_db_name(url, "new_db")
            == "postgresql://user@host/new_db?sslmode=require"
        )

    def test_preserves_fragment(self):
        url = "postgresql://user@host/db#fragment"
        assert (
            replace_db_name(url, "new_db") == "postgresql://user@host/new_db#fragment"
        )
