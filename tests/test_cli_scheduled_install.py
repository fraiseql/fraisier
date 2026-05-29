"""CliRunner tests for ``fraisier scheduled-install``.

The apply phase is exercised end-to-end with real ``shutil.copy2`` writes
under ``tmp_path`` — but the ``systemctl`` invocations are stubbed via the
``apply_unit_diffs`` ``runner=`` parameter, which the CLI fills in with
``LocalRunner`` in production. To avoid actually calling systemctl during
tests we monkeypatch ``apply_unit_diffs``'s ``runner`` argument by patching
``LocalRunner.run`` itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from fraisier.cli.main import main

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fake_local_runner(monkeypatch):
    """Patch ``LocalRunner.run`` so systemctl invocations don't actually fire.

    Yields the list that records every call argv. The CLI builds a
    ``LocalRunner()`` internally; this fixture intercepts that instance's
    ``run`` method.
    """
    calls: list[list[str]] = []

    def fake_run(self, cmd, **_kwargs):
        calls.append(list(cmd))

        class _Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Completed()

    monkeypatch.setattr("fraisier.runners.LocalRunner.run", fake_run)
    return calls


def _write_scheduled_yaml(
    tmp_path: Path,
    *,
    write_source_files: bool = True,
    dest_files: dict[str, str] | None = None,
) -> Path:
    """Write a minimal fraises.yaml + sandboxed app_path/scripts/systemd/ tree.

    Args:
        write_source_files: when True, creates the timer/service files at
            ``app_path/scripts/systemd/``.
        dest_files: dict of unit_name → content, written to a sandboxed
            ``etc-systemd-system/`` dir under tmp_path.
    """
    app_path = tmp_path / "app"
    src_dir = app_path / "scripts/systemd"
    src_dir.mkdir(parents=True, exist_ok=True)
    if write_source_files:
        (src_dir / "alerter.service").write_text("[Service]\nExecStart=/bin/true\n")
        (src_dir / "alerter.timer").write_text("[Timer]\nOnUnitActiveSec=1h\n")

    dest_dir = tmp_path / "etc-systemd-system"
    dest_dir.mkdir(exist_ok=True)
    for name, content in (dest_files or {}).items():
        (dest_dir / name).write_text(content)

    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(
        f"""
git:
  provider: github
  github:
    webhook_secret: test-secret

fraises:
  alerter:
    type: scheduled
    environments:
      production:
        app_path: {app_path}
        jobs:
          poll:
            name: alerter
            systemd_service: alerter.service
            systemd_timer: alerter.timer
            schedule: "*-*-* *:*:00"
      staging:
        app_path: {app_path}
        jobs:
          poll:
            name: alerter
            systemd_service: alerter.service
            systemd_timer: alerter.timer
            schedule: "*-*-* *:*:00"
"""
    )
    return cfg


@pytest.fixture
def patched_systemd_dest_dir(monkeypatch, tmp_path):
    """Redirect the SYSTEMD_DEST_DIR constant the apply layer uses.

    The CLI doesn't expose --systemd-dest-dir as a flag (production always
    writes to /etc/systemd/system/), so we monkey-patch the module constant
    instead. Also patches ScheduledUnitInstall construction so dest_path uses
    the test dir.
    """
    import fraisier.scheduled_install as si

    dest = tmp_path / "etc-systemd-system"
    dest.mkdir(exist_ok=True)
    monkeypatch.setattr(si, "SYSTEMD_DEST_DIR", dest)
    return dest


def test_dry_run_renders_plan_for_absent_units(
    tmp_path, fake_local_runner, patched_systemd_dest_dir
):
    """--dry-run prints [would copy] / [would run] lines and exits 0 when
    all units are ABSENT and would install cleanly."""
    cfg = _write_scheduled_yaml(tmp_path)

    result = CliRunner().invoke(
        main,
        ["-c", str(cfg), "scheduled-install", "--env", "production", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert result.output, "dry-run must not produce empty output"
    assert result.output.count("[would copy]") == 2  # service + timer
    assert "daemon-reload" in result.output
    assert "enable --now alerter.timer" in result.output
    assert "2 units would be installed, 1 timer enabled." in result.output
    # No actual systemctl calls fired in dry-run.
    assert fake_local_runner == []


def test_dry_run_idempotent_when_units_identical(
    tmp_path, fake_local_runner, patched_systemd_dest_dir
):
    """--dry-run reports 'nothing to do' when source == dest."""
    cfg = _write_scheduled_yaml(
        tmp_path,
        dest_files={
            "alerter.service": "[Service]\nExecStart=/bin/true\n",
            "alerter.timer": "[Timer]\nOnUnitActiveSec=1h\n",
        },
    )

    result = CliRunner().invoke(
        main,
        ["-c", str(cfg), "scheduled-install", "--env", "production", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "Nothing to do" in result.output
    assert fake_local_runner == []


def test_env_required_lists_available(tmp_path, fake_local_runner):
    """Omitting --env exits 2 with the list of envs declared on type:scheduled
    fraises."""
    cfg = _write_scheduled_yaml(tmp_path)

    result = CliRunner().invoke(main, ["-c", str(cfg), "scheduled-install"])

    assert result.exit_code == 2
    stderr = result.stderr
    assert "--env is required" in stderr
    assert "production" in stderr
    assert "staging" in stderr


def test_env_unknown_lists_available(tmp_path, fake_local_runner):
    """Unknown env exits 2 with the list of declared envs."""
    cfg = _write_scheduled_yaml(tmp_path)

    result = CliRunner().invoke(
        main, ["-c", str(cfg), "scheduled-install", "--env", "qa"]
    )

    assert result.exit_code == 2
    assert "qa" in result.stderr
    assert "production" in result.stderr


def test_validate_only_ok_when_identical(
    tmp_path, fake_local_runner, patched_systemd_dest_dir
):
    """--validate-only exits 0 with 'OK' when all dest files match source."""
    cfg = _write_scheduled_yaml(
        tmp_path,
        dest_files={
            "alerter.service": "[Service]\nExecStart=/bin/true\n",
            "alerter.timer": "[Timer]\nOnUnitActiveSec=1h\n",
        },
    )

    result = CliRunner().invoke(
        main,
        [
            "-c",
            str(cfg),
            "scheduled-install",
            "--env",
            "production",
            "--validate-only",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_validate_only_drifted_exits_2(
    tmp_path, fake_local_runner, patched_systemd_dest_dir
):
    """--validate-only exits 2 when any unit is DRIFTED."""
    cfg = _write_scheduled_yaml(
        tmp_path,
        dest_files={
            "alerter.timer": "[Timer]\nOnUnitActiveSec=30m\n",  # differs from source
        },
    )

    result = CliRunner().invoke(
        main,
        [
            "-c",
            str(cfg),
            "scheduled-install",
            "--env",
            "production",
            "--validate-only",
        ],
    )

    assert result.exit_code == 2
    assert "drifted" in result.stderr.lower()


def test_validate_only_missing_source_exits_1(
    tmp_path, fake_local_runner, patched_systemd_dest_dir
):
    """--validate-only exits 1 (operator error) when source files are missing."""
    cfg = _write_scheduled_yaml(tmp_path, write_source_files=False)

    result = CliRunner().invoke(
        main,
        [
            "-c",
            str(cfg),
            "scheduled-install",
            "--env",
            "production",
            "--validate-only",
        ],
    )

    assert result.exit_code == 1
    assert "source files missing" in result.stderr


def test_dry_run_exits_2_when_drifted_without_force(
    tmp_path, fake_local_runner, patched_systemd_dest_dir
):
    """--dry-run shows the plan but exits 2 to mirror the apply-path policy."""
    cfg = _write_scheduled_yaml(
        tmp_path,
        dest_files={"alerter.timer": "drifted"},
    )

    result = CliRunner().invoke(
        main,
        [
            "-c",
            str(cfg),
            "scheduled-install",
            "--env",
            "production",
            "--dry-run",
        ],
    )

    assert result.exit_code == 2, result.output
    assert "[would copy]" in result.output  # plan still rendered


def test_apply_writes_files_and_calls_systemctl(
    tmp_path, fake_local_runner, patched_systemd_dest_dir
):
    """--yes path actually copies files and fires systemctl daemon-reload +
    enable --now for the timer."""
    cfg = _write_scheduled_yaml(tmp_path)

    result = CliRunner().invoke(
        main,
        [
            "-c",
            str(cfg),
            "scheduled-install",
            "--env",
            "production",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    dest = patched_systemd_dest_dir
    assert (dest / "alerter.service").read_text() == "[Service]\nExecStart=/bin/true\n"
    assert (dest / "alerter.timer").read_text() == "[Timer]\nOnUnitActiveSec=1h\n"
    assert ["systemctl", "daemon-reload"] in fake_local_runner
    assert ["systemctl", "enable", "--now", "alerter.timer"] in fake_local_runner


def test_apply_idempotent_on_rerun(
    tmp_path, fake_local_runner, patched_systemd_dest_dir
):
    """Second --yes run with no source changes makes zero systemctl calls."""
    cfg = _write_scheduled_yaml(tmp_path)

    CliRunner().invoke(
        main,
        ["-c", str(cfg), "scheduled-install", "--env", "production", "--yes"],
    )
    first_run_calls = len(fake_local_runner)

    result2 = CliRunner().invoke(
        main,
        ["-c", str(cfg), "scheduled-install", "--env", "production", "--yes"],
    )

    assert result2.exit_code == 0, result2.output
    assert len(fake_local_runner) == first_run_calls  # no new calls
    assert "All 2 unit(s) already in sync" in result2.output


def test_apply_drifted_without_force_exits_2(
    tmp_path, fake_local_runner, patched_systemd_dest_dir
):
    """Drift exits 2; suggests --force; no writes happen."""
    cfg = _write_scheduled_yaml(
        tmp_path,
        dest_files={"alerter.timer": "old\n"},
    )
    pre_content = (patched_systemd_dest_dir / "alerter.timer").read_text()

    result = CliRunner().invoke(
        main,
        ["-c", str(cfg), "scheduled-install", "--env", "production", "--yes"],
    )

    assert result.exit_code == 2, (result.output, result.stderr)
    assert "--force" in result.stderr
    assert (patched_systemd_dest_dir / "alerter.timer").read_text() == pre_content


def test_apply_drifted_with_force_overwrites(
    tmp_path, fake_local_runner, patched_systemd_dest_dir
):
    """--force overwrites the drifted dest, no backup file."""
    cfg = _write_scheduled_yaml(
        tmp_path,
        dest_files={"alerter.timer": "old\n"},
    )

    result = CliRunner().invoke(
        main,
        [
            "-c",
            str(cfg),
            "scheduled-install",
            "--env",
            "production",
            "--yes",
            "--force",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (
        patched_systemd_dest_dir / "alerter.timer"
    ).read_text() == "[Timer]\nOnUnitActiveSec=1h\n"
    # No backup files left behind.
    leftovers = sorted(p.name for p in patched_systemd_dest_dir.iterdir())
    assert leftovers == ["alerter.service", "alerter.timer"]


def test_help_renders(tmp_path):
    """fraisier scheduled-install --help renders the docstring."""
    result = CliRunner().invoke(main, ["scheduled-install", "--help"])

    assert result.exit_code == 0
    assert "scheduled-install" in result.output
    assert "--env" in result.output
    assert "--force" in result.output
    assert "Exit codes" in result.output
