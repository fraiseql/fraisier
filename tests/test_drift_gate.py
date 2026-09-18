"""The post-migration schema-drift gate primitive (#395).

``fraisier.dbops.drift`` answers one question: does the live database match the
schema *this checkout* builds?  It is two confiture invocations — a schema build
that needs no database, then ``migrate validate --check-live-drift`` against it —
and the whole value of the gate is that its verdict can be trusted, so the
awkward parts of that contract are pinned here by name:

* ``confiture build`` takes ``--project-dir`` and ``--env``; ``migrate validate``
  takes neither, so its ``-c`` must be **absolute** or it resolves against a cwd
  the deploy worker does not control.
* exit 1 is **overloaded** — critical drift *and* "schema file not found" — so
  the built file's existence is proven before the verdict is read.
* the ``--format json`` payload **changes shape with the number of checks**: one
  check emits the bare report, two wrap it in a ``{"checks": {...}}`` envelope.
  A reader that knows only the bare shape sees ``has_critical_drift`` absent and
  calls a drifting database clean.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.dbops.drift import CHECK_FLAGS, DriftResult, check_schema_drift

CLEAN_BARE = {
    "check": "live_drift",
    "database_name": "app",
    "has_drift": False,
    "has_critical_drift": False,
    "critical_count": 0,
    "warning_count": 0,
    "drift_items": [],
}

DRIFT_BARE = {
    "check": "live_drift",
    "database_name": "app",
    "has_drift": True,
    "has_critical_drift": True,
    "critical_count": 1,
    "warning_count": 0,
    "drift_items": [
        {
            "type": "missing_column",
            "severity": "critical",
            "object": "core.tb_widget.label",
            "message": "Column 'core.tb_widget.label' is missing",
        }
    ],
}

#: A clean ``confiture build --format json`` envelope, copied from a 1.6.0 run
#: with the gate's own flags.  The schema goes to ``--output``, this to stdout,
#: and the progress lines to stderr.
BUILD_CLEAN = {
    "success": True,
    "files_processed": 3,
    "schema_size_bytes": 1008,
    "output_path": "/tmp/fraisier-drift-x/expected_schema.sql",
    "hash": None,
    "execution_time_ms": 0,
    "seed_files_applied": 0,
    "artifact_path": None,
    "artifact_hash": None,
    "seed_profile": None,
    "warnings": [],
    "error": None,
    "duplicates": [],
}

#: What confiture 1.6.0 puts on **stderr** under ``--format json``.  It is the
#: reason a failed build cannot be explained by reading stderr first.
BUILD_PROGRESS = "\U0001f528 Building schema for environment: production\n"

#: A build warning that costs the build nothing: exit 0, schema written, and one
#: file silently not checked.
SCHEMA_206 = {
    "code": "SCHEMA_206",
    "severity": "warning",
    "message": (
        "db/schema/003_unparseable.sql: pglast could not parse it "
        "\u2014 not checked for duplicates"
    ),
    "file": "db/schema/003_unparseable.sql",
}

#: An object defined in two of the build's files.  A separate envelope key from
#: ``warnings``, and the same class of fact about the built schema.
BUILD_001 = {
    "rule_id": "build_001",
    "kind": "table",
    "identity": "core.tb_widget",
    "definitions": [
        {"file": "db/schema/001_core.sql", "offset": 34, "line": 2},
        {"file": "db/schema/002_dup.sql", "offset": 0, "line": 1},
    ],
    "wins": "conflict",
}

#: A failed build's envelope — a different shape from ``BUILD_CLEAN``, and the
#: only place the failure is named.  Copied from a 1.6.0 run.
BUILD_FAILED = {
    "ok": False,
    "parser": {"pglast": "8.4", "pg_major": 18},
    "error": {
        "code": "SCHEMA_001",
        "message": (
            "Error writing schema to /proc/nope/expected.sql: "
            "[Errno 2] No such file or directory: '/proc/nope'"
        ),
        "severity": "error",
        "details": {},
        "migration": None,
        "file": None,
        "line": None,
        "actionable": (
            "Check that the output directory exists and you have write permissions"
        ),
    },
}

#: A clean ``--check-signatures`` report, copied from a 1.10.1 run.  Note what
#: is **not** here: ``drift_items``.  That key belongs to the live-drift report;
#: this check says what it found in ``stale_overloads``, so a reader that knows
#: only the other shape reports a failing gate with nothing in it.
SIGNATURES_CLEAN = {
    "check": "function_signature_drift",
    "has_drift": False,
    "has_critical_drift": False,
    "remediation_sql": [],
    "stale_overloads": [],
    "missing_from_db": ["core.fn_seen(timestamp with time zone)"],
    "schemas_checked": ["core", "public"],
    "functions_checked": 1,
    "detection_time_ms": 0.017,
}

#: The same report against a database carrying the second overload a
#: ``CREATE OR REPLACE`` with a changed parameter type silently leaves behind.
SIGNATURES_STALE = {
    **SIGNATURES_CLEAN,
    "has_drift": True,
    "has_critical_drift": True,
    "remediation_sql": ["DROP FUNCTION core.fn_seen(timestamp without time zone);"],
    "stale_overloads": [
        {
            "schema": "core",
            "name": "fn_seen",
            "stale_signature": "core.fn_seen(timestamp without time zone)",
            "source_signatures": ["core.fn_seen(timestamp with time zone)"],
            "drop_sql": "DROP FUNCTION core.fn_seen(timestamp without time zone);",
        }
    ],
}

DRIFT_ENVELOPE = {
    "version": "1",
    "status": "failed",
    "checks": {
        "live_drift": DRIFT_BARE,
        "function_signature_drift": SIGNATURES_CLEAN,
    },
}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A checkout whose confiture config lives where ``--env`` expects it."""
    config = tmp_path / "db" / "environments" / "production.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("name: production\ndatabase_url: postgresql:///app\n")
    return tmp_path


#: "no build payload given", distinct from "the build printed nothing".
_UNSET = "\u0000unset"


def _runs(mock_run: MagicMock) -> list[list[str]]:
    return [call.args[0] for call in mock_run.call_args_list]


def _fake_confiture(
    payload: dict | None,
    *,
    build_rc: int = 0,
    validate_rc: int = 0,
    build_payload: dict | str | None = _UNSET,
    build_stderr: str = BUILD_PROGRESS,
    built_schema: str = "CREATE TABLE part",
):
    """A ``subprocess.run`` double that also writes the built schema file.

    The file is written **even when the build fails**, because that is what a
    build failing part-way through actually leaves behind. A double that wrote
    nothing on failure would let the "no schema was written" guard absorb the
    failed-build case, and the exit-code check could be deleted unnoticed.

    The build's own streams are modelled the way confiture 1.6.0 fills them
    under ``--format json``: the envelope on stdout, the progress lines on
    stderr. Passing ``build_payload`` as a ``str`` puts that string on stdout
    verbatim, for the case where it is not an envelope at all.

    *built_schema* is what lands at ``--output``.  The default is deliberately
    not valid SQL: nothing here parses it, and a plausible-looking schema would
    invite a reader to believe it was checked.  The cases that need the built
    text to *mean* something — the schemas the signatures check is pointed at —
    pass their own.
    """
    if build_payload is _UNSET:
        build_payload = BUILD_CLEAN if build_rc == 0 else BUILD_FAILED

    def _build_stdout() -> str:
        if isinstance(build_payload, str):
            return build_payload
        return json.dumps(build_payload) if build_payload is not None else ""

    def run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "build":
            Path(cmd[cmd.index("--output") + 1]).write_text(built_schema)
            return MagicMock(
                returncode=build_rc, stdout=_build_stdout(), stderr=build_stderr
            )
        return MagicMock(
            returncode=validate_rc,
            stdout=json.dumps(payload) if payload is not None else "",
            stderr="",
        )

    return run


class TestBuildInvocation:
    def test_builds_the_expected_schema_without_a_database(self, project: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _fake_confiture(CLEAN_BARE)
            check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["live-drift"],
            )

        build = _runs(mock_run)[0]
        assert build[:2] == ["confiture", "build"]
        assert "--schema-only" in build
        assert build[build.index("--project-dir") + 1] == str(project)
        assert build[build.index("--env") + 1] == "production"
        # No --database-url: the expected schema comes from the DDL alone.
        assert "--database-url" not in build

    def test_the_built_schema_never_lands_in_the_checkout(self, project: Path) -> None:
        """``db/generated/`` is a gitignored build artefact, absent on a host."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _fake_confiture(CLEAN_BARE)
            check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["live-drift"],
            )

        output = Path(_runs(mock_run)[0][_runs(mock_run)[0].index("--output") + 1])
        assert project not in output.parents
        assert not list(project.rglob("*.sql"))


class TestValidateInvocation:
    def test_config_is_absolute_and_schema_is_the_built_file(
        self, project: Path
    ) -> None:
        """``migrate validate`` has no ``--project-dir``; a relative -c is a bug."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _fake_confiture(CLEAN_BARE)
            check_schema_drift(
                project_dir=project,
                confiture_config=Path("db/environments/production.yaml"),
                checks=["live-drift"],
            )

        build, validate = _runs(mock_run)
        assert validate[:3] == ["confiture", "migrate", "validate"]
        config = Path(validate[validate.index("-c") + 1])
        assert config.is_absolute()
        assert config == project / "db/environments/production.yaml"
        assert (
            validate[validate.index("--schema") + 1]
            == (build[build.index("--output") + 1])
        )
        assert validate[validate.index("--format") + 1] == "json"

    def test_each_configured_check_becomes_its_confiture_flag(
        self, project: Path
    ) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _fake_confiture(DRIFT_ENVELOPE, validate_rc=1)
            check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["live-drift", "signatures"],
            )

        validate = _runs(mock_run)[1]
        assert CHECK_FLAGS["live-drift"] in validate
        assert CHECK_FLAGS["signatures"] in validate
        # --check-body-replay is the heaviest signal and belongs on a timer.
        assert "--check-body-replay" not in validate


#: A built schema whose routines live where a FraiseQL project puts them, which
#: is to say nowhere near ``public`` — the only schema confiture inspects unless
#: it is told otherwise.  The quoted ``"Tenant"`` is here because confiture
#: lowercases and unquotes a routine's schema when it parses the source side; a
#: derivation that did not would name a schema the live side never matches.
SCHEMA_WITH_ROUTINES = """\
CREATE SCHEMA core;

CREATE TABLE core.tb_widget (id BIGINT PRIMARY KEY, serial TEXT NOT NULL);

CREATE OR REPLACE FUNCTION core.fn_seen(p_at TIMESTAMPTZ)
    RETURNS INT LANGUAGE sql AS $$ SELECT 1 $$;

CREATE FUNCTION app.fn_widget(p_id BIGINT)
    RETURNS INT LANGUAGE sql AS $$ SELECT 1 $$;

CREATE PROCEDURE "Tenant".pr_seed()
    LANGUAGE sql AS $$ SELECT 1 $$;
"""


class TestWhichSchemasTheSignaturesCheckScans:
    """#408: ``--schemas`` defaults to ``public``, and our routines are not there.

    confiture reports a stale overload only for a ``(schema, name)`` the
    **source** declares, and it reads that source from the very file this gate
    builds and hands it as ``--schema``.  So the schemas worth scanning are
    exactly the ones that file declares routines in — derived from it rather
    than configured, so they cannot drift from the tree.

    Every case here asserts the argv.  That is not enough on its own, which is
    the whole reason this defect survived: ``tests/integration/
    test_post_migrate_check_integration.py`` plants a real stale overload in
    ``core`` and requires a real confiture to fail the gate on it.
    """

    def _validate(self, project: Path, checks: list[str], schema: str) -> list[str]:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _fake_confiture(CLEAN_BARE, built_schema=schema)
            check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=checks,
            )
        return _runs(mock_run)[1]

    def test_every_schema_the_built_schema_declares_a_routine_in(
        self, project: Path
    ) -> None:
        validate = self._validate(project, ["signatures"], SCHEMA_WITH_ROUTINES)

        assert validate[validate.index("--schemas") + 1] == "app,core,public,tenant"

    def test_public_is_scanned_even_when_nothing_declares_a_routine_there(
        self, project: Path
    ) -> None:
        """The derivation may only ever widen what the gate looked at before.

        ``public`` is confiture's default, so dropping it for a project whose
        routines are all elsewhere would trade one blind spot for another — and
        it is also where an unqualified ``CREATE FUNCTION`` lands, since that is
        the schema confiture's own parser assigns one.
        """
        schema = "CREATE FUNCTION core.fn_only(p INT) RETURNS INT AS $$ SELECT 1 $$;"

        validate = self._validate(project, ["signatures"], schema)

        assert validate[validate.index("--schemas") + 1] == "core,public"

    def test_a_delimited_schema_name_survives_the_derivation(
        self, project: Path
    ) -> None:
        """A quoted identifier may hold anything, and a space is the common case.

        Matching the name with ``\\w+`` stops at that space and derives no schema
        at all — the one failure direction this must not have, since it puts the
        gate straight back to inspecting ``public`` alone.
        """
        schema = (
            'CREATE FUNCTION "billing archive".fn_sweep() '
            "RETURNS INT AS $$ SELECT 1 $$;"
        )

        validate = self._validate(project, ["signatures"], schema)

        assert validate[validate.index("--schemas") + 1] == "billing archive,public"

    def test_a_schema_with_no_routines_is_not_scanned(self, project: Path) -> None:
        """Tables alone buy nothing here: the check compares routine signatures."""
        schema = "CREATE SCHEMA audit;\nCREATE TABLE audit.tb_log (id BIGINT);\n"

        validate = self._validate(project, ["signatures"], schema)

        assert validate[validate.index("--schemas") + 1] == "public"

    def test_no_schemas_flag_when_signatures_was_not_asked_for(
        self, project: Path
    ) -> None:
        """``--schemas`` is documented as "used with --check-signatures"."""
        validate = self._validate(project, ["live-drift"], SCHEMA_WITH_ROUTINES)

        assert "--schemas" not in validate


class TestWhatAFiringSignaturesCheckSays:
    """The other half of #408: it had never fired, so it had never reported.

    ``--check-signatures`` puts its findings in ``stale_overloads``, not in the
    ``drift_items`` the live-drift report uses.  Reading only the latter, the
    gate would fail the deploy with ``0 critical schema drift item(s) …`` and
    name nothing — while confiture had the signature *and* the ``DROP FUNCTION``
    that fixes it sitting in the payload.
    """

    def _result(self, project: Path, payload: dict, rc: int = 0) -> DriftResult:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _fake_confiture(payload, validate_rc=rc)
            return check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["signatures"],
            )

    def test_a_stale_overload_fails_the_gate_and_is_named(self, project: Path) -> None:
        result = self._result(project, SIGNATURES_STALE, rc=1)

        assert not result.passed
        assert [item.object_name for item in result.critical] == [
            "core.fn_seen(timestamp without time zone)"
        ]
        assert result.critical[0].kind == "stale_overload"
        assert result.critical[0].severity == "critical"

    def test_the_remediation_reaches_the_operator(self, project: Path) -> None:
        """The whole point: the deploy log says what to run, not just what broke."""
        result = self._result(project, SIGNATURES_STALE, rc=1)

        assert (
            "DROP FUNCTION core.fn_seen(timestamp without time zone);"
            in result.summary()
        )

    def test_the_signature_the_source_does_declare_is_named_too(
        self, project: Path
    ) -> None:
        """Without it the report is one signature short of a diagnosis."""
        result = self._result(project, SIGNATURES_STALE, rc=1)

        assert "core.fn_seen(timestamp with time zone)" in result.critical[0].message

    def test_a_clean_signatures_report_contributes_nothing(self, project: Path) -> None:
        result = self._result(project, SIGNATURES_CLEAN)

        assert result.passed
        assert result.critical == ()
        assert result.warnings == ()

    def test_missing_from_db_is_not_read_as_drift(self, project: Path) -> None:
        """confiture documents it as informational and does not set ``has_drift``.

        It is also the thing this gate would most like to fail on — a routine the
        DDL declares that the migration never created — so the temptation to
        promote it here is real.  Doing so would fail every deploy whose DDL is
        ahead of its database by one function, which is not a verdict we can
        stand behind from this side (fraiseql/confiture#303).
        """
        result = self._result(project, SIGNATURES_CLEAN)

        assert result.passed
        assert "core.fn_seen" not in result.summary()


class TestVerdicts:
    def test_clean_schema_passes(self, project: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _fake_confiture(CLEAN_BARE)
            result = check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["live-drift"],
            )

        assert result.passed
        assert result.ran
        assert result.critical == ()
        assert result.error is None

    def test_critical_drift_fails_and_names_the_object(self, project: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _fake_confiture(DRIFT_BARE, validate_rc=1)
            result = check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["live-drift"],
            )

        assert not result.passed
        assert result.ran  # a verdict, not a failure to reach one
        assert [item.object_name for item in result.critical] == [
            "core.tb_widget.label"
        ]
        assert "core.tb_widget.label" in result.summary()

    def test_warnings_alone_do_not_fail(self, project: Path) -> None:
        """confiture's own rule: ``passed = not has_critical_drift``.

        ``confiture_lock_holder`` is not in its ``SYSTEM_TABLES``, so every
        fraisier-managed database reports it as an EXTRA_TABLE warning. A gate
        that failed on warnings would fail on every deploy.
        """
        payload = {
            **CLEAN_BARE,
            "has_drift": True,
            "warning_count": 1,
            "drift_items": [
                {
                    "type": "extra_table",
                    "severity": "warning",
                    "object": "public.confiture_lock_holder",
                    "message": "Table not in schema file",
                }
            ],
        }
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _fake_confiture(payload)
            result = check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["live-drift"],
            )

        assert result.passed
        assert len(result.warnings) == 1
        assert "1 warning" in result.summary()

    def test_the_envelope_shape_is_read_not_defaulted_away(self, project: Path) -> None:
        """Two checks wrap the reports; ``has_critical_drift`` moves down a level.

        Measured against confiture 1.0.0: with ``--check-live-drift
        --check-signatures`` the top level carries only
        ``{version, status, checks, hints, parser}``. A reader doing
        ``payload.get("has_critical_drift", False)`` calls this database clean
        while confiture itself exits 1.
        """
        assert "has_critical_drift" not in DRIFT_ENVELOPE

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _fake_confiture(DRIFT_ENVELOPE, validate_rc=1)
            result = check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["live-drift", "signatures"],
            )

        assert not result.passed
        assert [item.object_name for item in result.critical] == [
            "core.tb_widget.label"
        ]


class TestCannotReachAVerdict:
    """Every one of these must be distinguishable from "the schema is clean"."""

    def test_a_partial_build_is_never_validated_against(self, project: Path) -> None:
        """A build that failed part-way leaves a truncated schema on disk.

        Validating against it would compare the live database with half its own
        DDL — every table below the failure point reported missing. The build's
        exit code is the only thing that distinguishes it from a complete file.
        """
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _fake_confiture(None, build_rc=5)
            result = check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["live-drift"],
            )

        assert not result.passed
        assert not result.ran
        assert "build" in str(result.error)
        # validate must never have been reached with no schema to compare
        assert len(_runs(mock_run)) == 1

    def test_a_build_that_writes_nothing_is_not_a_clean_schema(
        self, project: Path
    ) -> None:
        """Guards the overloaded exit 1: no file would read as critical drift."""

        def run(cmd: list[str], **_kwargs: object) -> MagicMock:
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = run
            result = check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["live-drift"],
            )

        assert not result.passed
        assert not result.ran
        assert "wrote no schema" in str(result.error)
        assert len(_runs(mock_run)) == 1

    def test_non_json_output_is_not_a_clean_schema(self, project: Path) -> None:
        """Exit 3 (connection failure) prints prose, not a report."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _fake_confiture(None, validate_rc=3)
            result = check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["live-drift"],
            )

        assert not result.passed
        assert not result.ran
        assert result.exit_code == 3

    def test_an_unknown_json_shape_is_not_a_clean_schema(self, project: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _fake_confiture({"something": "else"})
            result = check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["live-drift"],
            )

        assert not result.passed
        assert not result.ran
        assert "no known shape" in str(result.error)

    def test_an_unknown_check_name_never_silently_runs_fewer_checks(
        self, project: Path
    ) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _fake_confiture(CLEAN_BARE)
            result = check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["live-drift", "body-replay"],
            )

        assert not result.passed
        assert not result.ran
        assert mock_run.call_count == 0


class TestGuardsAgainstCheckingSomethingElse:
    def test_refuses_a_config_confiture_build_cannot_resolve(
        self, tmp_path: Path
    ) -> None:
        """A root ``confiture.yaml`` has no ``--env`` name to derive."""
        config = tmp_path / "confiture.yaml"
        config.write_text("name: production\n")

        with patch("subprocess.run") as mock_run:
            result = check_schema_drift(
                project_dir=tmp_path,
                confiture_config=config,
                checks=["live-drift"],
            )

        assert not result.ran
        assert "db/environments" in str(result.error)
        assert mock_run.call_count == 0

    def test_refuses_when_the_deploy_migrated_a_different_database(
        self, project: Path
    ) -> None:
        """``migrate validate`` has no --database-url; it uses the config's."""
        with patch("subprocess.run") as mock_run:
            result = check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["live-drift"],
                database_url="postgresql:///some_other_db",
            )

        assert not result.ran
        assert "different database" in str(result.error)
        assert mock_run.call_count == 0

    def test_accepts_a_cosmetically_different_url_for_the_same_database(
        self, project: Path
    ) -> None:
        """The deploy adds ``statement_timeout`` to the URL it migrates with."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _fake_confiture(CLEAN_BARE)
            result = check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["live-drift"],
                database_url="postgresql:///app?options=-c%20statement_timeout%3D60s",
            )

        assert result.passed


def test_the_temporary_schema_file_never_outlives_the_check(
    project: Path,
) -> None:
    """On every path — clean, drifted, or failed."""
    seen: list[Path] = []

    def run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "build":
            out = Path(cmd[cmd.index("--output") + 1])
            out.write_text("CREATE TABLE t ();")
            seen.append(out)
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=1, stdout=json.dumps(DRIFT_BARE), stderr="")

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = run
        result = check_schema_drift(
            project_dir=project,
            confiture_config=project / "db/environments/production.yaml",
            checks=["live-drift"],
        )

    assert not result.passed
    assert seen and not any(path.exists() for path in seen)
    assert not any(path.parent.exists() for path in seen)


class TestTheBuildsOwnDiagnostics:
    """What the build said about the schema the verdict is measured against (#401).

    The gate used to keep ``build.stdout`` only inside its ``returncode != 0``
    branch, so a build that warned and exited 0 dropped the one line explaining
    the drift about to be reported.  Measured under confiture 1.5.0: an
    ``exclude`` pattern that had stopped matching printed ``CONFIG_013`` and
    exited 0, the built schema grew, and the gate reported an extra object with
    no mention of why.
    """

    def _gate(self, project: Path, run) -> DriftResult:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = run
            return check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["live-drift"],
            )

    def test_a_warning_on_a_zero_exit_build_reaches_the_verdict(
        self, project: Path
    ) -> None:
        """The whole of #401: exit 0, schema written, and something to say."""
        result = self._gate(
            project,
            _fake_confiture(
                CLEAN_BARE, build_payload={**BUILD_CLEAN, "warnings": [SCHEMA_206]}
            ),
        )

        assert result.passed  # a build warning is not drift
        assert [note.code for note in result.build_notes] == ["SCHEMA_206"]
        assert result.build_notes[0].file == "db/schema/003_unparseable.sql"
        assert result.build_notes[0].severity == "warning"
        assert "SCHEMA_206" in result.summary()
        assert "pglast could not parse it" in result.summary()

    def test_a_warning_is_reported_next_to_the_drift_it_explains(
        self, project: Path
    ) -> None:
        """The reported case: the operator sees the cause, not only the effect."""
        result = self._gate(
            project,
            _fake_confiture(
                DRIFT_BARE,
                validate_rc=1,
                build_payload={**BUILD_CLEAN, "warnings": [SCHEMA_206]},
            ),
        )

        assert not result.passed
        summary = result.summary()
        assert "core.tb_widget.label" in summary  # the effect
        assert "SCHEMA_206" in summary  # the cause

    def test_a_clean_build_says_nothing(self, project: Path) -> None:
        result = self._gate(project, _fake_confiture(CLEAN_BARE))

        assert result.build_notes == ()
        assert "SCHEMA_" not in result.summary()

    def test_the_build_asks_for_duplicate_detection(self, project: Path) -> None:
        """Without ``--warn-duplicates`` the channel is provably silent.

        Measured against confiture 1.6.0 over a tree with a duplicated table and
        a file pglast cannot parse: the gate's own flags produce
        ``warnings: []`` and ``duplicates: []``.  ``SCHEMA_206`` needs this
        flag; ``SEED_002``/``SEED_003`` need ``--sequential``, which
        ``--schema-only`` excludes.  Reading a channel nothing fills is a no-op.
        """
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _fake_confiture(CLEAN_BARE)
            check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["live-drift"],
            )

        build = _runs(mock_run)[0]
        assert "--warn-duplicates" in build
        assert build[build.index("--format") + 1] == "json"
        # ...and never the gate that refuses to build, which would turn a
        # reportable fact into an unreachable verdict.
        assert "--fail-on-duplicates" not in build

    def test_a_duplicate_definition_reaches_the_verdict(self, project: Path) -> None:
        """``duplicates[]`` is its own envelope key, and the same class of fact."""
        result = self._gate(
            project,
            _fake_confiture(
                CLEAN_BARE, build_payload={**BUILD_CLEAN, "duplicates": [BUILD_001]}
            ),
        )

        assert [note.code for note in result.build_notes] == ["build_001"]
        note = result.build_notes[0]
        assert "core.tb_widget" in note.message
        # both places, so the reader can go and look at them
        assert "db/schema/001_core.sql" in note.message
        assert "db/schema/002_dup.sql" in note.message
        # confiture's own word for what a later plain CREATE does, not ours
        assert "conflict" in note.message

    def test_a_failed_build_is_explained_by_the_envelope_not_the_progress_line(
        self, project: Path
    ) -> None:
        """``--format json`` moves the explanation from stderr to stdout.

        Measured: a build that fails after it has started prints
        ``🔨 Building schema for environment: production`` to stderr and the
        real error to stdout.  Reading ``(stderr or stdout)`` — which is what
        the gate did — hands the operator the progress line.
        """
        result = self._gate(project, _fake_confiture(None, build_rc=4))

        assert not result.ran
        assert "SCHEMA_001" in str(result.error)
        assert "No such file or directory" in str(result.error)
        # the actionable hint confiture wrote for exactly this moment
        assert "output directory exists" in str(result.error)
        assert "Building schema for environment" not in str(result.error)

    def test_a_failed_build_that_is_not_an_envelope_still_says_what_it_can(
        self, project: Path
    ) -> None:
        """confiture is not the only thing that can fail here — a missing binary,
        an OOM kill, a wrapper script.  None of those emit an envelope."""
        result = self._gate(
            project,
            _fake_confiture(
                None,
                build_rc=127,
                build_payload="",
                build_stderr="confiture: command not found",
            ),
        )

        assert not result.ran
        assert "command not found" in str(result.error)

    def test_an_unreadable_build_envelope_is_reported_not_swallowed(
        self, project: Path
    ) -> None:
        """Exit 0 with a complete schema: the verdict stands, the gap is named.

        The contrast with ``migrate validate`` is deliberate and is pinned by
        ``test_non_json_output_is_not_a_clean_schema``: there the payload *is*
        the verdict, so an unreadable one is fatal.  Here it is commentary on a
        schema whose completeness the exit code already vouched for, and failing
        the gate would cost a deploy for nothing.  Silently dropping it is what
        #401 is about, so it becomes a note instead.
        """
        result = self._gate(
            project,
            _fake_confiture(
                CLEAN_BARE, build_payload="Schema built successfully! 3 files"
            ),
        )

        assert result.ran
        assert result.passed  # the verdict was still reached
        assert len(result.build_notes) == 1
        assert "envelope" in result.build_notes[0].message
        assert "Schema built successfully" in result.build_notes[0].message

    def test_an_older_confiture_with_no_warnings_key_is_not_an_error(
        self, project: Path
    ) -> None:
        """Why #401 needs no floor bump: the key is read, never required."""
        older = {k: v for k, v in BUILD_CLEAN.items() if k != "warnings"}
        assert "warnings" not in older

        result = self._gate(project, _fake_confiture(CLEAN_BARE, build_payload=older))

        assert result.ran
        assert result.passed
        assert result.build_notes == ()
