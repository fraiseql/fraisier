"""Every in-process migrate carries the project it belongs to (#371).

The forward path already ran in the app: ``APIDeployer._run_strategy`` chdirs to
``app_path`` and has since #10. The rollback of that same deploy did not, and
neither did ``fraisier db restore`` or the exported ``ConfitureMigrateStrategy``.
Holding the invariant at one caller that remembered is what let the two halves
of a single deploy disagree, so it is held below them instead — each strategy
names the project, and ``dbops.confiture`` does the rest.

Patched at the importing module, not at ``fraisier.dbops.confiture``: ``_core``
and ``_restore`` do ``from fraisier.dbops.confiture import migrate_up``, which
binds the name locally and would ignore a patch on the source module.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fraisier.dbops.confiture import MigrationResult
from fraisier.strategies import MigrateStrategy, get_strategy

_PROJECT = Path("/opt/acme/app")


def _ok(steps: int = 1) -> MigrationResult:
    return MigrationResult(success=True, steps_applied=steps)


class TestMigrateStrategyCarriesTheProject:
    def test_execute_names_the_project(self, tmp_path):
        strategy = MigrateStrategy(project_dir=_PROJECT)

        with (
            patch("fraisier.strategies._core.preflight"),
            patch("fraisier.strategies._core.migrate_up", return_value=_ok()) as up,
        ):
            strategy.execute(tmp_path / "confiture.yaml", migrations_dir=tmp_path)

        assert up.call_args.kwargs["project_dir"] == _PROJECT

    def test_rollback_names_the_same_project_as_execute(self, tmp_path):
        """The asymmetry #371 is actually about.

        A migration's ``down()`` reverses what its ``up()`` did. Running the two
        from different directories means the same relative path is two files.
        """
        strategy = MigrateStrategy(project_dir=_PROJECT)

        with patch(
            "fraisier.strategies._core.migrate_down", return_value=_ok()
        ) as down:
            strategy.rollback(
                tmp_path / "confiture.yaml", migrations_dir=tmp_path, steps=1
            )

        assert down.call_args.kwargs["project_dir"] == _PROJECT

    def test_a_strategy_with_no_project_passes_none(self, tmp_path):
        """Nothing is guessed: no project named means the cwd is left alone."""
        strategy = MigrateStrategy()

        with (
            patch("fraisier.strategies._core.preflight"),
            patch("fraisier.strategies._core.migrate_up", return_value=_ok()) as up,
        ):
            strategy.execute(tmp_path / "confiture.yaml", migrations_dir=tmp_path)

        assert up.call_args.kwargs["project_dir"] is None


class TestGetStrategyPassesItThrough:
    def test_the_migrate_strategy_is_built_with_the_project(self):
        strategy = get_strategy("migrate", project_dir=_PROJECT)

        assert isinstance(strategy, MigrateStrategy)
        assert strategy._project_dir == _PROJECT

    def test_the_restore_migrate_strategy_is_built_with_the_project(self):
        from fraisier.strategies import RestoreMigrateStrategy

        strategy = get_strategy(
            "restore_migrate",
            db_name="acme",
            admin_url="postgresql://admin@localhost/postgres",
            restore_config={"backup_dir": "/backups"},
            project_dir=_PROJECT,
        )

        assert isinstance(strategy, RestoreMigrateStrategy)
        assert strategy._project_dir == _PROJECT


class TestRestoreStrategyCarriesTheProject:
    def _strategy(self):
        return get_strategy(
            "restore_migrate",
            db_name="acme",
            admin_url="postgresql://admin@localhost/postgres",
            restore_config={"backup_dir": "/backups"},
            project_dir=_PROJECT,
        )

    def test_rollback_names_the_project(self, tmp_path):
        strategy = self._strategy()

        with patch(
            "fraisier.strategies._restore.migrate_down", return_value=_ok()
        ) as down:
            strategy.rollback(
                tmp_path / "confiture.yaml", migrations_dir=tmp_path, steps=1
            )

        assert down.call_args.kwargs["project_dir"] == _PROJECT


class TestConfitureStrategyUsesTheProjectItIsGiven:
    """This one already received ``project_dir`` per call and ignored it."""

    def test_migrate_up_uses_it(self, tmp_path):
        from fraisier.strategies import ConfitureMigrateStrategy

        strategy = ConfitureMigrateStrategy("confiture.yaml")

        with (
            patch("fraisier.dbops.confiture.preflight"),
            patch("fraisier.dbops.confiture.migrate_up", return_value=_ok()) as up,
        ):
            strategy.migrate_up(tmp_path)

        assert up.call_args.kwargs["project_dir"] == tmp_path

    def test_migrate_down_uses_it(self, tmp_path):
        from fraisier.strategies import ConfitureMigrateStrategy

        strategy = ConfitureMigrateStrategy("confiture.yaml")

        with patch("fraisier.dbops.confiture.migrate_down", return_value=_ok()) as down:
            strategy.migrate_down(tmp_path, target="20260101000000")

        assert down.call_args.kwargs["project_dir"] == tmp_path


class TestTheDeployerNamesTheApp:
    """``app_path`` is the deployer's name for the project directory."""

    @staticmethod
    def _deployer(app_path: str | None, strategy: str = "apply"):
        from fraisier.deployers.api import APIDeployer

        deployer = APIDeployer.__new__(APIDeployer)
        deployer.database_config = {"strategy": strategy, "name": "acme"}
        deployer.app_path = app_path
        deployer.systemd_service = None
        return deployer

    @pytest.mark.parametrize("strategy_name", ["apply", "migrate"])
    def test_the_migrate_strategy_gets_the_app_path(self, strategy_name):
        deployer = self._deployer("/opt/acme/app", strategy_name)

        strategy, _, _, _ = deployer._resolve_strategy()

        assert strategy._project_dir == Path("/opt/acme/app")

    def test_no_app_path_names_no_project(self):
        deployer = self._deployer(None)

        strategy, _, _, _ = deployer._resolve_strategy()

        assert strategy._project_dir is None

    def test_the_rollback_path_builds_the_same_strategy(self):
        """``_rollback_database`` re-resolves the strategy; it must match.

        It builds its own instance rather than reusing the one ``_run_strategy``
        used, so "the deployer chdir'd earlier" was never going to cover it.
        """
        deployer = self._deployer("/opt/acme/app")
        deployer._migrations_applied = 1

        with (
            patch.object(
                deployer,
                "_resolve_paths_against_app",
                side_effect=lambda c, m: (c, m),
            ),
            patch("fraisier.strategies._core.migrate_down", return_value=_ok()) as down,
        ):
            deployer._rollback_database(current_version="1.0.0", target="2.0.0")

        assert down.call_args.kwargs["project_dir"] == Path("/opt/acme/app")
