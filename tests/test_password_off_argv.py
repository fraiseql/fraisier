"""No database password on a child's argv (#491).

Any local user can read argv through ``ps`` or ``/proc/<pid>/cmdline``. Each
call site below hands a URL to a child, so each must hand it over without its
password and put the password in the child's ``PGPASSWORD``. libpq reads
``PGPASSWORD`` whenever the connection string carries no password, so the
child still connects. The integration suite proves that part: CI's
``FRAISIER_TEST_PG_URL`` is a password TCP DSN.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fraisier.dbops._url import move_password_to_env

SECRET = "s3cr%40t"  # percent-encoded "s3cr@t", as a URL must carry it
DECODED = "s3cr@t"
URL = f"postgresql://postgres:{SECRET}@db:5432/acme"


class TestMovePasswordToEnv:
    def test_userinfo_password_moves_to_the_env(self):
        url, env = move_password_to_env(URL)
        assert url == "postgresql://postgres@db:5432/acme"
        assert env == {"PGPASSWORD": DECODED}

    def test_plus_is_not_a_space(self):
        """libpq percent-decodes a URI but leaves `+` alone."""
        _, env = move_password_to_env("postgresql://u:a+b@h/acme")
        assert env == {"PGPASSWORD": "a+b"}

    def test_query_password_moves_and_other_parameters_stay(self):
        url, env = move_password_to_env(
            "postgresql:///acme?host=/run/postgresql&password=x%26y&sslmode=disable"
        )
        assert url == "postgresql:///acme?host=/run/postgresql&sslmode=disable"
        assert env == {"PGPASSWORD": "x&y"}

    def test_query_password_wins_over_userinfo(self):
        """libpq reads the query after the userinfo, so the query's value is used."""
        url, env = move_password_to_env("postgresql://u:one@h/acme?password=two")
        assert url == "postgresql://u@h/acme"
        assert env == {"PGPASSWORD": "two"}

    def test_empty_password_is_dropped_without_an_env(self):
        assert move_password_to_env("postgresql://u:@h/acme") == (
            "postgresql://u@h/acme",
            {},
        )

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql:///acme?host=/var/run/postgresql",
            "postgresql://u@h:5432/acme",
            "postgres://h/acme",
            "host=h dbname=acme",
        ],
    )
    def test_a_url_without_a_password_is_unchanged(self, url):
        assert move_password_to_env(url) == (url, {})


class _Captured(Exception):
    """Raised by a fake child once it has recorded its argv and env."""


def _fake_run(calls):
    def run(cmd, *args, **kwargs):
        calls.append((list(cmd), kwargs.get("env")))
        raise _Captured

    return run


def _assert_password_off_argv(calls):
    assert calls, "the child was never started"
    for argv, env in calls:
        assert not [a for a in argv if SECRET in a or DECODED in a], argv
        assert env is not None
        assert env["PGPASSWORD"] == DECODED
        assert "PATH" in env, "the child's env must extend os.environ, not replace it"


class _Runner:
    def __init__(self):
        self.calls = []

    def run(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs.get("env")))
        raise _Captured


def _assert_runner_got_only_the_password(runner):
    """An SSHRunner exports every env key on the remote command line, so a
    runner is handed the password alone, never a copy of os.environ."""
    assert runner.calls, "the child was never started"
    for argv, env in runner.calls:
        assert not [a for a in argv if SECRET in a or DECODED in a], argv
        assert env == {"PGPASSWORD": DECODED}


def test_local_runner_extends_the_environment(monkeypatch):
    """A runner's env is added to ours: replacing it would drop PATH."""
    from fraisier.runners import LocalRunner

    monkeypatch.setenv("FRAISIER_TEST_INHERITED", "kept")
    result = LocalRunner().run(
        ["sh", "-c", 'printf "%s %s" "$FRAISIER_TEST_INHERITED" "$PGPASSWORD"'],
        env={"PGPASSWORD": "pw"},
    )
    assert result.stdout == "kept pw"


def test_restore_preflight_against(monkeypatch, tmp_path):
    from fraisier.dbops import preflight

    calls = []
    monkeypatch.setattr(preflight.subprocess, "run", _fake_run(calls))
    with pytest.raises(_Captured):
        preflight._run_confiture_preflight(tmp_path / "c.yaml", tmp_path, URL)
    _assert_password_off_argv(calls)


def test_template_decision_database_url(monkeypatch, tmp_path):
    from fraisier.dbops import template_rule

    calls = []
    monkeypatch.setattr(template_rule.subprocess, "run", _fake_run(calls))
    with pytest.raises(_Captured):
        template_rule.read_pending_preflight(
            tmp_path / "c.yaml", tmp_path, database_url=URL
        )
    _assert_password_off_argv(calls)


def test_strategy_apply_sql(monkeypatch, tmp_path):
    from fraisier.strategies import _core

    calls = []
    monkeypatch.setattr(_core.subprocess, "run", _fake_run(calls))
    with pytest.raises(_Captured):
        _core.RebuildStrategy._apply_sql(URL, tmp_path / "s.sql")
    _assert_password_off_argv(calls)


def test_database_size_query():
    from fraisier.dbops.sizing import query_database_size_mb

    runner = _Runner()
    with pytest.raises(_Captured):
        query_database_size_mb(URL, runner=runner)
    _assert_runner_got_only_the_password(runner)


def test_post_migrate_step():
    from fraisier.post_migrate import _run_one

    runner = _Runner()
    with pytest.raises(_Captured):
        _run_one(Path("step.sql"), database_url=URL, runner=runner)
    _assert_runner_got_only_the_password(runner)


def test_the_fake_child_sees_what_subprocess_would():
    """Meta: `_fake_run` must record a real call's argv and env unchanged."""
    calls = []
    with pytest.raises(_Captured):
        _fake_run(calls)(["psql", "x"], env={"PATH": "/bin"}, check=False)
    assert calls == [(["psql", "x"], {"PATH": "/bin"})]
