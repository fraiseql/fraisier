"""The shipped example configs pass fraisier's own validation (#448).

Nothing used to load them, so they drifted: three envs lost the
``database.admin_url`` their strategy requires, and two jobs-shaped fraises
never had an ``app_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fraisier.config import FraisierConfig
from fraisier.validation import ValidationCheckResult, ValidationRunner

_REPO = Path(__file__).resolve().parent.parent
_EXAMPLES = [
    _REPO / "fraises.example.yaml",
    *sorted((_REPO / "examples").glob("*/fraises.yaml")),
]


def _config_errors(config: FraisierConfig) -> list[ValidationCheckResult]:
    """Every failed error-level check, minus the ones that read this host."""
    return [
        r
        for r in ValidationRunner(config).run_all()
        if not r.passed and r.severity == "error" and not r.name.startswith("user_")
    ]


def test_the_examples_are_found() -> None:
    assert len(_EXAMPLES) >= 2


@pytest.mark.parametrize("path", _EXAMPLES, ids=lambda p: str(p.relative_to(_REPO)))
def test_an_example_config_validates(path: Path) -> None:
    errors = _config_errors(FraisierConfig(str(path)))
    assert [r.message for r in errors] == []


_ADMIN_URL_MISSING = """\
name: tp
fraises:
  my_api:
    type: api
    environments:
      development:
        app_path: /var/www/api
        database:
          name: db
          strategy: rebuild
"""


def test_an_env_that_fails_validation_reports_no_phantom_app_path(
    tmp_path: Path,
) -> None:
    """One mistake, one finding: ``app_path`` is set, ``admin_url`` is not.

    Warnings count too: the health-check warning read the broken env as empty
    the same way.
    """
    path = tmp_path / "fraises.yaml"
    path.write_text(_ADMIN_URL_MISSING)
    failed = [
        r.name
        for r in ValidationRunner(FraisierConfig(str(path))).run_all()
        if not r.passed and not r.name.startswith("user_")
    ]
    assert failed == ["section:fraises.my_api.development"]


@pytest.mark.parametrize("path", _EXAMPLES, ids=lambda p: str(p.relative_to(_REPO)))
def test_every_example_deployer_builds(path: Path) -> None:
    """Validation passing is not enough: a deploy starts by building this.

    The ETL example's ``notifications`` once named script paths where the
    dispatcher wants a list of notifier mappings, so building it raised.
    """
    from fraisier.deployers.registry import build_deployer
    from fraisier.runners import LocalRunner

    config = FraisierConfig(str(path))
    built = 0
    for name in config.list_fraises():
        fraise = config.get_fraise(name) or {}
        for env_name in fraise.get("environments") or {}:
            merged = config.get_fraise_environment(name, env_name) or {}
            for job in [None, *(merged.get("jobs") or {})]:
                build_deployer(
                    fraise.get("type"), merged, runner=LocalRunner(), job=job
                )
                built += 1
    assert built > 0
