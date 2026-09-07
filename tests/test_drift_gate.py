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

from fraisier.dbops.drift import CHECK_FLAGS, check_schema_drift

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

DRIFT_ENVELOPE = {
    "version": "1",
    "status": "failed",
    "checks": {
        "live_drift": DRIFT_BARE,
        "function_signature_drift": {
            "check": "function_signature_drift",
            "has_drift": False,
            "has_critical_drift": False,
            "drift_items": [],
        },
    },
}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A checkout whose confiture config lives where ``--env`` expects it."""
    config = tmp_path / "db" / "environments" / "production.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("name: production\ndatabase_url: postgresql:///app\n")
    return tmp_path


def _runs(mock_run: MagicMock) -> list[list[str]]:
    return [call.args[0] for call in mock_run.call_args_list]


def _fake_confiture(payload: dict | None, *, build_rc: int = 0, validate_rc: int = 0):
    """A ``subprocess.run`` double that also writes the built schema file.

    The file is written **even when the build fails**, because that is what a
    build failing part-way through actually leaves behind. A double that wrote
    nothing on failure would let the "no schema was written" guard absorb the
    failed-build case, and the exit-code check could be deleted unnoticed.
    """

    def run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "build":
            Path(cmd[cmd.index("--output") + 1]).write_text("CREATE TABLE part")
            return MagicMock(returncode=build_rc, stdout="", stderr="build failed")
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
