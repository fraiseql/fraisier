"""Which fraises migrate and which serve (#429, #432).

Nothing used to say either. Every loop walked every fraise, so doctor judged a
drift gate for scheduled fraises that never migrate (#429) and the scaffold
rendered a uvicorn unit on port 8000 for fraises that serve nothing (#432).
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest

from fraisier.config import FraisierConfig
from fraisier.deployers.api import APIDeployer
from fraisier.deployers.registry import FRAISE_TYPES, build_deployer
from fraisier.fraise_roles import fraise_migrates, fraise_serves
from fraisier.runners import LocalRunner

_REPO = Path(__file__).resolve().parent.parent
_PRINTOPTIM = Path.home() / "code" / "printoptim_backend" / "fraises.yaml"

_DATABASE = {"name": "db", "strategy": "migrate"}
_SERVICE = {"exec": "bin/serve"}


def _case(
    fraise_type: str, *, database: bool, service: bool, exec_command: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    fraise: dict[str, Any] = {"type": fraise_type}
    if exec_command:
        fraise["exec_command"] = "bin/legacy-serve"
    env: dict[str, Any] = {"app_path": "/srv/app"}
    if database:
        env["database"] = dict(_DATABASE)
    if service:
        env["service"] = dict(_SERVICE)
    return fraise, env


_MATRIX = list(
    itertools.product(sorted(FRAISE_TYPES), [False, True], [False, True], [False, True])
)


@pytest.mark.parametrize(
    ("fraise_type", "database", "service", "exec_command"), _MATRIX
)
def test_only_an_api_with_a_database_migrates(
    fraise_type: str, database: bool, service: bool, exec_command: bool
) -> None:
    fraise, env = _case(
        fraise_type, database=database, service=service, exec_command=exec_command
    )
    assert fraise_migrates(fraise, env) is (fraise_type == "api" and database)


@pytest.mark.parametrize(
    ("fraise_type", "database", "service", "exec_command"), _MATRIX
)
def test_an_api_or_a_declared_service_serves(
    fraise_type: str, database: bool, service: bool, exec_command: bool
) -> None:
    fraise, env = _case(
        fraise_type, database=database, service=service, exec_command=exec_command
    )
    expected = fraise_type == "api" or service or exec_command
    assert fraise_serves(fraise, env) is expected


def test_an_etl_with_a_database_does_not_migrate() -> None:
    """``ETLDeployer`` stores ``database`` and never runs a migration."""
    assert not fraise_migrates({"type": "etl"}, {"database": dict(_DATABASE)})


def test_an_etl_with_a_fraise_level_exec_command_serves() -> None:
    """``exec_command`` is read at fraise level, never copied into the env."""
    assert fraise_serves({"type": "etl", "exec_command": "bin/serve"}, {})


def test_a_legacy_env_level_exec_command_serves() -> None:
    """``ServiceConfig`` maps a flat env ``exec_command`` onto ``service.exec``."""
    assert fraise_serves({"type": "scheduled"}, {"exec_command": "bin/serve"})


def test_an_empty_service_block_does_not_serve() -> None:
    assert not fraise_serves({"type": "backup"}, {"service": {}})


def _config_paths() -> list[Path]:
    paths = [_REPO / "fraises.yaml"]
    if _PRINTOPTIM.is_file():
        paths.append(_PRINTOPTIM)
    return [p for p in paths if p.is_file()]


def _matrix_config(tmp_path: Path) -> Path:
    lines = ["name: matrix", "fraises:"]
    for fraise_type, database, service, exec_command in _MATRIX:
        name = f"{fraise_type}_{int(database)}{int(service)}{int(exec_command)}"
        lines += [f"  {name}:", f"    type: {fraise_type}"]
        if exec_command:
            lines.append("    exec_command: bin/legacy-serve")
        lines += ["    environments:", "      production:", "        app_path: /srv/a"]
        if database:
            lines += ["        database:", "          name: db"]
        if service:
            lines += ["        service:", "          exec: bin/serve"]
    path = tmp_path / "fraises.yaml"
    path.write_text("\n".join(lines) + "\n")
    return path


def _deployer_migrates(fraise_type: str, env_config: dict[str, Any]) -> bool:
    deployer = build_deployer(fraise_type, env_config, runner=LocalRunner())
    return isinstance(deployer, APIDeployer) and bool(deployer.database_config)


def _assert_doctor_agrees_with_the_deployer(config_path: Path) -> int:
    config = FraisierConfig(config_path)
    pairs = 0
    for name in config.list_fraises():
        fraise = config.get_fraise(name) or {}
        for env_name, raw_env in (fraise.get("environments") or {}).items():
            merged = config.get_fraise_environment(name, env_name) or {}
            assert fraise_migrates(fraise, raw_env or {}) is _deployer_migrates(
                str(fraise.get("type")), merged
            ), f"{config_path}: {name}/{env_name}"
            pairs += 1
    return pairs


@pytest.mark.parametrize("config_path", _config_paths(), ids=str)
def test_the_predicate_agrees_with_the_deployer_on_real_configs(
    config_path: Path,
) -> None:
    """Doctor and deployer must not disagree about who migrates.

    Built from the deployer, not by patching the predicate: inside
    ``APIDeployer`` the type is constant, so a patched predicate would pass
    there vacuously.
    """
    assert _assert_doctor_agrees_with_the_deployer(config_path) > 0


def test_the_predicate_agrees_with_the_deployer_on_every_shape(
    tmp_path: Path,
) -> None:
    assert _assert_doctor_agrees_with_the_deployer(_matrix_config(tmp_path)) == len(
        _MATRIX
    )
