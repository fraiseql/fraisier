"""The scaffolded database scripts say where libpq takes them.

``backup.sh``, ``db_deploy.sh`` and ``db_reset.sh`` run in whatever shell
starts them, so every ``PG*`` variable there reaches ``pg_dump``,
``confiture`` and ``dropdb``. ``backup.service`` starts ``backup.sh`` with
systemd's environment; a manual run gets the operator's. Each script prints,
on stderr, the target libpq resolves and which part a ``PG*`` variable
decided, never a password. ``db_reset.sh`` drops databases, so it refuses a
variable that could send ``dropdb`` elsewhere, and wants each name typed back.

Exercised by running the rendered scripts against stub clients.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
import yaml

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer

_CONFIG: dict[str, Any] = {
    "name": "acme",
    "scaffold": {"deploy_user": "deployer"},
    "fraises": {
        "api": {
            "type": "api",
            "environments": {
                "development": {
                    "app_path": "/var/www/api",
                    "git_repo": "/var/git/api.git",
                }
            },
        },
        "worker": {
            "type": "api",
            "environments": {
                "development": {
                    "app_path": "/var/www/worker",
                    "git_repo": "/var/git/worker.git",
                }
            },
        },
    },
}

_GUARDED = ("PGHOST", "PGHOSTADDR", "PGPORT", "PGSERVICE", "PGDATABASE")


@pytest.fixture
def scripts(tmp_path):
    cfg = tmp_path / "fraises.yaml"
    out = tmp_path / "out"
    config = {**_CONFIG, "scaffold": {**_CONFIG["scaffold"], "output_dir": str(out)}}
    cfg.write_text(yaml.safe_dump(config))
    ScaffoldRenderer(FraisierConfig(cfg)).render()
    return out


@pytest.fixture
def stubs(tmp_path):
    """pg_dump, dropdb, createdb and uv that record their argv and succeed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    for name in ("pg_dump", "dropdb", "createdb", "uv"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/bash\necho "{name} $*" >> "{calls}"\n')
        stub.chmod(0o755)
    return bin_dir, calls


def _run(script, stubs, tmp_path, *args, stdin="", **pg_env):
    bin_dir, calls = stubs
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "FRAISIER_BACKUP_DIR": str(tmp_path / "backups"),
        **pg_env,
    }
    proc = subprocess.run(
        ["bash", str(script), *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    ran = calls.read_text().splitlines() if calls.exists() else []
    return proc, ran


class TestBackupSh:
    def test_the_target_line_names_what_a_pg_variable_decided(
        self, scripts, stubs, tmp_path
    ):
        proc, _ = _run(
            scripts / "backup.sh",
            stubs,
            tmp_path,
            PGHOSTADDR="192.0.2.1",
            PGPORT="6543",
        )

        assert proc.returncode == 0, proc.stderr
        assert (
            "pg_dump: host=localhost hostaddr=192.0.2.1 (PGHOSTADDR) "
            "port=6543 (PGPORT) user=api db=api"
        ) in proc.stderr.splitlines()

    def test_with_no_pg_variables_it_names_the_defaults(self, scripts, stubs, tmp_path):
        proc, _ = _run(scripts / "backup.sh", stubs, tmp_path)

        assert "pg_dump: host=localhost port=5432 user=worker db=worker" in (
            proc.stderr.splitlines()
        )

    def test_no_password_is_printed(self, scripts, stubs, tmp_path):
        proc, _ = _run(scripts / "backup.sh", stubs, tmp_path, PGPASSWORD="s3cret")

        assert "s3cret" not in proc.stdout + proc.stderr


class TestDbDeploySh:
    def test_it_names_the_pg_variables_confiture_will_read(
        self, scripts, stubs, tmp_path
    ):
        proc, _ = _run(
            scripts / "db_deploy.sh",
            stubs,
            tmp_path,
            "development",
            PGHOST="/run/acme",
            PGUSER="acme_ro",
            PGPASSWORD="s3cret",
        )

        assert (
            "confiture migrate api: the URL in its confiture config decides; "
            "what it leaves out comes from PGHOST=/run/acme PGPASSWORD=(set) "
            "PGUSER=acme_ro"
        ) in proc.stderr.splitlines()
        assert "s3cret" not in proc.stdout + proc.stderr

    def test_with_no_pg_variables_it_says_so(self, scripts, stubs, tmp_path):
        proc, _ = _run(scripts / "db_deploy.sh", stubs, tmp_path, "development")

        assert (
            "confiture migrate api: the URL in its confiture config decides; "
            "no PG* variable is set"
        ) in proc.stderr.splitlines()


class TestDbResetSh:
    def test_the_typed_names_drop_and_name_the_target(self, scripts, stubs, tmp_path):
        proc, ran = _run(
            scripts / "db_reset.sh",
            stubs,
            tmp_path,
            "development",
            stdin="api\nworker\n",
        )

        assert proc.returncode == 0, proc.stderr
        assert "dropdb: host=localhost port=5432 user=postgres db=api" in (
            proc.stderr.splitlines()
        )
        assert [r for r in ran if r.startswith("dropdb")] == [
            "dropdb --if-exists -h localhost -U postgres api",
            "dropdb --if-exists -h localhost -U postgres worker",
        ]

    @pytest.mark.parametrize("var", _GUARDED)
    def test_a_variable_that_could_redirect_dropdb_is_refused(
        self, scripts, stubs, tmp_path, var
    ):
        proc, ran = _run(
            scripts / "db_reset.sh",
            stubs,
            tmp_path,
            "development",
            "--yes=api",
            "--yes=worker",
            **{var: "acme"},
        )

        assert proc.returncode != 0
        assert var in proc.stderr
        assert "FRAISIER_RESET_ALLOW_PG_ENV=1" in proc.stderr
        assert ran == []

    def test_the_refusal_names_every_variable_found(self, scripts, stubs, tmp_path):
        proc, _ = _run(
            scripts / "db_reset.sh",
            stubs,
            tmp_path,
            "development",
            PGPORT="6543",
            PGSERVICE="acme",
        )

        assert "PGPORT PGSERVICE" in proc.stderr

    def test_an_empty_variable_is_still_set(self, scripts, stubs, tmp_path):
        proc, ran = _run(
            scripts / "db_reset.sh",
            stubs,
            tmp_path,
            "development",
            "--yes=api",
            "--yes=worker",
            PGHOST="",
        )

        assert proc.returncode != 0
        assert "PGHOST is set" in proc.stderr
        assert ran == []

    def test_the_override_lets_it_run(self, scripts, stubs, tmp_path):
        proc, ran = _run(
            scripts / "db_reset.sh",
            stubs,
            tmp_path,
            "development",
            "--yes=api",
            "--yes=worker",
            PGPORT="6543",
            FRAISIER_RESET_ALLOW_PG_ENV="1",
        )

        assert proc.returncode == 0, proc.stderr
        assert "dropdb: host=localhost port=6543 (PGPORT) user=postgres db=api" in (
            proc.stderr.splitlines()
        )
        assert len([r for r in ran if r.startswith("dropdb")]) == 2

    def test_a_mistyped_name_refuses_before_any_drop(self, scripts, stubs, tmp_path):
        proc, ran = _run(
            scripts / "db_reset.sh", stubs, tmp_path, "development", stdin="api\nwork\n"
        )

        assert proc.returncode != 0
        assert ran == []

    def test_no_input_refuses(self, scripts, stubs, tmp_path):
        proc, ran = _run(scripts / "db_reset.sh", stubs, tmp_path, "development")

        assert proc.returncode != 0
        assert ran == []

    def test_a_yes_naming_another_database_refuses(self, scripts, stubs, tmp_path):
        proc, ran = _run(
            scripts / "db_reset.sh",
            stubs,
            tmp_path,
            "development",
            "--yes=api",
            "--yes=acme_other",
            stdin="worker\n",
        )

        assert proc.returncode != 0
        assert "acme_other" in proc.stderr
        assert ran == []

    def test_one_yes_still_asks_for_the_other(self, scripts, stubs, tmp_path):
        proc, ran = _run(
            scripts / "db_reset.sh",
            stubs,
            tmp_path,
            "development",
            "--yes=api",
            stdin="worker\n",
        )

        assert proc.returncode == 0, proc.stderr
        assert len([r for r in ran if r.startswith("dropdb")]) == 2

    def test_production_is_still_refused(self, scripts, stubs, tmp_path):
        proc, ran = _run(
            scripts / "db_reset.sh",
            stubs,
            tmp_path,
            "production",
            "--yes=api",
            "--yes=worker",
        )

        assert proc.returncode != 0
        assert ran == []
