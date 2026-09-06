"""The status file says what the deploy returned (#378).

Every terminal path of :class:`APIDeployer` is walked here, and each one
asserts the same invariant:

    read_status(fraise).state == result.status.value

written once, by the deployer, with this process stamped as the owner.

Ten of the thirteen findings of the v0.68.0 review were the same defect at
different heights: the record and the outcome were written by different code,
and nothing asserted they agree. This file is that assertion. A new terminal
path that forgets to file its own outcome shows up here as a row that fails,
not as silence.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from fraisier.deployers.api import APIDeployer
from fraisier.deployers.base import DeploymentResult, DeploymentStatus
from fraisier.errors import DeploymentError
from fraisier.smoke_tests import SmokeTestError
from fraisier.status import read_status
from fraisier.timeout import DeploymentTimeoutExpired

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager

FRAISE = "my_api"
OLD_SHA = "0" * 40
NEW_SHA = "1" * 40

INCIDENT = (
    "Database rollback failed — manual intervention required. "
    "Rolled back 1 of 2 migrations; 1 still applied. "
    "Do NOT restart the service until resolved."
)


def _deployer(tmp_path: Path, **extra: Any) -> APIDeployer:
    """An APIDeployer whose status file, repos and app tree are under tmp_path."""
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    config: dict[str, Any] = {
        "fraise_name": FRAISE,
        "environment": "production",
        "app_path": str(app),
        "status_dir": str(tmp_path / "status"),
        "systemd_service": "my_api.service",
        "clone_url": "git@github.com:org/my_api.git",
        "branch": "main",
        "repos_base": str(tmp_path / "repos"),
        "health_check": {"url": "http://127.0.0.1:1/health", "timeout": 1},
    }
    config.update(extra)
    return APIDeployer(config)


def _with_database(**extra: Any) -> dict[str, Any]:
    return {
        "database": {"strategy": "migrate", "database_url": "postgresql://x/y"},
        **extra,
    }


@contextlib.contextmanager
def _git_and_service(
    deployer: APIDeployer,
    *,
    health: bool | list[bool] = True,
    restart: object = None,
) -> Iterator[None]:
    """Stub the boundaries a deploy crosses: git, systemd, the health probe.

    ``health`` may be a list, one entry per call: the deploy's own check comes
    first, a rollback's check second.
    """
    health_patch = (
        patch.object(deployer, "_wait_for_health", side_effect=health)
        if isinstance(health, list)
        else patch.object(deployer, "_wait_for_health", return_value=health)
    )
    restart_patch = (
        patch.object(deployer, "_restart_service")
        if restart is None
        else patch.object(deployer, "_restart_service", side_effect=restart)
    )
    with (
        patch("fraisier.deployers.mixins.clone_bare_repo"),
        patch(
            "fraisier.deployers.mixins.fetch_and_checkout",
            return_value=(OLD_SHA, NEW_SHA),
        ),
        restart_patch,
        health_patch,
        patch.object(deployer, "_git_rollback"),
    ):
        yield


class _FakeStrategy:
    """A migration strategy with the two outcomes ``rollback`` can have.

    Stubbed at the strategy boundary, not at ``_rollback_database``: the
    incident text, the "rolled back N of M" arithmetic and the status write are
    the code under test here, and a stub of the method would replace exactly
    them.
    """

    def __init__(self, *, ok: bool) -> None:
        self.ok = ok

    def rollback(self, _config: Path, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            success=self.ok,
            migrations_applied=2 if self.ok else 1,
            errors=[] if self.ok else ['relation "orders" already exists'],
        )


def _fake_strategy(deployer: APIDeployer, *, ok: bool) -> AbstractContextManager[Any]:
    """Patch ``_resolve_strategy`` so the real ``_rollback_database`` runs."""
    app = Path(str(deployer.app_path))
    return patch.object(
        deployer,
        "_resolve_strategy",
        return_value=(
            _FakeStrategy(ok=ok),
            app / "confiture.toml",
            app / "migrations",
            "postgresql://x/y",
        ),
    )


def _migrations_ran(deployer: APIDeployer) -> AbstractContextManager[Any]:
    """Two migrations applied, without running any."""

    def _run() -> None:
        deployer._migrations_applied = 2

    return patch.object(deployer, "_run_database_migrations", side_effect=_run)


def _migrations_failed(deployer: APIDeployer) -> AbstractContextManager[Any]:
    def _run() -> None:
        deployer._migrations_applied = 2
        raise DeploymentError("migrate up failed on 0007_orders")

    return patch.object(deployer, "_run_database_migrations", side_effect=_run)


# --------------------------------------------------------------------------
# The terminal paths
# --------------------------------------------------------------------------


def _path_success(d: APIDeployer) -> DeploymentResult:
    with _git_and_service(d):
        return d.execute()


def _path_validation_failure(d: APIDeployer) -> DeploymentResult:
    """A wrapper script that is missing: the deploy stops before it starts."""
    with (
        patch.dict(
            os.environ,
            {
                "FRAISIER_SYSTEMCTL_WRAPPER": str(
                    Path(str(d.app_path)) / "no-such-wrapper"
                )
            },
        ),
        _git_and_service(d),
    ):
        return d.execute()


def _path_migrate_failure_db_rolled_back(d: APIDeployer) -> DeploymentResult:
    with _git_and_service(d), _migrations_failed(d), _fake_strategy(d, ok=True):
        return d.execute()


def _path_migrate_failure_db_rollback_failed(d: APIDeployer) -> DeploymentResult:
    with _git_and_service(d), _migrations_failed(d), _fake_strategy(d, ok=False):
        return d.execute()


def _path_health_failure_rolled_back(d: APIDeployer) -> DeploymentResult:
    with _git_and_service(d, health=[False, True]):
        return d.execute()


def _path_health_failure_db_rollback_failed(d: APIDeployer) -> DeploymentResult:
    with (
        _git_and_service(d, health=[False, True]),
        _migrations_ran(d),
        _fake_strategy(d, ok=False),
    ):
        return d.execute()


def _path_health_failure_git_rollback_raises(d: APIDeployer) -> DeploymentResult:
    with (
        _git_and_service(d, health=[False, True]),
        patch.object(d, "_git_rollback", side_effect=OSError("detached HEAD")),
    ):
        return d.execute()


def _path_health_failure_restored_unhealthy(d: APIDeployer) -> DeploymentResult:
    """The old version is back on disk and still does not answer."""
    with _git_and_service(d, health=[False, False]):
        return d.execute()


def _path_timeout_rolled_back(d: APIDeployer) -> DeploymentResult:
    with _git_and_service(
        d,
        health=[True, True],
        restart=[DeploymentTimeoutExpired("Deployment timed out after 600s"), None],
    ):
        return d.execute()


def _path_timeout_db_rollback_failed(d: APIDeployer) -> DeploymentResult:
    with (
        _git_and_service(
            d,
            health=[True, True],
            restart=[DeploymentTimeoutExpired("Deployment timed out after 600s"), None],
        ),
        _migrations_ran(d),
        _fake_strategy(d, ok=False),
    ):
        return d.execute()


def _path_timeout_before_checkout(d: APIDeployer) -> DeploymentResult:
    """Nothing was checked out, so there is nothing to roll back to."""
    with (
        _git_and_service(d),
        patch.object(
            d,
            "_check_service_file_staleness",
            side_effect=DeploymentTimeoutExpired("Deployment timed out after 600s"),
        ),
    ):
        return d.execute()


def _path_smoke_halt(d: APIDeployer) -> DeploymentResult:
    with (
        _git_and_service(d),
        patch(
            "fraisier.smoke_tests.resolve_and_run",
            side_effect=SmokeTestError("GET /orders returned 500", rollback=False),
        ),
    ):
        return d.execute()


def _path_smoke_rolled_back(d: APIDeployer) -> DeploymentResult:
    with (
        _git_and_service(d, health=[True, True]),
        patch(
            "fraisier.smoke_tests.resolve_and_run",
            side_effect=SmokeTestError("GET /orders returned 500", rollback=True),
        ),
    ):
        return d.execute()


@dataclass(frozen=True)
class TerminalPath:
    """One way a deploy can end, and the outcome it must report."""

    id: str
    run: Callable[[APIDeployer], DeploymentResult]
    expected: DeploymentStatus
    config: dict[str, Any] | None = None
    message_contains: str | None = None


SMOKE: dict[str, Any] = {
    "smoke_tests": [{"name": "orders", "url": "/orders", "on_failure": "halt"}]
}

PATHS = [
    TerminalPath("success", _path_success, DeploymentStatus.SUCCESS),
    TerminalPath(
        "validation_failure", _path_validation_failure, DeploymentStatus.FAILED
    ),
    TerminalPath(
        "migrate_failure_db_rolled_back",
        _path_migrate_failure_db_rolled_back,
        DeploymentStatus.ROLLED_BACK,
        config=_with_database(),
    ),
    TerminalPath(
        "migrate_failure_db_rollback_failed",
        _path_migrate_failure_db_rollback_failed,
        DeploymentStatus.ROLLBACK_FAILED,
        config=_with_database(),
        message_contains="Do NOT restart",
    ),
    TerminalPath(
        "health_failure_rolled_back",
        _path_health_failure_rolled_back,
        DeploymentStatus.ROLLED_BACK,
    ),
    TerminalPath(
        "health_failure_db_rollback_failed",
        _path_health_failure_db_rollback_failed,
        DeploymentStatus.ROLLBACK_FAILED,
        config=_with_database(),
        message_contains="Do NOT restart",
    ),
    TerminalPath(
        "health_failure_git_rollback_raises",
        _path_health_failure_git_rollback_raises,
        DeploymentStatus.ROLLBACK_FAILED,
    ),
    TerminalPath(
        "health_failure_restored_unhealthy",
        _path_health_failure_restored_unhealthy,
        DeploymentStatus.ROLLBACK_FAILED,
    ),
    TerminalPath(
        "timeout_rolled_back", _path_timeout_rolled_back, DeploymentStatus.ROLLED_BACK
    ),
    TerminalPath(
        "timeout_db_rollback_failed",
        _path_timeout_db_rollback_failed,
        DeploymentStatus.ROLLBACK_FAILED,
        config=_with_database(),
        message_contains="Do NOT restart",
    ),
    TerminalPath(
        "timeout_before_checkout",
        _path_timeout_before_checkout,
        DeploymentStatus.FAILED,
    ),
    TerminalPath("smoke_halt", _path_smoke_halt, DeploymentStatus.FAILED, config=SMOKE),
    TerminalPath(
        "smoke_rolled_back",
        _path_smoke_rolled_back,
        DeploymentStatus.ROLLED_BACK,
        config=SMOKE,
    ),
]


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.id)
def test_the_record_says_what_the_deploy_returned(path: TerminalPath, tmp_path: Path):
    deployer = _deployer(tmp_path, **(path.config or {}))

    result = path.run(deployer)

    assert result.status is path.expected, (
        f"path {path.id!r} returned {result.status.value!r}, expected "
        f"{path.expected.value!r}"
    )
    status = read_status(FRAISE, status_dir=tmp_path / "status")
    assert status is not None, f"path {path.id!r} filed no record at all"
    assert status.state == result.status.value, (
        f"path {path.id!r} returned {result.status.value!r} and filed {status.state!r}"
    )
    assert status.owner_pid == os.getpid(), (
        f"path {path.id!r} filed a record owned by {status.owner_pid} — a write "
        "from anything but the deployer blanks the owner and makes the "
        "reconciler abstain"
    )
    if path.message_contains:
        assert path.message_contains in (status.error_message or ""), (
            f"path {path.id!r} filed {status.error_message!r}, which drops "
            f"{path.message_contains!r}"
        )
