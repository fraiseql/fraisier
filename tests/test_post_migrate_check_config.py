"""``database.post_migrate_check`` — the gate's config surface (#395).

Named ``post_migrate_check``, not the ``pre_migrate_check`` the issue proposed.
``--check-live-drift`` grades the DDL against live and rates "in the DDL, not in
live" CRITICAL — which is precisely what a pending table-adding migration looks
like, so a pre-migration gate fails closed on every deploy that carries one.
Only the post-migration position tells a deploy in progress apart from a broken
schema.

Default off, and a typo is a config error rather than a quietly disabled gate:
a gate you believe is running and is not is worse than none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from fraisier.config._validation import (
    ValidationError,
    validate_one_fraise_environment,
)
from fraisier.post_migrate_check import (
    PostMigrateCheck,
    load_post_migrate_check,
)

if TYPE_CHECKING:
    from pathlib import Path

    from fraisier.config import FraisierConfig
    from fraisier.doctor import CheckResult


def _config(**check: object) -> dict:
    return {"database": {"post_migrate_check": check}} if check else {"database": {}}


class TestLoader:
    def test_absent_block_is_disabled(self) -> None:
        assert load_post_migrate_check({}) == PostMigrateCheck(enabled=False)

    def test_explicitly_disabled_stays_disabled(self) -> None:
        loaded = load_post_migrate_check({"post_migrate_check": {"enabled": False}})
        assert not loaded.enabled

    def test_enabled_defaults_to_live_drift_and_fail(self) -> None:
        loaded = load_post_migrate_check({"post_migrate_check": {"enabled": True}})
        assert loaded.enabled
        assert loaded.checks == ("live-drift",)
        assert loaded.on_critical == "fail"

    def test_checks_and_policy_are_read_from_config(self) -> None:
        loaded = load_post_migrate_check(
            {
                "post_migrate_check": {
                    "enabled": True,
                    "checks": ["live-drift", "signatures"],
                    "on_critical": "warn",
                }
            }
        )
        assert loaded.checks == ("live-drift", "signatures")
        assert loaded.on_critical == "warn"


class TestValidation:
    """Every one of these would otherwise be a gate silently doing nothing."""

    def test_unknown_check_name_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="body-replay"):
            validate_one_fraise_environment(
                "api",
                "production",
                _config(enabled=True, checks=["live-drift", "body-replay"]),
            )

    def test_unknown_on_critical_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="on_critical"):
            validate_one_fraise_environment(
                "api", "production", _config(enabled=True, on_critical="ignore")
            )

    def test_checks_must_be_a_list(self) -> None:
        with pytest.raises(ValidationError, match="must be a list"):
            validate_one_fraise_environment(
                "api", "production", _config(enabled=True, checks="live-drift")
            )

    def test_an_enabled_gate_with_no_checks_is_rejected(self) -> None:
        """`checks: []` reads as "on", runs nothing, and reports nothing."""
        with pytest.raises(ValidationError, match="at least one check"):
            validate_one_fraise_environment(
                "api", "production", _config(enabled=True, checks=[])
            )

    def test_the_block_must_be_a_mapping(self) -> None:
        with pytest.raises(ValidationError, match="must be a mapping"):
            validate_one_fraise_environment(
                "api",
                "production",
                {"database": {"post_migrate_check": ["live-drift"]}},
            )

    def test_a_valid_block_passes(self) -> None:
        validate_one_fraise_environment(
            "api",
            "production",
            _config(
                enabled=True, checks=["live-drift", "signatures"], on_critical="warn"
            ),
        )

    def test_a_disabled_block_is_still_validated(self) -> None:
        """A typo found only when someone switches the gate on is found too late."""
        with pytest.raises(ValidationError, match="body-replay"):
            validate_one_fraise_environment(
                "api", "production", _config(enabled=False, checks=["body-replay"])
            )


class TestDoctorCatchesItBeforeADeployDoes:
    """The gate's one hard prerequisite is checkable without a database.

    ``confiture build`` can only be pointed at an *environment name*, which it
    resolves to ``<project>/db/environments/<name>.yaml``. If the deploy's
    ``confiture_config`` does not live there, the gate cannot build the schema
    to compare against and refuses — at deploy time, after the migrations have
    already been applied. Doctor finds it before that.
    """

    def _run(self, cfg: FraisierConfig) -> CheckResult:
        from fraisier import doctor

        return doctor.DOCTOR_CHECKS["post_migrate_check_buildable"].fn(cfg)

    def _cfg(
        self, tmp_path: Path, *, enabled: bool = True, confiture_config: str
    ) -> FraisierConfig:
        from fraisier.config import FraisierConfig

        app = tmp_path / "app"
        (app / "db" / "environments").mkdir(parents=True)
        (app / "db" / "environments" / "production.yaml").write_text(
            "name: production\n"
        )
        (app / "confiture.yaml").write_text("name: production\n")

        path = tmp_path / "fraises.yaml"
        path.write_text(f"""
name: myproj
scaffold:
  deploy_user: fraisier
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: {app}
        database:
          name: db
          strategy: migrate
          confiture_config: {confiture_config}
          post_migrate_check:
            enabled: {str(enabled).lower()}
            checks: [live-drift]
""")
        return FraisierConfig(path)

    def test_registered(self) -> None:
        from fraisier import doctor

        assert "post_migrate_check_buildable" in doctor.DOCTOR_CHECKS

    def test_skips_when_the_gate_is_not_enabled(self, tmp_path: Path) -> None:
        cfg = self._cfg(tmp_path, enabled=False, confiture_config="confiture.yaml")
        assert self._run(cfg).status == "skip"

    def test_passes_when_the_config_is_where_build_resolves_it(self, tmp_path) -> None:
        cfg = self._cfg(tmp_path, confiture_config="db/environments/production.yaml")
        assert self._run(cfg).status == "pass"

    def test_warns_when_confiture_build_could_not_resolve_the_env(
        self, tmp_path: Path
    ) -> None:
        """A root ``confiture.yaml`` has no ``--env`` name to derive."""
        cfg = self._cfg(tmp_path, confiture_config="confiture.yaml")
        result = self._run(cfg)
        assert result.status == "warn"
        assert "confiture build --env" in result.detail
        assert result.fix_hint is not None
        assert "db/environments" in result.fix_hint
