"""``fraisier db receipt`` — asking the database when it was last rewritten (#358).

The restore's own read-back runs inside the process that did the writing. It
proves the write landed; it cannot prove a run happened, because when the run
does not happen that code does not execute either. What catches the failure #343
reported is the *durable* row: an independent caller, the next morning, asking
staging itself.

So the exit codes carry the three answers apart. A nightly that could not check
must not exit the same way as a nightly that checked and passed — that is the
``min_tables=0`` silent hole reappearing somewhere new.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fraisier.dbops.receipt import ActuationCheck, ActuationVerdict, RestoreReceipt

_ADMIN_URL = "postgresql://postgres@localhost:5432/postgres"


def _receipt(run_id="run-7", age_hours=1.5):
    from datetime import UTC, datetime

    return RestoreReceipt(
        run_id=run_id,
        backup_path="/backup/production/latest.dump",
        backup_bytes=987654,
        restored_at=datetime(2026, 8, 10, 3, 0, tzinfo=UTC),
        age_seconds=age_hours * 3600,
    )


def _config(*, max_age_hours=48.0, external=False):
    config = MagicMock()
    config.get_fraise.return_value = {"type": "api", "external_db": external}
    config.get_fraise_environment.return_value = {
        "type": "api",
        "app_path": "/var/www/api",
        "database": {
            "name": "mydb_staging",
            "admin_url": _ADMIN_URL,
            "restore": {
                "backup_dir": "/backup/production",
                "max_age_hours": max_age_hours,
            },
        },
    }
    config._config = {"backup": {}}
    config.list_fraises_detailed.return_value = []
    return config


def _invoke(check, *args, config=None, external=False):
    from fraisier.cli.main import main

    with (
        patch(
            "fraisier.cli.main.get_config",
            return_value=config or _config(external=external),
        ),
        patch("fraisier.dbops.guard.is_external_db", return_value=external),
        patch("fraisier.dbops.receipt.verify_actuation", return_value=check) as verify,
    ):
        result = CliRunner().invoke(main, ["db", "receipt", "api", "staging", *args])
    return result, verify


class TestTheThreeAnswersHaveThreeExits:
    def test_a_fresh_receipt_exits_zero(self):
        check = ActuationCheck(ActuationVerdict.ACTUATED, "ran 1.5h ago", _receipt())
        result, _ = _invoke(check)

        assert result.exit_code == 0, result.output

    def test_a_stale_receipt_exits_one(self):
        """The #343 signature: staging still holds an old run's receipt."""
        check = ActuationCheck(
            ActuationVerdict.STALE, "last rewritten 26.0h ago", _receipt(age_hours=26)
        )
        result, _ = _invoke(check)

        assert result.exit_code == 1

    @pytest.mark.parametrize(
        "verdict", [ActuationVerdict.MISSING, ActuationVerdict.UNVERIFIABLE]
    )
    def test_not_checked_exits_three_not_zero(self, verdict):
        """Distinct from success on purpose.

        Exiting 0 here would let a host that cannot check report the same thing
        as a host that checked and passed — which is the hole this whole
        mechanism exists to close, moved into the monitoring layer.
        """
        result, _ = _invoke(ActuationCheck(verdict, "no receipt"))

        assert result.exit_code == 3

    def test_the_exit_codes_are_documented_in_help(self):
        from fraisier.cli.main import main

        result = CliRunner().invoke(main, ["db", "receipt", "--help"])

        assert result.exit_code == 0
        for code in ("0", "1", "3"):
            assert code in result.output


class TestTheWindow:
    def test_the_restores_own_max_age_is_the_default(self):
        """How stale an input may be is reused as how stale an output may be."""
        check = ActuationCheck(ActuationVerdict.ACTUATED, "ok", _receipt())
        _, verify = _invoke(check, config=_config(max_age_hours=12.0))

        assert verify.call_args.kwargs["max_age_hours"] == 12.0

    def test_the_flag_overrides_it(self):
        check = ActuationCheck(ActuationVerdict.ACTUATED, "ok", _receipt())
        _, verify = _invoke(check, "--max-age-hours", "3")

        assert verify.call_args.kwargs["max_age_hours"] == 3.0

    def test_it_asks_about_the_configured_database(self):
        check = ActuationCheck(ActuationVerdict.ACTUATED, "ok", _receipt())
        _, verify = _invoke(check)

        assert verify.call_args.args[0] == "mydb_staging"
        assert verify.call_args.kwargs["connection_url"] == _ADMIN_URL


class TestWhatItPrints:
    def test_the_run_and_the_backup_are_named(self):
        check = ActuationCheck(ActuationVerdict.ACTUATED, "ok", _receipt("run-alpha"))
        result, _ = _invoke(check)

        assert "run-alpha" in result.output
        assert "/backup/production/latest.dump" in result.output

    def test_a_stale_receipt_says_how_old(self):
        check = ActuationCheck(
            ActuationVerdict.STALE, "26.0h ago", _receipt(age_hours=26)
        )
        result, _ = _invoke(check)

        assert "26.0" in result.output

    def test_missing_does_not_read_as_a_failed_database(self):
        result, _ = _invoke(
            ActuationCheck(ActuationVerdict.MISSING, "no fraisier.restore_receipt")
        )

        lowered = result.output.lower()
        assert "not checked" in lowered or "says nothing" in lowered

    def test_json_carries_the_verdict_and_the_receipt(self):
        check = ActuationCheck(ActuationVerdict.ACTUATED, "ok", _receipt("run-json"))
        result, _ = _invoke(check, "--json")

        payload = json.loads(result.output)
        assert payload["verdict"] == "actuated"
        assert payload["run_id"] == "run-json"
        assert payload["backup_path"] == "/backup/production/latest.dump"
        assert payload["backup_bytes"] == 987654
        assert payload["age_hours"] == pytest.approx(1.5)

    def test_json_without_a_receipt_is_still_valid_json(self):
        result, _ = _invoke(ActuationCheck(ActuationVerdict.MISSING, "none"), "--json")

        payload = json.loads(result.output)
        assert payload["verdict"] == "missing"
        assert payload["run_id"] is None


class TestTheHeapCheckCorroboratesWithoutVoting:
    """It is opt-in, it is a line, and it never moves the exit code."""

    @staticmethod
    def _invoke_with_heap(check, heap, *args):
        from fraisier.cli.main import main

        with (
            patch("fraisier.cli.main.get_config", return_value=_config()),
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch("fraisier.dbops.receipt.verify_actuation", return_value=check),
            patch(
                "fraisier.dbops.receipt.relation_freshness", return_value=heap
            ) as freshness,
        ):
            result = CliRunner().invoke(
                main, ["db", "receipt", "api", "staging", *args]
            )
        return result, freshness

    def test_it_does_not_run_unless_asked(self):
        """pg_stat_file is denied on most managed Postgres — do not ask by default."""
        check = ActuationCheck(ActuationVerdict.ACTUATED, "ok", _receipt())
        result, freshness = self._invoke_with_heap(check, None)

        assert freshness.call_count == 0
        assert result.exit_code == 0

    def test_it_reports_when_asked(self):
        check = ActuationCheck(ActuationVerdict.ACTUATED, "ok", _receipt())
        heap = ActuationCheck(ActuationVerdict.ACTUATED, "all 12 base table(s) fresh")
        result, freshness = self._invoke_with_heap(check, heap, "--check-heap")

        assert freshness.call_count == 1
        assert "all 12 base table(s) fresh" in result.output

    def test_it_asks_about_one_named_schema(self):
        check = ActuationCheck(ActuationVerdict.ACTUATED, "ok", _receipt())
        heap = ActuationCheck(ActuationVerdict.ACTUATED, "fresh")
        _, freshness = self._invoke_with_heap(
            check, heap, "--check-heap", "--heap-schema", "tenant"
        )

        assert freshness.call_args.kwargs["schema"] == "tenant"
        assert freshness.call_args.kwargs["within_hours"] == 48.0

    def test_a_stale_heap_does_not_fail_a_verified_receipt(self):
        """Autovacuum moves mtimes, so this signal cannot be allowed to vote.

        Both are printed: an operator wants to be told the two disagree.
        """
        check = ActuationCheck(ActuationVerdict.ACTUATED, "ok", _receipt())
        heap = ActuationCheck(ActuationVerdict.STALE, "3 of 12 written 40.0h ago")
        result, _ = self._invoke_with_heap(check, heap, "--check-heap")

        assert result.exit_code == 0
        assert "3 of 12" in result.output

    def test_a_fresh_heap_does_not_rescue_a_stale_receipt(self):
        check = ActuationCheck(ActuationVerdict.STALE, "26.0h ago", _receipt(26))
        heap = ActuationCheck(ActuationVerdict.ACTUATED, "all fresh")
        result, _ = self._invoke_with_heap(check, heap, "--check-heap")

        assert result.exit_code == 1

    def test_an_unverifiable_heap_is_a_note_not_an_error(self):
        check = ActuationCheck(ActuationVerdict.ACTUATED, "ok", _receipt())
        heap = ActuationCheck(
            ActuationVerdict.UNVERIFIABLE, "needs pg_read_server_files"
        )
        result, _ = self._invoke_with_heap(check, heap, "--check-heap")

        assert result.exit_code == 0
        assert "pg_read_server_files" in result.output

    def test_json_carries_it_too(self):
        check = ActuationCheck(ActuationVerdict.ACTUATED, "ok", _receipt())
        heap = ActuationCheck(ActuationVerdict.STALE, "3 of 12 stale")
        result, _ = self._invoke_with_heap(check, heap, "--check-heap", "--json")

        payload = json.loads(result.output)
        assert payload["heap"]["verdict"] == "stale"
        assert payload["heap"]["detail"] == "3 of 12 stale"

    def test_json_omits_it_when_not_asked(self):
        check = ActuationCheck(ActuationVerdict.ACTUATED, "ok", _receipt())
        result, _ = self._invoke_with_heap(check, None, "--json")

        assert json.loads(result.output)["heap"] is None


class TestConfigurationEdges:
    def test_an_external_database_is_skipped_like_a_restore_is(self):
        check = ActuationCheck(ActuationVerdict.ACTUATED, "ok", _receipt())
        result, verify = _invoke(check, external=True)

        assert result.exit_code == 0
        assert verify.call_count == 0

    def test_a_missing_admin_url_is_an_error_not_a_crash(self):
        from fraisier.cli.main import main

        config = _config()
        config.get_fraise_environment.return_value = {
            "type": "api",
            "database": {"name": "mydb_staging"},
        }
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
        ):
            result = CliRunner().invoke(main, ["db", "receipt", "api", "staging"])

        assert result.exit_code != 0
        assert "admin_url" in result.output

    def test_an_unknown_fraise_is_an_error(self):
        from fraisier.cli.main import main

        config = _config()
        config.get_fraise.return_value = None
        config.get_fraise_environment.return_value = None
        with patch("fraisier.cli.main.get_config", return_value=config):
            result = CliRunner().invoke(main, ["db", "receipt", "nope", "staging"])

        assert result.exit_code != 0
