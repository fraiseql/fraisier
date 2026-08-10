"""Relation-file mtimes: the check #358 asks for, as a secondary signal.

#358 names relation-file mtimes as "the only check known to catch" a staging
database that silently stayed stale, and it is right that they can. It is a
weaker instrument than the receipt, in two specific ways that belong in the code
rather than in a postmortem:

- ``pg_stat_file`` needs superuser or ``pg_read_server_files``, which managed
  PostgreSQL commonly refuses. A denial is UNVERIFIABLE — never a pass, and
  never a failure either.
- Autovacuum and HOT pruning touch heap files, so an mtime can move after a
  no-op. That is a false *pass*, never a false fail. An asymmetry that fails
  safe is fine for corroboration and disqualifying as a sole guard.

So this corroborates the receipt; it does not replace it.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from fraisier.dbops.receipt import ActuationVerdict, relation_freshness

_URL = "postgresql://admin@localhost:5432/postgres"


def _one(result):
    def fake(cmd, *, connection_url, input_text=None):
        return result

    return fake


def _summary(total=4, fresh=4, oldest_age_hours=0.5):
    return json.dumps(
        {"total": total, "fresh": fresh, "oldest_age_hours": oldest_age_hours}
    )


def test_every_heap_written_inside_the_window_is_actuated():
    with patch("fraisier.dbops.receipt._pg_cmd", _one((0, _summary() + "\n", ""))):
        check = relation_freshness(
            "stagingdb", schema="public", connection_url=_URL, within_hours=24
        )
    assert check.verdict is ActuationVerdict.ACTUATED


def test_a_heap_older_than_the_window_is_stale():
    with patch(
        "fraisier.dbops.receipt._pg_cmd",
        _one((0, _summary(total=4, fresh=3, oldest_age_hours=30.0) + "\n", "")),
    ):
        check = relation_freshness(
            "stagingdb", schema="public", connection_url=_URL, within_hours=24
        )
    assert check.verdict is ActuationVerdict.STALE
    assert check.is_bad is True
    assert "1 of 4" in check.detail


def test_permission_denied_is_unverifiable_not_a_pass():
    """A managed Postgres that forbids pg_stat_file has told us nothing.

    Reading a denial as a pass is the original silent hole in a new place; the
    check must also not demand the privilege, or it only runs where it is least
    needed.
    """
    with patch(
        "fraisier.dbops.receipt._pg_cmd",
        _one((1, "", "ERROR:  permission denied for function pg_stat_file")),
    ):
        check = relation_freshness(
            "stagingdb", schema="public", connection_url=_URL, within_hours=24
        )
    assert check.verdict is ActuationVerdict.UNVERIFIABLE
    assert check.is_bad is False
    assert check.is_actuated is False
    assert "pg_read_server_files" in check.detail


def test_a_schema_with_no_base_tables_is_unverifiable_not_all_fresh():
    """Zero of zero tables fresh is not evidence that a restore ran."""
    with patch(
        "fraisier.dbops.receipt._pg_cmd",
        _one((0, _summary(total=0, fresh=0, oldest_age_hours=None) + "\n", "")),
    ):
        check = relation_freshness(
            "stagingdb", schema="tenant", connection_url=_URL, within_hours=24
        )
    assert check.verdict is ActuationVerdict.UNVERIFIABLE
    assert "tenant" in check.detail


def test_unparseable_output_is_unverifiable():
    with patch("fraisier.dbops.receipt._pg_cmd", _one((0, "nonsense\n", ""))):
        check = relation_freshness(
            "stagingdb", schema="public", connection_url=_URL, within_hours=24
        )
    assert check.verdict is ActuationVerdict.UNVERIFIABLE


def test_a_missing_psql_does_not_raise():
    def boom(cmd, *, connection_url, input_text=None):
        raise FileNotFoundError("psql")

    with patch("fraisier.dbops.receipt._pg_cmd", boom):
        check = relation_freshness(
            "stagingdb", schema="public", connection_url=_URL, within_hours=24
        )
    assert check.verdict is ActuationVerdict.UNVERIFIABLE


def test_the_schema_is_bound_never_interpolated():
    seen = {}

    def fake(cmd, *, connection_url, input_text=None):
        seen["cmd"] = cmd
        seen["sql"] = input_text
        return 0, _summary() + "\n", ""

    with patch("fraisier.dbops.receipt._pg_cmd", fake):
        relation_freshness(
            "stagingdb", schema="ten'ant", connection_url=_URL, within_hours=24
        )

    assert "schema=ten'ant" in seen["cmd"]
    assert "ten'ant" not in seen["sql"]
    assert ":'schema'" in seen["sql"]


def test_the_window_is_measured_by_the_server():
    """``now()`` server-side, so a drifting client clock cannot fake freshness."""
    from fraisier.dbops.receipt import _FRESHNESS_SQL

    assert "now() -" in _FRESHNESS_SQL


def test_one_schema_never_summed():
    """The per-schema rule the floor already follows (archive.py:60-71)."""
    from fraisier.dbops.receipt import _FRESHNESS_SQL

    assert "n.nspname = :'schema'" in _FRESHNESS_SQL
