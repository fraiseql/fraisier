"""Tests for deployment duration estimator (issue #201)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from fraisier.duration_estimate import (
    STRATEGY_FALLBACK_SECONDS_PER_MB,
    STRATEGY_FLOOR_SECONDS,
    EstimateResult,
    build_estimate,
    estimate_duration,
    format_estimate_line,
    to_dispatch_dict,
)


def _db_with_durations(durations: list[float]) -> MagicMock:
    db = MagicMock()
    db.get_successful_deploy_durations.return_value = durations
    return db


class TestEstimateDurationHistory:
    def test_returns_history_median_with_buffer_when_three_or_more_samples(self):
        db = _db_with_durations([100.0, 120.0, 140.0])  # median 120 → +20% = 144
        result = estimate_duration(
            db, fraise="api", environment="prod", strategy="rebuild", db_size_mb=500
        )
        assert isinstance(result, EstimateResult)
        assert result.confidence == "history"
        assert result.samples_used == 3
        assert result.seconds == 144

    def test_uses_only_history_when_five_samples_present(self):
        db = _db_with_durations([90.0, 100.0, 110.0, 120.0, 130.0])
        result = estimate_duration(
            db, fraise="api", environment="prod", strategy="rebuild", db_size_mb=500
        )
        assert result.confidence == "history"
        # median = 110, +20% = 132
        assert result.seconds == 132
        assert result.samples_used == 5

    def test_query_filters_by_fraise_env_and_strategy(self):
        db = _db_with_durations([100.0, 100.0, 100.0])
        estimate_duration(
            db,
            fraise="api",
            environment="staging",
            strategy="restore_migrate",
            db_size_mb=200,
        )
        db.get_successful_deploy_durations.assert_called_once_with(
            fraise="api", environment="staging", strategy="restore_migrate", limit=5
        )


class TestEstimateDurationFallback:
    def test_returns_fallback_when_no_samples(self):
        db = _db_with_durations([])
        result = estimate_duration(
            db, fraise="api", environment="prod", strategy="rebuild", db_size_mb=400
        )
        assert result.confidence == "fallback"
        assert result.samples_used == 0
        # 400 MB * 0.0025 s/MB = 1.0s; should be clamped to floor 180s.
        assert result.seconds == STRATEGY_FLOOR_SECONDS["rebuild"]

    def test_fallback_scales_with_db_size_when_above_floor(self):
        db = _db_with_durations([])
        # 200_000 MB * 0.05 = 10_000s for restore_migrate
        result = estimate_duration(
            db,
            fraise="api",
            environment="prod",
            strategy="restore_migrate",
            db_size_mb=200_000,
        )
        assert result.confidence == "fallback"
        assert result.seconds == int(
            200_000 * STRATEGY_FALLBACK_SECONDS_PER_MB["restore_migrate"]
        )

    def test_fallback_uses_strategy_floor_when_db_size_unknown(self):
        db = _db_with_durations([])
        result = estimate_duration(
            db, fraise="api", environment="prod", strategy="migrate", db_size_mb=None
        )
        assert result.confidence == "fallback"
        assert result.seconds == STRATEGY_FLOOR_SECONDS["migrate"]

    def test_fallback_only_two_samples_still_falls_back(self):
        """Plan: <3 samples → fallback. Two samples isn't enough signal."""
        db = _db_with_durations([100.0, 200.0])
        result = estimate_duration(
            db, fraise="api", environment="prod", strategy="rebuild", db_size_mb=None
        )
        assert result.confidence == "fallback"
        assert result.samples_used == 2

    def test_fallback_for_unknown_strategy_uses_generic_floor(self):
        db = _db_with_durations([])
        result = estimate_duration(
            db,
            fraise="api",
            environment="prod",
            strategy="custom_xyz",
            db_size_mb=None,
        )
        assert result.confidence == "fallback"
        # Unknown strategy gets a non-zero generic floor (60s by convention).
        assert result.seconds >= 60


class TestEstimateDurationErrors:
    def test_swallows_db_error_and_falls_back(self):
        """A failing DB query must never break the estimator — return fallback."""
        db = MagicMock()
        db.get_successful_deploy_durations.side_effect = RuntimeError("db down")
        result = estimate_duration(
            db, fraise="api", environment="prod", strategy="rebuild", db_size_mb=500
        )
        assert result.confidence == "fallback"
        assert result.samples_used == 0


class TestGetSuccessfulDeployDurations:
    """The repository method consumed by the estimator."""

    def test_returns_durations_for_matching_fraise_env_strategy(self, test_db):
        from fraisier.database import get_connection

        # Three successful rebuild deploys for api/prod.
        for dur in (100.0, 110.0, 120.0):
            pk = test_db.start_deployment(fraise="api", environment="prod")
            test_db.complete_deployment(
                deployment_id=pk,
                success=True,
                new_version="v2",
                strategy="rebuild",
            )
            # Patch duration in place since timing is fast in tests.
            with get_connection() as conn:
                conn.execute(
                    "UPDATE tb_deployment SET duration_seconds=? WHERE pk_deployment=?",
                    (dur, pk),
                )
                conn.commit()
        # One failure that must NOT be returned.
        pk = test_db.start_deployment(fraise="api", environment="prod")
        test_db.complete_deployment(deployment_id=pk, success=False, strategy="rebuild")

        result = test_db.get_successful_deploy_durations(
            fraise="api", environment="prod", strategy="rebuild", limit=10
        )
        assert sorted(result) == [100.0, 110.0, 120.0]

    def test_filters_by_strategy(self, test_db):
        pk_rebuild = test_db.start_deployment(fraise="api", environment="prod")
        test_db.complete_deployment(
            deployment_id=pk_rebuild,
            success=True,
            new_version="v",
            strategy="rebuild",
        )
        pk_migrate = test_db.start_deployment(fraise="api", environment="prod")
        test_db.complete_deployment(
            deployment_id=pk_migrate,
            success=True,
            new_version="v",
            strategy="migrate",
        )

        rebuild = test_db.get_successful_deploy_durations(
            fraise="api", environment="prod", strategy="rebuild", limit=10
        )
        migrate = test_db.get_successful_deploy_durations(
            fraise="api", environment="prod", strategy="migrate", limit=10
        )
        assert len(rebuild) == 1
        assert len(migrate) == 1

    def test_limit_honoured(self, test_db):
        for _ in range(10):
            pk = test_db.start_deployment(fraise="api", environment="prod")
            test_db.complete_deployment(
                deployment_id=pk, success=True, new_version="v", strategy="rebuild"
            )
        result = test_db.get_successful_deploy_durations(
            fraise="api", environment="prod", strategy="rebuild", limit=5
        )
        assert len(result) == 5

    def test_returns_empty_for_no_matches(self, test_db):
        result = test_db.get_successful_deploy_durations(
            fraise="ghost", environment="prod", strategy="rebuild", limit=10
        )
        assert result == []


class TestBuildEstimate:
    """`build_estimate` is the shared helper consumed by both the webhook and
    CLI surfaces. Returns an `EstimateResult` dataclass or `None`."""

    def test_returns_none_when_no_database_strategy(self):
        db = MagicMock()
        assert build_estimate(db, {}, "api", "production") is None

    def test_returns_none_when_strategy_is_blank(self):
        db = MagicMock()
        assert build_estimate(db, {"database": {}}, "api", "production") is None

    def test_aliases_apply_to_migrate(self, monkeypatch):
        captured: dict[str, object] = {}

        def fake_estimate_duration(_db, **kwargs):
            captured.update(kwargs)
            return EstimateResult(seconds=42, confidence="fallback", samples_used=0)

        monkeypatch.setattr(
            "fraisier.duration_estimate.estimate_duration", fake_estimate_duration
        )
        db = MagicMock()
        result = build_estimate(
            db, {"database": {"strategy": "apply"}}, "api", "production"
        )
        assert result is not None
        assert captured["strategy"] == "migrate"

    def test_returns_estimate_result_dataclass(self):
        db = MagicMock()
        db.get_successful_deploy_durations.return_value = []
        result = build_estimate(
            db, {"database": {"strategy": "rebuild"}}, "api", "production"
        )
        assert isinstance(result, EstimateResult)
        assert result.confidence in {"history", "fallback"}
        assert isinstance(result.seconds, int)
        assert isinstance(result.samples_used, int)

    def test_returns_none_on_exception(self, monkeypatch):
        def raises(*_a, **_kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "fraisier.duration_estimate.estimate_duration", raises
        )
        db = MagicMock()
        assert (
            build_estimate(db, {"database": {"strategy": "rebuild"}}, "api", "prod")
            is None
        )


class TestToDispatchDict:
    def test_has_three_keys(self):
        result = EstimateResult(seconds=180, confidence="history", samples_used=5)
        out = to_dispatch_dict(result)
        assert set(out.keys()) == {
            "estimated_duration_s",
            "estimated_ready_at",
            "estimate_confidence",
        }

    def test_estimated_ready_at_is_iso_z_format(self):
        result = EstimateResult(seconds=60, confidence="fallback", samples_used=0)
        out = to_dispatch_dict(result)
        assert isinstance(out["estimated_ready_at"], str)
        assert out["estimated_ready_at"].endswith("Z")

    def test_duration_and_confidence_passed_through(self):
        result = EstimateResult(seconds=180, confidence="history", samples_used=5)
        out = to_dispatch_dict(result)
        assert out["estimated_duration_s"] == 180
        assert out["estimate_confidence"] == "history"


class TestFormatEstimateLine:
    def test_history(self):
        result = EstimateResult(seconds=180, confidence="history", samples_used=5)
        assert format_estimate_line(result) == (
            "Estimated completion: ~3m (history, 5 samples)"
        )

    def test_fallback_zero_samples(self):
        result = EstimateResult(seconds=60, confidence="fallback", samples_used=0)
        assert format_estimate_line(result) == (
            "Estimated completion: ~1m (fallback, 0 samples)"
        )

    @pytest.mark.parametrize(
        ("seconds", "expected_minutes"),
        [
            (150, 3),  # math.ceil(150/60) == 3 (round-half-up would give 2)
            (30, 1),
            (1, 1),  # never print ~0m
            (60, 1),
            (61, 2),
        ],
    )
    def test_rounds_up_to_minutes(self, seconds: int, expected_minutes: int):
        result = EstimateResult(
            seconds=seconds, confidence="fallback", samples_used=0
        )
        assert f"~{expected_minutes}m" in format_estimate_line(result)
