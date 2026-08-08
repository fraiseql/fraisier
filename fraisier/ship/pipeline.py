"""Phased pipeline orchestrator for fraisier ship."""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fraisier.ship.checks import CheckResult, run_check

if TYPE_CHECKING:
    from pathlib import Path

    from rich.console import Console

    from fraisier.config import ShipCheckConfig, ShipConfig

# Failed checks print their last N output lines — tools (pytest, ruff, mypy)
# put the verdict at the end, so the tail is what diagnoses the failure (#255).
_FAILURE_TAIL_LINES = 30


@dataclass(frozen=True)
class SkippedCheck:
    """A check that did not run, and why (#346).

    Deliberately **not** a :class:`CheckResult` with ``success=True``. That is
    precisely the conflation this issue is about — anything summing ``results``
    would count a skip as a pass, which is how twelve checks collapsing to four
    went unnoticed. Same reasoning as v0.61.0's ``CleanupOutcome.invalid`` being
    an overlay rather than a fourth partition member.
    """

    name: str
    reason: str


@dataclass
class PipelineResult:
    """Aggregate result of the full ship pipeline."""

    success: bool
    failed_phase: str | None = None
    results: list[CheckResult] = field(default_factory=list)
    duration_seconds: float = 0.0
    skipped: list[SkippedCheck] = field(default_factory=list)
    """Checks filtered out by their ``triggers:``. Never counted as passes."""


@dataclass(frozen=True)
class TriggerScope:
    """The files this ship touches, or the fact that we cannot tell (#346).

    ``files is None`` and ``files == frozenset()`` are **different**, and
    keeping them apart is the whole point. Empty means "this ship touches
    nothing, so a triggered check correctly does not run". ``None`` means "I
    could not determine it, so run everything rather than nothing".

    Before #346 both were ``[]`` and both skipped every triggered check —
    so a failed ``git`` invocation was indistinguishable from a no-op ship,
    and both resolved to the reading that runs no gates. The project's rule
    is the opposite: an unevaluable condition is never silently resolved.
    See ``db restore``'s lock ("a lock that cannot be evaluated is an error,
    not a skip") and v0.61.0's ``ArchiveVerdict.UNVERIFIABLE``.
    """

    files: frozenset[str] | None
    base: str | None
    detail: str

    @property
    def undetermined(self) -> bool:
        """Nothing is known about what changed.

        Branch on this rather than on the truthiness of :attr:`files`, which
        cannot tell "no changes" from "no idea" — the conflation #346 was
        filed for.
        """
        return self.files is None

    def matches(self, patterns: list[str]) -> bool:
        """Whether any changed file matches any of *patterns*.

        ``fnmatch`` semantics, deliberately unchanged (#346): ``*`` crosses
        ``/``, so ``db/*`` matches ``db/migrations/001.sql`` where a
        gitignore-literate reader would expect ``db/**``. That over-matches,
        which runs checks more often than the pattern suggests — the safe
        direction. Tightening it would stop a check that fires today, which is
        a regression in exactly the direction this issue is about.
        """
        if self.files is None:  # pragma: no cover - callers check undetermined
            return True
        return any(
            fnmatch.fnmatch(f, pattern) for f in self.files for pattern in patterns
        )


class ShipPipeline:
    """Run ship checks in phases: fix → validate+test."""

    def __init__(
        self,
        config: ShipConfig,
        cwd: Path,
        console: Console,
        pr_base: str | None = None,
    ) -> None:
        self._config = config
        self._cwd = cwd
        self._console = console
        # `--pr-base` never reached trigger evaluation before #346: the pipeline
        # was built from ShipConfig alone, and the CLI's resolved base was only
        # threaded to the version-race check (and only when --pr was passed). A
        # run with `--pr-base dev` therefore evaluated triggers against a
        # different base than the PR it was about to open.
        self._pr_base = pr_base if pr_base is not None else config.pr_base
        self._scope: TriggerScope | None = None

    def check_untracked_migrations(self, migrations_dir: Path) -> PipelineResult:
        """Fail if untracked files exist in the migrations directory.

        Prevents shipping a commit that references a migration file which
        was never ``git add``-ed.  ``git add --update`` (used by the ship
        pipeline) only stages *tracked* files, so a brand-new migration
        would be silently left behind.
        """
        if not migrations_dir.is_dir():
            return PipelineResult(success=True)

        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", str(migrations_dir)],
            cwd=self._cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        untracked = [line for line in result.stdout.strip().split("\n") if line]
        if not untracked:
            return PipelineResult(success=True)

        self._console.print("[red]Untracked migration files detected:[/red]")
        for path in untracked:
            self._console.print(f"  {path}")
        self._console.print(
            "\n[yellow]Why:[/yellow] git add --update only stages tracked files."
            " These new migrations would be silently left out of the commit,"
            " causing deployment failures when confiture migrate runs."
        )
        self._console.print(
            f"\nTo include them:  [bold]git add {' '.join(untracked)}[/bold]"
        )
        self._console.print("To ignore:        delete or .gitignore the files above.")

        return PipelineResult(
            success=False,
            failed_phase="untracked-migrations",
            results=[
                CheckResult(
                    name="untracked-migrations",
                    success=False,
                    output="\n".join(untracked),
                    duration_seconds=0.0,
                ),
            ],
        )

    def run_fix_phase(self) -> PipelineResult:
        """Run auto-fixer checks (before staging)."""
        return self._run_phase("fix")

    def run_verify_phase(self) -> PipelineResult:
        """Run validate + test checks concurrently (after staging)."""
        return self._run_phases(("validate", "test"), phase_label="validate+test")

    def _run_phase(self, phase: str) -> PipelineResult:
        return self._run_phases((phase,), phase_label=phase)

    def _run_phases(
        self, phases: tuple[str, ...], *, phase_label: str
    ) -> PipelineResult:
        """Partition this phase's checks into to-run and skipped, and say which.

        The partition is kept rather than discarded. Filtering with a
        comprehension threw the skipped checks away before ``_execute_checks``,
        which is the only thing that prints — so a skipped check produced no
        output at all and the run just looked short and green (#346). The
        issue's own headline: a skipped check was indistinguishable from a
        passing one.
        """
        scope = self.trigger_scope()
        to_run: list[ShipCheckConfig] = []
        skipped: list[SkippedCheck] = []

        triggered = [c for c in self._config.checks if c.phase in phases and c.triggers]
        if scope.undetermined and triggered:
            # Said once per phase rather than per check: the reason is a property
            # of the run, and repeating a long sentence N times buries it. But it
            # is said — a check running for a reason nobody can see is how the
            # next person concludes that `triggers:` does not work.
            self._console.print(
                f"  [yellow]note[/yellow] could not determine changed files "
                f"({scope.detail}) — running all {len(triggered)} triggered check(s)"
            )

        for check in self._config.checks:
            if check.phase not in phases:
                continue
            if check.triggers is None or scope.undetermined:
                to_run.append(check)
                continue
            if scope.matches(check.triggers):
                to_run.append(check)
                continue
            reason = (
                f"no file matched {', '.join(check.triggers)} "
                f"(vs {scope.base}, {len(scope.files or ())} file(s) changed)"
            )
            skipped.append(SkippedCheck(name=check.name, reason=reason))
            self._print_skip(check.name, reason)

        if not to_run:
            return PipelineResult(success=True, skipped=skipped)
        result = self._execute_checks(to_run, phase_label=phase_label)
        result.skipped = skipped
        return result

    def _execute_checks(
        self,
        checks: list[ShipCheckConfig],
        phase_label: str,
    ) -> PipelineResult:
        start = time.monotonic()
        results: list[CheckResult] = []

        if self._config.parallel and len(checks) > 1:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(run_check, c, self._cwd): c for c in checks}
                for future in futures:
                    result = future.result()
                    results.append(result)
                    self._print_result(result)
        else:
            for check in checks:
                result = run_check(check, self._cwd)
                results.append(result)
                self._print_result(result)

        duration = time.monotonic() - start
        failed = [r for r in results if not r.success]
        if failed:
            return PipelineResult(
                success=False,
                failed_phase=phase_label,
                results=results,
                duration_seconds=duration,
            )
        return PipelineResult(
            success=True,
            results=results,
            duration_seconds=duration,
        )

    def _should_run(self, check: ShipCheckConfig, scope: TriggerScope) -> bool:
        """Whether *check* runs, given what this ship is known to touch.

        A pure predicate over *scope*, so the decision is testable without a
        repository — and so the scope is resolved once per run rather than once
        per check, which is what it was before #346.
        """
        if check.triggers is None:
            return True
        if scope.undetermined:
            # "I cannot tell what changed" must never resolve to "nothing
            # changed". Running a check unnecessarily costs time; skipping one
            # silently cost the reporter the gate protecting migrate-only
            # production.
            return True
        return scope.matches(check.triggers)

    def trigger_scope(self) -> TriggerScope:
        """The changed set for this run, resolved once and reused."""
        if self._scope is None:
            self._scope = self._compute_trigger_scope()
        return self._scope

    def _resolve_trigger_base(self) -> tuple[str | None, str]:
        """The ref to compare against, most explicit source first.

        ``--pr-base``/``ship.pr_base`` → ``origin/HEAD`` → nothing. The second
        step is a local ref lookup needing no network, so a repo with no
        ``ship.pr_base`` still filters correctly instead of degrading to
        "run everything".

        Deliberately **not** ``pr_base or current_branch``.
        ``_assert_no_version_race`` resolves it that way and is right to, but
        here it is fatal: on an already-pushed feature branch
        ``merge-base(HEAD, origin/<current-branch>)`` is HEAD, so the diff is
        empty and #346 is reproduced by the fallback meant to fix it.
        """
        if self._pr_base:
            return f"origin/{self._pr_base}", f"ship.pr_base={self._pr_base}"

        head = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=self._cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if head.returncode == 0 and head.stdout.strip():
            return head.stdout.strip(), "origin/HEAD"

        return None, (
            "no ship.pr_base configured and origin/HEAD does not resolve "
            "(try `git remote set-head origin -a`)"
        )

    def _git_files(self, *args: str) -> list[str] | None:
        """``git`` output as a file list, or ``None`` if the command failed.

        ``None`` rather than ``[]``: the old code collapsed a failed invocation
        into an empty list, which read as "nothing changed" and skipped every
        gate.
        """
        result = subprocess.run(
            ["git", *args],
            cwd=self._cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return [line for line in result.stdout.strip().split("\n") if line]

    def _compute_trigger_scope(self) -> TriggerScope:
        """Committed changes on this branch, unioned with the working tree.

        A union rather than a replacement: the pipeline runs its checks *before*
        ``git add --update`` and before the commit, so uncommitted work must
        keep counting. Committed work has to count too, which is #346 — a
        changeset committed before ``ship`` left a clean tree and an empty diff.

        Untracked files are excluded; ``check_untracked_migrations`` (#181)
        already fails a ship whose migrations directory has untracked files, and
        including every untracked file would let a scratch file run unrelated
        checks.
        """
        base, why = self._resolve_trigger_base()

        worktree = self._git_files("diff", "--name-only", "HEAD")
        if worktree is None:
            return TriggerScope(None, None, "git diff failed (not a git tree?)")

        if base is None:
            return TriggerScope(None, None, why)

        merge_base = subprocess.run(
            ["git", "merge-base", "HEAD", base],
            cwd=self._cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if merge_base.returncode != 0 or not merge_base.stdout.strip():
            return TriggerScope(
                None, base, f"no merge base with {base} (unfetched or unrelated?)"
            )

        committed = self._git_files(
            "diff", "--name-only", f"{merge_base.stdout.strip()}..HEAD"
        )
        if committed is None:
            return TriggerScope(None, base, f"git diff against {base} failed")

        return TriggerScope(frozenset(committed) | frozenset(worktree), base, why)

    def _print_skip(self, name: str, reason: str) -> None:
        """A skip line, shaped like the pass/FAIL lines beside it (#346)."""
        self._console.print(f"  [yellow]skip[/yellow] {name} — {reason}")

    def _print_result(self, result: CheckResult) -> None:
        status = "[green]pass[/green]" if result.success else "[red]FAIL[/red]"
        self._console.print(
            f"  {status} {result.name} ({result.duration_seconds:.1f}s)"
        )
        if not result.success and result.output:
            self._print_failure_output(result)

    def _print_failure_output(self, result: CheckResult) -> None:
        """Surface *why* a check failed (issue #255).

        Tools like pytest and ruff print their verdict at the **end** of
        their output, so the console shows the **tail** (not the head, which
        is startup/collection noise). When the tail hides earlier lines, the
        *full* output is written to a log file and its path is printed so
        nothing is lost.
        """
        lines = result.output.strip().split("\n")
        tail = lines[-_FAILURE_TAIL_LINES:]
        hidden = len(lines) - len(tail)
        if hidden > 0:
            note = f"... {hidden} earlier line(s) hidden"
            log_path = self._write_failure_log(result)
            if log_path is not None:
                note += f"; full output: {log_path}"
            self._console.print(f"    [dim]{note}[/dim]")
        for line in tail:
            self._console.print(f"    {line}")

    def _write_failure_log(self, result: CheckResult) -> Path | None:
        """Write a failed check's full output to a 0o600 log file; return its path.

        Best-effort: reuses the XDG-compliant log directory shared with the
        ``fraisier._output`` tee layer. Returns ``None`` if the directory or
        file cannot be created so a logging hiccup never masks the failure.
        """
        from fraisier._output import _log_dir, _now_stamp

        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", result.name).strip("-") or "check"
        try:
            log_dir = _log_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"ship-check-{safe_name}-{_now_stamp()}.log"
            fd = os.open(
                str(log_path),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(fd, "w") as log_file:
                log_file.write(result.output)
        except OSError:
            return None
        return log_path
