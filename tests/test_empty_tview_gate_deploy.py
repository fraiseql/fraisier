"""The empty-TVIEW probe in the deploy path (#422).

confiture's drift is schema-only, so a pg_tviews TVIEW that came back empty —
after a physical restore, a crash-recovery start or a failover — over a view that
has rows is a clean schema and an outage.  The probe runs right after the drift
gate: after the migration (a migration may create a TVIEW), before the hooks and
the restart (nothing serves it yet, so failing needs no rollback).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import psycopg
import pytest

from fraisier.dbops.drift import DriftResult
from fraisier.dbops.tviews import EmptyTview, TviewError
from fraisier.errors import DeploymentError
from tests.test_post_migrate_check_deploy import _deploy, _deployer

if TYPE_CHECKING:
    from pathlib import Path

    from fraisier.deployers.api import APIDeployer

URL = "postgresql://app@db.example/app"
EMPTY = [EmptyTview("public", "tv_post", "public", "v_post")]
CLEAN = DriftResult(passed=True, exit_code=0)


@pytest.fixture
def app(tmp_path: Path) -> Path:
    app_dir = tmp_path / "api"
    (app_dir / "db" / "environments").mkdir(parents=True)
    (app_dir / "db" / "environments" / "p.yaml").write_text(
        f"name: p\ndatabase_url: {URL}\n"
    )
    return app_dir


def _probe(deployer: APIDeployer, app: Path, *, found=(), raises=None):
    """Run the probe the way a deploy reaches it: with the migration's config."""
    deployer._migrated_config = app / "db" / "environments" / "p.yaml"
    with patch(
        "fraisier.dbops.tviews.find_empty_tviews",
        side_effect=raises,
        return_value=list(found),
    ) as find:
        deployer._run_empty_tview_check()
    return find


class TestOnEmptyFail:
    def test_an_empty_tview_stops_the_deploy_and_names_the_pair(
        self, app: Path
    ) -> None:
        with pytest.raises(DeploymentError) as exc:
            _probe(_deployer(app, enabled=True), app, found=EMPTY)

        assert "public.tv_post" in str(exc.value)
        assert "public.v_post" in str(exc.value)
        assert "db tviews rebuild" in str(exc.value)

    def test_it_is_the_default_when_no_block_is_written(self, app: Path) -> None:
        with pytest.raises(DeploymentError):
            _probe(_deployer(app), app, found=EMPTY)

    def test_nothing_empty_lets_the_deploy_continue(self, app: Path) -> None:
        _probe(_deployer(app, enabled=True), app, found=[])

    def test_it_probes_the_database_the_migration_used(self, app: Path) -> None:
        find = _probe(_deployer(app, enabled=True), app)

        find.assert_called_once_with(URL)

    def test_a_probe_that_cannot_run_stops_a_declared_gate(self, app: Path) -> None:
        """A check that did not run has cleared nothing."""
        with pytest.raises(DeploymentError, match="could not"):
            _probe(
                _deployer(app, enabled=True),
                app,
                raises=psycopg.OperationalError("refused"),
            )

    def test_an_outdated_pg_tviews_is_reported_not_ignored(self, app: Path) -> None:
        with pytest.raises(DeploymentError, match=r"beta\.20"):
            _probe(
                _deployer(app, enabled=True),
                app,
                raises=TviewError("pg_tviews here predates read contract 1; beta.20"),
            )


class TestOnEmptyWarn:
    def test_an_empty_tview_is_logged_and_the_deploy_continues(
        self, app: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            _probe(_deployer(app, enabled=True, on_empty="warn"), app, found=EMPTY)

        assert "public.tv_post" in caplog.text


class TestADefaultedGate:
    def test_real_emptiness_still_fails(self, app: Path) -> None:
        with pytest.raises(DeploymentError):
            _probe(_deployer(app), app, found=EMPTY)

    def test_a_probe_that_cannot_run_only_warns(
        self, app: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Nobody asked for this gate, so it does not stop a deploy it cannot read."""
        with caplog.at_level("WARNING"):
            _probe(_deployer(app), app, raises=psycopg.OperationalError("refused"))

        assert "refused" in caplog.text

    def test_no_resolvable_database_only_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        deployer = _deployer(tmp_path)
        deployer._migrated_config = tmp_path / "missing.yaml"
        with caplog.at_level("WARNING"):
            deployer._run_empty_tview_check()

        assert "could not" in caplog.text


class TestDisabled:
    def test_declining_the_gate_declines_the_probe(self, app: Path) -> None:
        find = _probe(_deployer(app, enabled=False), app, found=EMPTY)

        find.assert_not_called()


class TestPosition:
    def test_it_runs_after_the_drift_gate_and_before_the_hooks_and_restart(
        self, app: Path
    ) -> None:
        order: list[str] = []
        deployer = _deployer(app, enabled=True)
        with (
            patch(
                "fraisier.dbops.drift.check_schema_drift",
                side_effect=lambda **_k: order.append("drift") or CLEAN,
            ),
            patch.object(
                deployer,
                "_run_empty_tview_check",
                side_effect=lambda: order.append("tviews"),
            ),
        ):
            _deploy(deployer, order)

        assert order == ["migrate", "drift", "tviews", "hooks", "restart"]
