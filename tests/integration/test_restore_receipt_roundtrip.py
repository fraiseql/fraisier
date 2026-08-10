"""The actuation receipt against a real PostgreSQL (#358).

The unit tests prove what fraisier does with ``psql``'s output. These prove the
SQL is SQL: that the schema and table are created where they are supposed to be,
that a token written by one call is read back by another, and — the part that
matters to every floor already guarding a restore — that none of it lands in
``public``.

Skipped unless a PostgreSQL with ``createdb`` privilege is reachable — over a
unix socket locally, or over TCP against CI's service container. The shared
``pg_target`` fixture in ``conftest.py`` decides that and hands back the DSNs.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.integration.conftest import PgTarget

pytestmark = pytest.mark.integration

psycopg = pytest.importorskip("psycopg")

_DB = "fraisier_it_receipt"


def _exec(db: str, target: PgTarget, *statements: str) -> None:
    with psycopg.connect(target.dsn(db), autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)


@pytest.fixture
def receipt_db(pg_target):
    """An empty database to write receipts into."""
    with contextlib.suppress(Exception):
        _exec("postgres", pg_target, f"DROP DATABASE IF EXISTS {_DB} WITH (FORCE)")
    _exec("postgres", pg_target, f"CREATE DATABASE {_DB}")
    try:
        yield pg_target
    finally:
        with contextlib.suppress(Exception):
            _exec("postgres", pg_target, f"DROP DATABASE IF EXISTS {_DB} WITH (FORCE)")


def _receipt(run_id: str, floor_schema: str | None = None):
    from fraisier.dbops.receipt import RestoreReceipt

    return RestoreReceipt(
        run_id=run_id,
        backup_path="/backup/production/latest.dump",
        backup_bytes=123456,
        restored_at=datetime.now(UTC),
        age_seconds=0.0,
        floor_schema=floor_schema,
    )


def test_a_database_with_no_receipt_reads_missing(receipt_db):
    """MISSING, not STALE: nothing has been claimed about this database yet."""
    from fraisier.dbops.receipt import ActuationVerdict, verify_actuation

    check = verify_actuation(
        _DB, connection_url=receipt_db.dsn("postgres"), max_age_hours=24
    )
    assert check.verdict is ActuationVerdict.MISSING
    assert check.is_bad is False


def test_written_receipt_reads_back_and_matches_its_token(receipt_db):
    from fraisier.dbops.receipt import (
        ActuationVerdict,
        verify_actuation,
        write_receipt,
    )

    url = receipt_db.dsn("postgres")
    assert write_receipt(_DB, connection_url=url, receipt=_receipt("run-one")) is None

    check = verify_actuation(_DB, connection_url=url, expected_run_id="run-one")
    assert check.verdict is ActuationVerdict.ACTUATED
    assert check.receipt is not None
    assert check.receipt.backup_path == "/backup/production/latest.dump"
    assert check.receipt.backup_bytes == 123456
    # Written seconds ago by the server's own clock.
    assert check.receipt.age_seconds < 60

    # The failure this whole mechanism exists for: a later run asks whether *it*
    # rewrote the database, and the answer is no — someone else's receipt is
    # still there.
    later = verify_actuation(_DB, connection_url=url, expected_run_id="run-two")
    assert later.verdict is ActuationVerdict.STALE
    assert later.is_bad is True
    assert later.receipt is not None
    assert later.receipt.run_id == "run-one"


def test_the_receipt_stays_out_of_public(receipt_db):
    """No floor that guards a restore may be moved by a bookkeeping row.

    confiture's pre-migration counter and ``validate_table_count`` both count
    ``relkind='r'`` in one schema. A receipt in ``public`` would raise both by
    one, quietly, on every host.
    """
    from fraisier.dbops.receipt import RECEIPT_SCHEMA, write_receipt

    url = receipt_db.dsn("postgres")
    assert write_receipt(_DB, connection_url=url, receipt=_receipt("run-one")) is None

    with psycopg.connect(receipt_db.dsn(_DB)) as conn:
        public_tables = conn.execute(
            "SELECT count(*) FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'r' AND n.nspname = 'public'"
        ).fetchone()[0]
        own_tables = conn.execute(
            "SELECT count(*) FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'r' AND n.nspname = %s",
            (RECEIPT_SCHEMA,),
        ).fetchone()[0]
    assert public_tables == 0
    assert own_tables == 1


def test_the_schema_the_floor_covered_survives_the_round_trip(receipt_db):
    """Recorded as data, read back as the default for the mtime cross-check.

    A schema name is bound like every other value, so a host whose heaps live in
    ``tenant`` gets ``tenant`` back without the operator naming it — and a name
    with a quote in it is still data.
    """
    from fraisier.dbops.receipt import verify_actuation, write_receipt

    url = receipt_db.dsn("postgres")
    assert (
        write_receipt(_DB, connection_url=url, receipt=_receipt("run-one", "tenant"))
        is None
    )

    check = verify_actuation(_DB, connection_url=url, expected_run_id="run-one")
    assert check.receipt is not None
    assert check.receipt.floor_schema == "tenant"


def test_a_receipt_recording_no_schema_reads_back_as_none(receipt_db):
    """NULL, not the empty string psql binds a missing value as."""
    from fraisier.dbops.receipt import verify_actuation, write_receipt

    url = receipt_db.dsn("postgres")
    write_receipt(_DB, connection_url=url, receipt=_receipt("run-one"))

    check = verify_actuation(_DB, connection_url=url, expected_run_id="run-one")
    assert check.receipt is not None
    assert check.receipt.floor_schema is None

    with psycopg.connect(receipt_db.dsn(_DB)) as conn:
        stored = conn.execute("SELECT floor_schema FROM fraisier.restore_receipt")
        assert stored.fetchone()[0] is None


def test_a_receipt_predating_the_column_is_still_readable(receipt_db):
    """``CREATE TABLE IF NOT EXISTS`` never migrates, so the read must not care.

    A database restored by a fraisier that wrote the four-column receipt keeps
    that table for as long as nothing drops it. Degrading it to UNVERIFIABLE
    would report "could not check" about a receipt sitting right there.
    """
    from fraisier.dbops.receipt import ActuationVerdict, verify_actuation

    _exec(
        _DB,
        receipt_db,
        "CREATE SCHEMA fraisier",
        "CREATE TABLE fraisier.restore_receipt ("
        " run_id text PRIMARY KEY, backup_path text NOT NULL,"
        " backup_bytes bigint NOT NULL,"
        " restored_at timestamptz NOT NULL DEFAULT now())",
        "INSERT INTO fraisier.restore_receipt (run_id, backup_path, backup_bytes) "
        "VALUES ('run-old', '/backup/old.dump', 99)",
    )

    check = verify_actuation(
        _DB, connection_url=receipt_db.dsn("postgres"), max_age_hours=24
    )

    assert check.verdict is ActuationVerdict.ACTUATED
    assert check.receipt is not None
    assert check.receipt.run_id == "run-old"
    assert check.receipt.floor_schema is None


def test_a_failed_write_leaves_the_standing_receipt_intact(receipt_db):
    """The write is one transaction, so a failure part-way changes nothing.

    The script deletes the standing receipt before inserting the new one. Run as
    separate statements, a failure between the two commits the delete and leaves
    the table present and empty — which reads as MISSING, a database claiming
    that no run has ever written it. That is a worse answer than the truth.
    """
    from fraisier.dbops.receipt import ActuationVerdict, verify_actuation, write_receipt

    url = receipt_db.dsn("postgres")
    assert write_receipt(_DB, connection_url=url, receipt=_receipt("run-one")) is None

    # Fail the INSERT specifically, after the DELETE has already run.
    _exec(
        _DB,
        receipt_db,
        "ALTER TABLE fraisier.restore_receipt "
        "ADD CONSTRAINT rejects_run_two CHECK (run_id <> 'run-two')",
    )

    failure = write_receipt(_DB, connection_url=url, receipt=_receipt("run-two"))
    assert failure is not None

    survivor = verify_actuation(_DB, connection_url=url, expected_run_id="run-one")
    assert survivor.verdict is ActuationVerdict.ACTUATED
    assert survivor.receipt is not None
    assert survivor.receipt.run_id == "run-one"


def test_a_second_write_replaces_the_first(receipt_db):
    """One row: "what wrote this database" has one answer, not a history."""
    from fraisier.dbops.receipt import verify_actuation, write_receipt

    url = receipt_db.dsn("postgres")
    write_receipt(_DB, connection_url=url, receipt=_receipt("run-one"))
    write_receipt(_DB, connection_url=url, receipt=_receipt("run-two"))

    with psycopg.connect(receipt_db.dsn(_DB)) as conn:
        rows = conn.execute("SELECT count(*) FROM fraisier.restore_receipt").fetchone()[
            0
        ]
    assert rows == 1
    assert verify_actuation(
        _DB, connection_url=url, expected_run_id="run-two"
    ).is_actuated


def test_a_window_narrower_than_the_receipt_is_stale(receipt_db):
    """The age comparison is real, and it is the server that measures it."""
    from fraisier.dbops.receipt import ActuationVerdict, verify_actuation, write_receipt

    url = receipt_db.dsn("postgres")
    write_receipt(_DB, connection_url=url, receipt=_receipt("run-one"))
    _exec(
        _DB,
        receipt_db,
        "UPDATE fraisier.restore_receipt SET restored_at = now() - interval '30 hours'",
    )

    stale = verify_actuation(_DB, connection_url=url, max_age_hours=24)
    assert stale.verdict is ActuationVerdict.STALE
    assert stale.receipt is not None
    assert 29 < stale.receipt.age_hours < 31

    assert verify_actuation(_DB, connection_url=url, max_age_hours=48).is_actuated


def test_the_strategy_records_and_confirms_its_own_run(receipt_db, tmp_path):
    """The pipeline's receipt step, end to end, against real SQL.

    The unit tests mock the seam and prove the strategy calls it correctly. This
    proves the call works: the strategy's own ``_record_actuation`` writes a
    token into a real database and reads it back as ACTUATED, and a *different*
    run asking the same database gets STALE rather than a shrug.
    """
    from fraisier.config.schema import PreflightConfig
    from fraisier.dbops.receipt import ActuationVerdict, verify_actuation
    from fraisier.strategies import RestoreConfig, RestoreMigrateStrategy

    url = receipt_db.dsn("postgres")
    backup = tmp_path / "production.dump"
    backup.write_bytes(b"x" * 2048)

    strategy = RestoreMigrateStrategy(
        RestoreConfig(
            db_name=_DB,
            backup_dir=tmp_path,
            preflight=PreflightConfig(enabled=False),
        ),
        admin_url=url,
    )

    check = strategy._record_actuation(backup, "run-alpha", "tenant")
    assert check.verdict is ActuationVerdict.ACTUATED
    assert check.receipt is not None
    assert check.receipt.run_id == "run-alpha"
    assert check.receipt.backup_path == str(backup)
    # The size is read off the file, so the receipt names which archive.
    assert check.receipt.backup_bytes == 2048
    # And where this database's heaps live, for the caller that reads it back
    # the next morning with no archive to derive it from.
    assert check.receipt.floor_schema == "tenant"

    # A later run that did *not* rewrite this database learns so.
    assert (
        verify_actuation(_DB, connection_url=url, expected_run_id="run-beta").verdict
        is ActuationVerdict.STALE
    )


def test_relation_mtimes_against_a_real_server(receipt_db):
    """The mtime cross-check, run for real — whichever way this role is allowed.

    Two outcomes are both correct and both asserted, because which one happens
    is a property of the server rather than of the code: a role that may call
    ``pg_stat_file`` sees the just-written table as fresh, and a role that may
    not gets UNVERIFIABLE naming the privilege. What must never happen is a
    denial reading as a pass.
    """
    from fraisier.dbops.receipt import ActuationVerdict, relation_freshness

    url = receipt_db.dsn("postgres")
    _exec(_DB, receipt_db, "CREATE TABLE fresh_table (id int)")

    check = relation_freshness(
        _DB, schema="public", connection_url=url, within_hours=24
    )

    assert check.verdict in {
        ActuationVerdict.ACTUATED,
        ActuationVerdict.UNVERIFIABLE,
    }, check.detail
    if check.verdict is ActuationVerdict.UNVERIFIABLE:
        assert check.is_actuated is False
        assert "pg_read_server_files" in check.detail
    else:
        assert "1 base table(s)" in check.detail


def test_an_empty_schema_is_unverifiable_against_a_real_server(receipt_db):
    """Zero tables is not "everything is fresh"."""
    from fraisier.dbops.receipt import ActuationVerdict, relation_freshness

    check = relation_freshness(
        _DB,
        schema="public",
        connection_url=receipt_db.dsn("postgres"),
        within_hours=24,
    )

    assert check.verdict is ActuationVerdict.UNVERIFIABLE
    assert check.is_actuated is False


def test_a_quoted_backup_path_does_not_break_the_write(receipt_db):
    """Values bind as psql variables, so a quote in a path is data."""
    from fraisier.dbops.receipt import RestoreReceipt, verify_actuation, write_receipt

    url = receipt_db.dsn("postgres")
    nasty = "/backup/o'brien'; DROP TABLE fraisier.restore_receipt; --.dump"
    receipt = RestoreReceipt(
        run_id="run-quote",
        backup_path=nasty,
        backup_bytes=1,
        restored_at=datetime.now(UTC),
        age_seconds=0.0,
    )
    assert write_receipt(_DB, connection_url=url, receipt=receipt) is None

    check = verify_actuation(_DB, connection_url=url, expected_run_id="run-quote")
    assert check.is_actuated
    assert check.receipt is not None
    assert check.receipt.backup_path == nasty
