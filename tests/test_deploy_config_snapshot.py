"""One config snapshot per deploy (#376).

A deploy checks out a commit, syncs that commit's ``fraises.yaml`` to the
server-side config path and regenerates the units from it. Everything the
deploy does afterwards must read the same file, or the units describe one
configuration while the deploy runs another.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

from fraisier.deployers.api import APIDeployer

if TYPE_CHECKING:
    from pathlib import Path


def _write_server_config(path: Path, *, body: str) -> None:
    """Write a fraises.yaml holding a single api fraise's production env.

    *body* is the production environment's mapping, at any consistent
    indentation; it is re-indented to sit under ``production:``.
    """
    header = (
        "fraises:\n"
        "  my_api:\n"
        "    type: api\n"
        "    description: Test API service\n"
        "    environments:\n"
        "      production:\n"
    )
    path.write_text(header + textwrap.indent(textwrap.dedent(body), " " * 8))


@pytest.fixture
def server_config(tmp_path, monkeypatch):
    """Point ``_opt_config_path()`` at a writable tmp fraises.yaml."""
    path = tmp_path / "fraises.yaml"
    monkeypatch.setenv("FRAISIER_CONFIG", str(path))
    return path


def _deployer(stale: dict) -> APIDeployer:
    """An APIDeployer holding a pre-checkout config dict, as callers pass it."""
    return APIDeployer(
        {
            "fraise_name": "my_api",
            "environment": "production",
            "app_path": "/var/www/api",
            "deploy_user": "deployer",
            **stale,
        }
    )


class TestConfigSnapshotFollowsTheSyncedFile:
    """The deploy reads what the scaffold rendered from."""

    def test_changed_pre_migrate_dump_output_dir_reaches_the_strategy(
        self, server_config
    ):
        """#376: the dump goes where the installed unit says it goes.

        The reported failure: a deploy moving ``output_dir`` onto a filesystem
        with room installed the new path into the unit and wrote its dump to
        the old one, in the same run.
        """
        deployer = _deployer(
            {
                "database": {
                    "strategy": "migrate",
                    "name": "printoptim",
                    "pre_migrate_dump": {
                        "enabled": True,
                        "output_dir": "/var/backups/printoptim/pre_migrate",
                    },
                }
            }
        )
        _write_server_config(
            server_config,
            body="""\
            app_path: /var/www/api
            database:
              strategy: migrate
              name: printoptim
              pre_migrate_dump:
                enabled: true
                output_dir: /var/lib/postgresql/pre_migrate
            """,
        )

        deployer._refresh_config_from_synced_file()

        assert (
            deployer.database_config["pre_migrate_dump"]["output_dir"]
            == "/var/lib/postgresql/pre_migrate"
        )

    def test_a_newly_added_database_block_is_not_skipped(self, server_config):
        """A deploy that *adds* a database block must run migrations.

        ``execute()`` gates migrations on ``if self.database_config``. Read
        from the pre-checkout dict that is ``{}``, so the whole step vanished
        without a word.
        """
        deployer = _deployer({})
        assert deployer.database_config == {}

        _write_server_config(
            server_config,
            body="""\
            app_path: /var/www/api
            database:
              strategy: migrate
              name: printoptim
            """,
        )

        deployer._refresh_config_from_synced_file()

        assert deployer.database_config, "migrations would have been skipped"
        assert deployer.database_config["strategy"] == "migrate"

    def test_changed_install_command_is_the_one_executed(self, server_config):
        """#279 re-bakes the allowlist from the new config; run the new command.

        Same shape as #376: the allowlist on disk was rebuilt from the synced
        file while ``install_command`` still held the pre-checkout value.
        """
        deployer = _deployer({"install": {"command": ["uv", "sync"]}})
        assert deployer.install_command == ["uv", "sync"]

        _write_server_config(
            server_config,
            body="""\
            app_path: /var/www/api
            install:
              command: ["uv", "sync", "--frozen"]
              user: appuser
            """,
        )

        deployer._refresh_config_from_synced_file()

        assert deployer.install_command == ["uv", "sync", "--frozen"]
        assert deployer.install_user == "appuser"


class TestTheSyncItselfRefreshes:
    """The refresh is wired into the sync, not merely available."""

    def test_sync_config_if_needed_rebinds_from_the_file_it_synced(
        self, server_config, tmp_path, monkeypatch
    ):
        """Step 1.5 syncs the checkout's fraises.yaml; the deploy must follow it.

        The scaffold is regenerated from the synced file in this same call, so
        this is the point where the two snapshots would otherwise diverge.
        """
        app_path = tmp_path / "app"
        app_path.mkdir()
        _write_server_config(
            app_path / "fraises.yaml",
            body="""\
            app_path: /var/www/api
            database:
              strategy: migrate
              name: printoptim
              pre_migrate_dump:
                enabled: true
                output_dir: /var/lib/postgresql/pre_migrate
            """,
        )

        deployer = _deployer(
            {
                "app_path": str(app_path),
                "database": {
                    "strategy": "migrate",
                    "name": "printoptim",
                    "pre_migrate_dump": {
                        "enabled": True,
                        "output_dir": "/var/backups/printoptim/pre_migrate",
                    },
                },
            }
        )
        monkeypatch.setattr(deployer, "_regenerate_scaffold", lambda **_: None)
        monkeypatch.setattr(deployer, "_install_scaffold", lambda **_: None)

        deployer._sync_config_if_needed()

        assert (
            deployer.database_config["pre_migrate_dump"]["output_dir"]
            == "/var/lib/postgresql/pre_migrate"
        )


class TestRunAnchoredStateSurvives:
    """The refresh moves configuration, never this run's identity."""

    def test_checkout_and_identity_keys_are_not_moved(self, server_config):
        """The deploy keeps deploying the tree it checked out.

        Anchors describe *this run*. Taking them from the file would point the
        migration at a tree this deploy never checked out, or run it as a user
        the unit is not running as.
        """
        deployer = _deployer({"branch": "main", "git_commit": "abc1234"})
        _write_server_config(
            server_config,
            body="""\
            app_path: /somewhere/else
            git_repo: /var/repos/other.git
            branch: staging
            deploy_user: someone_else
            """,
        )

        deployer._refresh_config_from_synced_file()

        assert deployer.app_path == "/var/www/api"
        assert deployer.branch == "main"
        assert deployer.config["deploy_user"] == "deployer"
        assert deployer.config["git_commit"] == "abc1234"

    def test_previous_sha_survives_the_refresh(self, server_config):
        """The rollback anchor is not reset.

        ``_init_git_deploy`` sets ``_previous_sha = None``. The refresh runs
        after ``_git_pull`` has set it, so re-running that initializer would
        destroy the only record of what to roll back to.
        """
        deployer = _deployer({})
        deployer._previous_sha = "deadbee"
        _write_server_config(
            server_config,
            body="""\
            app_path: /var/www/api
            """,
        )

        deployer._refresh_config_from_synced_file()

        assert deployer._previous_sha == "deadbee"


class TestTheGateNamesItsDirectoryUpFront:
    """Where the dump is going, said before the bytes land there."""

    def test_dump_gate_logs_the_resolved_output_dir_before_dumping(
        self, tmp_path, caplog, monkeypatch
    ):
        """#376: the effective output_dir appeared only in the success line.

        That line arrives after the dump — fifteen minutes and 3.5 GB later in
        the reported case. An operator watching a deploy that is moving the
        dump directory has no way to see which one it actually picked until
        the disk has already taken the hit.
        """
        import logging

        from fraisier.strategies._core import MigrateStrategy

        output_dir = str(tmp_path / "dumps")
        strategy = MigrateStrategy(
            pre_migrate_dump={"enabled": True, "output_dir": output_dir},
            db_name="printoptim",
        )

        logged_before_dump: list[str] = []

        def _capture_then_fail(**_):
            logged_before_dump.extend(r.getMessage() for r in caplog.records)
            raise AssertionError("stop after the gate has committed to a path")

        monkeypatch.setattr(
            "fraisier.dbops.confiture.has_pending", lambda *_a, **_k: True
        )
        monkeypatch.setattr("fraisier.dbops.backup.run_backup", _capture_then_fail)

        with caplog.at_level(logging.INFO), pytest.raises(AssertionError):
            strategy._run_dump_gate(
                tmp_path / "confiture.yaml",
                migrations_dir=tmp_path,
                db_url="postgresql://localhost/printoptim",
            )

        assert any(output_dir in message for message in logged_before_dump), (
            "the gate never named the directory it was about to write to; "
            f"logged so far: {logged_before_dump}"
        )


class TestRefreshDegradesQuietlyEnough:
    """A refresh that finds nothing is never worse than not refreshing."""

    def test_missing_file_keeps_the_existing_config(self, tmp_path, monkeypatch):
        """No file at the config path: keep what we have, do not raise."""
        monkeypatch.setenv("FRAISIER_CONFIG", str(tmp_path / "absent.yaml"))
        deployer = _deployer({"database": {"strategy": "migrate"}})

        deployer._refresh_config_from_synced_file()

        assert deployer.database_config == {"strategy": "migrate"}

    def test_fraise_absent_from_the_file_keeps_the_existing_config(
        self, server_config, caplog
    ):
        """The (fraise, env) pair is not in the synced file: keep and warn."""
        deployer = _deployer({"database": {"strategy": "migrate"}})
        server_config.write_text(
            textwrap.dedent(
                """\
                fraises:
                  other_api:
                    type: api
                    environments:
                      production:
                        app_path: /var/www/other
                """
            )
        )

        deployer._refresh_config_from_synced_file()

        assert deployer.database_config == {"strategy": "migrate"}
        assert any(r.levelname == "WARNING" for r in caplog.records)
