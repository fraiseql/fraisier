"""`allow_destructive` from the environment config down to confiture (#398).

The integration tests prove the flag *works* against a real confiture. These
prove it *arrives*: a knob accepted at the top and dropped at any of the three
hops is indistinguishable from one that was never set, and the deploy silently
keeps refusing.

It mirrors `allow_irreversible`, which is already threaded
env config -> `APIDeployer` -> `strategy.execute` -> `migrate_up`.

`RestoreMigrateStrategy` runs `migrate up` too and threads the flag as well;
that test lives in `test_restore_strategy_preflight.py`, which already owns
the harness for driving that strategy's `execute` end to end.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.deployers.api import APIDeployer
from fraisier.strategies import MigrateStrategy


class TestTheStrategyThreadsIt:
    @pytest.mark.parametrize("allow", [False, True])
    def test_migrate_strategy_passes_it_to_migrate_up(self, allow: bool) -> None:
        strategy = MigrateStrategy()

        with (
            patch("fraisier.strategies._core.preflight"),
            patch("fraisier.strategies._core.migrate_up") as migrate,
        ):
            migrate.return_value.steps_applied = 0
            strategy.execute(Path("confiture.yaml"), allow_destructive=allow)

        assert migrate.call_args.kwargs["allow_destructive"] is allow

    def test_it_defaults_to_refusing(self) -> None:
        """Data loss is an explicit decision, never a default."""
        strategy = MigrateStrategy()

        with (
            patch("fraisier.strategies._core.preflight"),
            patch("fraisier.strategies._core.migrate_up") as migrate,
        ):
            migrate.return_value.steps_applied = 0
            strategy.execute(Path("confiture.yaml"))

        assert migrate.call_args.kwargs["allow_destructive"] is False


class TestTheDeployerReadsTheConfig:
    def _deployer(self, tmp_path: Path, **env: object) -> APIDeployer:
        return APIDeployer({"app_path": str(tmp_path), "database": {}, **env})

    def test_absent_key_refuses(self, tmp_path: Path) -> None:
        assert self._deployer(tmp_path).allow_destructive is False

    def test_the_key_is_read(self, tmp_path: Path) -> None:
        deployer = self._deployer(tmp_path, allow_destructive=True)
        assert deployer.allow_destructive is True

    @pytest.mark.parametrize("allow", [False, True])
    def test_it_reaches_the_strategy(self, tmp_path: Path, allow: bool) -> None:
        deployer = self._deployer(tmp_path, allow_destructive=allow)
        fake = MagicMock()
        fake.execute.return_value = MagicMock(
            success=True, migrations_applied=0, errors=[]
        )

        with patch.object(
            deployer,
            "_resolve_strategy",
            return_value=(fake, Path("confiture.yaml"), Path("db/migrations"), None),
        ):
            deployer._run_strategy()

        assert fake.execute.call_args.kwargs["allow_destructive"] is allow


class TestTheConversionIsNarrow:
    """Only the destructive refusal is relabelled — not every validation error.

    `migrate_up` turns confiture's refusal into a fraisier `MigrationError`
    whose remediation is "set `allow_destructive: true`". That advice is right
    for `VALID_002` and wrong for anything else, so the conversion keys on the
    code rather than on the exception class. Today the destructive gate is the
    only `raise ValidationError` in confiture's `_migrator` package, so nothing
    real distinguishes the two — this constructs the difference directly.
    """

    def _migrate_raising(self, exc: BaseException):
        from fraisier.dbops.confiture import migrate_up

        session = MagicMock()
        session.up.side_effect = exc
        env = MagicMock()
        env.migration.view_helpers = "off"

        with (
            patch("fraisier.dbops.confiture._load_env", return_value=env),
            patch("fraisier.dbops.confiture.Migrator") as migrator,
        ):
            migrator.from_config.return_value.__enter__ = MagicMock(
                return_value=session
            )
            migrator.from_config.return_value.__exit__ = MagicMock(return_value=False)
            return migrate_up("confiture.yaml", database_url="postgresql:///app")

    def test_the_destructive_refusal_is_converted(self) -> None:
        from confiture.exceptions import ValidationError

        from fraisier.errors import MigrationError

        with pytest.raises(MigrationError) as excinfo:
            self._migrate_raising(
                ValidationError("Destructive migration refused", error_code="VALID_002")
            )

        assert "allow_destructive" in str(excinfo.value)

    def test_any_other_validation_error_passes_through_unchanged(self) -> None:
        """Relabelling this one would send the operator after the wrong knob."""
        from confiture.exceptions import ValidationError

        from fraisier.errors import MigrationError

        original = ValidationError(
            "Row count mismatch: expected 10000, got 9999", error_code="VALID_500"
        )

        with pytest.raises(ValidationError) as excinfo:
            self._migrate_raising(original)

        assert excinfo.value is original
        assert not isinstance(excinfo.value, MigrationError)
