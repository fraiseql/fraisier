"""``fraisier backup prune --pre-migrate FRAISE -e ENV`` (#420).

``pre_migrate_dump`` pruned only inside a deploy, so a quiet week left the whole
corpus on disk. This is the prune without the deploy: the *same* call the gate
makes (``prune_pre_migrate_corpus``), driven from the gate's own config.

It is deliberately not a doctor check — doctor reports, this deletes — and its
"nothing to do" cases are errors where the config is wrong and an exit 0 only
where the project said it wants no gate.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from fraisier.cli.main import main
from fraisier.dbops.archive import ArchiveCheck, ArchiveVerdict
from fraisier.errors import DeploymentLockError


@pytest.fixture(autouse=True)
def dumps_are_valid():
    """The fixtures write ``x``, not a pg_dump archive; say what they are."""
    with patch(
        "fraisier.dbops.backup.verify_archive",
        side_effect=lambda _p: ArchiveCheck(ArchiveVerdict.VALID, ""),
    ):
        yield


@pytest.fixture
def runner():
    return CliRunner()


def make_corpus(directory: Path, **ages: float) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, hours in ages.items():
        path = directory / f"{name}.dump"
        path.write_text("x")
        when = time.time() - hours * 3600
        os.utime(path, (when, when))
    return directory


def write_config(tmp_path: Path, gate: dict | None, *, external: bool = False) -> Path:
    database: dict = {"strategy": "apply", "name": "api"}
    if gate is not None:
        database["pre_migrate_dump"] = gate
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "project": {"name": "my-project"},
                "scaffold": {"deploy_user": "fraisier"},
                "fraises": {
                    "api": {
                        "type": "api",
                        "environments": {
                            "production": {
                                "app_path": "/var/app/api",
                                "git_repo": "/srv/git/api.git",
                                "database": database,
                            }
                        },
                    }
                },
            }
        )
    )
    return cfg


def prune(runner, cfg: Path, *args: str):
    return runner.invoke(
        main,
        [
            "-c",
            str(cfg),
            "backup",
            "prune",
            "--pre-migrate",
            "api",
            "-e",
            "production",
            *args,
        ],
    )


class TestPrune:
    def test_a_quiet_week_is_pruned_without_a_deploy(self, runner, tmp_path):
        """The case #420 is about: every dump past its cutoff, nothing deploying."""
        out = make_corpus(tmp_path / "gate", newest=100, old=150, older=200)
        cfg = write_config(
            tmp_path, {"enabled": True, "output_dir": str(out), "retention_hours": 72}
        )

        result = prune(runner, cfg)

        assert result.exit_code == 0, result.output
        assert (out / "newest.dump").exists()
        assert not (out / "old.dump").exists()
        assert not (out / "older.dump").exists()

    def test_the_newest_dump_survives_an_all_expired_corpus(self, runner, tmp_path):
        """``keep_minimum=1`` is not configuration and a timer cannot relax it."""
        out = make_corpus(tmp_path / "gate", only=500)
        cfg = write_config(
            tmp_path, {"enabled": True, "output_dir": str(out), "retention_hours": 1}
        )

        result = prune(runner, cfg)

        assert result.exit_code == 0, result.output
        assert (out / "only.dump").exists()

    def test_keep_last_alone_prunes(self, runner, tmp_path):
        out = make_corpus(tmp_path / "gate", a=1, b=2, c=3)
        cfg = write_config(
            tmp_path, {"enabled": True, "output_dir": str(out), "keep_last": 1}
        )

        prune(runner, cfg)

        assert [p.name for p in out.iterdir()] == ["a.dump"]

    def test_dry_run_lists_and_removes_nothing(self, runner, tmp_path):
        out = make_corpus(tmp_path / "gate", newest=100, old=150)
        cfg = write_config(
            tmp_path, {"enabled": True, "output_dir": str(out), "retention_hours": 72}
        )

        result = prune(runner, cfg, "--dry-run")

        assert result.exit_code == 0
        assert "old.dump" in result.output
        assert (out / "old.dump").exists()

    def test_json_is_the_only_thing_on_stdout(self, runner, tmp_path):
        out = make_corpus(tmp_path / "gate", newest=100, old=150)
        cfg = write_config(
            tmp_path, {"enabled": True, "output_dir": str(out), "retention_hours": 72}
        )

        result = prune(runner, cfg, "--json")

        report = json.loads(result.stdout)
        (entry,) = report["entries"]
        assert [Path(p).name for p in entry["removed"]] == ["old.dump"]
        assert entry["keep_minimum"] == 1
        assert report["environment"] == "production"

    def test_a_stalled_producer_is_warned_about_on_stderr(self, runner, tmp_path):
        """Everything past its window: only the floor holds the corpus open."""
        out = make_corpus(tmp_path / "gate", only=500)
        cfg = write_config(
            tmp_path, {"enabled": True, "output_dir": str(out), "retention_hours": 1}
        )

        result = prune(runner, cfg)

        assert "keep_minimum=1" in result.stderr


class TestRefusals:
    def test_no_retention_rule_is_an_error_not_a_quiet_zero(self, runner, tmp_path):
        """A timer that exits 0 having pruned nothing looks like one that worked."""
        out = make_corpus(tmp_path / "gate", a=1)
        cfg = write_config(tmp_path, {"enabled": True, "output_dir": str(out)})

        result = prune(runner, cfg)

        assert result.exit_code == 1
        assert "retention_hours" in result.output + result.stderr

    def test_a_disabled_gate_has_nothing_to_do_and_exits_zero(self, runner, tmp_path):
        cfg = write_config(
            tmp_path, {"enabled": False, "output_dir": str(tmp_path), "keep_last": 1}
        )

        result = prune(runner, cfg)

        assert result.exit_code == 0
        assert "nothing to do" in result.output.lower()

    def test_no_gate_at_all_has_nothing_to_do(self, runner, tmp_path):
        result = prune(runner, write_config(tmp_path, None))

        assert result.exit_code == 0

    def test_a_missing_output_dir_is_an_error(self, runner, tmp_path):
        """Pruning a path that is not there reports success every night."""
        cfg = write_config(
            tmp_path,
            {"enabled": True, "output_dir": str(tmp_path / "nope"), "keep_last": 1},
        )

        result = prune(runner, cfg)

        assert result.exit_code == 1
        assert "not a directory" in result.output + result.stderr

    def test_an_unknown_fraise_is_an_error(self, runner, tmp_path):
        cfg = write_config(tmp_path, None)

        result = runner.invoke(
            main,
            [
                "-c",
                str(cfg),
                "backup",
                "prune",
                "--pre-migrate",
                "nope",
                "-e",
                "production",
            ],
        )

        assert result.exit_code == 1

    def test_it_cannot_be_combined_with_a_retain_entry_name(self, runner, tmp_path):
        result = prune(runner, write_config(tmp_path, None), "--name", "x")

        assert result.exit_code != 0


class TestLocking:
    def test_it_holds_the_per_fraise_deployment_lock(self, runner, tmp_path):
        out = make_corpus(tmp_path / "gate", a=1)
        cfg = write_config(
            tmp_path, {"enabled": True, "output_dir": str(out), "keep_last": 1}
        )
        with patch("fraisier.locking.deployment_lock") as lock:
            prune(runner, cfg)

        lock.assert_called_once_with("api")

    def test_a_held_lock_skips_and_exits_zero_and_deletes_nothing(
        self, runner, tmp_path
    ):
        """A gate may be mid-dump: its newest dump is still being written."""
        out = make_corpus(tmp_path / "gate", a=1, b=2, c=3)
        cfg = write_config(
            tmp_path, {"enabled": True, "output_dir": str(out), "keep_last": 1}
        )
        with patch(
            "fraisier.locking.deployment_lock", side_effect=DeploymentLockError("busy")
        ):
            result = prune(runner, cfg)

        assert result.exit_code == 0
        assert "Skipping" in result.output
        assert len(list(out.iterdir())) == 3

    def test_dry_run_does_not_take_the_lock(self, runner, tmp_path):
        out = make_corpus(tmp_path / "gate", a=1)
        cfg = write_config(
            tmp_path, {"enabled": True, "output_dir": str(out), "keep_last": 1}
        )
        with patch("fraisier.locking.deployment_lock") as lock:
            prune(runner, cfg, "--dry-run")

        lock.assert_not_called()


class TestTheSameCallAsTheGate:
    def test_the_command_prunes_through_the_shared_helper(self, runner, tmp_path):
        out = make_corpus(tmp_path / "gate", a=1)
        gate = {"enabled": True, "output_dir": str(out), "keep_last": 1}
        cfg = write_config(tmp_path, gate)
        with patch("fraisier.dbops.backup.prune_pre_migrate_corpus") as helper:
            helper.return_value = None
            prune(runner, cfg)

        (call,) = helper.call_args_list
        assert call.args[0]["output_dir"] == str(out)
        assert call.args[0]["keep_last"] == 1
