"""``PgConn`` reads a PostgreSQL URL the way libpq does.

Every expectation here was captured from libpq (psql 18 against a live
server), not composed: ``.phases/2026-10-26-v0.95/probes/libpq-parse.sh``.
"""

from __future__ import annotations

import pytest

from fraisier.dbops._pgconn import (
    _KEYWORD_ENV,
    ENV_KEYWORD,
    OPERATOR_DEFAULTS,
    PgConn,
    decode,
    environment_refusal,
)


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
    """Every ambient ``PG*`` is dropped but a fixed operator allowlist.

    The URL names the target fully. An ambient variable that could change the
    server, the user, the database or the session (``PGPASSWORD`` would go
    to whatever host the URL names) must not reach the child. ``PGSERVICE`` is
    refused instead (``TestAmbientServiceIsRefused``).
    """

    URL = "postgresql://u:pw@h:6543/db"

    @pytest.mark.parametrize(
        "name",
        [
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
    keep a URL on argv, parse with ``PgConn`` first
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


class TestLibpqQueryLeniency:
    """What libpq accepts in a query, measured with psql 18."""

    @pytest.mark.parametrize("query", ["", "sslmode=disable&"])
    def test_an_empty_query_or_one_trailing_ampersand_is_accepted(self, query):
        conn = PgConn.parse(f"postgresql://h/db?{query}")

        assert conn.params["dbname"] == "db"

    @pytest.mark.parametrize(
        "query", ["&sslmode=disable", "sslmode=disable&&application_name=x"]
    )
    def test_an_empty_parameter_elsewhere_is_refused(self, query):
        with pytest.raises(ValueError, match="separator"):
            PgConn.parse(f"postgresql://h/db?{query}")

    def test_ssl_true_is_sslmode_require(self):
        assert PgConn.parse("postgresql://h/db?ssl=true").pg_env()["PGSSLMODE"] == (
            "require"
        )

    def test_a_later_sslmode_wins_over_ssl_true(self):
        conn = PgConn.parse("postgresql://h/db?ssl=true&sslmode=disable")

        assert conn.params["sslmode"] == "disable"

    def test_ssl_other_than_true_is_refused(self):
        with pytest.raises(ValueError, match='"ssl"'):
            PgConn.parse("postgresql://h/db?ssl=false")


class TestOneSplitForArgvAndEnv:
    """The argv URL is cut where PgConn cuts it, so no character can hide the
    password from one reading and not the other."""

    URL = "postgresql://app:se#cret@db.acme.test/prod"

    def test_a_hash_is_part_of_the_password(self):
        assert PgConn.parse(self.URL).params["password"] == "se#cret"

    def test_a_password_with_a_hash_leaves_argv(self):
        import fraisier.config  # _url's import cycle
        from fraisier.dbops._url import url_child, url_runner_child

        for child in (url_child, url_runner_child):
            argv_url, env = child(self.URL)

            assert "se#cret" not in argv_url
            assert argv_url == "postgresql://app@db.acme.test/prod"
            assert env["PGPASSWORD"] == "se#cret"

    def test_a_hash_survives_a_database_rename(self):
        import fraisier.config  # _url's import cycle
        from fraisier.dbops._url import replace_db_name

        renamed = replace_db_name(self.URL, "other")

        assert renamed == "postgresql://app:se#cret@db.acme.test/other"
        assert PgConn.parse(renamed).params["password"] == "se#cret"

    def test_a_trailing_ampersand_survives_both(self):
        import fraisier.config  # _url's import cycle
        from fraisier.dbops._url import move_password_to_env, replace_db_name

        url = "postgresql://app:pw@h/db?sslmode=require&"

        assert move_password_to_env(url) == (
            "postgresql://app@h/db?sslmode=require",
            {"PGPASSWORD": "pw"},
        )
        assert replace_db_name(url, "x") == "postgresql://app:pw@h/x?sslmode=require"


class TestTransportPolicyFailsClosed:
    """The allowlist drops ambient TLS/GSS policy, so a child it would
    have reached is refused, naming the URL fix; never silently downgraded."""

    TCP = "postgresql://acme@db.acme.test/acme_db"

    @pytest.mark.parametrize(
        ("var", "keyword"),
        [
            ("PGSSLMODE", "sslmode"),
            ("PGSSLCERTMODE", "sslcertmode"),
            ("PGSSLNEGOTIATION", "sslnegotiation"),
            ("PGSSLMINPROTOCOLVERSION", "ssl_min_protocol_version"),
            ("PGSSLMAXPROTOCOLVERSION", "ssl_max_protocol_version"),
            ("PGGSSENCMODE", "gssencmode"),
            ("PGCHANNELBINDING", "channel_binding"),
            ("PGKRBSRVNAME", "krbsrvname"),
            ("PGGSSLIB", "gsslib"),
        ],
    )
    def test_each_dropped_policy_refuses_a_tcp_child(self, monkeypatch, var, keyword):
        monkeypatch.setenv(var, "acme-value")

        with pytest.raises(ValueError, match=f"{var}.*{keyword}=acme-value"):
            PgConn.parse(self.TCP).child_env()

    def test_even_the_libpq_default_refuses(self, monkeypatch):
        monkeypatch.setenv("PGSSLMODE", "prefer")

        with pytest.raises(ValueError, match="sslmode=prefer"):
            PgConn.parse(self.TCP).child_env()

    def test_a_url_that_pins_it_is_fine(self, monkeypatch):
        monkeypatch.setenv("PGSSLMODE", "verify-full")

        env = PgConn.parse(self.TCP + "?sslmode=disable").child_env()

        assert env["PGSSLMODE"] == "disable"

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql:///acme_db",
            "postgresql:///acme_db?host=/run/postgresql",
            "postgresql://%2Frun%2Fpostgresql/acme_db",
            "postgresql:///acme_db?host=@acme",
            "postgresql:///acme_db?host=/run/a,/run/b",
        ],
    )
    def test_a_socket_url_is_exempt(self, monkeypatch, url):
        monkeypatch.setenv("PGSSLMODE", "verify-full")

        assert "PGSSLMODE" not in PgConn.parse(url).child_env()

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql:///acme_db?host=/run/postgresql&hostaddr=10.0.0.5",
            "postgresql:///acme_db?host=/run/postgresql,db.acme.test",
        ],
    )
    def test_any_tcp_host_is_checked(self, monkeypatch, url):
        monkeypatch.setenv("PGSSLMODE", "verify-full")

        with pytest.raises(ValueError, match="PGSSLMODE"):
            PgConn.parse(url).child_env()

    def test_check_environment_is_the_same_rule(self, monkeypatch):
        monkeypatch.setenv("PGGSSENCMODE", "require")

        with pytest.raises(ValueError, match="gssencmode=require"):
            PgConn.parse(self.TCP).check_environment()

    @pytest.mark.parametrize(
        ("url", "shown"),
        [
            (TCP, "prefer (no certificate check)"),
            (TCP + "?sslmode=verify-full", "verify-full"),
            (TCP + "?ssl=true", "require"),
            ("postgresql:///acme_db", "none (socket)"),
        ],
    )
    def test_the_effective_sslmode(self, url, shown):
        assert PgConn.parse(url).effective_sslmode() == shown


class TestAmbientServiceIsRefused:
    """An in-process connect cannot ignore ``PGSERVICE``: libpq reads it from
    the process environment, ``service=''`` names a service called ``""``,
    and a service fills every keyword left unpinned (measured:
    ``.phases/2026-10-28-v0.96/probes/service_and_resolved.out``). So it is
    refused wherever fraisier connects, children included, whatever its value.
    """

    @pytest.mark.parametrize("value", ["acme", ""])
    @pytest.mark.parametrize(
        "door", ["check_environment", "child_env", "connect_kwargs", "conninfo"]
    )
    def test_refused(self, monkeypatch, door, value):
        monkeypatch.setenv("PGSERVICE", value)

        with pytest.raises(ValueError, match="PGSERVICE is set in the environment"):
            getattr(PgConn.parse("postgresql:///acme_db"), door)()

    def test_the_runner_rule_is_the_same(self):
        refusal = environment_refusal({"PGSERVICE": "acme"}, {"PGDATABASE": "db"})

        assert refusal is not None
        assert "unset it" in refusal

    def test_a_service_file_alone_is_not(self, monkeypatch):
        """libpq reads a service file only for a named service."""
        monkeypatch.setenv("PGSERVICEFILE", "/etc/acme/pg_service.conf")

        PgConn.parse("postgresql:///acme_db").check_environment()


class TestAmbientSessionVariablesAreRefusedInProcess:
    """libpq sends ``PGTZ``, ``PGDATESTYLE`` and ``PGGEQO`` as startup
    settings. They have no conninfo keyword, so a connection fraisier opens
    in process cannot pin them, and an ambient value outranks even
    ``options='-c TimeZone=…'``. A child drops them with the rest of ``PG*``.
    """

    @pytest.mark.parametrize("var", ["PGTZ", "PGDATESTYLE", "PGGEQO"])
    @pytest.mark.parametrize("door", ["connect_kwargs", "conninfo"])
    def test_refused_in_process(self, monkeypatch, door, var):
        monkeypatch.setenv(var, "")

        with pytest.raises(ValueError, match=f"{var} is set in the environment"):
            getattr(PgConn.parse("postgresql:///acme_db"), door)()

    @pytest.mark.parametrize("var", ["PGTZ", "PGDATESTYLE", "PGGEQO"])
    def test_a_child_drops_it(self, monkeypatch, var):
        monkeypatch.setenv(var, "SQL, DMY")

        assert var not in PgConn.parse("postgresql:///acme_db").child_env()

    def test_the_message_names_the_fix(self, monkeypatch):
        monkeypatch.setenv("PGTZ", "Asia/Tokyo")

        with pytest.raises(ValueError, match="unset it"):
            PgConn.parse("postgresql:///acme_db").conninfo()


def _env_backed_keywords() -> dict[str, str | None]:
    """keyword → compiled default, for each keyword the installed libpq reads
    from an environment variable."""
    from psycopg import pq

    return {
        o.keyword.decode(): None if o.compiled is None else o.compiled.decode()
        for o in pq.Conninfo.get_defaults()
        if o.envvar
    }


class TestConnectKwargs:
    """In process there is no child environment to scrub: libpq fills every
    keyword the conninfo leaves out from ``os.environ``. So every env-backed
    keyword but the operator defaults is pinned, and the connection gets what
    a child given ``child_env()`` gets. Measured on libpq 18:
    ``.phases/2026-10-28-v0.96/probes/pin_every_keyword.out``.
    """

    URL = "postgresql://u:p%40w@h:6543/acme_db?sslmode=verify-full"

    def test_the_urls_keywords_are_passed_decoded(self):
        kwargs = PgConn.parse(self.URL).connect_kwargs()

        assert {k: kwargs[k] for k in ("user", "password", "host", "port")} == {
            "user": "u",
            "password": "p@w",
            "host": "h",
            "port": "6543",
        }
        assert (kwargs["dbname"], kwargs["sslmode"]) == ("acme_db", "verify-full")

    def test_every_other_env_backed_keyword_is_pinned(self):
        kwargs = PgConn.parse("postgresql:///acme_db").connect_kwargs()
        allowed = {k for k, v in _KEYWORD_ENV.items() if v in OPERATOR_DEFAULTS}
        expected = set(_env_backed_keywords()) - allowed - {"service"}

        assert set(kwargs) == expected

    @pytest.mark.parametrize(
        ("keyword", "pin"),
        [
            ("user", ""),
            ("options", ""),
            ("host", ""),
            ("hostaddr", ""),
            ("password", ""),
            ("client_encoding", ""),
            ("sslmode", "prefer"),
            ("port", "5432"),
            ("target_session_attrs", "any"),
            ("sslcertmode", "allow"),
            ("min_protocol_version", "3.0"),
            ("max_protocol_version", "3.0"),
        ],
    )
    def test_a_keyword_the_url_leaves_out_gets_libpqs_default(self, keyword, pin):
        """``''`` reads as unset; the three that refuse ``''`` get the value a
        clean environment resolves to."""
        assert PgConn.parse("postgresql:///acme_db").connect_kwargs()[keyword] == pin

    @pytest.mark.parametrize(
        ("query", "minimum", "maximum"),
        [
            ("min_protocol_version=3.2", "3.2", "latest"),
            ("min_protocol_version=latest", "latest", "latest"),
            ("min_protocol_version=3.0", "3.0", "3.0"),
            ("min_protocol_version=3.2&max_protocol_version=3.2", "3.2", "3.2"),
            ("max_protocol_version=3.2", "3.0", "3.2"),
        ],
    )
    def test_the_maximum_follows_the_urls_minimum(self, query, minimum, maximum):
        """libpq 18 derives an unset maximum: ``latest`` above a 3.0 minimum,
        else 3.0. A fixed 3.0 under a 3.2 minimum refuses to connect."""
        kwargs = PgConn.parse(f"postgresql:///acme_db?{query}").connect_kwargs()

        assert (kwargs["min_protocol_version"], kwargs["max_protocol_version"]) == (
            minimum,
            maximum,
        )

    def test_service_is_never_passed(self):
        assert "service" not in PgConn.parse("postgresql:///acme_db").connect_kwargs()

    @pytest.mark.parametrize("var", sorted(OPERATOR_DEFAULTS))
    def test_an_operator_default_is_left_to_libpq(self, var):
        kwargs = PgConn.parse("postgresql:///acme_db").connect_kwargs()

        assert ENV_KEYWORD[var] not in kwargs

    def test_ambient_variables_change_nothing(self, monkeypatch):
        clean = PgConn.parse("postgresql:///acme_db").connect_kwargs()
        for var in ("PGUSER", "PGOPTIONS", "PGHOST", "PGPORT", "PGDATABASE"):
            monkeypatch.setenv(var, "ambient")

        assert PgConn.parse("postgresql:///acme_db").connect_kwargs() == clean

    def test_conninfo_is_the_same_keywords(self):
        from psycopg.conninfo import conninfo_to_dict

        conn = PgConn.parse("postgresql://u:a%20'b@h/acme_db?options=-c%20x%3D1")

        assert conninfo_to_dict(conn.conninfo()) == conn.connect_kwargs()

    def test_the_transport_policy_applies(self, monkeypatch):
        monkeypatch.setenv("PGSSLMODE", "verify-full")

        with pytest.raises(ValueError, match="sslmode=verify-full"):
            PgConn.parse("postgresql://u@db.acme.test/acme_db").connect_kwargs()
