"""Tests for fraisier.dbops.archive — is this file a readable pg_dump archive?

The three-valued answer is the point. A host that holds dumps and has no
PostgreSQL client tools cannot tell whether a dump is good, and "I could not
check" must never collapse into "the dump is bad": one of the callers deletes
files and another refuses to restore. Both read :attr:`ArchiveCheck.is_bad`,
which is true for ``INVALID`` alone.
"""

import re
import subprocess
import tokenize
from pathlib import Path
from unittest.mock import patch

import fraisier
from fraisier.dbops.archive import ArchiveVerdict, verify_archive

# pg_dump custom/directory-format magic. A truncated dump still carries it,
# which is exactly why the header is not the check — pg_restore --list is.
_PGDUMP_MAGIC = b"PGDMP"


class TestVerifyArchiveVerdicts:
    """The three verdicts, from the three things that can happen."""

    def test_readable_archive_is_valid(self, tmp_path: Path):
        dump = tmp_path / "proddb_full.dump"
        dump.write_bytes(_PGDUMP_MAGIC + b"\x00" * 64)
        with patch("fraisier.dbops.archive.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="; Archive: proddb\n", stderr=""
            )
            check = verify_archive(dump)
        assert check.verdict is ArchiveVerdict.VALID
        assert check.is_valid is True
        assert check.is_bad is False

    def test_rejected_archive_is_invalid_and_carries_stderr(self, tmp_path: Path):
        dump = tmp_path / "truncated.dump"
        dump.write_bytes(_PGDUMP_MAGIC + b"\x00" * 8)
        with patch("fraisier.dbops.archive.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="pg_restore: error: could not read from input file: end of file\n",
            )
            check = verify_archive(dump)
        assert check.verdict is ArchiveVerdict.INVALID
        assert check.is_bad is True
        assert "end of file" in check.detail

    def test_directory_archive_is_valid(self, tmp_path: Path):
        dump = tmp_path / "proddb_full.dump"
        dump.mkdir()
        (dump / "toc.dat").write_bytes(_PGDUMP_MAGIC + b"\x00" * 64)
        with patch("fraisier.dbops.archive.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="; Archive: proddb\n", stderr=""
            )
            check = verify_archive(dump)
        assert check.verdict is ArchiveVerdict.VALID


class TestUnverifiableIsNotInvalid:
    """A host that cannot check has not found a bad dump."""

    def test_missing_pg_restore_is_unverifiable(self, tmp_path: Path):
        dump = tmp_path / "proddb_full.dump"
        dump.write_bytes(_PGDUMP_MAGIC + b"\x00" * 64)
        with patch("fraisier.dbops.archive.subprocess.run") as run:
            run.side_effect = FileNotFoundError("pg_restore")
            check = verify_archive(dump)
        assert check.verdict is ArchiveVerdict.UNVERIFIABLE
        assert check.is_bad is False
        assert check.is_valid is False
        assert "pg_restore" in check.detail

    def test_absent_path_is_unverifiable_and_names_the_path(self, tmp_path: Path):
        missing = tmp_path / "never_arrived.dump"
        check = verify_archive(missing)
        assert check.verdict is ArchiveVerdict.UNVERIFIABLE
        assert check.is_bad is False
        assert "never_arrived.dump" in check.detail

    def test_absent_path_does_not_shell_out(self, tmp_path: Path):
        with patch("fraisier.dbops.archive.subprocess.run") as run:
            verify_archive(tmp_path / "never_arrived.dump")
        run.assert_not_called()

    def test_timeout_is_unverifiable(self, tmp_path: Path):
        dump = tmp_path / "proddb_full.dump"
        dump.write_bytes(_PGDUMP_MAGIC + b"\x00" * 64)
        with patch("fraisier.dbops.archive.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd="pg_restore", timeout=60)
            check = verify_archive(dump)
        assert check.verdict is ArchiveVerdict.UNVERIFIABLE
        assert check.is_bad is False


class TestNoCallerConvictsOnAbsence:
    """The rule, enforced where it can be rather than inferred per call site.

    #341's lesson in a different costume: a green test listed `ProtectHome=true`
    as *required* for a unit that could not exec because of it, so the assertion
    read as evidence the unit was correct. The equivalent here would be every
    caller happening to write `is_bad` today and the next one writing
    `verdict != VALID`, which convicts a dump for the absence of the tool that
    would have cleared it. State it once, over the tree.
    """

    _COMPARISON = re.compile(
        r"verdict\s*(?:!=|is\s+not)\s*(?:ArchiveVerdict\s*\.\s*)?VALID"
    )

    @staticmethod
    def _code_only(path: Path) -> str:
        """*path*'s source with comments and string literals blanked out.

        Docstrings are the reason this is not a grep: `archive.py` names the
        anti-pattern in its own documentation, which is the module doing its job
        rather than an offender. Blanking rather than dropping keeps line numbers
        intact so an offender can be pointed at.
        """
        lines = path.read_text().splitlines()
        blanked = list(lines)
        with path.open("rb") as handle:
            for tok in tokenize.tokenize(handle.readline):
                if tok.type not in (tokenize.COMMENT, tokenize.STRING):
                    continue
                first, last = tok.start[0] - 1, tok.end[0] - 1
                for index in range(first, min(last + 1, len(blanked))):
                    blanked[index] = ""
        return "\n".join(blanked)

    def test_no_source_file_compares_a_verdict_against_valid(self):
        package = Path(fraisier.__file__).parent
        offenders = [
            f"{path.relative_to(package)}:{lineno}"
            for path in sorted(package.rglob("*.py"))
            for lineno, line in enumerate(self._code_only(path).splitlines(), start=1)
            if self._COMPARISON.search(line)
        ]

        assert not offenders, (
            "code treating UNVERIFIABLE as a bad dump (use ArchiveCheck.is_bad, "
            "which is INVALID-only):\n" + "\n".join(offenders)
        )

    def test_the_guard_would_catch_a_real_offender(self, tmp_path: Path):
        """A guard that cannot fail is not a guard.

        Pins that `_code_only` blanks prose without also blanking the code the
        pattern is meant to find — the failure mode that would make the test
        above pass on a tree that does convict on absence.
        """
        offender = tmp_path / "offender.py"
        offender.write_text(
            '"""A docstring mentioning verdict != VALID harmlessly."""\n'
            "def f(check):\n"
            "    return check.verdict != ArchiveVerdict.VALID\n"
        )
        code = self._code_only(offender)
        assert self._COMPARISON.search(code)
        assert "harmlessly" not in code

    def test_is_bad_is_true_for_invalid_only(self):
        from fraisier.dbops.archive import ArchiveCheck

        bad = [v for v in ArchiveVerdict if ArchiveCheck(v, "").is_bad]
        assert bad == [ArchiveVerdict.INVALID]


class TestVerifyArchiveCommand:
    """No database is required to read an archive's table of contents."""

    def test_runs_pg_restore_list_against_the_path(self, tmp_path: Path):
        dump = tmp_path / "proddb_full.dump"
        dump.write_bytes(_PGDUMP_MAGIC + b"\x00" * 64)
        with patch("fraisier.dbops.archive.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            verify_archive(dump)
        cmd = run.call_args[0][0]
        assert cmd[0] == "pg_restore"
        assert "--list" in cmd
        assert str(dump) in cmd

    def test_command_carries_no_connection_flags(self, tmp_path: Path):
        """A receiving host may have no database at all.

        ``pg_restore --list`` reads the archive and never connects, so a
        connection flag here would be a requirement the check does not have —
        and on a dump-only host, one it could not satisfy.
        """
        dump = tmp_path / "proddb_full.dump"
        dump.write_bytes(_PGDUMP_MAGIC + b"\x00" * 64)
        with patch("fraisier.dbops.archive.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            verify_archive(dump)
        cmd = run.call_args[0][0]
        for flag in ("-h", "-p", "-U", "-d", "--dbname", "--host"):
            assert flag not in cmd

    def test_accepts_a_string_path(self, tmp_path: Path):
        dump = tmp_path / "proddb_full.dump"
        dump.write_bytes(_PGDUMP_MAGIC + b"\x00" * 64)
        with patch("fraisier.dbops.archive.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            check = verify_archive(str(dump))
        assert check.verdict is ArchiveVerdict.VALID


#: A faithful `pg_restore --list` table of contents: header comments, a SCHEMA
#: entry whose schema field is `-`, base tables in two schemas, a matview (whose
#: data tag is MATERIALIZED VIEW DATA, not TABLE DATA), a view (no data entry at
#: all), and a quoted table name that *contains* the words TABLE DATA.
_TOC = """;
; Archive created at 2026-08-09 02:00:00 UTC
;     dbname: printoptim
;     TOC Entries: 12
;     Format: CUSTOM
;     Dumped by pg_dump version: 16.3
;
;
; Selected TOC Entries:
;
5; 2615 16385 SCHEMA - tenant postgres
216; 1259 16456 TABLE tenant tb_order postgres
217; 1259 16460 TABLE tenant tb_customer postgres
218; 1259 16470 TABLE public flyway_schema_history postgres
219; 1259 16480 VIEW public v_orders postgres
220; 1259 16490 MATERIALIZED VIEW public mv_daily postgres
221; 1259 16495 TABLE public my TABLE DATA table postgres
4102; 0 16456 TABLE DATA tenant tb_order postgres
4103; 0 16460 TABLE DATA tenant tb_customer postgres
4104; 0 16470 TABLE DATA public flyway_schema_history postgres
4105; 0 16490 MATERIALIZED VIEW DATA public mv_daily postgres
"""


def _valid_with_toc(tmp_path: Path, toc: str):
    dump = tmp_path / "proddb_full.dump"
    dump.write_bytes(_PGDUMP_MAGIC + b"\x00" * 64)
    with patch("fraisier.dbops.archive.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=toc, stderr=""
        )
        return verify_archive(dump)


class TestTheArchiveStatesItsSchemaFloor:
    """The TOC is already in `result.stdout` and was being thrown away (#343).

    Counting it costs one pass over a string held in memory; no second
    `pg_restore` invocation is needed.
    """

    def test_table_data_is_counted_per_schema(self, tmp_path: Path):
        check = _valid_with_toc(tmp_path, _TOC)

        assert check.verdict is ArchiveVerdict.VALID
        assert check.table_data_by_schema == {"tenant": 2, "public": 1}

    def test_schemas_are_never_summed(self, tmp_path: Path):
        """A whole-TOC floor against a single schema's count false-fails.

        #356's own host keeps its heaps in `tenant`; a floor of 3 derived from
        the whole TOC and compared against `public` would fail a perfect
        restore.
        """
        check = _valid_with_toc(tmp_path, _TOC)

        assert sum(check.table_data_by_schema.values()) == 3
        assert check.table_data_by_schema["tenant"] == 2
        assert check.table_data_by_schema["public"] == 1

    def test_matview_data_is_not_table_data(self, tmp_path: Path):
        """pg_dump emits MATERIALIZED VIEW DATA for matviews, and it is not it."""
        check = _valid_with_toc(tmp_path, _TOC)

        assert check.table_data_by_schema.get("public") == 1

    def test_the_tag_is_read_positionally_not_by_substring(self, tmp_path: Path):
        """A table *named* `my TABLE DATA table` must not count as an entry.

        `"TABLE DATA" in line` counts it; reading the tag field positionally
        does not. Header and comment lines share the same stream.
        """
        one_liner = "221; 1259 16495 TABLE public my TABLE DATA table postgres\n"
        check = _valid_with_toc(tmp_path, one_liner)

        assert check.table_data_by_schema == {}

    def test_a_schema_only_dump_has_no_floor_to_state(self, tmp_path: Path):
        """`--schema-only` produces zero TABLE DATA entries — a VALID archive."""
        schema_only = (
            ";\n; Selected TOC Entries:\n;\n"
            "216; 1259 16456 TABLE tenant tb_order postgres\n"
        )
        check = _valid_with_toc(tmp_path, schema_only)

        assert check.verdict is ArchiveVerdict.VALID
        assert check.table_data_by_schema == {}

    def test_an_unverifiable_archive_states_no_counts(self, tmp_path: Path):
        """Absence of a tool is not a floor of zero."""
        check = verify_archive(tmp_path / "gone.dump")

        assert check.verdict is ArchiveVerdict.UNVERIFIABLE
        assert check.table_data_by_schema == {}

    def test_an_invalid_archive_states_no_counts(self, tmp_path: Path):
        dump = tmp_path / "truncated.dump"
        dump.write_bytes(_PGDUMP_MAGIC + b"\x00" * 8)
        with patch("fraisier.dbops.archive.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="unexpected end of file"
            )
            check = verify_archive(dump)

        assert check.verdict is ArchiveVerdict.INVALID
        assert check.table_data_by_schema == {}
