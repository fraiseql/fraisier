"""A confiture child reads its URLs from its config, and no ambient ``PG*``.

``confiture migrate``/``rebuild``/``status``, the drift gate's ``migrate
validate`` and restore step 8's ``migrate preflight`` connect to whatever the
config names, the drift child to its ``scratch_url`` too. They inherited all
of ``os.environ``, so libpq filled every keyword those URLs leave out from the
deploy user's environment. They now get ``scrubbed(os.environ)``: never one
URL's ``child_env()``, which would fill the scratch connection with the
target's host and password. Each URL fraisier can read is refused as a child
of its own would be.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.dbops import confiture as dbops_confiture
from fraisier.dbops.drift import check_schema_drift
from fraisier.dbops.template_rule import read_pending_preflight

#: What a deploy user's shell might export, none of which a child may see.
AMBIENT = {
    "PGOPTIONS": "-c search_path=acme_ambient",
    "PGPORT": "5999",
    "PGUSER": "acme_ambient",
    "PGHOST": "/tmp/acme_ambient",
    "PGPASSWORD": "acme_ambient",
}


def _proc(stdout: str = "{}") -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr="")


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "db" / "environments" / "production.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("name: production\ndatabase_url: postgresql:///acme_db\n")
    return path


@pytest.fixture
def ambient(monkeypatch) -> None:
    for var, value in AMBIENT.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setenv("PGAPPNAME", "acme_operator")


def _wrappers(config: Path):
    cwd = str(config.parent)
    return {
        "migrate": lambda: dbops_confiture.confiture_migrate(
            config_path=config.name, cwd=cwd
        ),
        "rebuild": lambda: dbops_confiture.confiture_rebuild(
            config_path=config.name, cwd=cwd
        ),
        "status": lambda: dbops_confiture.confiture_status(
            config_path=config.name, cwd=cwd
        ),
        "preflight": lambda: read_pending_preflight(
            Path(config.name), Path("db/migrations"), project_dir=config.parent
        ),
    }


WRAPPERS = ["migrate", "rebuild", "status", "preflight"]


@pytest.mark.usefixtures("ambient")
@pytest.mark.parametrize("site", WRAPPERS)
def test_a_wrapper_child_sees_no_ambient_pg(config, site):
    with patch("subprocess.run", return_value=_proc()) as run:
        _wrappers(config)[site]()

    env = run.call_args.kwargs["env"]
    assert env is not None
    assert not set(AMBIENT) & set(env)
    assert env["PGAPPNAME"] == "acme_operator"
    assert env["PATH"]


@pytest.mark.parametrize("site", WRAPPERS)
def test_an_ambient_service_is_refused_before_the_launch(config, monkeypatch, site):
    monkeypatch.setenv("PGSERVICE", "acme")
    with patch("subprocess.run", return_value=_proc()) as run:
        outcome = _wrappers(config)[site]()

    run.assert_not_called()
    assert "PGSERVICE" in str(outcome)


@pytest.mark.parametrize("site", WRAPPERS)
@pytest.mark.parametrize(
    "line",
    [
        "database_url: postgresql:///acme_db?keepalives=1",
        "scratch_url: postgresql://db.acme.test/scratch?keepalives=1",
    ],
)
def test_a_config_url_fraisier_refuses_is_not_launched(config, site, line):
    config.write_text(f"name: production\n{line}\n")
    with patch("subprocess.run", return_value=_proc()) as run:
        outcome = _wrappers(config)[site]()

    run.assert_not_called()
    assert "keepalives" in str(outcome)


@pytest.mark.parametrize("site", WRAPPERS)
@pytest.mark.parametrize("key", ["database_url", "scratch_url"])
def test_a_tcp_url_would_lose_an_ambient_transport_policy(
    config, monkeypatch, site, key
):
    """``scrubbed`` drops ``PGSSLMODE``; a TCP URL the child reads must not
    then connect without the policy the operator exported."""
    other = "database_url: postgresql:///acme_db\n" if key == "scratch_url" else ""
    config.write_text(f"name: production\n{other}{key}: postgresql://db.acme.test/x\n")
    monkeypatch.setenv("PGSSLMODE", "verify-full")
    with patch("subprocess.run", return_value=_proc()) as run:
        outcome = _wrappers(config)[site]()

    run.assert_not_called()
    assert "sslmode=verify-full" in str(outcome)


@pytest.mark.usefixtures("ambient")
def test_a_config_fraisier_cannot_read_is_left_to_the_child(tmp_path):
    """confiture reports a missing or broken config better than a guess."""
    with patch("subprocess.run", return_value=_proc()) as run:
        dbops_confiture.confiture_status(config_path="missing.yaml", cwd=str(tmp_path))

    assert not set(AMBIENT) & set(run.call_args.kwargs["env"])


@pytest.mark.usefixtures("ambient")
def test_the_drift_gate_children_see_no_ambient_pg(config):
    clean = {"check": "live_drift", "has_drift": False, "drift_items": []}
    envs = []

    def run(cmd, **kwargs):
        envs.append((cmd[1], kwargs.get("env")))
        if cmd[1] == "build":
            Path(cmd[cmd.index("--output") + 1]).write_text("CREATE TABLE x")
            return _proc(json.dumps({"status": "success"}))
        return _proc(json.dumps(clean))

    with patch("subprocess.run", side_effect=run):
        check_schema_drift(
            project_dir=config.parents[2],
            confiture_config=config,
            checks=["live-drift"],
        )

    validate = [env for verb, env in envs if verb == "migrate"]
    assert validate
    assert all(env is not None and not set(AMBIENT) & set(env) for env in validate)


def test_the_drift_gate_refuses_a_refused_scratch_url(config, monkeypatch):
    config.write_text(
        "name: production\ndatabase_url: postgresql:///acme_db\n"
        "scratch_url: postgresql://db.acme.test/scratch\n"
    )
    monkeypatch.setenv("PGSSLMODE", "verify-full")
    with patch("subprocess.run") as run:
        result = check_schema_drift(
            project_dir=config.parents[2],
            confiture_config=config,
            checks=["live-drift"],
        )

    run.assert_not_called()
    assert not result.passed
    assert "sslmode=verify-full" in str(result.error)


def test_an_ambient_service_is_refused_without_a_readable_config(tmp_path, monkeypatch):
    monkeypatch.setenv("PGSERVICE", "acme")
    with patch("subprocess.run") as run:
        result = dbops_confiture.confiture_migrate(
            config_path="missing.yaml", cwd=str(tmp_path)
        )

    run.assert_not_called()
    assert "PGSERVICE" in result.error
