"""``restore.source: pgbackrest`` — the config surface (#424).

A physical restore is cluster-scoped: it replaces *every* database in the staging
cluster with production's, and its roles and password hashes come along. So most
of what this validates is a refusal to be asked something that would destroy
more than the operator meant: a cluster shared with another fraise, a config
that names pgBackRest while the default source is quietly still ``dump``, a
target that is not an unambiguous instant.

``dump`` stays the default, and nothing in an existing config changes.
"""

from __future__ import annotations

import pytest

from fraisier.config._validation import (
    ValidationError,
    validate_one_fraise_environment,
    validate_restore_clusters,
)

PGBACKREST = {"stanza": "main", "repo": 1, "cluster": "18/staging", "target": "latest"}


def _env(restore: dict | None = None, **database: object) -> dict:
    return {
        "database": {
            "strategy": "restore_migrate",
            "name": "app",
            "admin_url": "postgresql://postgres@localhost:5433/postgres",
            "restore": restore,
            **database,
        }
    }


def _pgbackrest(**overrides: object) -> dict:
    return _env({"source": "pgbackrest", "pgbackrest": {**PGBACKREST, **overrides}})


def check(env: dict) -> None:
    validate_one_fraise_environment("api", "staging", env)


class TestTheDefaultIsUnchanged:
    def test_no_source_still_requires_a_backup_dir(self) -> None:
        with pytest.raises(ValidationError, match="backup_dir"):
            check(_env({}))

    def test_a_dump_config_validates_exactly_as_before(self) -> None:
        check(_env({"backup_dir": "/backup"}))
        check(_env({"source": "dump", "backup_dir": "/backup"}))

    def test_an_unknown_source_is_rejected_naming_the_choices(self) -> None:
        with pytest.raises(ValidationError, match=r"source.*dump.*pgbackrest"):
            check(_env({"source": "rsync", "backup_dir": "/backup"}))

    def test_a_pgbackrest_block_under_the_dump_source_is_a_mistake(self) -> None:
        """Believing pgBackRest is on while the dump runs is the silent failure."""
        with pytest.raises(ValidationError, match=r"restore\.source"):
            check(_env({"backup_dir": "/backup", "pgbackrest": PGBACKREST}))


class TestPgbackrest:
    def test_a_complete_block_validates_without_a_backup_dir(self) -> None:
        check(_pgbackrest())

    @pytest.mark.parametrize("missing", ["stanza", "repo", "cluster"])
    def test_stanza_repo_and_cluster_are_required(self, missing: str) -> None:
        block = {k: v for k, v in PGBACKREST.items() if k != missing}
        with pytest.raises(ValidationError, match=missing):
            check(_env({"source": "pgbackrest", "pgbackrest": block}))

    def test_the_block_is_required(self) -> None:
        with pytest.raises(ValidationError, match=r"restore\.pgbackrest"):
            check(_env({"source": "pgbackrest"}))

    def test_an_unknown_key_is_rejected(self) -> None:
        """A misspelt ``targt:`` would silently restore ``latest``."""
        with pytest.raises(ValidationError, match="targt"):
            check(_pgbackrest(targt="2026-01-01 00:00:00+00"))

    def test_it_still_needs_the_database_name(self) -> None:
        env = _pgbackrest()
        del env["database"]["name"]
        with pytest.raises(ValidationError, match=r"database\.name"):
            check(env)

    @pytest.mark.parametrize("bad", ["", "ma in", "main;x", "../x", 7])
    def test_a_stanza_must_be_a_plain_name(self, bad: object) -> None:
        """It becomes an argv element in a root helper's unit file."""
        with pytest.raises(ValidationError, match="stanza"):
            check(_pgbackrest(stanza=bad))

    @pytest.mark.parametrize("bad", [0, 257, True, "1", 1.5])
    def test_a_repo_is_an_integer_from_1_to_256(self, bad: object) -> None:
        with pytest.raises(ValidationError, match="repo"):
            check(_pgbackrest(repo=bad))

    @pytest.mark.parametrize("bad", ["staging", "18", "18/", "18/a b", "/18/x", "x/y"])
    def test_a_cluster_is_version_slash_name(self, bad: str) -> None:
        with pytest.raises(ValidationError, match="cluster"):
            check(_pgbackrest(cluster=bad))

    @pytest.mark.parametrize(
        "good",
        [
            "latest",
            "2026-10-01 14:18:37+00",
            "2026-10-01T14:18:37Z",
            "2026-10-01 14:18:37.5+02:00",
        ],
    )
    def test_a_target_is_latest_or_an_instant_with_an_offset(self, good: str) -> None:
        check(_pgbackrest(target=good))

    @pytest.mark.parametrize(
        "bad",
        ["yesterday", "2026-10-01", "2026-10-01 14:18:37", "latest; rm", "", None, 5],
    )
    def test_a_target_without_an_unambiguous_instant_is_rejected(
        self, bad: object
    ) -> None:
        """No offset reads in the *server's* zone — a different instant per host."""
        with pytest.raises(ValidationError, match="target"):
            check(_pgbackrest(target=bad))

    def test_the_target_defaults_to_latest(self) -> None:
        block = {k: v for k, v in PGBACKREST.items() if k != "target"}
        check(_env({"source": "pgbackrest", "pgbackrest": block}))

    @pytest.mark.parametrize("bad", [0, 59, True, "600", -1])
    def test_a_timeout_is_an_integer_of_at_least_a_minute(self, bad: object) -> None:
        with pytest.raises(ValidationError, match="timeout_seconds"):
            check(_pgbackrest(timeout_seconds=bad))

    def test_a_sane_timeout_is_accepted(self) -> None:
        check(_pgbackrest(timeout_seconds=21600))


def fraises(**envs: dict) -> dict:
    """``{fraise: {environments: {env: config}}}`` from ``fraise_env=config`` pairs."""
    out: dict = {}
    for key, config in envs.items():
        fraise, env = key.split("__")
        out.setdefault(fraise, {"environments": {}})["environments"][env] = config
    return out


def staging(
    cluster: str = "18/staging",
    *,
    server: str | None = None,
    port: int = 5433,
    db: str = "app",
) -> dict:
    env: dict = {
        "database": {
            "strategy": "restore_migrate",
            "name": db,
            "admin_url": f"postgresql://postgres@localhost:{port}/postgres",
            "restore": {
                "source": "pgbackrest",
                "pgbackrest": {**PGBACKREST, "cluster": cluster},
            },
        }
    }
    if server:
        env["server"] = server
    return env


class TestAClusterIsNotShared:
    """A physical restore replaces every database in the cluster."""

    def test_one_fraise_alone_is_fine(self) -> None:
        validate_restore_clusters(fraises(api__staging=staging()))

    def test_two_environments_naming_one_cluster_on_one_host_are_refused(self) -> None:
        with pytest.raises(
            ValidationError, match=r"api/staging.*worker/staging.*18/staging"
        ):
            validate_restore_clusters(
                fraises(api__staging=staging(), worker__staging=staging(port=5434))
            )

    def test_the_same_cluster_name_on_two_hosts_is_two_clusters(self) -> None:
        validate_restore_clusters(
            fraises(
                api__staging=staging(server="a.example.io", port=5433),
                worker__staging=staging(server="b.example.io", port=5433),
            )
        )

    def test_another_database_reached_through_the_same_endpoint_is_refused(
        self,
    ) -> None:
        """Two clusters by name, one by address: the restore would take both databases."""
        other = {
            "database": {
                "strategy": "apply",
                "name": "worker",
                "admin_url": "postgresql://postgres@localhost:5433/postgres",
            }
        }
        with pytest.raises(ValidationError, match="worker"):
            validate_restore_clusters(
                fraises(api__staging=staging(), worker__staging=other)
            )

    def test_a_dump_restore_elsewhere_changes_nothing(self) -> None:
        dump = {
            "database": {
                "strategy": "restore_migrate",
                "name": "worker",
                "admin_url": "postgresql://postgres@localhost:5434/postgres",
                "restore": {"backup_dir": "/backup"},
            }
        }
        validate_restore_clusters(fraises(api__staging=staging(), worker__staging=dump))

    def test_the_same_fraise_in_two_environments_on_one_endpoint_is_refused(
        self,
    ) -> None:
        with pytest.raises(ValidationError):
            validate_restore_clusters(
                fraises(api__staging=staging(), api__qa=staging(db="qa"))
            )

    def test_an_envvar_admin_url_is_not_inspected_so_it_cannot_prove_a_collision(
        self,
    ) -> None:
        from fraisier.config._lazy_env import LazyEnv

        env = staging()
        env["database"]["admin_url"] = LazyEnv("ADMIN_URL")
        validate_restore_clusters(fraises(api__staging=env))
