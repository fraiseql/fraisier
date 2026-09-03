"""The in-process migrate runs in the project, like the shell path does (#371).

fraisier has two routes that run confiture migrations and they disagreed about
the working directory. The scaffolded shell path does ``cd "${PROJECT_DIR}"``
first (``db_deploy.sh.j2``). The in-process path chdir'd only in
``APIDeployer._run_strategy`` — so the *forward* migrate ran in the app, and the
rollback of that same failed deploy did not, resolving the same relative path
against ``/home/<deploy_user>`` instead.

The construct exercised here is deliberately ``Path(...).read_text()`` and not
``Migration.execute_file``: confiture 0.46 resolves ``execute_file`` against the
migration's own project root, so a test written on it would pass whether or not
fraisier holds the invariant, and would go on passing after a regression.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from fraisier.dbops.confiture import _project_cwd


class TestProjectCwdWindow:
    """Save, chdir, restore — the window every in-process migrate runs inside."""

    def test_the_body_runs_in_the_project(self, tmp_path):
        project = tmp_path / "app"
        project.mkdir()
        seen = []

        with _project_cwd(project):
            seen.append(Path.cwd().resolve())

        assert seen == [project.resolve()]

    def test_the_previous_directory_is_restored(self, tmp_path, monkeypatch):
        project = tmp_path / "app"
        project.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        with _project_cwd(project):
            pass

        assert Path.cwd().resolve() == elsewhere.resolve()

    def test_the_previous_directory_is_restored_after_a_failure(
        self, tmp_path, monkeypatch
    ):
        """A migration that raises is the normal case, not the exotic one.

        It is also the case that matters most: the rollback runs next, and it
        must not inherit a cwd left behind by the migration that failed.
        """
        project = tmp_path / "app"
        project.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        with (
            pytest.raises(RuntimeError, match="migration blew up"),
            _project_cwd(project),
        ):
            msg = "migration blew up"
            raise RuntimeError(msg)

        assert Path.cwd().resolve() == elsewhere.resolve()

    def test_no_project_means_no_chdir(self, tmp_path, monkeypatch):
        """A caller that cannot name a project leaves the cwd exactly as it was.

        The alternative is deriving one from the config or migrations path, which
        is a second source of truth that can disagree with the one the deployer
        already uses.
        """
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        with _project_cwd(None):
            assert Path.cwd().resolve() == elsewhere.resolve()

        assert Path.cwd().resolve() == elsewhere.resolve()

    def test_the_window_is_reentrant(self, tmp_path):
        """``migrate_up(pre_migrate_verify=True)`` re-enters via dry_run_execute.

        A plain ``Lock`` would deadlock the deploy that asked for verification.
        """
        outer = tmp_path / "outer"
        outer.mkdir()
        inner = tmp_path / "inner"
        inner.mkdir()

        with _project_cwd(outer):
            with _project_cwd(inner):
                assert Path.cwd().resolve() == inner.resolve()
            assert Path.cwd().resolve() == outer.resolve()

    def test_two_threads_cannot_interleave_their_windows(self, tmp_path):
        """``os.chdir`` is process-global, so the windows are serialised.

        Nothing in fraisier runs a migrate concurrently with other work today —
        the deploy worker's event loop is blocked for the whole deploy and the
        deploy daemon's socket unit is ``Accept=yes``, one process per connection.
        That is a property a refactor could undo in silence, so the window does
        not depend on it.

        This serialises fraisier's *own* migrate windows against each other. It
        cannot make the process cwd safe for unrelated concurrent code; only a
        subprocess could, and claiming more would be the borrowed invariant #371
        is about.
        """
        first = tmp_path / "first"
        first.mkdir()
        second = tmp_path / "second"
        second.mkdir()

        inside_first = threading.Event()
        release_first = threading.Event()
        observed: list[Path] = []

        def hold_first() -> None:
            with _project_cwd(first):
                inside_first.set()
                release_first.wait(timeout=5)
                observed.append(Path.cwd().resolve())

        def take_second() -> None:
            with _project_cwd(second):
                observed.append(Path.cwd().resolve())

        holder = threading.Thread(target=hold_first)
        contender = threading.Thread(target=take_second)
        holder.start()
        assert inside_first.wait(timeout=5)
        contender.start()
        # The contender is blocked on the window, so it has not chdir'd yet:
        # the holder still observes its own project after the contender started.
        release_first.set()
        holder.join(timeout=5)
        contender.join(timeout=5)

        assert observed == [first.resolve(), second.resolve()]

    def test_a_project_that_is_not_a_directory_is_loud(self, tmp_path):
        """Silently not chdir'ing would reintroduce exactly this issue."""
        with pytest.raises(OSError, match="nope"), _project_cwd(tmp_path / "nope"):
            pass  # pragma: no cover

    def test_the_cwd_survives_a_window_that_never_moved(self, tmp_path, monkeypatch):
        """Entering with the project already current is not a special case."""
        project = tmp_path / "app"
        project.mkdir()
        monkeypatch.chdir(project)

        with _project_cwd(project):
            assert Path.cwd().resolve() == project.resolve()

        assert Path.cwd().resolve() == project.resolve()

    def test_a_string_path_is_accepted(self, tmp_path):
        """Callers hold these as ``Path`` or ``str`` depending on the layer."""
        project = tmp_path / "app"
        project.mkdir()

        with _project_cwd(str(project)):
            assert Path.cwd().resolve() == project.resolve()

    def test_the_window_does_not_leak_into_os_getcwd(self, tmp_path, monkeypatch):
        """``os.getcwd`` and ``Path.cwd`` must agree on the way out."""
        project = tmp_path / "app"
        project.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        with _project_cwd(project):
            pass

        assert Path(os.getcwd()).resolve() == elsewhere.resolve()  # noqa: PTH109
