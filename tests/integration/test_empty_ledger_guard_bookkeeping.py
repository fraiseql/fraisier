"""The #262 guard counts the application's relations, not confiture's or fraisier's.

The guard refuses a preflight whose ledger restored empty beside a populated
schema. A database where confiture ran but applied nothing holds an empty ledger
and confiture's own tables: its checkpoint and lock-holder tables, and
``confiture.saved_views`` / ``saved_view_indexes`` under ``view_helpers: auto``.
Counting those read a never-migrated backup as a failed ledger restore. A restored
database can also hold fraisier's own receipt schema.

The database is made by the real ``confiture migrate up``, never by hand, so the
test sees what confiture leaves.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from fraisier.dbops.confiture_executable import confiture_executable
from fraisier.dbops.preflight import PreflightDatabase, _guard_against_empty_ledger
from fraisier.dbops.receipt import RestoreReceipt, write_receipt
from fraisier.errors import DatabaseError

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


def _migrate_nothing(url: str, tmp_path: Path) -> None:
    """``confiture migrate up`` with no migrations: the bookkeeping alone.

    The config sits where a project keeps it: ``migrate up`` reads its
    ``migration:`` settings, ``view_helpers: auto`` (the default) among them,
    only from ``db/environments/<name>.yaml``.
    """
    migrations = tmp_path / "db" / "migrations"
    migrations.mkdir(parents=True)
    config = tmp_path / "db" / "environments" / "acme.yaml"
    config.parent.mkdir()
    config.write_text(f"name: acme\ndatabase_url: {url}\n")
    subprocess.run(
        [
            confiture_executable(),
            "migrate",
            "up",
            "--config",
            str(config),
            "--migrations-dir",
            str(migrations),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )


@pytest.fixture
def migrated_nothing(pg_target, tmp_path: Path):
    with PreflightDatabase(admin_url=pg_target.dsn("postgres")) as pf:
        _migrate_nothing(pf.url, tmp_path)
        yield pf


def test_confiture_s_own_tables_are_not_application_objects(migrated_nothing):
    assert migrated_nothing.has_table("confiture.saved_views")
    assert migrated_nothing.count_table_rows("tb_confiture") == 0
    assert migrated_nothing.count_user_relations("tb_confiture") == 0

    _guard_against_empty_ledger(migrated_nothing, "tb_confiture")


def test_fraisier_s_receipt_is_not_an_application_object(migrated_nothing, pg_target):
    failure = write_receipt(
        migrated_nothing.db_name,
        connection_url=pg_target.dsn("postgres"),
        receipt=RestoreReceipt(
            run_id="acme-run",
            backup_path="/backups/acme.dump",
            backup_bytes=1,
            restored_at=datetime.now(UTC),
            age_seconds=0.0,
        ),
    )
    assert failure is None

    assert migrated_nothing.count_user_relations("tb_confiture") == 0


def test_an_application_table_beside_an_empty_ledger_is_still_refused(
    migrated_nothing,
):
    """The control: #262 itself still fires."""
    subprocess.run(
        ["psql", migrated_nothing.url, "-qc", "CREATE TABLE public.orders (id int)"],
        check=True,
        capture_output=True,
    )

    assert migrated_nothing.count_user_relations("tb_confiture") == 1
    with pytest.raises(DatabaseError, match="262"):
        _guard_against_empty_ledger(migrated_nothing, "tb_confiture")
