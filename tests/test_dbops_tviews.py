"""``fraisier.dbops.tviews``: what pg_tviews says about a database (#422).

The queries are pinned against a fake connection here; the same functions run
against a real pg_tviews in ``tests/integration/test_tviews_integration.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from fraisier.dbops import tviews


class FakeConn:
    """A psycopg connection that answers each query from a ``(match, rows)`` script.

    The first entry whose ``match`` occurs in the query text answers; a raised
    ``Exception`` entry is raised.  Every query is kept so a test can assert on
    what was asked, parameters included.
    """

    def __init__(self, *script: tuple[str, Any]) -> None:
        self.script = script
        self.queries: list[tuple[str, object]] = []
        #: column names for a query whose rows are asked for by name
        self.columns: tuple[str, ...] = ()

    def __enter__(self) -> FakeConn:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, query: Any, params: object = None) -> Any:
        text = query if isinstance(query, str) else query.as_string()
        self.queries.append((text, params))
        for match, answer in self.script:
            if match in text:
                if isinstance(answer, Exception):
                    raise answer
                rows = list(answer)
                return SimpleNamespace(
                    fetchone=lambda rows=rows: rows[0] if rows else None,
                    fetchall=lambda rows=rows: rows,
                    description=[SimpleNamespace(name=c) for c in self.columns],
                )
        raise AssertionError(f"unscripted query: {text}")


def patch_connect(monkeypatch: pytest.MonkeyPatch, conn: FakeConn) -> None:
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: conn)


EXTENSION = ("FROM pg_extension", [("tviews",)])
CONTRACT_ONE = ("contract_version", [(1,)])


class TestSupport:
    def test_absent_when_the_extension_is_not_installed(self) -> None:
        support = tviews.read_support(FakeConn(("FROM pg_extension", [])))

        assert (support.state, support.schema) == ("absent", None)

    def test_outdated_when_contract_version_is_missing(self) -> None:
        missing = psycopg.errors.UndefinedFunction("no contract_version")
        support = tviews.read_support(
            FakeConn(EXTENSION, ("contract_version", missing))
        )

        assert support.state == "outdated"

    def test_outdated_when_the_contract_is_not_one(self) -> None:
        support = tviews.read_support(FakeConn(EXTENSION, ("contract_version", [(0,)])))

        assert support.state == "outdated"

    def test_ok_on_contract_one_and_carries_the_extension_schema(self) -> None:
        conn = FakeConn(("FROM pg_extension", [("pgx",)]), CONTRACT_ONE)

        support = tviews.read_support(conn)

        assert (support.state, support.schema) == ("ok", "pgx")
        # resolved from the catalog, not assumed to be `tviews`
        assert '"pgx".contract_version()' in conn.queries[-1][0]

    def test_installed_is_true_only_on_contract_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_connect(monkeypatch, FakeConn(EXTENSION, CONTRACT_ONE))
        assert tviews.tviews_installed("postgresql:///app") is True

        patch_connect(monkeypatch, FakeConn(("FROM pg_extension", [])))
        assert tviews.tviews_installed("postgresql:///app") is False

        patch_connect(monkeypatch, FakeConn(EXTENSION, ("contract_version", [(0,)])))
        assert tviews.tviews_installed("postgresql:///app") is False


REGISTRY = (
    'FROM "tviews".registry',
    [("public", "tv_post", "public", "v_post"), ("app", "tv_user", "app", "v_user")],
)


def probe(tview: str, *, tv: bool, view: bool) -> tuple[str, list[tuple[bool, bool]]]:
    return (f'EXISTS (SELECT 1 FROM "{tview}"', [(tv, view)])


class TestAnUnreadableTviewCannotBeVerified:
    """A role that cannot read a TVIEW or its backing view is told nothing about it.

    From pg_tviews 0.1.0-beta.25 the backing view lives in ``tviews`` and takes
    its TVIEW table's ``SELECT`` grants, so a role that reads ``tv_<entity>``
    reads the view.  A role that read the *old* ``v_<entity>`` only through a
    grant on the view loses it.  That is a configuration the probe cannot judge,
    not an empty TVIEW and not a reason to stop the deploy: it is reported, by
    name, and the TVIEWs it can read are still checked.  Any other failure is
    still an error.
    """

    @staticmethod
    def _denied() -> psycopg.errors.InsufficientPrivilege:
        return psycopg.errors.InsufficientPrivilege("permission denied for view")

    def _conn(self, first: Any) -> FakeConn:
        return FakeConn(
            EXTENSION,
            CONTRACT_ONE,
            REGISTRY,
            (
                'EXISTS (SELECT 1 FROM "public"."tv_post"',
                first,
            ),
            probe('app"."tv_user', tv=False, view=True),
        )

    def test_a_denied_probe_does_not_fail_and_the_rest_are_still_checked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_connect(monkeypatch, self._conn(self._denied()))

        found = tviews.find_empty_tviews("postgresql:///app")

        assert [e.tview for e in found] == ["app.tv_user"]

    def test_the_warning_names_the_tview_and_its_view(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        patch_connect(monkeypatch, self._conn(self._denied()))

        with caplog.at_level("WARNING", logger="fraisier.dbops.tviews"):
            tviews.find_empty_tviews("postgresql:///app")

        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "public.tv_post" in warnings[0]
        assert "public.v_post" in warnings[0]
        assert "GRANT SELECT ON public.tv_post" in warnings[0]

    def test_any_other_failure_is_still_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = psycopg.errors.UndefinedTable("relation does not exist")
        patch_connect(monkeypatch, self._conn(missing))

        with pytest.raises(psycopg.errors.UndefinedTable):
            tviews.find_empty_tviews("postgresql:///app")

    def test_a_readable_database_warns_about_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        patch_connect(monkeypatch, self._conn([(True, True)]))

        with caplog.at_level("WARNING", logger="fraisier.dbops.tviews"):
            tviews.find_empty_tviews("postgresql:///app")

        assert not caplog.records


class TestFindEmpty:
    def test_a_tview_with_no_rows_over_a_view_with_rows_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = FakeConn(
            EXTENSION,
            CONTRACT_ONE,
            REGISTRY,
            probe('public"."tv_post', tv=False, view=True),
            probe('app"."tv_user', tv=True, view=True),
        )
        patch_connect(monkeypatch, conn)

        found = tviews.find_empty_tviews("postgresql:///app")

        assert [(e.tview, e.view) for e in found] == [
            ("public.tv_post", "public.v_post")
        ]

    def test_an_empty_tview_over_an_empty_view_is_not_a_finding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = FakeConn(
            EXTENSION,
            CONTRACT_ONE,
            REGISTRY,
            probe('public"."tv_post', tv=False, view=False),
            probe('app"."tv_user', tv=False, view=False),
        )
        patch_connect(monkeypatch, conn)

        assert tviews.find_empty_tviews("postgresql:///app") == []

    def test_a_database_without_pg_tviews_has_nothing_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_connect(monkeypatch, FakeConn(("FROM pg_extension", [])))

        assert tviews.find_empty_tviews("postgresql:///app") == []

    def test_an_outdated_pg_tviews_is_an_error_not_a_clean_bill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_connect(monkeypatch, FakeConn(EXTENSION, ("contract_version", [(0,)])))

        with pytest.raises(tviews.TviewError, match=r"0\.1\.0-beta\.20"):
            tviews.find_empty_tviews("postgresql:///app")


class TestRebuild:
    def test_only_empty_is_asked_for_by_default_and_rows_are_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = FakeConn(
            EXTENSION,
            CONTRACT_ONE,
            ("pg_is_in_recovery", [(False,)]),
            ("pg_tviews_rebuild_all", [("post", 3)]),
        )
        patch_connect(monkeypatch, conn)

        rebuilt = tviews.rebuild_empty_tviews("postgresql:///app")

        assert rebuilt == [tviews.TviewRebuilt("post", 3)]
        assert conn.queries[-1] == (
            'SELECT entity, rows FROM "tviews".pg_tviews_rebuild_all(only_empty => %s)',
            (True,),
        )

    def test_rebuilding_everything_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = FakeConn(
            EXTENSION,
            CONTRACT_ONE,
            ("pg_is_in_recovery", [(False,)]),
            ("pg_tviews_rebuild_all", []),
        )
        patch_connect(monkeypatch, conn)

        tviews.rebuild_all_tviews("postgresql:///app")

        assert conn.queries[-1][1] == (False,)

    def test_a_standby_is_refused_in_plain_words(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = FakeConn(EXTENSION, CONTRACT_ONE, ("pg_is_in_recovery", [(True,)]))
        patch_connect(monkeypatch, conn)

        with pytest.raises(tviews.TviewError, match=r"recovery.*primary"):
            tviews.rebuild_empty_tviews("postgresql:///app")

    def test_a_database_without_pg_tviews_rebuilds_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_connect(monkeypatch, FakeConn(("FROM pg_extension", [])))

        assert tviews.rebuild_empty_tviews("postgresql:///app") == []

    def test_a_failed_rebuild_surfaces_the_server_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = FakeConn(
            EXTENSION,
            CONTRACT_ONE,
            ("pg_is_in_recovery", [(False,)]),
            ("pg_tviews_rebuild_all", psycopg.errors.InternalError("refresh blew up")),
        )
        patch_connect(monkeypatch, conn)

        with pytest.raises(tviews.TviewError, match="refresh blew up"):
            tviews.rebuild_empty_tviews("postgresql:///app")


class TestProfile:
    def test_each_row_is_keyed_by_column_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        columns = ("entity", "tview", "persistence", "rows_estimate", "warnings")
        conn = FakeConn(
            EXTENSION,
            CONTRACT_ONE,
            ("pg_tviews_profile", [("post", "tv_post", "unlogged", 12, [])]),
        )
        conn.columns = columns
        patch_connect(monkeypatch, conn)

        (row,) = tviews.profile_tviews("postgresql:///app")

        assert row["entity"] == "post"
        assert row["persistence"] == "unlogged"
        assert row["rows_estimate"] == 12
        assert conn.queries[-1][0] == 'SELECT * FROM "tviews".pg_tviews_profile()'

    def test_a_database_without_pg_tviews_has_no_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_connect(monkeypatch, FakeConn(("FROM pg_extension", [])))

        assert tviews.profile_tviews("postgresql:///app") == []
