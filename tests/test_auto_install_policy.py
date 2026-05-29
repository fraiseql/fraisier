"""Tests for the auto_install policy schema — #240 follow-up 01 Phase 1.

Pure parser over a dict env_config. The deployer (Phase 2) consumes it.
"""

from __future__ import annotations

import pytest

from fraisier.scheduled_install import (
    AutoInstallPolicy,
    ScheduledInstallError,
    parse_auto_install_policy,
)

# ---------------------------------------------------------------------------
# Cycle 1.1 — parsing a fully-specified env config
# ---------------------------------------------------------------------------


def test_parse_full_auto_install_block() -> None:
    env = {
        "scheduled": {
            "auto_install": {
                "on_missing": "install",
                "on_drift": "overwrite",
            }
        }
    }
    policy = parse_auto_install_policy(env)
    assert isinstance(policy, AutoInstallPolicy)
    assert policy.on_missing == "install"
    assert policy.on_drift == "overwrite"


# ---------------------------------------------------------------------------
# Cycle 1.2 — defaults when fields are missing
# ---------------------------------------------------------------------------


def test_parse_applies_locked_defaults_when_block_absent() -> None:
    """No scheduled.auto_install block → on_missing=install, on_drift=fail."""
    policy = parse_auto_install_policy({})
    assert policy.on_missing == "install"
    assert policy.on_drift == "fail"


def test_parse_applies_locked_defaults_for_missing_subfields() -> None:
    """Block present with only one subfield → other gets default."""
    env = {"scheduled": {"auto_install": {"on_drift": "skip"}}}
    policy = parse_auto_install_policy(env)
    assert policy.on_missing == "install"  # default
    assert policy.on_drift == "skip"


def test_parse_handles_null_auto_install_block() -> None:
    """``auto_install: ~`` in YAML loads as ``None``; treat as empty."""
    env = {"scheduled": {"auto_install": None}}
    policy = parse_auto_install_policy(env)
    assert policy == AutoInstallPolicy(on_missing="install", on_drift="fail")


def test_parse_handles_null_scheduled_block() -> None:
    env = {"scheduled": None}
    policy = parse_auto_install_policy(env)
    assert policy == AutoInstallPolicy()


# ---------------------------------------------------------------------------
# Cycle 1.3 — unknown values raise with a clear message
# ---------------------------------------------------------------------------


def test_parse_rejects_unknown_on_drift_value() -> None:
    env = {"scheduled": {"auto_install": {"on_drift": "yolo"}}}
    with pytest.raises(ScheduledInstallError, match="on_drift"):
        parse_auto_install_policy(env)


def test_parse_rejects_unknown_on_missing_value() -> None:
    env = {"scheduled": {"auto_install": {"on_missing": "explode"}}}
    with pytest.raises(ScheduledInstallError, match="on_missing"):
        parse_auto_install_policy(env)


def test_parse_rejects_non_mapping_auto_install_block() -> None:
    env = {"scheduled": {"auto_install": "string-value-not-mapping"}}
    with pytest.raises(ScheduledInstallError, match="auto_install"):
        parse_auto_install_policy(env)


# ---------------------------------------------------------------------------
# AutoInstallPolicy is frozen (defence-in-depth for the deployer hook)
# ---------------------------------------------------------------------------


def test_policy_is_frozen() -> None:
    policy = AutoInstallPolicy()
    with pytest.raises(Exception):  # noqa: B017
        policy.on_drift = "overwrite"  # type: ignore[misc]
