"""Did this restore actually run? (#358)

A table-count floor proves the schema arrived. A row-count floor would prove how
much data arrived. Neither can see the failure #343 reported and #356 was filed
against: a nightly staging restore that reports success in 21 seconds while
staging keeps yesterday's database. Nothing about that database's *content* is
wrong — every count is correct — it is only old. Counting harder cannot see it,
because the pipeline that would have changed the counts never ran.

What can see it is evidence that the heap was written *this run*. fraisier owns
the whole restore pipeline, so it leaves a receipt: a token minted per run and
written into the freshly restored database, which an independent caller reads
back later and compares against what it expected.

The token is the point. A no-op leaves the *previous* run's receipt in place, so
"a receipt exists" always matches and proves nothing — which is why
:func:`verify_actuation` refuses to answer without a criterion to check the
receipt against.

The verdict is **four-valued**, and only one value is proof:

- ``ACTUATED``    — a run rewrote this database and the receipt satisfies the
                    criterion asked of it.
- ``STALE``       — a receipt is present and does not. The known-bad verdict, and
                    the only one :attr:`ActuationCheck.is_bad` convicts.
- ``MISSING``     — no receipt. **Not bad.** A database restored by hand or by a
                    fraisier older than this feature has none, and convicting it
                    would make the check unusable on the hosts that need it first.
- ``UNVERIFIABLE``— the check could not run. Says nothing about the database.

That split is the same rule :class:`~fraisier.dbops.archive.ArchiveCheck` already
applies, and for the same reason: an unevaluable condition is never silently
resolved in either direction. A check that reads as a pass when it could not run
is how ``min_tables=0`` was a silent hole for a year (#343).

Storage is a single row in a dedicated ``fraisier`` schema, created inside the
restored database. Outside ``public`` deliberately — both floors that guard a
restore count ``relkind='r'`` in one schema, so a bookkeeping table in ``public``
would quietly raise them, and a schema comparison would read it as drift. It
needs no migration and no cleanup: the next restore's DROP+CREATE takes it with
everything else.

Access is ``psql``, like the ownership reassignment in
:mod:`fraisier.dbops.restore`, over the admin URL the restore already holds. No
new dependency, no new configuration, no new privilege.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from fraisier.dbops._validation import validate_pg_identifier
from fraisier.dbops.operations import _pg_cmd

#: Dedicated schema, never ``public`` — see the module docstring.
RECEIPT_SCHEMA = "fraisier"

#: Schema-qualified receipt table. A constant, so no caller-supplied value ever
#: reaches an identifier position in the SQL below.
RECEIPT_TABLE = f"{RECEIPT_SCHEMA}.restore_receipt"

_SECONDS_PER_HOUR = 3600.0

#: ``floor_schema`` is the schema the restore derived its table-count floor for,
#: recorded because that is the only moment it is knowable: it comes off the
#: archive's table of contents during the restore and is configured nowhere, so
#: a caller reading the receipt the next morning has no archive to derive it
#: from. NULL means the archive stated no floor — the reader falls back, the
#: writer does not invent.
_WRITE_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {RECEIPT_SCHEMA};
CREATE TABLE IF NOT EXISTS {RECEIPT_TABLE} (
    run_id       text        PRIMARY KEY,
    backup_path  text        NOT NULL,
    backup_bytes bigint      NOT NULL,
    restored_at  timestamptz NOT NULL DEFAULT now(),
    floor_schema text
);
DELETE FROM {RECEIPT_TABLE};
INSERT INTO {RECEIPT_TABLE} (run_id, backup_path, backup_bytes, floor_schema)
VALUES (
    :'run_id',
    :'backup_path',
    :'backup_bytes'::bigint,
    NULLIF(:'floor_schema', '')
);
"""

#: Does the receipt table exist at all? Asked separately, and by ``to_regclass``
#: rather than by matching ``psql``'s error text: PostgreSQL localises its
#: messages, so "relation does not exist" is not a string a check may rely on.
_PROBE_SQL = f"SELECT to_regclass('{RECEIPT_TABLE}') IS NOT NULL"

#: ``age_seconds`` is computed by the server, from the server's own clock, so a
#: host whose clock has drifted cannot make a stale restore look fresh.
#:
#: ``t.*`` rather than a column list, because the table above is created with
#: ``CREATE TABLE IF NOT EXISTS`` and therefore never migrated: naming a column
#: added later would turn a readable receipt written by an earlier fraisier into
#: UNVERIFIABLE. Whatever columns are there arrive as JSON keys, and
#: :func:`_parse_receipt` treats the ones it does not find as absent.
_READ_SQL = f"""
SELECT to_json(r) FROM (
    SELECT t.*,
           extract(epoch FROM (now() - restored_at)) AS age_seconds
    FROM {RECEIPT_TABLE} t
    ORDER BY restored_at DESC
    LIMIT 1
) r
"""


#: Relation-file mtimes, per schema and never summed — the rule the archive
#: floor already follows. ``pg_stat_file`` raises rather than returning NULL when
#: the role may not read server files, so a denial arrives as a non-zero exit and
#: is classified UNVERIFIABLE by the caller.
_FRESHNESS_SQL = """
SELECT to_json(r) FROM (
    SELECT count(*) AS total,
           count(*) FILTER (
               WHERE m IS NOT NULL
                 AND m >= now() - (:'within_hours' || ' hours')::interval
           ) AS fresh,
           extract(epoch FROM (now() - min(m))) / 3600 AS oldest_age_hours
    FROM (
        SELECT (pg_stat_file(pg_relation_filepath(c.oid))).modification AS m
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r' AND n.nspname = :'schema'
    ) s
) r
"""


class ActuationVerdict(Enum):
    """What was learned about the last run to rewrite a database."""

    ACTUATED = "actuated"
    """A run rewrote this database and its receipt met the criterion."""

    STALE = "stale"
    """A receipt is present and does not meet the criterion.

    Either it names a different run than the caller expected, or it is older
    than the caller's window. This is the failure #343 reported.
    """

    MISSING = "missing"
    """No receipt table, or no row in it.

    Says nothing about the database: one restored by hand, or by a fraisier
    predating this check, carries none. Callers must not treat it as STALE.
    """

    UNVERIFIABLE = "unverifiable"
    """The check could not run: no ``psql``, no connection, unreadable output.

    Says nothing about the database. Callers must not act as though it does.
    """


@dataclass(frozen=True)
class RestoreReceipt:
    """What one restore run recorded about itself."""

    run_id: str
    """The token that run minted. Unique per run — presence is not proof."""

    backup_path: str
    """The archive that run claims to have loaded."""

    backup_bytes: int
    """Its size, so a receipt names *which* backup and not merely a path."""

    restored_at: datetime
    """When the run finished, by the database server's clock."""

    age_seconds: float
    """How long ago that was, measured by the server rather than the client."""

    floor_schema: str | None = None
    """The schema that run derived its table-count floor for, if it derived one.

    Where this database's heaps actually live, recorded by the only party that
    can know: the restore, from the archive's table of contents. ``None`` means
    the archive stated no floor, or the receipt predates this column — in both
    cases a reader falls back rather than believing ``public``.
    """

    @property
    def age_hours(self) -> float:
        return self.age_seconds / _SECONDS_PER_HOUR


@dataclass(frozen=True)
class ActuationCheck:
    """A verdict, why it was reached, and the receipt behind it if there is one."""

    verdict: ActuationVerdict
    detail: str
    receipt: RestoreReceipt | None = None
    """The row that was read, carried even when it *fails* the criterion.

    A caller reporting STALE should be able to say whose run the database is
    still holding, and from which backup.
    """

    @property
    def is_actuated(self) -> bool:
        """A run rewrote this database and proved it. The only proof verdict."""
        return self.verdict is ActuationVerdict.ACTUATED

    @property
    def is_bad(self) -> bool:
        """The database is known not to have been rewritten as expected.

        ``MISSING`` and ``UNVERIFIABLE`` are **not** bad. Branch on this rather
        than on ``verdict is not ACTUATED``, which convicts a database for the
        absence of the evidence that would have cleared it.
        """
        return self.verdict is ActuationVerdict.STALE


def _psql(
    db_name: str,
    sql: str,
    *,
    connection_url: str,
    bind: dict[str, str],
    single_transaction: bool = False,
):
    """Run *sql* against *db_name*, binding *bind* as psql variables.

    Values go in through ``-v name=value`` and are read back in the SQL as
    ``:'name'``, which psql quotes and escapes as a string literal. Nothing is
    interpolated into the statement text.

    The script goes in on **stdin** (``-f -``) rather than in ``-c``. That is
    not a style choice: ``-c`` hands its string to the server unread, so psql
    never sees ``:'run_id'`` and the server rejects it as a syntax error. Only
    input psql lexes itself — a file, or stdin — gets variable substitution.

    ``ON_ERROR_STOP=1`` matters more than it looks: without it psql exits 0 on a
    statement that failed, so a caller checking the exit code would read a
    failed write as a successful one.

    *single_transaction* adds ``-1``, for a script whose statements are only
    meaningful together. It is the write's: that script deletes the standing
    receipt before inserting the new one, so a failure between the two would
    otherwise commit the delete and leave a present-but-empty table, which reads
    as MISSING — a database claiming no run ever wrote it.
    """
    cmd = ["psql", "-d", db_name, "-t", "-A", "-v", "ON_ERROR_STOP=1"]
    if single_transaction:
        cmd.append("-1")
    for name, value in bind.items():
        cmd += ["-v", f"{name}={value}"]
    cmd += ["-f", "-"]
    return _pg_cmd(cmd, connection_url=connection_url, input_text=sql)


def write_receipt(
    db_name: str,
    *,
    connection_url: str,
    receipt: RestoreReceipt,
) -> str | None:
    """Record *receipt* as the one receipt of *db_name*.

    Returns ``None`` when the receipt was written, or a detail string saying why
    it was not. It never raises for an unreachable database or a missing
    ``psql``: the caller is a restore that has already passed every check it
    has, and failing an otherwise-good restore over a bookkeeping row would be
    worse than the problem this row exists to solve.

    One row, not an append log — "what wrote this database" has one answer.
    """
    validate_pg_identifier(db_name, "database name")
    try:
        code, _, stderr = _psql(
            db_name,
            _WRITE_SQL,
            connection_url=connection_url,
            bind={
                "run_id": receipt.run_id,
                "backup_path": receipt.backup_path,
                "backup_bytes": str(receipt.backup_bytes),
                # Bound like every other value: a schema name is data here, not
                # an identifier position, and NULLIF turns "" back into NULL.
                "floor_schema": receipt.floor_schema or "",
            },
            single_transaction=True,
        )
    except FileNotFoundError:
        return "psql not found on PATH — the restore receipt was not written"
    except OSError as exc:  # pragma: no cover - defensive
        return f"could not run psql to write the restore receipt: {exc}"
    if code != 0:
        return (
            f"writing the restore receipt to {db_name} failed: "
            f"{stderr.strip() or f'psql exited with code {code}'}"
        )
    return None


def _parse_receipt(payload: str) -> RestoreReceipt | None:
    """Parse one ``to_json`` row, or None if it is not one.

    ``floor_schema`` is read with ``.get``: a receipt written before that column
    existed is a complete receipt missing one optional fact, not an unreadable
    one, and the table it lives in is never migrated.
    """
    try:
        row = json.loads(payload)
        floor_schema = row.get("floor_schema")
        return RestoreReceipt(
            run_id=str(row["run_id"]),
            backup_path=str(row["backup_path"]),
            backup_bytes=int(row["backup_bytes"]),
            restored_at=datetime.fromisoformat(row["restored_at"]),
            age_seconds=float(row["age_seconds"]),
            floor_schema=str(floor_schema) if floor_schema else None,
        )
    except (ValueError, TypeError, KeyError):
        return None


def read_receipt(db_name: str, *, connection_url: str) -> ActuationCheck:
    """Read *db_name*'s receipt without judging it.

    Returns MISSING or UNVERIFIABLE as appropriate, or — when a row was read —
    an ``ACTUATED`` check whose only claim is that a receipt exists. **That is
    not proof the database is fresh**; a no-op leaves the previous run's receipt
    in place. Callers deciding anything use :func:`verify_actuation`, which
    requires a criterion. This exists for reporting what is there.
    """
    validate_pg_identifier(db_name, "database name")
    try:
        code, stdout, stderr = _psql(
            db_name, _PROBE_SQL, connection_url=connection_url, bind={}
        )
    except (FileNotFoundError, OSError) as exc:
        return ActuationCheck(
            ActuationVerdict.UNVERIFIABLE,
            f"could not run psql to read the restore receipt: {exc}",
        )
    if code != 0:
        return ActuationCheck(
            ActuationVerdict.UNVERIFIABLE,
            f"could not reach {db_name} to read its restore receipt: "
            f"{stderr.strip() or f'psql exited with code {code}'}",
        )
    if stdout.strip() != "t":
        return ActuationCheck(
            ActuationVerdict.MISSING,
            f"{db_name} has no {RECEIPT_TABLE} — it has not been restored by a "
            "fraisier that writes one. This is not evidence either way.",
        )

    code, stdout, stderr = _psql(
        db_name, _READ_SQL, connection_url=connection_url, bind={}
    )
    if code != 0:
        return ActuationCheck(
            ActuationVerdict.UNVERIFIABLE,
            f"could not read {RECEIPT_TABLE} in {db_name}: "
            f"{stderr.strip() or f'psql exited with code {code}'}",
        )
    payload = stdout.strip()
    if not payload:
        return ActuationCheck(
            ActuationVerdict.MISSING,
            f"{RECEIPT_TABLE} exists in {db_name} but holds no row",
        )
    receipt = _parse_receipt(payload)
    if receipt is None:
        return ActuationCheck(
            ActuationVerdict.UNVERIFIABLE,
            f"could not parse the receipt read from {db_name}",
        )
    return ActuationCheck(
        ActuationVerdict.ACTUATED,
        f"run {receipt.run_id} restored {db_name} from {receipt.backup_path} "
        f"{receipt.age_hours:.1f}h ago",
        receipt,
    )


def relation_freshness(
    db_name: str,
    *,
    schema: str,
    connection_url: str,
    within_hours: float,
) -> ActuationCheck:
    """Were *schema*'s base tables written to disk within *within_hours*?

    #358's own suggestion, and a genuine cross-check where fraisier is not the
    only thing that writes a database — a hand-run ``psql`` restore leaves no
    receipt but does move every heap file.

    It is **secondary** to the receipt for two reasons this code cannot fix:

    - ``pg_stat_file`` needs superuser or ``pg_read_server_files``, which many
      managed PostgreSQL deployments refuse. A denial is UNVERIFIABLE. It is not
      a pass, and the check does not ask for the privilege — a check that only
      runs where it is least needed is worse than no check.
    - Autovacuum and HOT pruning touch heap files, so an mtime can move after a
      restore that did nothing. That is a false *pass*, never a false fail. Safe
      to corroborate with, unsafe to rely on alone.

    One schema, never summed across schemas — the rule the archive-derived floor
    already follows.
    """
    validate_pg_identifier(db_name, "database name")
    try:
        code, stdout, stderr = _psql(
            db_name,
            _FRESHNESS_SQL,
            connection_url=connection_url,
            bind={"schema": schema, "within_hours": str(within_hours)},
        )
    except (FileNotFoundError, OSError) as exc:
        return ActuationCheck(
            ActuationVerdict.UNVERIFIABLE,
            f"could not run psql to read relation mtimes: {exc}",
        )
    if code != 0:
        return ActuationCheck(
            ActuationVerdict.UNVERIFIABLE,
            f"could not read relation mtimes in {db_name}: "
            f"{stderr.strip() or f'psql exited with code {code}'} — this needs "
            "superuser or pg_read_server_files, which many managed PostgreSQL "
            "deployments do not grant. Nothing was learned either way.",
        )
    try:
        row = json.loads(stdout.strip())
        total = int(row["total"])
        fresh = int(row["fresh"])
    except (ValueError, TypeError, KeyError):
        return ActuationCheck(
            ActuationVerdict.UNVERIFIABLE,
            f"could not parse the relation mtimes read from {db_name}",
        )

    if total == 0:
        return ActuationCheck(
            ActuationVerdict.UNVERIFIABLE,
            f"schema '{schema}' in {db_name} has no base tables, so their "
            "mtimes say nothing about whether a restore ran",
        )
    if fresh < total:
        oldest = row.get("oldest_age_hours")
        oldest_text = f", oldest {float(oldest):.1f}h" if oldest is not None else ""
        return ActuationCheck(
            ActuationVerdict.STALE,
            f"{total - fresh} of {total} base table(s) in '{schema}' were last "
            f"written more than {within_hours:.1f}h ago{oldest_text}",
        )
    return ActuationCheck(
        ActuationVerdict.ACTUATED,
        f"all {total} base table(s) in '{schema}' were written within "
        f"{within_hours:.1f}h",
    )


def verify_actuation(
    db_name: str,
    *,
    connection_url: str,
    expected_run_id: str | None = None,
    max_age_hours: float | None = None,
) -> ActuationCheck:
    """Check *db_name*'s receipt against a criterion.

    Exactly one thing makes this useful and it is the criterion:

    - *expected_run_id* — the token the caller minted for **this** run. Used
      from inside the pipeline: the receipt must name the run now claiming to
      have written it.
    - *max_age_hours* — how recently a run must have rewritten the database.
      Used from outside: this is the question a stale staging database fails.

    Raises:
        ValueError: If neither criterion is given. Reading a receipt is not a
            verdict on it — a no-op restore leaves the previous run's receipt in
            place, so "a row exists" matches every time and would report a
            database that was never rewritten as proven fresh.
    """
    if expected_run_id is None and max_age_hours is None:
        msg = (
            "verify_actuation needs a criterion: expected_run_id or "
            "max_age_hours. A receipt's presence is not proof — a restore that "
            "never ran leaves the previous run's receipt in place."
        )
        raise ValueError(msg)

    check = read_receipt(db_name, connection_url=connection_url)
    if check.receipt is None:
        return check
    receipt = check.receipt

    if expected_run_id is not None and receipt.run_id != expected_run_id:
        return ActuationCheck(
            ActuationVerdict.STALE,
            f"{db_name} still holds the receipt of run {receipt.run_id} "
            f"(from {receipt.backup_path}, {receipt.age_hours:.1f}h ago); "
            f"run {expected_run_id} did not rewrite it",
            receipt,
        )
    if max_age_hours is not None and receipt.age_hours > max_age_hours:
        return ActuationCheck(
            ActuationVerdict.STALE,
            f"{db_name} was last rewritten {receipt.age_hours:.1f}h ago by run "
            f"{receipt.run_id}, which is older than the {max_age_hours:.1f}h "
            "window",
            receipt,
        )
    return check
