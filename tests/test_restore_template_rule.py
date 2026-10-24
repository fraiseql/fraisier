"""#481: take the rollback template only when a pending migration needs it.

Every payload here was **captured**, never composed, from confiture 1.39.0's
``migrate preflight --format json --config … --database-url … --scope pending``
against a throwaway database (``tests/fixtures/confiture/preflight-static-
pending-*.json``). That static, pending-scoped run is what step 8 reads.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from fraisier.config.schema import PreflightConfig
from fraisier.dbops.confiture import MigrationResult
from fraisier.dbops.restore import RestoreResult
from fraisier.dbops.template_rule import decide_template, read_pending_preflight
from fraisier.strategies import RestoreConfig, RestoreMigrateStrategy

FIXTURES = Path(__file__).parent / "fixtures" / "confiture"
CONFIG = Path("confiture.yaml")
MDIR = Path("db/migrations")
_ADMIN_URL = "postgresql://admin@localhost/postgres"


def _captured(case: str) -> dict:
    return json.loads((FIXTURES / f"preflight-static-pending-{case}.json").read_text())


class TestZeroPending:
    """Nothing pending means nothing for a template to undo."""

    def test_premise_the_capture_has_no_pending_migration(self):
        payload = _captured("zero-pending")

        assert payload["scope"] == "pending"
        assert payload["summary"]["migrations_checked"] == 0
        assert payload["ledger"]["exists"] is True

    def test_auto_takes_no_template(self):
        decision = decide_template("auto", _captured("zero-pending"))

        assert decision.take is False

    def test_the_reason_says_nothing_is_pending(self):
        decision = decide_template("auto", _captured("zero-pending"))

        assert "no pending migration" in decision.reason


class TestTheRule:
    """``auto``: take the template unless every pending migration can be undone.

    One captured payload per condition, each firing that condition alone, so
    breaking any one branch fails exactly the test named for it.
    """

    def test_a_reversible_transactional_sql_set_skips_the_template(self):
        decision = decide_template("auto", _captured("reversible"))

        assert decision.take is False
        assert decision.forced_by == ()

    def test_the_skip_says_why(self):
        decision = decide_template("auto", _captured("reversible"))

        assert "2 pending migration(s)" in decision.reason
        assert "migrate down" in decision.reason

    def test_a_migration_with_no_down_takes_the_template(self):
        decision = decide_template("auto", _captured("no-down"))

        assert decision.take is True
        assert decision.forced_by == ("002",)
        assert "002 has no down" in decision.reason

    def test_a_non_transactional_migration_takes_the_template(self):
        decision = decide_template("auto", _captured("non-transactional"))

        assert decision.take is True
        assert decision.forced_by == ("002",)
        assert "002 is not transactional" in decision.reason

    def test_a_python_migration_takes_the_template(self):
        """confiture assumes a ``.py`` reversible; its change has no tier."""
        decision = decide_template("auto", _captured("python"))

        assert decision.take is True
        assert decision.forced_by == ("002",)
        assert "002 is unclassified (python_migration)" in decision.reason

    def test_an_unparseable_migration_takes_the_template(self):
        decision = decide_template("auto", _captured("unparseable"))

        assert decision.take is True
        assert decision.forced_by == ("002",)

    def test_a_destructive_gate_takes_the_template(self):
        """The marker alone: the DDL under it classifies as additive."""
        decision = decide_template("auto", _captured("destructive-marker-additive"))

        assert decision.take is True
        assert decision.forced_by == ("002",)
        assert "002 is gated as destructive" in decision.reason

    def test_an_irreversible_change_takes_the_template(self):
        """An unmarked DROP COLUMN: its down re-adds the column, empty."""
        decision = decide_template("auto", _captured("drop-column-unmarked"))

        assert decision.take is True
        assert decision.forced_by == ("002",)
        assert "002 is irreversible (drop_column)" in decision.reason

    def test_a_destructive_change_takes_the_template(self):
        """An UPDATE under `-- confiture:irreversible`, which confiture drops."""
        decision = decide_template("auto", _captured("irreversible-marker"))

        assert decision.take is True
        assert "002 is destructive (update)" in decision.reason

    def test_an_already_applied_python_migration_does_not_count(self):
        decision = decide_template("auto", _captured("applied-python-pending-sql"))

        assert decision.take is False

    def test_a_payload_not_scoped_to_pending_takes_the_template(self):
        payload = json.loads((FIXTURES / "preflight-static-scope-all.json").read_text())

        decision = decide_template("auto", payload)

        assert decision.take is True
        assert "scope" in decision.reason

    def test_an_unreadable_payload_takes_the_template(self):
        decision = decide_template("auto", {})

        assert decision.take is True

    def test_no_payload_takes_the_template(self):
        decision = decide_template("auto", None)

        assert decision.take is True
        assert decision.reason == "no preflight result to decide on"

    @pytest.mark.parametrize("case", ["zero-pending", "reversible"])
    def test_always_takes_it(self, case):
        assert decide_template("always", _captured(case)).take is True

    @pytest.mark.parametrize("case", ["python", "no-down"])
    def test_never_does_not(self, case):
        assert decide_template("never", _captured(case)).take is False


def _proc(returncode: int, stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class TestReadPendingPreflight:
    """The one confiture call step 8 makes."""

    def test_it_reads_the_pending_set_of_the_named_database(self):
        stdout = (FIXTURES / "preflight-static-pending-reversible.json").read_text()
        with patch("subprocess.run", return_value=_proc(0, stdout)) as run:
            payload, error = read_pending_preflight(
                CONFIG,
                MDIR,
                database_url="postgresql:///staging_db",
                project_dir=Path("/srv/app"),
            )

        assert error is None
        assert payload == json.loads(stdout)
        argv = run.call_args.args[0]
        assert argv[1:3] == ["migrate", "preflight"]
        assert "--against" not in argv
        assert argv[argv.index("--scope") + 1] == "pending"
        assert argv[argv.index("--database-url") + 1] == "postgresql:///staging_db"
        assert argv[argv.index("--config") + 1] == str(CONFIG)
        assert argv[argv.index("--migrations-dir") + 1] == str(MDIR)
        assert run.call_args.kwargs["cwd"] == Path("/srv/app")

    def test_without_a_database_url_the_config_names_the_database(self):
        stdout = (FIXTURES / "preflight-static-pending-reversible.json").read_text()
        with patch("subprocess.run", return_value=_proc(0, stdout)) as run:
            read_pending_preflight(CONFIG, MDIR)

        assert "--database-url" not in run.call_args.args[0]

    def test_an_error_exit_with_a_report_is_still_read(self):
        """Exit 7 is a completed report with an error-severity finding."""
        stdout = (FIXTURES / "preflight-static-pending-no-down.json").read_text()
        with patch("subprocess.run", return_value=_proc(7, stdout)):
            payload, error = read_pending_preflight(CONFIG, MDIR)

        assert error is None
        assert payload is not None

    def test_output_that_is_not_json_is_an_error(self):
        with patch("subprocess.run", return_value=_proc(3, "connection refused")):
            payload, error = read_pending_preflight(CONFIG, MDIR)

        assert payload is None
        assert error == "confiture preflight exited 3 without JSON"

    @pytest.mark.parametrize(
        "exc", [OSError("no confiture"), subprocess.TimeoutExpired("confiture", 120)]
    )
    def test_a_preflight_that_cannot_run_is_an_error(self, exc):
        with patch("subprocess.run", side_effect=exc):
            payload, error = read_pending_preflight(CONFIG, MDIR)

        assert payload is None
        assert error is not None
        assert error.startswith("confiture preflight did not run")


def _strategy(**overrides) -> RestoreMigrateStrategy:
    config = RestoreConfig(
        db_name="staging_db",
        backup_dir=Path("/backup"),
        # The archive replay (step 2.5) is not what step 8 reads.
        preflight=PreflightConfig(enabled=False),
        **overrides,
    )
    return RestoreMigrateStrategy(config, admin_url=_ADMIN_URL)


@patch("fraisier.strategies._restore.migrate_up")
@patch("fraisier.dbops.restore.restore_backup")
@patch("fraisier.dbops.operations.create_db", return_value=(0, "", ""))
@patch("fraisier.dbops.operations.drop_db", return_value=(0, "", ""))
@patch("fraisier.dbops.operations.terminate_backends")
@patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
@patch("fraisier.dbops.restore.find_latest_backup")
class TestStep8:
    """What step 8 does with the decision."""

    @pytest.fixture(autouse=True)
    def _payload(self):
        self.payload: dict | None = _captured("zero-pending")
        with patch(
            "fraisier.strategies._restore.read_pending_preflight",
            side_effect=lambda *_a, **_kw: (self.payload, None),
        ) as reader:
            self.reader = reader
            yield

    @staticmethod
    def _arrange(mock_find, mock_restore, mock_up):
        mock_find.return_value = Path("/backup/db.dump")
        mock_restore.return_value = RestoreResult(success=True)
        mock_up.return_value = MigrationResult(success=True, steps_applied=0)

    @staticmethod
    def _template_copies(mock_create) -> list:
        return [
            c
            for c in mock_create.call_args_list
            if c.args and c.args[0] == "template_staging_db"
        ]

    def test_auto_with_nothing_pending_copies_no_template(
        self, mock_find, _age, _term, _drop, mock_create, mock_restore, mock_up
    ):
        self._arrange(mock_find, mock_restore, mock_up)

        _strategy(create_template=True).execute(CONFIG, migrations_dir=MDIR)

        assert self._template_copies(mock_create) == []

    def test_the_result_carries_the_decision(
        self, mock_find, _age, _term, _drop, _create, mock_restore, mock_up
    ):
        self._arrange(mock_find, mock_restore, mock_up)

        result = _strategy(create_template=True).execute(CONFIG, migrations_dir=MDIR)

        assert result.template is not None
        assert result.template.take is False
