"""Tests for fraisier.scheduled_install — the `fraisier scheduled-install` command's pure layers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fraisier.config import FraisierConfig
from fraisier.scheduled_install import (
    ApplyReport,
    ScheduledInstallError,
    ScheduledUnitInstall,
    UnitDiff,
    UnitState,
    apply_unit_diffs,
    classify_unit,
    enumerate_scheduled_units,
)


class FakeRunner:
    """Records every ``run`` call for assertion. Does NOT execute anything.

    The signature mirrors ``runners.CommandRunner`` exactly: it is a structural
    stand-in for that Protocol, so a fake whose ``run`` returns a duck-typed
    object rather than a real ``CompletedProcess`` is drift, not shorthand.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout: int = 300,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _sandbox_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (app_path, src_dir, dest_dir) under tmp_path with src_dir created."""
    app_path = tmp_path / "app"
    src_dir = app_path / "scripts/systemd"
    src_dir.mkdir(parents=True, exist_ok=True)
    dest_dir = tmp_path / "etc-systemd-system"
    dest_dir.mkdir(exist_ok=True)
    return app_path, src_dir, dest_dir


def _make_install(
    tmp_path: Path,
    *,
    unit_name: str = "foo.timer",
    is_timer: bool = True,
    source_content: str | None = None,
    dest_content: str | None = None,
) -> ScheduledUnitInstall:
    """Build a ScheduledUnitInstall pointing at sandboxed source/dest paths.

    Pass ``source_content=None`` to leave the source path missing;
    likewise for ``dest_content``.
    """
    app_path, src_dir, dest_dir = _sandbox_dirs(tmp_path)
    src = src_dir / unit_name
    if source_content is not None:
        src.write_text(source_content)
    dst = dest_dir / unit_name
    if dest_content is not None:
        dst.write_text(dest_content)

    return ScheduledUnitInstall(
        fraise_name="x",
        environment="prod",
        job_name="p",
        unit_name=unit_name,
        is_timer=is_timer,
        source_path=src,
        dest_path=dst,
        app_path=app_path,
    )


def _dest_dir(tmp_path: Path) -> Path:
    """Convenience: same dest_dir _make_install uses, for apply_unit_diffs."""
    return tmp_path / "etc-systemd-system"


def _write_scheduled_yaml(tmp_path: Path) -> Path:
    """Minimal fraises.yaml with one type:scheduled fraise + one job declaring
    both a systemd_service and a systemd_timer.
    """
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(
        """
git:
  provider: github
  github:
    webhook_secret: test-secret

fraises:
  alerter:
    type: scheduled
    description: Long-lock alerter
    environments:
      production:
        app_path: /var/www/api.printoptim.io
        jobs:
          poll:
            name: printoptim-long-lock-alerter
            description: Polls pg_locks every minute
            systemd_service: printoptim-long-lock-alerter.service
            systemd_timer: printoptim-long-lock-alerter.timer
            schedule: "*-*-* *:*:00"
"""
    )
    return cfg


def test_enumerate_scheduled_units_yields_service_and_timer(tmp_path):
    """A type:scheduled fraise with one job declaring both a service and a timer
    yields exactly two ScheduledUnitInstall rows pointing at the consumer's
    scripts/systemd/ directory."""
    cfg = _write_scheduled_yaml(tmp_path)
    config = FraisierConfig(str(cfg))

    units = enumerate_scheduled_units(config, environment="production")

    assert len(units) == 2, f"expected service + timer, got {units!r}"
    assert {u.unit_name for u in units} == {
        "printoptim-long-lock-alerter.service",
        "printoptim-long-lock-alerter.timer",
    }

    by_name = {u.unit_name: u for u in units}
    timer = by_name["printoptim-long-lock-alerter.timer"]
    service = by_name["printoptim-long-lock-alerter.service"]

    assert timer.is_timer is True
    assert service.is_timer is False

    assert timer.fraise_name == "alerter"
    assert timer.environment == "production"
    assert timer.job_name == "poll"

    assert timer.source_path == Path(
        "/var/www/api.printoptim.io/scripts/systemd/printoptim-long-lock-alerter.timer"
    )
    assert timer.dest_path == Path(
        "/etc/systemd/system/printoptim-long-lock-alerter.timer"
    )
    assert isinstance(timer, ScheduledUnitInstall)


def test_enumerate_skips_non_scheduled_fraise_with_jobs(tmp_path):
    """A type:backup fraise (which also uses the jobs.* substructure per
    list_all_deployments) must NOT be enumerated — only type:scheduled."""
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(
        """
git:
  provider: github
  github:
    webhook_secret: test-secret

fraises:
  nightly_backup:
    type: backup
    environments:
      production:
        app_path: /var/www/api
        jobs:
          dump:
            name: api-nightly-backup
            systemd_service: api-nightly-backup.service
            systemd_timer: api-nightly-backup.timer
            schedule: "0 3 * * *"
"""
    )
    config = FraisierConfig(str(cfg))

    units = enumerate_scheduled_units(config, environment="production")

    assert units == []


def test_enumerate_job_without_timer_yields_only_service(tmp_path):
    """A scheduled job that omits systemd_timer (one-shot service, not a timer)
    yields just the .service row."""
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(
        """
git:
  provider: github
  github:
    webhook_secret: test-secret

fraises:
  one_shot:
    type: scheduled
    environments:
      production:
        app_path: /var/www/api
        jobs:
          run:
            name: api-one-shot
            systemd_service: api-one-shot.service
            schedule: "*-*-* *:*:00"
"""
    )
    config = FraisierConfig(str(cfg))

    units = enumerate_scheduled_units(config, environment="production")

    assert len(units) == 1
    assert units[0].unit_name == "api-one-shot.service"
    assert units[0].is_timer is False


def test_enumerate_unknown_environment_returns_empty_list(tmp_path):
    """Asking for an env the fraise doesn't declare returns [] — no exception."""
    cfg = _write_scheduled_yaml(tmp_path)
    config = FraisierConfig(str(cfg))

    units = enumerate_scheduled_units(config, environment="staging")

    assert units == []


def test_enumerate_invalid_unit_name_raises(tmp_path):
    """A systemd_service/systemd_timer that fails validate_service_name must
    raise loudly. Silent skipping would hide config bugs.

    Note: validate_service_name only enforces a charset; it accepts ``..``
    (Phase 03 adds the path-traversal guards that catch that). Here we use a
    name with ``/`` which the validator does reject.
    """
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(
        """
git:
  provider: github
  github:
    webhook_secret: test-secret

fraises:
  bad:
    type: scheduled
    environments:
      production:
        app_path: /var/www/api
        jobs:
          poll:
            name: bad
            systemd_service: "bad/name.service"
            schedule: "*-*-* *:*:00"
"""
    )
    config = FraisierConfig(str(cfg))

    with pytest.raises(ValueError, match="Invalid service name"):
        enumerate_scheduled_units(config, environment="production")


# -- Phase 2: classification -------------------------------------------------


def test_classify_unit_absent(tmp_path):
    """Source present, dest missing → ABSENT, no diff summary."""
    install = _make_install(tmp_path, source_content="[Timer]\n")

    result = classify_unit(install)

    assert result.state is UnitState.ABSENT
    assert result.diff_summary is None
    assert result.install is install


def test_classify_unit_identical(tmp_path):
    """Source and dest byte-equal → IDENTICAL, no diff summary."""
    body = "[Timer]\nOnUnitActiveSec=1h\n"
    install = _make_install(tmp_path, source_content=body, dest_content=body)

    result = classify_unit(install)

    assert result.state is UnitState.IDENTICAL
    assert result.diff_summary is None


def test_classify_unit_drifted(tmp_path):
    """Source and dest differ → DRIFTED with a one-line summary."""
    install = _make_install(
        tmp_path,
        source_content="[Timer]\nOnUnitActiveSec=1h\nPersistent=true\n",
        dest_content="[Timer]\nOnUnitActiveSec=30m\n",
    )

    result = classify_unit(install)

    assert result.state is UnitState.DRIFTED
    assert result.diff_summary is not None
    assert "unit body differs" in result.diff_summary


def test_classify_unit_drifted_summary_counts_lines(tmp_path):
    """The summary line counts added/removed accurately for a known fixture.

    Source has one extra line and one changed line vs dest, so unified_diff
    sees: 2 lines added (the new line + the new value of the changed line),
    1 line removed (the old value of the changed line).
    """
    install = _make_install(
        tmp_path,
        source_content="[Timer]\nOnUnitActiveSec=1h\nPersistent=true\n",
        dest_content="[Timer]\nOnUnitActiveSec=30m\n",
    )

    result = classify_unit(install)

    assert result.diff_summary == "unit body differs (2 lines added, 1 removed)"


def test_classify_unit_missing_source(tmp_path):
    """Source path missing → MISSING_SOURCE (operator error: deploy didn't land?)."""
    install = _make_install(tmp_path, source_content=None, dest_content="[Timer]\n")

    result = classify_unit(install)

    assert result.state is UnitState.MISSING_SOURCE
    assert result.diff_summary is None


def test_classify_unit_is_pure_does_not_mutate_filesystem(tmp_path):
    """classify_unit reads but never writes."""
    install = _make_install(tmp_path, source_content="src", dest_content="dst")
    snapshot = {p: p.read_text() for p in [install.source_path, install.dest_path]}

    classify_unit(install)

    assert install.source_path.read_text() == snapshot[install.source_path]
    assert install.dest_path.read_text() == snapshot[install.dest_path]
    assert list((tmp_path / "etc-systemd-system").iterdir()) == [install.dest_path]
    assert list((tmp_path / "app/scripts/systemd").iterdir()) == [install.source_path]


def test_unit_diff_is_a_dataclass(tmp_path):
    """Quick contract check on the UnitDiff shape — frozen, has the right fields."""
    install = _make_install(tmp_path, source_content="x")
    diff = classify_unit(install)

    assert isinstance(diff, UnitDiff)
    assert diff.install is install
    assert diff.state in UnitState
    with pytest.raises((AttributeError, TypeError)):
        diff.state = UnitState.ABSENT  # ty: ignore[invalid-assignment]


# -- Phase 3: apply (the only write-the-filesystem path) ---------------------


def test_apply_copies_absent_unit_and_enables_timer(tmp_path):
    """ABSENT timer → copy, chmod 0644, daemon-reload, enable --now."""
    install = _make_install(tmp_path, source_content="[Timer]\nOnUnitActiveSec=1h\n")
    diff = classify_unit(install)
    assert diff.state is UnitState.ABSENT
    fake = FakeRunner()

    report = apply_unit_diffs([diff], runner=fake, systemd_dest_dir=_dest_dir(tmp_path))

    assert install.dest_path.read_text() == "[Timer]\nOnUnitActiveSec=1h\n"
    assert install.dest_path.stat().st_mode & 0o777 == 0o644
    assert fake.calls == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "foo.timer"],
    ]
    assert report.reloaded is True
    assert report.written == (install,)
    assert report.enabled_timers == (install,)
    assert report.skipped_identical == ()


def test_apply_absent_service_does_not_enable(tmp_path):
    """ABSENT service (is_timer=False) → copy + reload, but no enable --now.

    Services are not enabled — only timers fire the enable --now step.
    """
    install = _make_install(
        tmp_path,
        unit_name="foo.service",
        is_timer=False,
        source_content="[Service]\nExecStart=/bin/true\n",
    )
    diff = classify_unit(install)
    fake = FakeRunner()

    report = apply_unit_diffs([diff], runner=fake, systemd_dest_dir=_dest_dir(tmp_path))

    assert install.dest_path.exists()
    assert fake.calls == [["systemctl", "daemon-reload"]]
    assert report.enabled_timers == ()


def test_apply_is_idempotent_on_identical(tmp_path):
    """IDENTICAL → zero writes, zero daemon-reload, zero enable --now.

    Core invariant: a re-run after convergence is a complete no-op at the
    filesystem-and-systemctl level.
    """
    body = "[Timer]\nOnUnitActiveSec=1h\n"
    install = _make_install(tmp_path, source_content=body, dest_content=body)
    diff = classify_unit(install)
    assert diff.state is UnitState.IDENTICAL
    pre = install.dest_path.read_text()
    fake = FakeRunner()

    report = apply_unit_diffs([diff], runner=fake, systemd_dest_dir=_dest_dir(tmp_path))

    assert install.dest_path.read_text() == pre
    assert fake.calls == []
    assert report.reloaded is False
    assert report.written == ()
    assert report.skipped_identical == (install,)
    assert report.enabled_timers == ()


def test_apply_two_consecutive_calls_are_idempotent(tmp_path):
    """End-to-end: first call writes; second call is a complete no-op."""
    install = _make_install(tmp_path, source_content="[Timer]\nOnUnitActiveSec=1h\n")
    fake = FakeRunner()

    apply_unit_diffs(
        [classify_unit(install)], runner=fake, systemd_dest_dir=_dest_dir(tmp_path)
    )
    first_call_count = len(fake.calls)

    # Second call: source-side unchanged, dest now equals source.
    report2 = apply_unit_diffs(
        [classify_unit(install)], runner=fake, systemd_dest_dir=_dest_dir(tmp_path)
    )

    assert len(fake.calls) == first_call_count  # no new calls
    assert report2.written == ()
    assert report2.reloaded is False


def test_apply_drifted_without_force_raises(tmp_path):
    """DRIFTED + no force → ScheduledInstallError, no writes, no systemctl calls."""
    install = _make_install(
        tmp_path,
        source_content="new content\n",
        dest_content="old content\n",
    )
    diff = classify_unit(install)
    assert diff.state is UnitState.DRIFTED
    pre_dest = install.dest_path.read_text()
    fake = FakeRunner()

    with pytest.raises(ScheduledInstallError, match="drifted"):
        apply_unit_diffs([diff], runner=fake, systemd_dest_dir=_dest_dir(tmp_path))

    assert install.dest_path.read_text() == pre_dest  # untouched
    assert fake.calls == []


def test_apply_drifted_with_force_overwrites_cleanly_no_backup(tmp_path):
    """DRIFTED + force=True → overwrite, daemon-reload, enable --now, NO backup file."""
    install = _make_install(
        tmp_path,
        source_content="new content\n",
        dest_content="old content\n",
    )
    diff = classify_unit(install)
    fake = FakeRunner()

    report = apply_unit_diffs(
        [diff],
        runner=fake,
        force=True,
        systemd_dest_dir=_dest_dir(tmp_path),
    )

    assert install.dest_path.read_text() == "new content\n"
    # No *.pre-fraisier-* or similar backup file left behind.
    dest_dir_contents = sorted(p.name for p in _dest_dir(tmp_path).iterdir())
    assert dest_dir_contents == ["foo.timer"]
    assert report.reloaded is True
    assert install in report.written


def test_apply_missing_source_raises(tmp_path):
    """MISSING_SOURCE → ScheduledInstallError before any writes."""
    install = _make_install(tmp_path, source_content=None, dest_content="[Timer]\n")
    diff = classify_unit(install)
    assert diff.state is UnitState.MISSING_SOURCE
    fake = FakeRunner()

    with pytest.raises(ScheduledInstallError, match="source not found"):
        apply_unit_diffs([diff], runner=fake, systemd_dest_dir=_dest_dir(tmp_path))

    assert fake.calls == []


def test_apply_rejects_slash_in_unit_name(tmp_path):
    """unit_name containing '/' is rejected before any filesystem mutation."""
    app_path, src_dir, dest_dir = _sandbox_dirs(tmp_path)
    # Manually build install — _make_install would itself reject this name.
    install = ScheduledUnitInstall(
        fraise_name="x",
        environment="prod",
        job_name="p",
        unit_name="bad/name.timer",
        is_timer=True,
        source_path=src_dir / "bad-name.timer",
        dest_path=dest_dir / "bad-name.timer",
        app_path=app_path,
    )
    diff = UnitDiff(install, UnitState.ABSENT, None)
    fake = FakeRunner()

    with pytest.raises(ScheduledInstallError, match="unsafe unit name"):
        apply_unit_diffs([diff], runner=fake, systemd_dest_dir=dest_dir)

    assert fake.calls == []


def test_apply_rejects_double_dot_in_unit_name(tmp_path):
    """unit_name containing '..' is rejected — validate_service_name lets this
    through (its regex accepts dot-runs); apply_unit_diffs catches it."""
    app_path, src_dir, dest_dir = _sandbox_dirs(tmp_path)
    install = ScheduledUnitInstall(
        fraise_name="x",
        environment="prod",
        job_name="p",
        unit_name="..foo.timer",
        is_timer=True,
        source_path=src_dir / "..foo.timer",
        dest_path=dest_dir / "..foo.timer",
        app_path=app_path,
    )
    diff = UnitDiff(install, UnitState.ABSENT, None)
    fake = FakeRunner()

    with pytest.raises(ScheduledInstallError, match="unsafe unit name"):
        apply_unit_diffs([diff], runner=fake, systemd_dest_dir=dest_dir)

    assert fake.calls == []


def test_apply_rejects_symlink_escape_in_source(tmp_path):
    """A hostile worktree where scripts/systemd/foo.timer symlinks outside the
    source root is rejected before copy."""
    app_path, src_dir, dest_dir = _sandbox_dirs(tmp_path)
    outside = tmp_path / "elsewhere" / "evil.timer"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("malicious")
    sym = src_dir / "foo.timer"
    sym.symlink_to(outside)

    install = ScheduledUnitInstall(
        fraise_name="x",
        environment="prod",
        job_name="p",
        unit_name="foo.timer",
        is_timer=True,
        source_path=sym,
        dest_path=dest_dir / "foo.timer",
        app_path=app_path,
    )
    diff = UnitDiff(install, UnitState.ABSENT, None)
    fake = FakeRunner()

    with pytest.raises(ScheduledInstallError, match="escapes"):
        apply_unit_diffs([diff], runner=fake, systemd_dest_dir=dest_dir)

    assert fake.calls == []
    assert not (dest_dir / "foo.timer").exists()


def test_apply_daemon_reload_fires_once_for_multiple_writes(tmp_path):
    """daemon-reload fires *exactly once* per call regardless of write count."""
    install_a = _make_install(
        tmp_path,
        unit_name="a.timer",
        source_content="a\n",
    )
    install_b = _make_install(
        tmp_path,
        unit_name="b.timer",
        source_content="b\n",
    )
    diffs = [classify_unit(install_a), classify_unit(install_b)]
    fake = FakeRunner()

    apply_unit_diffs(diffs, runner=fake, systemd_dest_dir=_dest_dir(tmp_path))

    reload_calls = [c for c in fake.calls if c == ["systemctl", "daemon-reload"]]
    assert len(reload_calls) == 1
    enable_calls = [c for c in fake.calls if c[:3] == ["systemctl", "enable", "--now"]]
    assert {c[3] for c in enable_calls} == {"a.timer", "b.timer"}


def test_apply_report_is_a_frozen_dataclass(tmp_path):
    """ApplyReport contract check."""
    install = _make_install(tmp_path, source_content="x")
    fake = FakeRunner()

    report = apply_unit_diffs(
        [classify_unit(install)], runner=fake, systemd_dest_dir=_dest_dir(tmp_path)
    )

    assert isinstance(report, ApplyReport)
    with pytest.raises((AttributeError, TypeError)):
        report.reloaded = False  # ty: ignore[invalid-assignment]
