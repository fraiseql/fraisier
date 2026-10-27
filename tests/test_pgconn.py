"""``PgConn`` reads a PostgreSQL URL the way libpq does.

Every expectation here was captured from libpq (psql 18 against a live
server), not composed: ``.phases/2026-10-26-v0.95/probes/libpq-parse.sh``.
"""

from __future__ import annotations

import pytest

from fraisier.dbops._pgconn import PgConn, decode


def env(url: str) -> dict[str, str]:
    return PgConn.parse(url).pg_env()


class TestUserinfoAndPath:
    def test_every_netloc_part_is_decoded(self):
        assert env("postgresql://u:p%40ss@h:6543/d%62") == {
            "PGUSER": "u",
            "PGPASSWORD": "p@ss",
            "PGHOST": "h",
            "PGPORT": "6543",
            "PGDATABASE": "db",
        }

    def test_plus_stays_plus(self):
        assert env("postgresql://u:p+w@h/db")["PGPASSWORD"] == "p+w"

    def test_socket_url_names_only_the_database(self):
        assert env("postgresql:///db") == {"PGDATABASE": "db"}

    def test_postgres_scheme_is_accepted(self):
        assert env("postgres:///db") == {"PGDATABASE": "db"}

    def test_user_without_password(self):
        assert env("postgresql://u@h/db") == {
            "PGUSER": "u",
            "PGHOST": "h",
            "PGDATABASE": "db",
        }

    def test_empty_password_is_a_password(self):
        assert env("postgresql://u:@h/db")["PGPASSWORD"] == ""

    def test_explicit_empty_host(self):
        assert env("postgresql://@/db") == {"PGDATABASE": "db"}

    def test_ipv6_brackets_are_stripped(self):
        """``PGHOST=[::1]`` fails to resolve; ``PGHOST=::1`` connects."""
        assert env("postgresql://[::1]:5433/db") == {
            "PGHOST": "::1",
            "PGPORT": "5433",
            "PGDATABASE": "db",
        }

    def test_encoded_socket_directory_as_host(self):
        assert env("postgresql://%2Frun%2Fpostgresql/db")["PGHOST"] == "/run/postgresql"

    def test_multi_host(self):
        assert env("postgresql://h1:5432,h2:5433/db") == {
            "PGHOST": "h1,h2",
            "PGPORT": "5432,5433",
            "PGDATABASE": "db",
        }

    def test_multi_host_missing_port_is_an_empty_entry(self):
        assert env("postgresql://h1,h2:5433/db")["PGPORT"] == ",5433"


class TestQuery:
    def test_query_host(self):
        assert env("postgresql:///db?host=/run/postgresql") == {
            "PGHOST": "/run/postgresql",
            "PGDATABASE": "db",
        }

    def test_query_user_beats_userinfo(self):
        assert env("postgresql://u@h/db?user=other")["PGUSER"] == "other"

    def test_query_password_beats_userinfo(self):
        assert env("postgresql://u:x@h/db?password=pw")["PGPASSWORD"] == "pw"

    def test_query_plus_stays_plus(self):
        assert env("postgresql:///db?password=a+b")["PGPASSWORD"] == "a+b"

    def test_query_dbname_beats_the_path(self):
        assert env("postgresql:///db?dbname=x")["PGDATABASE"] == "x"

    def test_query_dbname_alone_names_the_database(self):
        assert env("postgresql://h?dbname=x") == {"PGHOST": "h", "PGDATABASE": "x"}

    def test_repeated_key_last_wins(self):
        assert env("postgresql:///db?host=a&host=b")["PGHOST"] == "b"

    def test_keys_are_decoded(self):
        assert env("postgresql:///db?h%6Fst=/s")["PGHOST"] == "/s"

    def test_values_are_decoded(self):
        assert env("postgresql:///db?password=a%26b%3Dc")["PGPASSWORD"] == "a&b=c"

    def test_sslmode_maps_to_its_variable(self):
        assert env("postgresql://h/db?sslmode=require")["PGSSLMODE"] == "require"


class TestRefusals:
    @pytest.mark.parametrize(
        ("url", "names"),
        [
            ("postgresql:///db?bogus_key=1", "bogus_key"),
            ("postgresql:///db?HOST=x", "HOST"),
            ("postgresql:///db?keepalives=1", "keepalives"),
            ("postgresql:///db?sslpassword=x", "sslpassword"),
            ("postgresql:///db?service=acme", "service"),
            ("postgresql:///db?flag", "flag"),
            ("postgresql:///db?=x", '""'),
            ("postgresql://u:p%zz@h/db", "%XX escape"),
            ("postgresql:///db?password=a%zz", "%XX escape"),
            ("postgresql:///db?password=a%00b", "%00"),
            ("postgresql://u:p@ss@h/db", "%40"),
            ("ftp://h/db", "postgresql://"),
            ("host=h dbname=d", "postgresql://"),
        ],
    )
    def test_refused_naming_the_problem(self, url, names):
        with pytest.raises(ValueError) as exc:
            PgConn.parse(url)
        assert names in str(exc.value)

    @pytest.mark.parametrize(
        "url",
        ["postgresql://u@h", "postgresql://u@h/", "postgresql:///?host=/s"],
    )
    def test_a_url_without_a_database_is_refused(self, url):
        with pytest.raises(ValueError, match="names no database"):
            PgConn.parse(url)

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://u:hunter2@h/db?bogus=1",
            "postgresql://u:hunter2%zz@h/db",
            "postgresql:///db?password=hunter2%zz",
            "postgresql://u:hunter2@ss@h/db",
            "postgresql:///db?password:hunter2",
        ],
    )
    def test_a_refusal_never_echoes_the_password(self, url):
        with pytest.raises(ValueError) as exc:
            PgConn.parse(url)
        assert "hunter2" not in str(exc.value)
        assert "zz" not in str(exc.value)

    def test_repr_has_no_password(self):
        assert "hunter2" not in repr(PgConn.parse("postgresql://u:hunter2@h/db"))


class TestProperties:
    def test_discrete_fields(self):
        conn = PgConn.parse("postgresql://u:p%40ss@h:6543/db")
        assert (conn.host, conn.port, conn.user, conn.password, conn.dbname) == (
            "h",
            6543,
            "u",
            "p@ss",
            "db",
        )

    def test_absent_fields_are_none(self):
        conn = PgConn.parse("postgresql:///db")
        assert (conn.host, conn.port, conn.user, conn.password) == (None,) * 4

    def test_multi_host_has_no_single_port(self):
        assert PgConn.parse("postgresql://h1:1,h2:2/db").port is None


class TestDecode:
    @pytest.mark.parametrize(
        ("raw", "decoded"),
        [("a%41", "aA"), ("%C3%A9", "é"), ("a+b", "a+b"), ("", ""), ("%25", "%")],
    )
    def test_decodes_like_libpq(self, raw, decoded):
        assert decode(raw) == decoded

    @pytest.mark.parametrize("raw", ["%", "%4", "%zz", "a%g1"])
    def test_malformed_escape_is_refused(self, raw):
        with pytest.raises(ValueError, match="invalid percent-encoded token"):
            decode(raw)

    def test_nul_is_refused(self):
        with pytest.raises(ValueError, match="forbidden value %00"):
            decode("a%00b")


class TestChildEnv:
    """Every ambient ``PG*`` is dropped but a fixed operator allowlist (D6).

    The URL names the target fully. An ambient variable that could change the
    server, the user, the database or the session (``PGSERVICE`` outranks
    every other variable; ``PGPASSWORD`` would go to whatever host the URL
    names) must not reach the child.
    """

    URL = "postgresql://u:pw@h:6543/db"

    @pytest.mark.parametrize(
        "name",
        [
            "PGSERVICE",
            "PGSERVICEFILE",
            "PGSYSCONFDIR",
            "PGPASSWORD",
            "PGHOST",
            "PGHOSTADDR",
            "PGPORT",
            "PGUSER",
            "PGDATABASE",
            "PGOPTIONS",
            "PGTARGETSESSIONATTRS",
            "PGCLIENTENCODING",
            "PGTZ",
            "PGDATESTYLE",
            "PGSOMETHING_NEW",
        ],
    )
    def test_ambient_variable_does_not_reach_the_child(self, monkeypatch, name):
        monkeypatch.setenv(name, "ambient")

        child = PgConn.parse("postgresql:///db").child_env()

        assert child.get(name) != "ambient"

    @pytest.mark.parametrize(
        "name",
        [
            "PGSSLROOTCERT",
            "PGSSLCERT",
            "PGSSLKEY",
            "PGSSLCRL",
            "PGSSLCRLDIR",
            "PGPASSFILE",
            "PGREQUIREAUTH",
            "PGCONNECT_TIMEOUT",
            "PGAPPNAME",
        ],
    )
    def test_operator_default_reaches_the_child(self, monkeypatch, name):
        monkeypatch.setenv(name, "operator")

        assert PgConn.parse("postgresql:///db").child_env()[name] == "operator"

    def test_the_url_beats_an_operator_default(self, monkeypatch):
        monkeypatch.setenv("PGAPPNAME", "operator")

        child = PgConn.parse("postgresql:///db?application_name=url").child_env()

        assert child["PGAPPNAME"] == "url"

    def test_the_url_reaches_the_child(self, monkeypatch):
        monkeypatch.setenv("PGHOST", "elsewhere")

        child = PgConn.parse(self.URL).child_env()

        assert {k: child[k] for k in child if k.startswith("PG")} == {
            "PGUSER": "u",
            "PGPASSWORD": "pw",
            "PGHOST": "h",
            "PGPORT": "6543",
            "PGDATABASE": "db",
        }

    def test_non_pg_environment_is_kept(self, monkeypatch):
        monkeypatch.setenv("FRAISIER_KEPT", "yes")

        assert PgConn.parse(self.URL).child_env()["FRAISIER_KEPT"] == "yes"


class TestMovePasswordToEnvSharesTheDecoder:
    """``move_password_to_env`` decodes a URL as ``PgConn`` does.

    It applies no keyword refusals of its own: its callers, the sites that
    keep a URL on argv (D2), parse with ``PgConn`` first
    (``url_child``/``url_runner_child``).
    """

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://u:p%zz@h/db",
            "postgresql:///db?password=a%zz",
            "postgresql:///db?password=a%00b",
        ],
    )
    def test_malformed_password_escape_is_refused(self, url):
        from fraisier.dbops._url import move_password_to_env

        with pytest.raises(ValueError, match="percent-encoded"):
            move_password_to_env(url)

    def test_a_second_unencoded_at_is_refused(self):
        """libpq splits on the first ``@``; this used to split on the last."""
        from fraisier.dbops._url import move_password_to_env

        with pytest.raises(ValueError, match="%40"):
            move_password_to_env("postgresql://u:p@ss@h/db")

    def test_a_bare_password_parameter_is_refused(self):
        from fraisier.dbops._url import move_password_to_env

        with pytest.raises(ValueError, match="separator"):
            move_password_to_env("postgresql:///db?password")

    def test_a_keyword_without_env_var_is_still_passed_through(self):
        from fraisier.dbops._url import move_password_to_env

        url, extra = move_password_to_env("postgresql://u:pw@h/db?keepalives=1")

        assert (url, extra) == (
            "postgresql://u@h/db?keepalives=1",
            {"PGPASSWORD": "pw"},
        )


def test_imports_first_in_a_fresh_process():
    """Config validation imports it; ``fraisier.dbops._url`` cannot be first."""
    import subprocess
    import sys

    subprocess.run(
        [sys.executable, "-I", "-c", "import fraisier.dbops._pgconn"], check=True
    )
