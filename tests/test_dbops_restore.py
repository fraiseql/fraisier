"""Tests for fraisier.dbops.restore module."""

import contextlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from confiture.exceptions import RestoreError

from fraisier.dbops._pgconn import _KEYWORD_ENV as ENV_VAR
from fraisier.dbops._pgconn import PgConn
from fraisier.dbops.archive import ArchiveCheck, ArchiveVerdict, _read_toc
from fraisier.dbops.restore import (
    find_latest_backup,
    restore_backup,
    validate_table_count,
)
from fraisier.dbops.tviews import TviewError, TviewRebuilt

_TEST_URL = "postgresql://postgres:pass@localhost:5432/postgres"


def _confiture_result(
    *,
    success=True,
    errors=None,
    matviews_deferred=None,
    matviews_refreshed=None,
    analyze_ran=False,
):
    """Build a stand-in for confiture's ``RestoreResult``.

    Mirrors the fields fraisier reads off ``DatabaseRestorer.restore``.
    """
    return SimpleNamespace(
        success=success,
        errors=errors or [],
        warnings=[],
        diagnostics=[],
        phases_completed=[],
        table_count=None,
        matviews_deferred=matviews_deferred,
        matviews_refreshed=matviews_refreshed,
        analyze_ran=analyze_ran,
    )


@contextlib.contextmanager
def _mock_restorer(result=None, *, raises=None):
    """Patch ``DatabaseRestorer`` so ``.restore()`` returns *result* / *raises*.

    Yields the patched class so tests can inspect the ``RestoreOptions`` passed
    to ``DatabaseRestorer().restore(options)``.
    """
    with patch("fraisier.dbops.restore.DatabaseRestorer") as restorer_cls:
        restore = restorer_cls.return_value.restore
        if raises is not None:
            restore.side_effect = raises
        else:
            restore.return_value = result if result is not None else _confiture_result()
        yield restorer_cls


def _options_from(restorer_cls):
    """Return the ``RestoreOptions`` passed to the mocked restore call."""
    return restorer_cls.return_value.restore.call_args[0][0]


class TestRestoreBackup:
    """restore_backup delegates to confiture's three-phase DatabaseRestorer."""

    def test_restore_success(self):
        with _mock_restorer() as restorer_cls:
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
            )

        assert result.success is True
        assert result.error == ""
        restorer_cls.return_value.restore.assert_called_once()
        opts = _options_from(restorer_cls)
        assert opts.target_db == "staging"
        assert opts.backup_path == Path("/backups/prod.dump")
        assert opts.no_owner is True
        assert opts.no_acl is True
        # fraisier validates the table count itself after `migrate up`.
        assert opts.min_tables == 0

    def test_restore_maps_tcp_connection_url(self):
        """Host/port/user from a TCP URL land on RestoreOptions (confiture has
        no connection-URL entry point, only discrete -h/-p/-U)."""
        with _mock_restorer() as restorer_cls:
            restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
            )

        opts = _options_from(restorer_cls)
        assert opts.host == "localhost"
        assert opts.port == 5432
        assert opts.username == "postgres"

    def test_restore_maps_socket_host_query(self):
        """A socket URL carries the host in a ?host= query parameter."""
        with _mock_restorer() as restorer_cls:
            restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url="postgresql:///staging?host=/var/run/postgresql",
            )

        assert _options_from(restorer_cls).host == "/var/run/postgresql"

    @pytest.mark.parametrize(
        ("url", "password"),
        [
            ("postgresql://u@localhost/postgres?password=a+b", "a+b"),
            ("postgresql://u:p%40ss@localhost/postgres", "p@ss"),
        ],
    )
    def test_restore_password_goes_to_restore_options_decoded(self, url, password):
        """#493: libpq's decoding (``+`` stays ``+``), through RestoreOptions."""
        with _mock_restorer() as restorer_cls:
            restore_backup(backup_path="/b.dump", db_name="staging", connection_url=url)

        assert _options_from(restorer_cls).password == password

    def test_restore_query_host_beats_the_netloc(self):
        """libpq reads the query after the netloc; this used to be URI-first."""
        with _mock_restorer() as restorer_cls:
            restore_backup(
                backup_path="/b.dump",
                db_name="staging",
                connection_url="postgresql://b/postgres?host=a",
            )

        assert _options_from(restorer_cls).host == "a"

    def test_os_environ_never_holds_the_password_during_restore(self, monkeypatch):
        """Confiture hands RestoreOptions.password to its children itself."""
        monkeypatch.delenv("PGPASSWORD", raising=False)
        seen: list[str | None] = []
        with _mock_restorer() as restorer_cls:
            restorer_cls.return_value.restore.side_effect = lambda _o: (
                seen.append(__import__("os").environ.get("PGPASSWORD"))
                or _confiture_result()
            )
            restore_backup(
                backup_path="/b.dump", db_name="staging", connection_url=_TEST_URL
            )

        assert seen == [None]

    def _restored_env(self, url=_TEST_URL):
        """The ``RestoreOptions.env`` a restore of *url* hands confiture."""
        with _mock_restorer() as restorer_cls:
            restore_backup(backup_path="/b.dump", db_name="staging", connection_url=url)
        return _options_from(restorer_cls).env

    def test_restore_children_get_the_child_environment(self):
        """Every restore child runs in what any other fraisier child runs in,
        plus the three keywords confiture's --min-tables connection needs."""
        url = _TEST_URL + "?sslmode=require"
        conn = PgConn.parse(url)
        pins = {ENV_VAR[k]: v for k, v in conn.unset_pins().items()}
        assert self._restored_env(url) == {**conn.child_env(), **pins}
        assert set(pins) <= {
            "PGSSLCERTMODE",
            "PGMINPROTOCOLVERSION",
            "PGMAXPROTOCOLVERSION",
        }

    def test_an_ambient_libpq_variable_never_reaches_a_restore_child(self, monkeypatch):
        monkeypatch.setenv("PGOPTIONS", "-c search_path=acme")
        assert "PGOPTIONS" not in self._restored_env()

    def test_the_urls_other_libpq_keys_reach_the_restore_children(self):
        env = self._restored_env(_TEST_URL + "?sslmode=require")
        assert env["PGSSLMODE"] == "require"

    def test_the_restore_children_keep_path(self, monkeypatch):
        """confiture looks pg_restore up on the PATH of the env it is given."""
        monkeypatch.setenv("PATH", "/opt/acme/bin")
        assert self._restored_env()["PATH"] == "/opt/acme/bin"

    def test_os_environ_is_untouched_during_the_restore(self, monkeypatch):
        monkeypatch.delenv("PGSSLMODE", raising=False)
        before = dict(os.environ)
        seen: list[dict[str, str]] = []
        with _mock_restorer() as restorer_cls:
            restorer_cls.return_value.restore.side_effect = lambda _o: (
                seen.append(dict(os.environ)) or _confiture_result()
            )
            restore_backup(
                backup_path="/b.dump",
                db_name="staging",
                connection_url=_TEST_URL + "?sslmode=require",
            )

        assert seen == [before]

    def test_an_ambient_pgservice_refuses_the_restore_before_it_starts(
        self, monkeypatch
    ):
        monkeypatch.setenv("PGSERVICE", "acme")
        with _mock_restorer() as restorer_cls:
            with pytest.raises(ValueError, match="PGSERVICE"):
                restore_backup(
                    backup_path="/b.dump", db_name="staging", connection_url=_TEST_URL
                )
            restorer_cls.return_value.restore.assert_not_called()

    def test_restore_never_runs_as_a_superuser(self):
        """confiture refuses ``env`` beside ``superuser`` (sudo resets the env)."""
        with _mock_restorer() as restorer_cls:
            restore_backup(
                backup_path="/b.dump", db_name="staging", connection_url=_TEST_URL
            )

        assert _options_from(restorer_cls).superuser is None

    def test_restore_refuses_a_multi_host_url(self):
        """confiture passes exactly one -h/-p."""
        with _mock_restorer(), pytest.raises(ValueError, match="host"):
            restore_backup(
                backup_path="/b.dump",
                db_name="staging",
                connection_url="postgresql://h1,h2/postgres",
            )

    def test_restore_requires_connection_url(self):
        with pytest.raises(TypeError):
            restore_backup(  # ty: ignore[missing-argument]
                backup_path="/backups/prod.dump", db_name="staging"
            )

    def test_restore_failure(self):
        result_obj = _confiture_result(success=False, errors=["pg_restore: error"])
        with _mock_restorer(result_obj):
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
            )

        assert result.success is False
        assert "pg_restore: error" in result.error

    def test_restore_raises_restoreerror_returns_failure(self):
        """A RestoreError (e.g. unsupported dump format) becomes a failed result,
        not a raised exception — the strategy expects to branch on .success."""
        with _mock_restorer(raises=RestoreError("Unrecognised dump format")):
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
            )

        assert result.success is False
        assert "dump format" in result.error

    def test_restore_backup_accepts_directory_dump_path(self, tmp_path: Path):
        """restore_backup forwards a directory-format dump path unchanged.

        confiture auto-detects ``-Fd`` from the archive; fraisier passes the
        directory path straight through as ``RestoreOptions.backup_path``.
        Lock-in for #202 Phase 3 directory dumps.
        """
        dir_dump = tmp_path / "mydb_full_20260520_1200_zstd.dump"
        dir_dump.mkdir()
        (dir_dump / "toc.dat").write_text("stub toc")

        with _mock_restorer() as restorer_cls:
            result = restore_backup(
                backup_path=str(dir_dump),
                db_name="staging",
                connection_url=_TEST_URL,
            )

        assert result.success is True
        assert _options_from(restorer_cls).backup_path == dir_dump

    def test_restore_with_owner_fix(self):
        with _mock_restorer(), patch("fraisier.dbops.restore._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                db_owner="appuser",
                connection_url=_TEST_URL,
            )

        assert result.success is True
        # confiture does the restore; _pg_cmd runs only the REASSIGN OWNED step,
        # and it runs it through stdin: `psql -c` never substitutes psql
        # variables, on any version (#380).
        mock_cmd.assert_called_once()
        reassign_cmd = mock_cmd.call_args[0][0]
        assert "psql" in reassign_cmd
        assert reassign_cmd[-2:] == ["-f", "-"]
        assert "-c" not in reassign_cmd
        assert "owner=appuser" in reassign_cmd
        piped = mock_cmd.call_args.kwargs["input_text"]
        assert "REASSIGN OWNED BY CURRENT_USER" in piped

    def test_restore_owner_fix_failure_reported(self):
        """REASSIGN OWNED BY failure sets success=False with error."""
        with (
            _mock_restorer(),
            patch("fraisier.dbops.restore._pg_cmd") as mock_cmd,
        ):
            mock_cmd.return_value = (1, "", "ERROR: role does not exist")
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                db_owner="baduser",
                connection_url=_TEST_URL,
            )

        assert result.success is False
        assert "baduser" in result.error or "role" in result.error

    def test_restore_with_jobs_enables_parallel(self):
        with _mock_restorer() as restorer_cls:
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
                jobs=4,
            )

        assert result.success is True
        opts = _options_from(restorer_cls)
        assert opts.jobs == 4
        # jobs > 1 → parallel data phase; transient FK errors must not abort.
        assert opts.parallel_restore is True

    def test_restore_jobs_1_serial(self):
        with _mock_restorer() as restorer_cls:
            restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
                jobs=1,
            )

        opts = _options_from(restorer_cls)
        assert opts.jobs == 1
        assert opts.parallel_restore is False

    def test_restore_default_serial(self):
        with _mock_restorer() as restorer_cls:
            restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
            )

        assert _options_from(restorer_cls).parallel_restore is False

    def test_restore_surfaces_matview_accounting(self):
        """confiture's deferred-matview fields flow onto fraisier's RestoreResult."""
        result_obj = _confiture_result(
            matviews_deferred=2, matviews_refreshed=2, analyze_ran=True
        )
        with _mock_restorer(result_obj):
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
            )

        assert result.matviews_deferred == 2
        assert result.matviews_refreshed == 2
        assert result.analyze_ran is True

    def test_restore_matview_fields_none_without_matviews(self):
        with _mock_restorer():
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
            )

        assert result.matviews_deferred is None
        assert result.matviews_refreshed is None
        assert result.analyze_ran is False

    def test_restore_returns_duration(self):
        with _mock_restorer():
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
            )

        assert result.duration_seconds > 0

    def test_restore_failure_still_has_duration(self):
        result_obj = _confiture_result(success=False, errors=["pg_restore: error"])
        with _mock_restorer(result_obj):
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
            )

        assert not result.success
        assert result.duration_seconds > 0

    def test_restore_rejects_bad_db_name(self):
        with pytest.raises(ValueError, match="Invalid database name"):
            restore_backup(
                backup_path="/backups/prod.dump",
                db_name="bad name!",
                connection_url=_TEST_URL,
            )

    def test_restore_rejects_bad_owner(self):
        with pytest.raises(ValueError, match="Invalid database owner"):
            restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                db_owner="bad;owner",
                connection_url=_TEST_URL,
            )


class TestTviewRebuild:
    """A restored database gets ``pg_tviews_rebuild_all(only_empty)`` (#422).

    On a pg_dump restore the rows come back and this is one query that finds
    nothing to do.  It is here because the same chain is reused where it is not a
    no-op: a physical restore empties every UNLOGGED TVIEW (#424).
    """

    def _restore(self):
        return restore_backup(
            backup_path="/backups/prod.dump",
            db_name="staging",
            connection_url=_TEST_URL,
        )

    def test_the_rebuild_is_reported_when_pg_tviews_is_installed(self):
        rebuilt = [TviewRebuilt("post", 3)]
        with (
            _mock_restorer(),
            patch("fraisier.dbops.restore.tviews_installed", return_value=True),
            patch(
                "fraisier.dbops.restore.rebuild_empty_tviews", return_value=rebuilt
            ) as rebuild,
        ):
            result = self._restore()

        assert result.success
        assert result.tviews_rebuilt == (TviewRebuilt("post", 3),)
        # asked of the restored database, not of the maintenance one
        assert rebuild.call_args.args[0].endswith("/staging")

    def test_nothing_to_rebuild_is_an_empty_tuple_not_none(self):
        with (
            _mock_restorer(),
            patch("fraisier.dbops.restore.tviews_installed", return_value=True),
            patch("fraisier.dbops.restore.rebuild_empty_tviews", return_value=[]),
        ):
            result = self._restore()

        assert result.tviews_rebuilt == ()

    def test_without_pg_tviews_it_is_none_and_nothing_is_rebuilt(self):
        with (
            _mock_restorer(),
            patch("fraisier.dbops.restore.rebuild_empty_tviews") as rebuild,
        ):
            result = self._restore()

        assert result.tviews_rebuilt is None
        rebuild.assert_not_called()

    def test_a_failed_rebuild_fails_the_restore_at_its_own_stage(self):
        with (
            _mock_restorer(),
            patch("fraisier.dbops.restore.tviews_installed", return_value=True),
            patch(
                "fraisier.dbops.restore.rebuild_empty_tviews",
                side_effect=TviewError("refresh blew up"),
            ),
        ):
            result = self._restore()

        assert not result.success
        assert result.stage == "tview_rebuild"
        assert "refresh blew up" in result.error

    def test_an_outdated_pg_tviews_fails_it_too(self):
        with (
            _mock_restorer(),
            patch(
                "fraisier.dbops.restore.tviews_installed",
                side_effect=TviewError("predates read contract 1"),
            ),
        ):
            result = self._restore()

        assert result.stage == "tview_rebuild"

    def test_a_failed_restore_never_reaches_the_rebuild(self):
        with (
            _mock_restorer(_confiture_result(success=False, errors=["boom"])),
            patch("fraisier.dbops.restore.tviews_installed") as installed,
        ):
            result = self._restore()

        assert result.stage == "restore"
        installed.assert_not_called()


class TestValidateTableCount:
    """Test validate_table_count."""

    def test_validate_table_count_pass(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="75\n", stderr="")
            ok, count = validate_table_count(
                "staging", min_threshold=50, connection_url=_TEST_URL
            )

        assert ok is True
        assert count == 75

    def test_validate_table_count_fail(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="10\n", stderr="")
            ok, count = validate_table_count(
                "staging", min_threshold=50, connection_url=_TEST_URL
            )

        assert ok is False
        assert count == 10


class TestFindLatestBackup:
    """Test find_latest_backup."""

    def test_find_latest_backup(self, tmp_path: Path):
        older = tmp_path / "prod_full_20250101.dump"
        older.write_text("old")

        newer = tmp_path / "prod_full_20250320.dump"
        newer.write_text("new")

        # Ensure distinct mtimes
        import os

        os.utime(older, (1000, 1000))
        os.utime(newer, (2000, 2000))

        result = find_latest_backup(tmp_path)
        assert result == newer

    def test_find_latest_backup_empty(self, tmp_path: Path):
        result = find_latest_backup(tmp_path)
        assert result is None

    def test_find_latest_backup_preferred_compression(self, tmp_path: Path):
        """With preferred_compression='lz4', returns newest lz4 dump even if
        a newer zstd dump exists."""
        import os

        zstd_old = tmp_path / "db_full_20260501_0000_zstd.dump"
        zstd_old.write_text("old zstd")
        os.utime(zstd_old, (1000, 1000))

        lz4_mid = tmp_path / "db_full_20260502_0000_lz4.dump"
        lz4_mid.write_text("mid lz4")
        os.utime(lz4_mid, (2000, 2000))

        zstd_new = tmp_path / "db_full_20260503_0000_zstd.dump"
        zstd_new.write_text("new zstd")
        os.utime(zstd_new, (3000, 3000))

        result = find_latest_backup(tmp_path, preferred_compression="lz4")
        assert result == lz4_mid

    def test_find_latest_backup_preferred_compression_fallback(self, tmp_path: Path):
        """When no dump matches the preferred compression, fall back to newest."""
        import os

        zstd = tmp_path / "db_full_20260501_0000_zstd.dump"
        zstd.write_text("zstd")
        os.utime(zstd, (1000, 1000))

        result = find_latest_backup(tmp_path, preferred_compression="lz4")
        assert result == zstd

    def test_find_latest_backup_preferred_compression_none(self, tmp_path: Path):
        """No preference returns newest regardless of compression."""
        import os

        lz4 = tmp_path / "db_full_20260501_0000_lz4.dump"
        lz4.write_text("lz4")
        os.utime(lz4, (1000, 1000))

        zstd = tmp_path / "db_full_20260502_0000_zstd.dump"
        zstd.write_text("zstd")
        os.utime(zstd, (2000, 2000))

        result = find_latest_backup(tmp_path)
        assert result == zstd

    def test_find_latest_backup_returns_directory_dump(self, tmp_path: Path):
        """find_latest_backup discovers pg_dump -Fd directory dumps (#202 Phase 2).

        Parallel pg_dump produces a directory containing toc.dat + per-table
        .dat files. Path.glob matches both files and directories, so the
        function should locate it without special-casing.
        """
        import os

        dir_dump = tmp_path / "mydb_full_20260520_1200_zstd.dump"
        dir_dump.mkdir()
        (dir_dump / "toc.dat").write_text("stub toc")
        os.utime(dir_dump, (2000, 2000))

        result = find_latest_backup(tmp_path)
        assert result == dir_dump
        assert result is not None and result.is_dir()

    def test_find_latest_backup_picks_newer_directory_over_older_file(
        self, tmp_path: Path
    ):
        """When a directory dump is newer than the latest file dump, it wins."""
        import os

        file_dump = tmp_path / "mydb_full_20260519_1200_zstd.dump"
        file_dump.write_text("file dump bytes")
        os.utime(file_dump, (1000, 1000))

        dir_dump = tmp_path / "mydb_full_20260520_1200_zstd.dump"
        dir_dump.mkdir()
        (dir_dump / "toc.dat").write_text("stub toc")
        os.utime(dir_dump, (2000, 2000))

        result = find_latest_backup(tmp_path)
        assert result == dir_dump

    def test_find_latest_backup_picks_newer_file_over_older_directory(
        self, tmp_path: Path
    ):
        """Symmetric case: newer file dump wins over older directory dump."""
        import os

        dir_dump = tmp_path / "mydb_full_20260519_1200_zstd.dump"
        dir_dump.mkdir()
        (dir_dump / "toc.dat").write_text("stub toc")
        os.utime(dir_dump, (1000, 1000))

        file_dump = tmp_path / "mydb_full_20260520_1200_zstd.dump"
        file_dump.write_text("file dump bytes")
        os.utime(file_dump, (2000, 2000))

        result = find_latest_backup(tmp_path)
        assert result == file_dump

    def test_find_latest_backup_preferred_compression_matches_directory(
        self, tmp_path: Path
    ):
        """preferred_compression matches directory dumps by filename suffix.

        The producer (Phase 3) names directory dumps `<db>_<mode>_<ts>_<algo>.dump/`,
        so the filename-suffix match used by file dumps works unchanged.
        """
        import os

        zstd_file = tmp_path / "mydb_full_20260520_1300_zstd.dump"
        zstd_file.write_text("zstd file dump")
        os.utime(zstd_file, (2000, 2000))  # newer

        lz4_dir = tmp_path / "mydb_full_20260520_1200_lz4.dump"
        lz4_dir.mkdir()
        (lz4_dir / "toc.dat").write_text("stub toc")
        os.utime(lz4_dir, (1000, 1000))  # older

        result = find_latest_backup(tmp_path, preferred_compression="lz4")
        assert result == lz4_dir


_TOCS = Path(__file__).parent / "fixtures" / "pg_tviews"
_REGISTRY = [("Mixed", "tv_note"), ("public", "tv_post"), ("public", "tv_user")]


def _archive(kind: str) -> ArchiveCheck:
    """An archive check read from a real ``pg_restore --list`` (beta.29)."""
    toc = _read_toc((_TOCS / f"toc-{kind}-beta29.txt").read_text())
    return ArchiveCheck(
        ArchiveVerdict.VALID,
        "",
        toc.table_data_by_schema,
        table_entries=toc.tables,
        table_data_entries=toc.table_data,
    )


class TestLeanArchiveRefill:
    """A dump that left TVIEW rows out is refilled whole, not ``only_empty`` (#471).

    ``only_empty`` refills only a TVIEW PostgreSQL *reset*: never a LOGGED one
    (the default from pg_tviews beta.28), and on beta.29 not an UNLOGGED one
    either, since its validity row survives ``--exclude-table-data`` (measured).
    """

    def _restore(self, archive: ArchiveCheck | None, path="/backups/prod.dump"):
        return restore_backup(
            backup_path=path,
            db_name="staging",
            connection_url=_TEST_URL,
            archive=archive,
        )

    def test_a_lean_archive_is_refilled_whole(self):
        with (
            _mock_restorer(),
            patch("fraisier.dbops.restore.tviews_installed", return_value=True),
            patch("fraisier.dbops.restore.list_tviews", return_value=_REGISTRY),
            patch(
                "fraisier.dbops.restore.rebuild_all_tviews",
                return_value=[TviewRebuilt("post", 5000)],
            ) as whole,
            patch("fraisier.dbops.restore.rebuild_empty_tviews") as only_empty,
        ):
            result = self._restore(_archive("lean"))

        assert result.success, result.error
        assert result.tviews_rebuilt == (TviewRebuilt("post", 5000),)
        assert result.tviews_refilled_lean is True
        assert whole.call_args.args[0].endswith("/staging")
        only_empty.assert_not_called()

    def test_a_full_archive_keeps_only_empty(self):
        with (
            _mock_restorer(),
            patch("fraisier.dbops.restore.tviews_installed", return_value=True),
            patch("fraisier.dbops.restore.list_tviews", return_value=_REGISTRY),
            patch("fraisier.dbops.restore.rebuild_all_tviews") as whole,
            patch(
                "fraisier.dbops.restore.rebuild_empty_tviews", return_value=[]
            ) as only_empty,
        ):
            result = self._restore(_archive("full"))

        assert result.tviews_rebuilt == ()
        assert result.tviews_refilled_lean is False
        only_empty.assert_called_once()
        whole.assert_not_called()

    def test_a_table_without_data_that_is_no_tview_is_not_lean(self):
        """A partitioned parent has a ``TABLE`` entry and no ``TABLE DATA``."""
        with (
            _mock_restorer(),
            patch("fraisier.dbops.restore.tviews_installed", return_value=True),
            patch(
                "fraisier.dbops.restore.list_tviews",
                return_value=[("Mixed", "tb_note")],
            ),
            patch("fraisier.dbops.restore.rebuild_all_tviews") as whole,
            patch("fraisier.dbops.restore.rebuild_empty_tviews", return_value=[]),
        ):
            self._restore(_archive("lean"))

        whole.assert_not_called()

    def test_an_unread_archive_falls_back_to_the_lean_name(self):
        with (
            _mock_restorer(),
            patch("fraisier.dbops.restore.tviews_installed", return_value=True),
            patch(
                "fraisier.dbops.restore.rebuild_all_tviews", return_value=[]
            ) as whole,
        ):
            self._restore(None, path="/b/prod_full_20261013_1200_lean_zstd.dump")

        whole.assert_called_once()

    def test_a_lean_archive_over_an_unreadable_pg_tviews_fails_not_skips(self):
        """Skipping, as a full archive does, would serve every TVIEW empty."""
        with (
            _mock_restorer(),
            patch("fraisier.dbops.restore.tviews_installed", return_value=False),
            patch(
                "fraisier.dbops.restore.rebuild_all_tviews",
                side_effect=TviewError("predates read contract 1"),
            ),
        ):
            result = self._restore(
                _archive("lean"), path="/b/prod_full_20261013_1200_lean_zstd.dump"
            )

        assert not result.success
        assert result.stage == "tview_rebuild"
        assert "contract 1" in result.error

    def test_the_log_says_which_refill_ran(self, caplog):
        from fraisier.strategies._restore_sources import _log_rebuilt

        with caplog.at_level("INFO"):
            _log_rebuilt([TviewRebuilt("post", 5000)], lean=True)
            _log_rebuilt([TviewRebuilt("user", 3)])

        assert (
            "pg_tviews: refilled 1 TVIEW(s) left out of a lean dump post=5000rows"
            in caplog.text
        )
        assert "pg_tviews: rebuilt 1 reset TVIEW(s) user=3rows" in caplog.text
