"""The subprocess wrappers read confiture's contract, not its prose (#414).

``confiture_rebuild`` counted migrations by matching ``Migrations:\\s+(\\d+)``
against console output.  ``migrate rebuild`` prints ``Migrations marked: 7``,
which matches neither pattern, so every rebuild reported **0** — nothing fails,
so nothing noticed.  The layout of a console line is not a contract; the JSON
keys are, and are covered by confiture's published schemas.

The same call sites classified failures with :func:`classify_error`, a
substring match over confiture's **English**, whose vocabulary
(``lock_error``, ``connection_error``) is not the contract's
(``lock_contention``, ``db_unreachable``).  One tool answered to two names
depending on which fraisier function you called.

⚠️ ``--format json`` moves the error: measured on 1.23.1, a failing
``migrate up`` writes 154 bytes to **stderr** and nothing to stdout in text
mode, and a 417-byte envelope to **stdout** with nothing on stderr in JSON
mode.  Reading ``result.stderr`` after adding the flag yields an empty error,
which is why these tests assert the message survives.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from fraisier.dbops.confiture import confiture_migrate, confiture_rebuild

_ENVELOPE = json.dumps(
    {
        "ok": False,
        "error": {
            "code": "CONFIG_004",
            "message": "Configuration file not found: /nonexistent/x.yaml",
            "severity": "error",
        },
    }
)


def _migration(version: str) -> dict[str, object]:
    """One entry as confiture serializes it — a dict, not a bare version string."""
    return {"version": version, "name": "m", "duration_ms": 1, "rows_affected": 0}


#: A real `confiture migrate up --format json` payload, captured from a live
#: run on 1.23.1 (`.phases/2026-09-26-confiture-adoption/capture/`), trimmed
#: only of the `parser` block.
#:
#: Captured rather than written, because writing it is what produced the
#: defect this file exists to catch: `_COUNT_KEYS` first shipped the *attribute*
#: names (`migrations_applied`), which appear in no payload, and hand-written
#: fixtures using the same wrong names agreed with it perfectly.
REAL_UP_PAYLOAD: dict[str, object] = {
    "success": True,
    "applied": [_migration("20260101120000"), _migration("20260101130000")],
    "skipped": [],
    "skipped_superuser": [],
    "pending": [],
    "errors": [],
    "total_duration_ms": 1,
    "checksums_verified": True,
    "dry_run": False,
    "dry_run_execute": False,
    "warnings": [],
    "ok": True,
    "command": "migrate up",
}


def test_the_payload_does_not_carry_the_attribute_names() -> None:
    """The premise, pinned: `migrations_applied` is the dataclass field, not the key.

    Without this, every other test here could pass against a `_COUNT_KEYS`
    that reads names no confiture payload contains.
    """
    assert "applied" in REAL_UP_PAYLOAD
    assert "migrations_applied" not in REAL_UP_PAYLOAD


def _proc(returncode: int, stdout: str, stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class TestTheRebuildCountIsRead:
    def test_the_count_comes_from_the_typed_payload(self) -> None:
        """`marked` — the key `MigrateRebuildResult.to_dict` writes.

        Note there is no `migrate-rebuild.schema.json`, so unlike `applied`
        and `rolled_back` this key is pinned by `to_dict` alone upstream.
        """
        payload = json.dumps(
            {"success": True, "marked": [_migration(str(n)) for n in range(7)]}
        )
        with patch("subprocess.run", return_value=_proc(0, payload)):
            result = confiture_rebuild(config_path="c.yaml")

        assert result.success is True
        assert result.migration_count == 7, (
            "the rebuild reported its marked migrations and fraisier dropped them"
        )

    def test_the_command_asks_for_json(self) -> None:
        payload = json.dumps({"success": True, "marked": []})
        with patch("subprocess.run", return_value=_proc(0, payload)) as run:
            confiture_rebuild(config_path="c.yaml")

        argv = run.call_args[0][0]
        assert "--format" in argv and "json" in argv

    def test_output_that_is_not_the_payload_does_not_crash(self) -> None:
        """An unreadable payload is a count of zero, not an exception."""
        with patch("subprocess.run", return_value=_proc(0, "Rebuild complete\n")):
            result = confiture_rebuild(config_path="c.yaml")

        assert result.success is True
        assert result.migration_count == 0


class TestTheMigrateCountIsRead:
    def test_up_counts_what_it_applied_from_a_real_payload(self) -> None:
        with patch(
            "subprocess.run", return_value=_proc(0, json.dumps(REAL_UP_PAYLOAD))
        ):
            assert confiture_migrate(config_path="c.yaml").migration_count == 2

    def test_down_counts_what_it_rolled_back(self) -> None:
        payload = json.dumps({"success": True, "rolled_back": [_migration("a")]})
        with patch("subprocess.run", return_value=_proc(0, payload)):
            result = confiture_migrate(config_path="c.yaml", direction="down")
        assert result.migration_count == 1


class TestTheFailureIsClassifiedByContract:
    def test_the_message_survives_the_stream_swap(self) -> None:
        """With ``--format json`` stderr is empty; the message is in the envelope."""
        with patch("subprocess.run", return_value=_proc(5, _ENVELOPE, "")):
            result = confiture_migrate(config_path="c.yaml")

        assert result.success is False
        assert "Configuration file not found" in result.error, (
            f"the error was read from an empty stderr: {result.error!r}"
        )

    def test_the_class_is_the_contracts_word_not_the_prose_one(self) -> None:
        """Exit 5 is ``invalid_config``; no prose may override the exit code.

        The stderr here carries ``already exists``, one of
        ``_SCHEMA_ERROR_PATTERNS``.  Under the old precedence the prose won and
        this was ``schema_error``.  An empty stderr would not discriminate:
        the prose classifier returns ``unknown`` for it and the exit-code
        fallback then gives the right answer for the wrong reason.
        """
        with patch(
            "subprocess.run",
            return_value=_proc(5, _ENVELOPE, "ERROR: relation already exists"),
        ):
            result = confiture_migrate(config_path="c.yaml")

        assert result.error_type == "invalid_config", (
            f"prose beat the frozen exit code: {result.error_type!r}"
        )

    def test_a_lock_failure_keeps_its_retriable_class(self) -> None:
        """Exit 6 is ``lock_contention`` — the one class that is retriable.

        The prose classifier called this ``lock_error``, which is not a member
        of :class:`ConfitureFailureClass` at all, so the retriable property was
        unreachable for the only failure that has it.
        """
        envelope = json.dumps(
            {"ok": False, "error": {"code": "LOCK_600", "message": "lock timeout"}}
        )
        # Prose that the old classifier read as `schema_error`, on a failure the
        # contract calls retriable.
        with patch(
            "subprocess.run",
            return_value=_proc(6, envelope, "ERROR: relation already exists"),
        ):
            result = confiture_migrate(config_path="c.yaml")

        assert result.error_type == "lock_contention"

    def test_prose_still_refines_the_unclassified_bucket(self) -> None:
        """Exit 1 is the contract declining to be specific, so prose may refine it.

        The contract calls exit 1 ``internal_error`` — "generic failure: SQL or
        hook execution". A message saying ``column does not exist`` carries
        strictly more than that, so it is kept. The defect was never that prose
        was consulted; it was that prose overruled a class confiture *had*
        committed to, which is how a retriable exit 6 became ``lock_error``.
        """
        with patch(
            "subprocess.run",
            return_value=_proc(1, "", "ERROR: column does not exist"),
        ):
            result = confiture_migrate(config_path="c.yaml")

        assert result.error_type == "schema_error"

    def test_a_specific_class_is_never_refined_away(self) -> None:
        """The same prose against exit 6 must not displace ``lock_contention``."""
        with patch(
            "subprocess.run",
            return_value=_proc(6, "", "ERROR: column does not exist"),
        ):
            result = confiture_migrate(config_path="c.yaml")

        assert result.error_type == "lock_contention"

    def test_rebuild_is_classified_the_same_way(self) -> None:
        with patch("subprocess.run", return_value=_proc(5, _ENVELOPE, "")):
            result = confiture_rebuild(config_path="c.yaml")

        assert result.error_type == "invalid_config"
        assert "Configuration file not found" in result.error
