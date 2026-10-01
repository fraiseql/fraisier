"""Refresh a staging cluster from a real pgBackRest backup (#424).

Real pgBackRest 2.59.2 and two real PostgreSQL clusters in a Docker container: a
"production" cluster archiving into a pgBackRest repository, and a dedicated
"staging" cluster that ``restore.source: pgbackrest`` refreshes with ``--delta``.

What runs for real: the restore **source**, the **client**, the **socket** with its
``SO_PEERCRED`` check, the **helper's operations** (argv construction, parsing, the
guards), the strategy's whole post-restore chain, the actuation receipt, and
pgBackRest and PostgreSQL themselves.  What is substituted is only the process
spawner: the helper's commands run inside the container via ``docker exec``.

Not exercised: the helper under a real *systemd* (socket activation, the unit's
sandbox).  The container has no systemd; ``systemd-analyze verify`` and the unit
tests cover the unit files.

Needs Docker, network (to ``apt-get install pgbackrest`` once) and
``FRAISIER_INTEGRATION=1``.  CI has no pgBackRest and no second cluster, so there
this skips — it is not a pass; the PR records the local run.  Set
``FRAISIER_PGBACKREST_BASE_IMAGE`` to an image with pg_tviews to exercise the
TVIEW rebuild too (the default is stock ``postgres:18``).
"""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn
from unittest.mock import MagicMock, patch

import pytest

from fraisier import pgbackrest_helper as helper
from fraisier.config.restore_source import PgBackRestSpec
from fraisier.dbops.confiture import MigrationResult
from fraisier.dbops.receipt import ActuationVerdict
from fraisier.errors import DatabaseError, RestoreFailedClosed
from fraisier.strategies import RestoreConfig, RestoreMigrateStrategy
from tests.integration.pgbackrest import lab as labmod

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

#: Not skipped by ``unavailable()``: that is for tests CI is *supposed* to run, and
#: CI has no pgBackRest harness. A skip here says so.
_WHY = "no pgBackRest harness in CI (needs Docker and FRAISIER_INTEGRATION=1)"
_SEED = (
    "create table tb_user (pk_user bigint primary key, "
    "id uuid not null unique, name text);"
    "create table tb_post (pk_post bigint primary key, "
    "id uuid not null unique, fk_user bigint references tb_user, title text);"
    "insert into tb_user values (1, '00000000-0000-0000-0000-000000000001', 'ada');"
    "insert into tb_post values (1, '00000000-0000-0000-0000-0000000000a1', 1, 'first');"
)
_TVIEW = (
    "create table tv_post as select p.pk_post, p.id, "
    "jsonb_build_object('id', p.id, 'author', u.name) as data "
    "from tb_post p join tb_user u on u.pk_user = p.fk_user;"
)


def _skip(reason: str) -> NoReturn:
    pytest.skip(reason)  # ty: ignore[too-many-positional-arguments]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def lab() -> Iterator[labmod.Lab]:
    if os.getenv("FRAISIER_INTEGRATION") != "1":
        _skip(f"FRAISIER_INTEGRATION is not 1: {_WHY}")
    if (reason := labmod.docker_available()) is not None:
        _skip(f"{reason}: {_WHY}")
    base = os.getenv("FRAISIER_PGBACKREST_BASE_IMAGE", "postgres:18")
    try:
        image = labmod.build_image(base)
    except subprocess.CalledProcessError as exc:
        _skip(f"could not build the harness image from {base}: {exc.stderr[-200:]!r}")
    host = labmod.start_lab(image, _free_port())
    try:
        host.psql(5432, "postgres", "create database app")
        tviews = host.has_pg_tviews()
        if tviews:
            host.psql(5432, "app", "create extension pg_tviews")
        host.psql(5432, "app", _SEED + (_TVIEW if tviews else ""))
        host.sh("su postgres -c 'pgbackrest --stanza=main --type=full backup'")
        yield host
    finally:
        labmod.stop_lab(host)


def _backup_after(lab: labmod.Lab, sql: str) -> None:
    """Mutate production, push the change into the archive, take an incremental."""
    lab.psql(5432, "app", sql)
    lab.psql(5432, "postgres", "select pg_switch_wal()")
    lab.sh("su postgres -c 'pgbackrest --stanza=main --type=incr backup'")


@pytest.fixture
def helper_socket(lab: labmod.Lab) -> Iterator[str]:
    """The real helper behind a real socket, with its commands run in the container."""
    operations = helper.Operations(
        helper.HelperConfig(
            stanza="main",
            repo=1,
            version="18",
            name="staging",
            target="latest",
            timeout_seconds=900,
        ),
        run=lab.run,
    )
    directory = tempfile.mkdtemp(prefix="fsr-", dir="/tmp")
    path = str(Path(directory) / "h.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(4)
    stopping = threading.Event()

    def serve() -> None:
        while not stopping.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            helper._serve_connection(conn, operations, expected_uid=os.getuid())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield path
    finally:
        stopping.set()
        server.close()
        thread.join(timeout=5)


def _refresh(lab: labmod.Lab, socket_path: str, service: MagicMock) -> object:
    config = RestoreConfig(
        db_name="app",
        backup_dir=Path(),
        pgbackrest=PgBackRestSpec(
            stanza="main", repo=1, cluster="18/staging", timeout_seconds=900
        ),
        pgbackrest_socket=socket_path,
    )
    strategy = RestoreMigrateStrategy(
        config,
        admin_url=lab.admin_url(),
        service_manager=service,
        service_name="api.service",
    )
    with patch(
        "fraisier.strategies._restore.migrate_up",
        return_value=MigrationResult(True, steps_applied=0),
    ):
        return strategy.execute(Path("confiture.yaml"))


def test_a_refresh_brings_production_changes_over_and_runs_the_whole_chain(
    lab: labmod.Lab, helper_socket: str, caplog: pytest.LogCaptureFixture
) -> None:
    _backup_after(lab, "update tb_user set name = 'grace' where pk_user = 1;")
    latest = lab.sh(
        "su postgres -c 'pgbackrest --stanza=main --repo=1 info --output=json' "
        '| grep -o \'"label":"[^"]*"\' | tail -1 | cut -d\'"\' -f4'
    ).stdout.strip()
    service = MagicMock()

    with caplog.at_level(logging.INFO):
        result = _refresh(lab, helper_socket, service)

    # The mutation is visible in staging.
    assert (
        lab.psql(5433, "app", "select name from tb_user where pk_user = 1") == "grace"
    )
    # The service was stopped first and started last, once each.
    assert [c[0] for c in service.method_calls if c[0] in {"stop", "start"}] == [
        "stop",
        "start",
    ]
    # The restored cluster is safe: it cannot archive, carries no replication
    # settings, and has left recovery.
    assert lab.psql(5433, "postgres", "show archive_mode") == "off"
    assert lab.psql(5433, "postgres", "show restore_command") == ""
    assert lab.psql(5433, "postgres", "show primary_conninfo") == ""
    assert lab.psql(5433, "postgres", "select pg_is_in_recovery()") == "f"
    assert (
        lab.sh(
            "ls /var/lib/postgresql/18/staging/*.signal 2>/dev/null; true"
        ).stdout.strip()
        == ""
    )
    # The post-restore chain ran: the actuation receipt names this run and backup.
    actuation = result.actuation  # ty: ignore[unresolved-attribute]
    assert actuation.verdict is ActuationVerdict.ACTUATED
    assert actuation.receipt.backup_path == f"pgbackrest:main/{latest}"
    # The deploy log reports label, stop time and the rewritten-file count.
    assert latest in caplog.text
    assert re.search(r"stopped 20\d\d-", caplog.text)
    assert re.search(r"\d+ of \d+ files rewritten", caplog.text)


def test_the_unlogged_tviews_a_physical_restore_empties_are_rebuilt(
    lab: labmod.Lab, helper_socket: str
) -> None:
    if not lab.has_pg_tviews():
        _skip(
            "the harness base image has no pg_tviews; set FRAISIER_PGBACKREST_BASE_IMAGE"
        )
    _backup_after(
        lab,
        "update tb_user set name = 'hopper' where pk_user = 1;"
        "insert into tb_post values (2, '00000000-0000-0000-0000-0000000000a2', 1, 'second');",
    )

    _refresh(lab, helper_socket, MagicMock())

    # `tv_post` is UNLOGGED: a physical restore leaves it empty, and the rebuild
    # (#422) is what puts the rows — and the propagated change — back.
    assert lab.psql(5433, "app", "select count(*) from tv_post") == "2"
    assert (
        lab.psql(5433, "app", "select data->>'author' from tv_post where pk_post = 1")
        == "hopper"
    )


def test_a_second_refresh_rewrites_only_what_changed(
    lab: labmod.Lab, helper_socket: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The reason for ``--delta``: staging keeps files that already match."""
    _backup_after(lab, "update tb_user set name = 'turing' where pk_user = 1;")

    with caplog.at_level(logging.INFO):
        _refresh(lab, helper_socket, MagicMock())

    match = re.search(r"(\d+) of (\d+) files rewritten", caplog.text)
    assert match is not None
    rewritten, total = int(match[1]), int(match[2])
    assert total > 100
    assert rewritten < total * 0.1, f"rewrote {rewritten} of {total}: not a delta"


def test_a_cluster_that_comes_up_archiving_fails_closed(
    lab: labmod.Lab, helper_socket: str
) -> None:
    """The restored cluster must never archive into the source stanza.

    ``archive_mode = 'off'`` is written into ``postgresql.auto.conf``, and a
    command-line ``-c`` still beats it — here through the cluster's own
    ``pg_ctl.conf``.  The check reads what the server *is running with*, so this
    is caught; the cluster is stopped again and the service never started.
    """
    pg_ctl_conf = "/etc/postgresql/18/staging/pg_ctl.conf"
    lab.sh(f"printf \"pg_ctl_options = '-o -carchive_mode=on'\\n\" > {pg_ctl_conf}")
    service = MagicMock()
    try:
        with pytest.raises(RestoreFailedClosed, match="archive_mode"):
            _refresh(lab, helper_socket, service)

        assert lab.cluster_status("staging") == "down"
        service.start.assert_not_called()
    finally:
        lab.sh(f"printf \"pg_ctl_options = ''\\n\" > {pg_ctl_conf}")

    # And the failure is recoverable: the same refresh succeeds once the cause is gone.
    result = _refresh(lab, helper_socket, MagicMock())
    assert result.success  # ty: ignore[unresolved-attribute]
    assert lab.psql(5433, "postgres", "show archive_mode") == "off"


def test_a_missing_backup_is_refused_before_anything_is_stopped(
    lab: labmod.Lab, helper_socket: str
) -> None:
    config = RestoreConfig(
        db_name="app",
        backup_dir=Path(),
        pgbackrest=PgBackRestSpec(
            stanza="main",
            repo=1,
            cluster="18/staging",
            target="2000-01-01 00:00:00+00",
        ),
        pgbackrest_socket=helper_socket,
    )
    service = MagicMock()
    strategy = RestoreMigrateStrategy(
        config,
        admin_url=lab.admin_url(),
        service_manager=service,
        service_name="x.service",
    )
    before = lab.cluster_status("staging")

    with pytest.raises(DatabaseError, match="no backup stopped"):
        strategy.execute(Path("confiture.yaml"))

    service.stop.assert_not_called()
    assert lab.cluster_status("staging") == before
