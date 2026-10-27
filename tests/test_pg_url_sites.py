"""The children that still take a URL on argv read it through ``PgConn``.

confiture's interface takes a URL, and so do these psql calls, so the URL
stays on argv, password-free. What changes is everything around it: the URL
is refused when libpq would read it differently, and the child's environment
is :meth:`PgConn.child_env` — no ambient ``PGSERVICE`` (a service file
outranks every variable), ``PGPASSWORD`` (sent to whatever host the URL
names) or ``PGOPTIONS``.
"""

from __future__ import annotations

import contextlib
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

URL = "postgresql://u:pw@h:5432/db"

_AMBIENT = {"PGSERVICE": "acme", "PGPASSWORD": "ambient", "PGOPTIONS": "-c x=1"}


def _apply_sql(url: str, tmp_path: Path) -> None:
    from fraisier.strategies import RebuildStrategy

    RebuildStrategy._apply_sql(url, tmp_path / "s.sql")


def _preflight_against(url: str, tmp_path: Path) -> None:
    from fraisier.dbops.preflight import _run_confiture_preflight

    _run_confiture_preflight(tmp_path / "c.yaml", tmp_path, url)


def _pending_preflight(url: str, tmp_path: Path) -> None:
    from fraisier.dbops.template_rule import read_pending_preflight

    read_pending_preflight(tmp_path / "c.yaml", tmp_path, database_url=url)


SITES: dict[str, tuple[str, Callable[[str, Path], None]]] = {
    "rebuild _apply_sql": ("fraisier.strategies._core.subprocess.run", _apply_sql),
    "preflight --against": (
        "fraisier.dbops.preflight.subprocess.run",
        _preflight_against,
    ),
    "pending preflight": (
        "fraisier.dbops.template_rule.subprocess.run",
        _pending_preflight,
    ),
}


def _launch(site: str, url: str, tmp_path: Path) -> tuple[list[str], dict[str, str]]:
    """Run *site* against *url*; return the argv and env its child got."""
    target, call = SITES[site]
    done = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
    with patch(target, return_value=done) as run:
        try:
            call(url, tmp_path)
        except Exception:
            # Only the launch is under test; parsing the fake result may fail.
            if not run.called:
                raise
    return run.call_args.args[0], run.call_args.kwargs["env"]


@pytest.mark.parametrize("site", SITES)
class TestEachUrlSite:
    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://u@h/db?keepalives=1",
            "postgresql://u@h/db?service=acme",
            "postgresql://u@h",
        ],
    )
    def test_a_url_libpq_would_read_differently_is_refused(self, site, url, tmp_path):
        target, call = SITES[site]
        with patch(target) as run, pytest.raises(ValueError):
            call(url, tmp_path)
        run.assert_not_called()

    def test_the_password_is_off_argv(self, site, tmp_path):
        argv, env = _launch(site, URL, tmp_path)

        assert not any("pw" in arg for arg in argv)
        assert env["PGPASSWORD"] == "pw"

    @pytest.mark.parametrize("name", sorted(_AMBIENT))
    def test_an_ambient_variable_does_not_reach_the_child(
        self, site, name, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(name, _AMBIENT[name])

        _, env = _launch(site, URL, tmp_path)

        assert env.get(name) != _AMBIENT[name]

    def test_an_operator_default_still_reaches_the_child(
        self, site, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("PGSSLROOTCERT", "/etc/acme/root.crt")

        _, env = _launch(site, URL, tmp_path)

        assert env["PGSSLROOTCERT"] == "/etc/acme/root.crt"


def _size(url: str, runner: MagicMock) -> None:
    from fraisier.dbops.sizing import query_database_size_mb

    query_database_size_mb(url, runner=runner)


def _post_migrate(url: str, runner: MagicMock) -> None:
    from pathlib import Path

    from fraisier.post_migrate import _run_one

    _run_one(Path("/srv/acme/post.sql"), database_url=url, runner=runner)


RUNNER_SITES: dict[str, Callable[[str, MagicMock], None]] = {
    "database size": _size,
    "post-migrate step": _post_migrate,
}


@pytest.mark.parametrize("site", RUNNER_SITES)
class TestEachRunnerUrlSite:
    """Through a ``CommandRunner``, which may be on another host: the env is
    the URL's own ``PG*`` and nothing of ``os.environ``, and the runner
    scrubs whatever ``PG*`` the child's own environment holds."""

    @staticmethod
    def _launch(site: str, url: str) -> MagicMock:
        runner = MagicMock()
        runner.run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="1048576\n", stderr=""
        )
        RUNNER_SITES[site](url, runner)
        return runner.run

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://u@h/db?keepalives=1",
            "postgresql://u@h/db?service=acme",
            "postgresql://u@h",
        ],
    )
    def test_a_url_libpq_would_read_differently_is_refused(self, site, url):
        """Refused before the runner runs anything. The size query, which
        is best-effort, reports it as no size; the post-migrate step raises."""
        runner = MagicMock()
        with contextlib.suppress(ValueError):
            RUNNER_SITES[site](url, runner)
        runner.run.assert_not_called()

    def test_the_runner_scrubs_and_gets_only_the_urls_env(self, site, monkeypatch):
        monkeypatch.setenv("FRAISIER_AMBIENT", "local")

        run = self._launch(site, URL)

        argv, kwargs = run.call_args.args[0], run.call_args.kwargs
        assert not any("pw" in arg for arg in argv)
        assert kwargs["scrub_pg"] is True
        assert kwargs["env"] == {
            "PGUSER": "u",
            "PGPASSWORD": "pw",
            "PGHOST": "h",
            "PGPORT": "5432",
            "PGDATABASE": "db",
        }


def test_a_refused_url_is_no_size():
    from fraisier.dbops.sizing import query_database_size_mb

    assert query_database_size_mb("postgresql://u@h", runner=MagicMock()) is None


def test_a_refused_url_fails_the_post_migrate_step():
    from pathlib import Path

    from fraisier.post_migrate import _run_one

    with pytest.raises(ValueError, match="names no database"):
        _run_one(
            Path("/srv/acme/p.sql"), database_url="postgresql://u@h", runner=MagicMock()
        )
