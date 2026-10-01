"""Validation for ``database.pre_migrate_dump`` (#419).

The block had **no** validation at all — `_core.py` read it as a plain dict
with `.get()`. `keep_last` is the first key where a wrong value is worse than
no value: `keep_last: 0` would delete the rollback point for the migration
about to run, and a ceiling below the floor asks for a corpus that cannot
exist.

Validated whether or not the gate is ``enabled``, for the reason
``post_migrate_check`` is: a typo found only when someone switches the gate on
is found too late.
"""

from __future__ import annotations

import pytest

from fraisier.config._validation import (
    ValidationError,
    validate_one_fraise_environment,
)


def _config(**dump: object) -> dict:
    return {"database": {"pre_migrate_dump": dump}}


class TestKeepLast:
    def test_a_sane_ceiling_is_accepted(self) -> None:
        validate_one_fraise_environment(
            "api", "production", _config(enabled=True, keep_last=3)
        )

    def test_absent_is_accepted(self) -> None:
        """The key is optional; age-only remains a valid configuration."""
        validate_one_fraise_environment(
            "api", "production", _config(enabled=True, retention_hours=72)
        )

    def test_zero_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="keep_last"):
            validate_one_fraise_environment(
                "api", "production", _config(enabled=True, keep_last=0)
            )

    def test_negative_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="keep_last"):
            validate_one_fraise_environment(
                "api", "production", _config(enabled=True, keep_last=-1)
            )

    def test_a_non_integer_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="keep_last"):
            validate_one_fraise_environment(
                "api", "production", _config(enabled=True, keep_last="three")
            )

    def test_a_bool_is_not_an_integer(self) -> None:
        """`True` is an `int` in Python; `keep_last: true` is a typo, not a 1."""
        with pytest.raises(ValidationError, match="keep_last"):
            validate_one_fraise_environment(
                "api", "production", _config(enabled=True, keep_last=True)
            )

    def test_it_is_validated_even_when_the_gate_is_off(self) -> None:
        """A typo found only on the day someone enables the gate is found late."""
        with pytest.raises(ValidationError, match="keep_last"):
            validate_one_fraise_environment(
                "api", "production", _config(enabled=False, keep_last=0)
            )


class TestTheBlockItself:
    def test_a_non_mapping_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="pre_migrate_dump"):
            validate_one_fraise_environment(
                "api", "production", {"database": {"pre_migrate_dump": "yes"}}
            )

    def test_an_absent_block_is_fine(self) -> None:
        validate_one_fraise_environment("api", "production", {"database": {}})


class TestRetentionHours:
    """``retention_hours`` was read with ``.get()`` and never validated (#420).

    The same trap #419 recorded for ``keep_last``, and one step worse now that a
    timer applies it with nobody watching: ``true`` reads as 1 hour, and ``0``
    expires every dump the moment it is written.
    """

    def test_a_sane_window_is_accepted(self) -> None:
        validate_one_fraise_environment(
            "api", "production", _config(enabled=True, retention_hours=72)
        )

    @pytest.mark.parametrize("bad", [True, False, 0, -5, "72", 1.5])
    def test_anything_but_a_positive_integer_is_rejected(self, bad: object) -> None:
        with pytest.raises(ValidationError, match="retention_hours"):
            validate_one_fraise_environment(
                "api", "production", _config(enabled=True, retention_hours=bad)
            )

    def test_it_is_validated_even_when_the_gate_is_off(self) -> None:
        with pytest.raises(ValidationError, match="retention_hours"):
            validate_one_fraise_environment(
                "api", "production", _config(enabled=False, retention_hours=0)
            )


class TestPruneSchedule:
    @pytest.mark.parametrize("good", ["daily", "hourly", "*-*-* 05:30:00 UTC"])
    def test_systemd_calendar_text_is_accepted(self, good: str) -> None:
        validate_one_fraise_environment(
            "api", "production", _config(enabled=True, prune_schedule=good)
        )

    @pytest.mark.parametrize("bad", [42, True, "", "whenever", ["daily"]])
    def test_anything_else_is_rejected(self, bad: object) -> None:
        with pytest.raises(ValidationError, match="prune_schedule"):
            validate_one_fraise_environment(
                "api", "production", _config(enabled=True, prune_schedule=bad)
            )


class TestWhatReachesAUnitFile:
    """``output_dir`` and ``prune_schedule`` are written verbatim into a unit (#420).

    ``ReadWritePaths=`` is space-separated, so a space in ``output_dir`` would
    silently grant a second path; a newline appends a directive and ``%`` expands.
    """

    @pytest.mark.parametrize("bad", ["/var/a\n/etc", "/var/a b", "/var/%h", "/a\tb"])
    def test_an_output_dir_that_cannot_be_written_into_a_unit_is_rejected(
        self, bad: str
    ) -> None:
        with pytest.raises(ValidationError, match="output_dir"):
            validate_one_fraise_environment(
                "api", "production", _config(enabled=True, output_dir=bad)
            )

    def test_a_plain_output_dir_is_accepted(self) -> None:
        validate_one_fraise_environment(
            "api",
            "production",
            _config(enabled=True, output_dir="/var/lib/postgresql/pre_migrate"),
        )

    def test_a_prune_schedule_with_a_newline_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="prune_schedule"):
            validate_one_fraise_environment(
                "api",
                "production",
                _config(enabled=True, prune_schedule="daily\nExecStartPre=/bin/x"),
            )
