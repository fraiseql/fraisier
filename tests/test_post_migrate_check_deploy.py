"""The drift gate in the deploy path (#395).

The gate sits between the migration and the ``post_migrate`` SQL hooks, so it
runs *after* ``migrate up`` and *before* the service restart. That position is
the whole design:

* after the migration, because ``--check-live-drift`` rates "in the DDL, not in
  live" CRITICAL — before it, every deploy carrying a table-adding migration
  would fail closed;
* before the restart, because nothing is serving the new code yet, so failing
  here needs no rollback — the same scoping ``_run_post_migrate`` already
  documents, and the ``pre_migrate_dump`` gate has by then produced the
  rollback point that makes failing closed safe.

It must also inspect what the migration actually used: ``_run_strategy``
resolves the confiture config and database URL, and the gate reads *those*
resolved values rather than resolving a second time. One deploy reading two
config snapshots is the #376 defect.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.dbops.drift import DriftItem, DriftResult
from fraisier.deployers.api import APIDeployer
from fraisier.errors import DeploymentError

CLEAN = DriftResult(passed=True, exit_code=0)
DRIFTED = DriftResult(
    passed=False,
    exit_code=1,
    critical=(
        DriftItem(
            kind="missing_column",
            severity="critical",
            object_name="core.tb_widget.label",
            message="Column 'core.tb_widget.label' is missing",
        ),
    ),
)
UNRUNNABLE = DriftResult(passed=False, error="database unreachable")


def _deployer(app_dir: Path, **check: object) -> APIDeployer:
    database: dict = {"strategy": "apply", "confiture_config": "db/environments/p.yaml"}
    if check:
        database["post_migrate_check"] = check
    return APIDeployer(
        {
            "app_path": str(app_dir),
            # A service to restart, so "the gate runs before the restart"
            # is an assertion with something to assert against.
            "systemd_service": "api.service",
            "database": database,
        }
    )


def _deploy(deployer: APIDeployer, order: list[str] | None = None):
    """Run ``execute`` with everything but the migration path stubbed out."""

    def record(name: str):
        def _fn(*_a: object, **_k: object) -> None:
            if order is not None:
                order.append(name)

        return _fn

    with (
        patch.object(deployer, "_git_pull", return_value=("oldsha", "newsha")),
        patch.object(deployer, "_install_dependencies"),
        patch.object(deployer, "_check_service_file_staleness"),
        patch.object(deployer, "_validate_wrapper_scripts"),
        patch.object(deployer, "_validate_sandbox_writes"),
        patch.object(deployer, "_sync_config_if_needed"),
        patch.object(deployer, "_snapshot_version_json"),
        patch.object(deployer, "_generate_version_json"),
        patch.object(
            deployer, "_run_database_migrations", side_effect=record("migrate")
        ),
        patch.object(deployer, "_run_post_migrate", side_effect=record("hooks")),
        patch.object(deployer, "_restart_service", side_effect=record("restart")),
        patch.object(deployer, "_restore_previous_state"),
        patch.object(deployer, "_write_status"),
        patch.object(deployer, "_start_db_record", return_value=None),
        patch.object(deployer, "_complete_db_record"),
        patch.object(deployer, "_notify"),
    ):
        return deployer.execute()


@pytest.fixture
def app(tmp_path: Path) -> Path:
    app_dir = tmp_path / "api"
    (app_dir / "db" / "environments").mkdir(parents=True)
    (app_dir / "db" / "environments" / "p.yaml").write_text("name: p\n")
    return app_dir


class TestDefaultOff:
    def test_no_block_runs_no_check(self, app: Path) -> None:
        with patch("fraisier.dbops.drift.check_schema_drift") as check:
            result = _deploy(_deployer(app))

        assert result.success
        check.assert_not_called()

    def test_disabled_block_runs_no_check(self, app: Path) -> None:
        with patch("fraisier.dbops.drift.check_schema_drift") as check:
            result = _deploy(_deployer(app, enabled=False))

        assert result.success
        check.assert_not_called()


class TestEnabled:
    def test_a_clean_schema_lets_the_deploy_finish(self, app: Path) -> None:
        with patch(
            "fraisier.dbops.drift.check_schema_drift", return_value=CLEAN
        ) as check:
            result = _deploy(_deployer(app, enabled=True))

        assert result.success
        check.assert_called_once()

    def test_it_runs_after_the_migration_and_before_the_hooks_and_restart(
        self, app: Path
    ) -> None:
        """The ordering *is* the fix, so swapping two lines must fail a test."""
        order: list[str] = []

        def record_gate(**_kwargs: object) -> DriftResult:
            order.append("gate")
            return CLEAN

        with patch("fraisier.dbops.drift.check_schema_drift", record_gate):
            result = _deploy(_deployer(app, enabled=True), order)

        assert result.success
        assert order == ["migrate", "gate", "hooks", "restart"]


class TestOnCriticalFail:
    def test_drift_aborts_before_the_hooks_and_the_restart(self, app: Path) -> None:
        order: list[str] = []

        def record_gate(**_kwargs: object) -> DriftResult:
            order.append("gate")
            return DRIFTED

        with patch("fraisier.dbops.drift.check_schema_drift", record_gate):
            result = _deploy(_deployer(app, enabled=True), order)

        assert not result.success
        assert order == ["migrate", "gate"]
        assert "core.tb_widget.label" in (result.error_message or "")

    def test_a_gate_that_could_not_run_also_aborts(self, app: Path) -> None:
        """A check that did not run has cleared nothing."""
        with patch("fraisier.dbops.drift.check_schema_drift", return_value=UNRUNNABLE):
            result = _deploy(_deployer(app, enabled=True))

        assert not result.success
        assert "database unreachable" in (result.error_message or "")


class TestOnCriticalWarn:
    def test_drift_is_logged_and_the_deploy_continues(
        self, app: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        order: list[str] = []

        def record_gate(**_kwargs: object) -> DriftResult:
            order.append("gate")
            return DRIFTED

        with patch("fraisier.dbops.drift.check_schema_drift", record_gate):
            result = _deploy(_deployer(app, enabled=True, on_critical="warn"), order)

        assert result.success
        assert order == ["migrate", "gate", "hooks", "restart"]
        assert "core.tb_widget.label" in caplog.text

    def test_a_gate_that_could_not_run_only_warns_too(self, app: Path) -> None:
        with patch("fraisier.dbops.drift.check_schema_drift", return_value=UNRUNNABLE):
            result = _deploy(_deployer(app, enabled=True, on_critical="warn"))

        assert result.success


class TestOneConfigSnapshot:
    def test_the_gate_inspects_what_the_migration_used(self, app: Path) -> None:
        """Not a second `_resolve_strategy()` call — that is the #376 defect.

        `_resolve_strategy` is made to answer differently on a second call, so a
        gate that re-resolves picks up the wrong config and fails this test.
        """
        deployer = _deployer(app, enabled=True)
        migrated = app / "db" / "environments" / "p.yaml"
        other = app / "db" / "environments" / "second-call.yaml"
        other.write_text("name: second-call\n")

        answers = [
            (
                MagicMock(),
                Path("db/environments/p.yaml"),
                Path("db/migrations"),
                "postgresql:///app",
            ),
            (
                MagicMock(),
                Path("db/environments/second-call.yaml"),
                Path("db/migrations"),
                "postgresql:///other",
            ),
        ]

        with (
            patch.object(deployer, "_resolve_strategy", side_effect=answers),
            patch(
                "fraisier.dbops.drift.check_schema_drift", return_value=CLEAN
            ) as check,
            patch.object(deployer, "_run_post_migrate"),
        ):
            deployer._run_strategy()
            deployer._run_post_migrate_check()

        assert check.call_args.kwargs["confiture_config"] == migrated
        assert check.call_args.kwargs["database_url"] == "postgresql:///app"
        assert other.name not in str(check.call_args.kwargs["confiture_config"])

    def test_the_gate_is_told_the_directory_the_migration_ran_in(
        self, app: Path
    ) -> None:
        with patch(
            "fraisier.dbops.drift.check_schema_drift", return_value=CLEAN
        ) as check:
            _deploy(_deployer(app, enabled=True))

        assert check.call_args.kwargs["project_dir"] == app
