"""Config refuses identities that would make a fraisier unit root (#433, paths 6 and 9).

Defence in depth, never the fix. This validation runs from code the deploy user
can change, on a ``fraises.yaml`` a commit author chose; the root helper's
validator and the root policy are what hold. The last test shows that: a unit
rendered to ``User=root`` with this validation bypassed is still refused.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import yaml

from fraisier.config.loader import FraisierConfig
from fraisier.errors import ValidationError
from fraisier.root_policy import RootPolicy
from fraisier.scaffold.renderer import ScaffoldRenderer
from fraisier.unit_validator import validate_unit
from tests.test_backup_retention_config import REQUIRED, write_entries

if TYPE_CHECKING:
    from pathlib import Path

BASE = """\
name: proj
scaffold:
  deploy_user: deployer
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        systemd_service: api.service
"""


def _load(tmp_path: Path, service: dict) -> FraisierConfig:
    """Load, and read the environment: validation is lazy, per environment."""
    block = textwrap.indent(yaml.safe_dump({"service": service}), " " * 8)
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(BASE + block)
    config = FraisierConfig(str(cfg))
    config.get_fraise_environment("api", "production")
    return config


@pytest.mark.parametrize("key", ["user", "group"])
@pytest.mark.parametrize("value", ["root", "0", "00", 0])
def test_a_root_service_identity_is_refused(tmp_path, key, value):
    with pytest.raises(ValidationError) as raised:
        _load(tmp_path, {key: value})
    assert f"service.{key}" in str(raised.value)
    assert "#433" in str(raised.value)


@pytest.mark.parametrize("key", ["user", "group"])
def test_a_named_service_identity_is_accepted(tmp_path, key):
    _load(tmp_path, {key: "www-data"})


def test_a_root_retention_user_is_refused(tmp_path):
    with pytest.raises(ValidationError) as raised:
        write_entries(tmp_path, {**REQUIRED, "user": "root"}).retain_entries(
            "development"
        )
    assert "retain[0].user: 'root' would run the prune as root" in str(raised.value)


def test_a_numeric_retention_user_is_already_refused(tmp_path):
    """``0`` is not a username at all; the name rule refuses it before #433's."""
    with pytest.raises(ValidationError, match=r"retain\[0\]\.user"):
        write_entries(tmp_path, {**REQUIRED, "user": "0"}).retain_entries("development")


def test_postgres_may_still_prune(tmp_path):
    config = write_entries(tmp_path, {**REQUIRED, "user": "postgres"})
    (entry,) = config.retain_entries("development")
    assert entry.user == "postgres"


def test_the_helper_refuses_a_root_unit_that_validation_never_saw(tmp_path):
    """Validation bypassed, the render says User=root, and the unit is refused."""
    with patch(
        "fraisier.config._validation._refuse_root_service_identity", return_value=[]
    ):
        config = _load(tmp_path, {"user": "root", "group": "root"})
        renderer = ScaffoldRenderer(config)
        renderer.output_dir = tmp_path / "out"
        renderer.render()
    unit = (tmp_path / "out" / "systemd" / "api.service").read_text()
    assert "User=root" in unit

    policy = RootPolicy(
        project="proj",
        scaffold_dir="/var/lib/fraisier/proj/scaffold",
        users=frozenset({"deployer", "root"}),
        groups=frozenset({"deployer", "root"}),
        exec_prefixes=("/var/www/api/",),
        read_paths=frozenset(),
        directories=frozenset(),
    )
    rules = {r.rule for r in validate_unit("api.service", unit, policy)}
    assert {"user", "group"} <= rules
