"""The restore actuation receipt (#358).

The receipt answers "which fraisier run last rewrote this database, and when?" —
the question no table count and no row count can answer, because a database that
was never rewritten has entirely correct counts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from fraisier.dbops.receipt import (
    RECEIPT_TABLE,
    ActuationVerdict,
    RestoreReceipt,
    verify_actuation,
    write_receipt,
)

_URL = "postgresql://admin@localhost:5432/postgres"


def _row_json(**overrides) -> str:
    row = {
        "run_id": "abc123",
        "backup_path": "/backup/prod.dump",
        "backup_bytes": 4096,
        "restored_at": "2026-08-10T09:00:00+00:00",
        "age_seconds": 120.0,
        "floor_schema": "public",
    }
    row.update(overrides)
    return json.dumps(row)


def _responses(*results):
    """Return a fake ``_pg_cmd`` yielding *results* in order."""
    calls = list(results)

    def fake(cmd, *, connection_url, input_text=None):
        return calls.pop(0)

    return fake


# --------------------------------------------------------------------------
# Cycle 1: the verdicts
# --------------------------------------------------------------------------


def test_only_stale_is_bad():
    """``is_bad`` convicts STALE alone — MISSING and UNVERIFIABLE are silence.

    Same rule as ``ArchiveCheck.is_bad``: a database restored by hand or by an
    older fraisier carries no receipt, and a host that cannot reach it learned
    nothing. Neither is evidence of a stale database.
    """
    from fraisier.dbops.receipt import ActuationCheck

    assert ActuationCheck(ActuationVerdict.STALE, "d").is_bad is True
    assert ActuationCheck(ActuationVerdict.MISSING, "d").is_bad is False
    assert ActuationCheck(ActuationVerdict.UNVERIFIABLE, "d").is_bad is False
    assert ActuationCheck(ActuationVerdict.ACTUATED, "d").is_bad is False


def test_only_actuated_is_proof():
    """``is_actuated`` is the only verdict a caller may read as "it ran"."""
    from fraisier.dbops.receipt import ActuationCheck

    assert ActuationCheck(ActuationVerdict.ACTUATED, "d").is_actuated is True
    for other in (
        ActuationVerdict.STALE,
        ActuationVerdict.MISSING,
        ActuationVerdict.UNVERIFIABLE,
    ):
        assert ActuationCheck(other, "d").is_actuated is False


# --------------------------------------------------------------------------
# Cycle 2: writing
# --------------------------------------------------------------------------


def test_write_binds_every_value_and_stops_on_error():
    """Values reach SQL as psql variables, never by interpolation.

    ``ON_ERROR_STOP=1`` is not decoration: psql exits 0 on a failed statement
    without it, so the caller would read a failed write as a successful one.

    The script goes in on stdin because ``-c`` would not be lexed by psql at
    all — the server would receive a literal ``:'run_id'`` and reject it.
    """
    seen = {}

    def fake(cmd, *, connection_url, input_text=None):
        seen["cmd"] = cmd
        seen["url"] = connection_url
        seen["sql"] = input_text
        return 0, "", ""

    receipt = RestoreReceipt(
        run_id="tok",
        backup_path="/backup/x.dump",
        backup_bytes=17,
        restored_at=datetime.now(UTC),
        age_seconds=0.0,
    )
    with patch("fraisier.dbops.receipt._pg_cmd", fake):
        assert write_receipt("stagingdb", connection_url=_URL, receipt=receipt) is None

    cmd = seen["cmd"]
    assert cmd[0] == "psql"
    assert "ON_ERROR_STOP=1" in cmd
    assert "run_id=tok" in cmd
    assert "backup_path=/backup/x.dump" in cmd
    assert "backup_bytes=17" in cmd
    # Read by psql, not handed to the server unread — otherwise no substitution.
    assert cmd[-2:] == ["-f", "-"]
    assert "-c" not in cmd
    # The SQL body carries placeholders, not the values themselves.
    sql = seen["sql"]
    assert ":'run_id'" in sql
    assert "tok" not in sql
    assert "/backup/x.dump" not in sql


def test_write_reports_failure_instead_of_raising():
    """A failed write returns why. It must not raise into the restore pipeline."""
    receipt = RestoreReceipt("t", "/b.dump", 1, datetime.now(UTC), 0.0)
    with patch(
        "fraisier.dbops.receipt._pg_cmd", _responses((1, "", "permission denied"))
    ):
        detail = write_receipt("stagingdb", connection_url=_URL, receipt=receipt)
    assert detail is not None
    assert "permission denied" in detail


def test_write_survives_a_host_without_psql():
    """No client tools is a reported failure, not a traceback."""
    receipt = RestoreReceipt("t", "/b.dump", 1, datetime.now(UTC), 0.0)

    def boom(cmd, *, connection_url, input_text=None):
        raise FileNotFoundError("psql")

    with patch("fraisier.dbops.receipt._pg_cmd", boom):
        detail = write_receipt("stagingdb", connection_url=_URL, receipt=receipt)
    assert detail is not None
    assert "psql" in detail


def test_write_validates_the_database_name():
    receipt = RestoreReceipt("t", "/b.dump", 1, datetime.now(UTC), 0.0)
    with pytest.raises(ValueError, match="database name"):
        write_receipt('bad"; DROP', connection_url=_URL, receipt=receipt)


# --------------------------------------------------------------------------
# Cycle 3: reading and verifying
# --------------------------------------------------------------------------


def test_absent_table_is_missing_not_stale():
    """A database that has never been restored by this fraisier has no receipt."""
    with patch("fraisier.dbops.receipt._pg_cmd", _responses((0, "f\n", ""))):
        check = verify_actuation("stagingdb", connection_url=_URL, max_age_hours=24)
    assert check.verdict is ActuationVerdict.MISSING
    assert check.is_bad is False
    assert RECEIPT_TABLE in check.detail


def test_unreachable_database_is_unverifiable():
    """Cannot ask is not the same as asked and got a bad answer."""
    with patch(
        "fraisier.dbops.receipt._pg_cmd",
        _responses((2, "", "could not connect to server")),
    ):
        check = verify_actuation("stagingdb", connection_url=_URL, max_age_hours=24)
    assert check.verdict is ActuationVerdict.UNVERIFIABLE
    assert check.is_bad is False
    assert check.is_actuated is False


def test_table_present_but_empty_is_missing():
    with patch(
        "fraisier.dbops.receipt._pg_cmd", _responses((0, "t\n", ""), (0, "\n", ""))
    ):
        check = verify_actuation("stagingdb", connection_url=_URL, max_age_hours=24)
    assert check.verdict is ActuationVerdict.MISSING


def test_unparseable_row_is_unverifiable():
    """Garbage out of psql is not a verdict about the database."""
    with patch(
        "fraisier.dbops.receipt._pg_cmd",
        _responses((0, "t\n", ""), (0, "not json\n", "")),
    ):
        check = verify_actuation("stagingdb", connection_url=_URL, max_age_hours=24)
    assert check.verdict is ActuationVerdict.UNVERIFIABLE


def test_matching_token_is_actuated():
    """The run that minted the token reads it back out of the database."""
    with patch(
        "fraisier.dbops.receipt._pg_cmd",
        _responses((0, "t\n", ""), (0, _row_json(run_id="abc123") + "\n", "")),
    ):
        check = verify_actuation(
            "stagingdb", connection_url=_URL, expected_run_id="abc123"
        )
    assert check.verdict is ActuationVerdict.ACTUATED
    assert check.receipt is not None
    assert check.receipt.run_id == "abc123"
    assert check.receipt.backup_bytes == 4096


def test_a_different_token_is_stale():
    """The receipt names someone else's run: this one did not write it.

    This is the #343 signature seen from inside the pipeline — the database was
    not rewritten by the run now claiming to have rewritten it.
    """
    with patch(
        "fraisier.dbops.receipt._pg_cmd",
        _responses((0, "t\n", ""), (0, _row_json(run_id="yesterday") + "\n", "")),
    ):
        check = verify_actuation(
            "stagingdb", connection_url=_URL, expected_run_id="today"
        )
    assert check.verdict is ActuationVerdict.STALE
    assert check.is_bad is True
    # The row is carried even when it fails, so the caller can say whose it is.
    assert check.receipt is not None
    assert check.receipt.run_id == "yesterday"


def test_a_receipt_inside_the_window_is_actuated():
    with patch(
        "fraisier.dbops.receipt._pg_cmd",
        _responses((0, "t\n", ""), (0, _row_json(age_seconds=3600.0) + "\n", "")),
    ):
        check = verify_actuation("stagingdb", connection_url=_URL, max_age_hours=24)
    assert check.verdict is ActuationVerdict.ACTUATED


def test_a_receipt_older_than_the_window_is_stale():
    """A day-stale staging database, which is what #343 reported."""
    with patch(
        "fraisier.dbops.receipt._pg_cmd",
        _responses((0, "t\n", ""), (0, _row_json(age_seconds=100_000.0) + "\n", "")),
    ):
        check = verify_actuation("stagingdb", connection_url=_URL, max_age_hours=24)
    assert check.verdict is ActuationVerdict.STALE
    assert "27.8" in check.detail or "27" in check.detail


def test_age_is_measured_by_the_server_not_the_client():
    """``age_seconds`` comes out of the database, so clock skew cannot fake it.

    A host whose clock drifts would otherwise be able to make a stale restore
    look fresh, or a fresh one look stale.
    """
    from fraisier.dbops.receipt import _READ_SQL

    assert "now() - restored_at" in _READ_SQL


def test_a_criterion_is_required():
    """Reading the receipt is not a verdict on it.

    Without something to check against, "a row exists" would be reported as
    proof — and a no-op restore leaves the previous run's row in place, so
    presence alone always matches.
    """
    with pytest.raises(ValueError, match="criterion"):
        verify_actuation("stagingdb", connection_url=_URL)


def test_verify_validates_the_database_name():
    with pytest.raises(ValueError, match="database name"):
        verify_actuation('bad"; DROP', connection_url=_URL, max_age_hours=1)


def test_the_write_is_one_transaction():
    """A half-written receipt reads as MISSING, which is a lie about the run.

    The script creates, deletes and inserts. Run as separate statements, a
    failure at the INSERT commits the DELETE and leaves the table present and
    empty — the previous run's receipt destroyed and nothing in its place.
    ``-1`` makes the four statements one unit; ``ON_ERROR_STOP=1`` still decides
    that a failure is a failure.
    """
    seen = {}

    def fake(cmd, *, connection_url, input_text=None):
        seen["cmd"] = cmd
        return 0, "", ""

    receipt = RestoreReceipt("t", "/b.dump", 1, datetime.now(UTC), 0.0)
    with patch("fraisier.dbops.receipt._pg_cmd", fake):
        write_receipt("stagingdb", connection_url=_URL, receipt=receipt)

    assert "-1" in seen["cmd"]
    assert "ON_ERROR_STOP=1" in seen["cmd"]


def test_reads_are_not_wrapped_in_a_transaction():
    """``-1`` is for the write. A read has nothing to roll back."""
    seen = []

    def fake(cmd, *, connection_url, input_text=None):
        seen.append(cmd)
        return 0, "f\n", ""

    with patch("fraisier.dbops.receipt._pg_cmd", fake):
        verify_actuation("stagingdb", connection_url=_URL, max_age_hours=24)

    assert all("-1" not in cmd for cmd in seen)


# --------------------------------------------------------------------------
# Cycle 4: which schema the heaps are in
# --------------------------------------------------------------------------


def test_the_write_records_the_schema_the_floor_was_derived_for():
    """The restore knows it; nothing that reads the receipt later can.

    ``min_tables_schema`` is derived from the archive's table of contents at
    restore time and is not configured anywhere, so a caller arriving the next
    morning has no source to read it from — unless the receipt carries it. A
    host whose heaps live in ``tenant`` and a cross-check defaulting to
    ``public`` is the same mismatch v0.64.0 removed from the floor.
    """
    seen = {}

    def fake(cmd, *, connection_url, input_text=None):
        seen["cmd"] = cmd
        seen["sql"] = input_text
        return 0, "", ""

    receipt = RestoreReceipt(
        run_id="tok",
        backup_path="/backup/x.dump",
        backup_bytes=17,
        restored_at=datetime.now(UTC),
        age_seconds=0.0,
        floor_schema="tenant",
    )
    with patch("fraisier.dbops.receipt._pg_cmd", fake):
        assert write_receipt("stagingdb", connection_url=_URL, receipt=receipt) is None

    assert "floor_schema=tenant" in seen["cmd"]
    assert ":'floor_schema'" in seen["sql"]
    # Bound, not interpolated — an identifier-shaped value is still data.
    assert "tenant" not in seen["sql"]


def test_a_receipt_with_no_schema_writes_null_rather_than_an_empty_name():
    """psql binds a missing value as ``''``; a schema named "" exists nowhere.

    NULL is the honest record of "this run derived no floor", and it is what
    the reader falls back to ``public`` on.
    """
    from fraisier.dbops.receipt import _WRITE_SQL

    seen = {}

    def fake(cmd, *, connection_url, input_text=None):
        seen["cmd"] = cmd
        return 0, "", ""

    receipt = RestoreReceipt("t", "/b.dump", 1, datetime.now(UTC), 0.0)
    with patch("fraisier.dbops.receipt._pg_cmd", fake):
        write_receipt("stagingdb", connection_url=_URL, receipt=receipt)

    assert "floor_schema=" in seen["cmd"]
    assert "NULLIF(:'floor_schema', '')" in _WRITE_SQL


def test_the_recorded_schema_reads_back():
    with patch(
        "fraisier.dbops.receipt._pg_cmd",
        _responses((0, "t\n", ""), (0, _row_json(floor_schema="tenant") + "\n", "")),
    ):
        check = verify_actuation("stagingdb", connection_url=_URL, max_age_hours=24)

    assert check.receipt is not None
    assert check.receipt.floor_schema == "tenant"


@pytest.mark.parametrize("row", [{"floor_schema": None}, {}])
def test_a_receipt_without_a_schema_is_still_a_receipt(row):
    """A NULL column, and a table written before the column existed.

    ``CREATE TABLE IF NOT EXISTS`` never migrates an existing table, so the read
    must survive a receipt that predates the column rather than degrading a
    perfectly good verdict to UNVERIFIABLE.
    """
    payload = json.loads(_row_json())
    payload.pop("floor_schema", None)
    payload.update(row)

    with patch(
        "fraisier.dbops.receipt._pg_cmd",
        _responses((0, "t\n", ""), (0, json.dumps(payload) + "\n", "")),
    ):
        check = verify_actuation("stagingdb", connection_url=_URL, max_age_hours=24)

    assert check.verdict is ActuationVerdict.ACTUATED
    assert check.receipt is not None
    assert check.receipt.floor_schema is None


def test_receipt_lives_outside_public():
    """The receipt cannot inflate a table-count floor or read as schema drift.

    Both floors that guard a restore count ``relkind='r'`` in one schema —
    confiture's pre-migration counter and ``validate_table_count`` — so a
    bookkeeping table in ``public`` would quietly raise both.
    """
    from fraisier.dbops.receipt import RECEIPT_SCHEMA

    assert RECEIPT_SCHEMA == "fraisier"
    assert RECEIPT_TABLE.startswith("fraisier.")
