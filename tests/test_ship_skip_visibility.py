"""A check that did not run says so (#346).

This is the half that let the bug live. The issue's own headline is **"the
failure is silent, which is the worst part: a skipped check is
indistinguishable from a passing one in the output."** Twelve checks collapsing
to four would have been noticed the first time it happened had the skips been
visible.

Fixing only the changed-set calculation would leave the next trigger bug just as
invisible, so the reporting is tested separately and on its own terms.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING
from unittest.mock import patch

from rich.console import Console

from fraisier.config import ShipCheckConfig, ShipConfig
from fraisier.ship.checks import CheckResult
from fraisier.ship.pipeline import ShipPipeline, TriggerScope


def _check(name: str, triggers: list[str] | None = None) -> ShipCheckConfig:
    return ShipCheckConfig(
        name=name, command=["true"], phase="validate", triggers=triggers
    )


def _run(checks, scope: TriggerScope):
    """Run the verify phase with *scope* stubbed, returning (result, output)."""
    buffer = io.StringIO()
    pipeline = ShipPipeline(
        config=ShipConfig(checks=list(checks), parallel=False),
        cwd=__import__("pathlib").Path(),
        console=Console(file=buffer, width=200, no_color=True),
    )
    with (
        patch.object(ShipPipeline, "_compute_trigger_scope", return_value=scope),
        patch(
            "fraisier.ship.pipeline.run_check",
            side_effect=lambda c, _cwd: CheckResult(
                name=c.name, success=True, output="", duration_seconds=0.1
            ),
        ),
    ):
        result = pipeline.run_verify_phase()
    return result, buffer.getvalue()


_RESOLVED = TriggerScope(
    files=frozenset({"src/a.py", "src/b.py"}),
    base="origin/main",
    detail="ship.pr_base=main",
)
_UNDETERMINED = TriggerScope(
    files=None, base=None, detail="no ship.pr_base configured and origin/HEAD ..."
)


class TestASkippedCheckIsVisible:
    def test_the_skipped_check_is_named(self):
        result, output = _run([_check("db-lint", ["db/**"])], _RESOLVED)

        assert result.success
        assert "db-lint" in output
        assert "skip" in output.lower()

    def test_every_skipped_check_gets_its_own_line(self):
        checks = [
            _check("db-lint", ["db/**"]),
            _check("schema-gate", ["db/migrations/**"]),
            _check("proto-gate", ["proto/**"]),
        ]
        _, output = _run(checks, _RESOLVED)

        for name in ("db-lint", "schema-gate", "proto-gate"):
            assert name in output, f"{name} was skipped without a word"

    def test_the_reason_names_the_pattern_the_base_and_the_count(self):
        """ "no file matched" alone is what an operator reads as fine."""
        _, output = _run([_check("db-lint", ["db/**"])], _RESOLVED)

        assert "db/**" in output
        assert "origin/main" in output
        assert "2" in output, "the changed-file count is what makes it checkable"

    def test_an_untriggered_check_prints_nothing_extra(self):
        _, output = _run([_check("ruff")], _RESOLVED)

        assert "skip" not in output.lower()
        assert "pass" in output.lower()

    def test_a_matching_check_is_not_reported_as_skipped(self):
        _, output = _run([_check("py-lint", ["src/**"])], _RESOLVED)

        assert "skip" not in output.lower()


class TestAForcedRunIsVisibleToo:
    """The safe fallback has to be a legible one.

    A check running for a reason nobody can see is how the next person
    concludes that `triggers:` does not work.
    """

    def test_undetermined_runs_the_check_and_says_why(self):
        result, output = _run([_check("db-lint", ["db/**"])], _UNDETERMINED)

        assert result.success
        assert len(result.results) == 1, "the check must actually run"
        assert "db-lint" in output
        assert "origin/HEAD" in output or "could not determine" in output.lower()

    def test_undetermined_does_not_report_a_skip(self):
        _, output = _run([_check("db-lint", ["db/**"])], _UNDETERMINED)

        assert "skip" not in output.lower()


class TestASkipIsNeverAPass:
    def test_skipped_checks_are_not_in_results(self):
        result, _ = _run([_check("db-lint", ["db/**"])], _RESOLVED)

        assert result.results == [], (
            "a skipped check in `results` is the exact conflation #346 is about"
        )

    def test_skipped_checks_are_carried_separately(self):
        result, _ = _run([_check("db-lint", ["db/**"])], _RESOLVED)

        assert [s.name for s in result.skipped] == ["db-lint"]
        assert result.skipped[0].reason

    def test_a_phase_of_only_skips_still_succeeds(self):
        """Skipping is not failing — a no-op ship is a real, valid outcome."""
        result, _ = _run(
            [_check("db-lint", ["db/**"]), _check("proto", ["proto/**"])], _RESOLVED
        )

        assert result.success
        assert result.failed_phase is None
        assert len(result.skipped) == 2

    def test_a_pass_and_a_skip_are_distinguishable_in_the_result(self):
        result, _ = _run([_check("ruff"), _check("db-lint", ["db/**"])], _RESOLVED)

        assert [r.name for r in result.results] == ["ruff"]
        assert [s.name for s in result.skipped] == ["db-lint"]


class TestExistingOutputIsUnchanged:
    def test_pass_lines_keep_their_shape(self):
        _, output = _run([_check("ruff")], _RESOLVED)

        assert "pass" in output
        assert "ruff" in output
        assert "0.1s" in output
