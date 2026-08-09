"""Is the binary each installed unit names still there? (#351)

A failed `uv tool install --force` can leave the tool venv half-removed — `bin/`
gone, `lib/` intact — so every `~/.local/bin/fraisier*` symlink dangles,
including the one the webhook unit names in `ExecStart=`.

Nothing catches that today. The running process outlives its deleted binary, so
`systemctl is-active`, the health check and the version endpoint all look
normal; the failure surfaces only at the next restart, as 203/EXEC. This check
asks the question directly, and it does not care *how* the host got there — a
failed self-upgrade, a half-finished manual install, a pruned venv all read the
same.
"""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest

from fraisier.doctor import DOCTOR_CHECKS


@pytest.fixture
def unit_dir(tmp_path, monkeypatch):
    d = tmp_path / "systemd"
    d.mkdir()
    monkeypatch.setattr("fraisier.doctor.SYSTEMD_UNIT_DIR", d)
    return d


@pytest.fixture
def bindir(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    return d


def _executable(bindir, name="fraisier-webhook"):
    p = bindir / name
    p.write_text("#!/bin/sh\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def _unit(unit_dir, name, exec_start):
    (unit_dir / name).write_text(
        f"[Unit]\nDescription=x\n\n[Service]\nExecStart={exec_start}\n"
    )


def _run(config=None):
    return DOCTOR_CHECKS["unit_entrypoints"].fn(config)


def _config():
    return SimpleNamespace(deployment=SimpleNamespace(lock_dir="/run/fraisier"))


class TestItIsRegistered:
    def test_the_check_exists(self):
        assert "unit_entrypoints" in DOCTOR_CHECKS


class TestAHealthyHost:
    def test_a_resolvable_entrypoint_passes(self, unit_dir, bindir):
        exe = _executable(bindir)
        _unit(unit_dir, "fraisier-api-webhook.service", str(exe))
        result = _run(_config())
        assert result.status == "pass"

    def test_arguments_after_the_binary_are_ignored(self, unit_dir, bindir):
        exe = _executable(bindir, "fraisier-systemctl-helper")
        _unit(
            unit_dir, "fraisier-api-systemctl-helper.service", f"{exe} --deploy-user x"
        )
        assert _run(_config()).status == "pass"

    @pytest.mark.parametrize("prefix", ["-", "@", "+", "!", "!!", ":"])
    def test_systemd_exec_prefixes_are_stripped(self, unit_dir, bindir, prefix):
        """systemd allows `-`, `@`, `+`, `!`, `!!` and `:` before the path."""
        exe = _executable(bindir)
        _unit(unit_dir, "fraisier-api-webhook.service", f"{prefix}{exe}")
        assert _run(_config()).status == "pass"


class TestTheHalfRemovedVenv:
    def test_a_dangling_entrypoint_fails_and_names_it(self, unit_dir, bindir):
        missing = bindir / "fraisier-webhook"  # never created
        _unit(unit_dir, "fraisier-api-webhook.service", str(missing))
        result = _run(_config())
        assert result.status == "fail"
        assert "fraisier-api-webhook.service" in result.detail
        assert result.fix_hint

    def test_a_present_but_non_executable_entrypoint_fails(self, unit_dir, bindir):
        p = bindir / "fraisier-webhook"
        p.write_text("#!/bin/sh\n")
        p.chmod(p.stat().st_mode & ~stat.S_IXUSR & ~stat.S_IXGRP & ~stat.S_IXOTH)
        _unit(unit_dir, "fraisier-api-webhook.service", str(p))
        assert _run(_config()).status == "fail"

    def test_a_dangling_symlink_fails(self, unit_dir, bindir):
        link = bindir / "fraisier-webhook"
        link.symlink_to(bindir / "gone")
        _unit(unit_dir, "fraisier-api-webhook.service", str(link))
        assert _run(_config()).status == "fail"

    def test_every_broken_unit_is_reported_not_just_the_first(self, unit_dir, bindir):
        _unit(
            unit_dir, "fraisier-api-webhook.service", str(bindir / "fraisier-webhook")
        )
        _unit(unit_dir, "fraisier-api-mcp.service", str(bindir / "fraisier-mcp"))
        result = _run(_config())
        assert result.status == "fail"
        assert "fraisier-api-webhook.service" in result.detail
        assert "fraisier-api-mcp.service" in result.detail


class TestScope:
    def test_a_non_fraisier_binary_is_not_our_business(self, unit_dir):
        _unit(unit_dir, "nginx.service", "/usr/sbin/nginx-does-not-exist")
        assert _run(_config()).status == "skip"

    def test_a_missing_unit_dir_skips(self, tmp_path, monkeypatch):
        monkeypatch.setattr("fraisier.doctor.SYSTEMD_UNIT_DIR", tmp_path / "nope")
        assert _run(_config()).status == "skip"

    def test_no_fraisier_units_is_skip_not_pass(self, unit_dir):
        """A scan that matches nothing must not read as a clean bill of health.

        v0.61.0's rule, applied to the check itself: `pass` here would be
        indistinguishable from "checked, and everything resolved".
        """
        result = _run(_config())
        assert result.status == "skip"
        assert "no" in result.detail.lower()

    def test_no_config_still_runs(self, unit_dir, bindir):
        """This one does not need config — the units on disk are the input."""
        _unit(
            unit_dir, "fraisier-api-webhook.service", str(bindir / "fraisier-webhook")
        )
        assert _run(None).status == "fail"

    def test_an_unparseable_unit_does_not_crash_the_check(self, unit_dir, bindir):
        exe = _executable(bindir)
        (unit_dir / "fraisier-broken.service").write_bytes(b"\xff\xfe not utf-8")
        _unit(unit_dir, "fraisier-api-webhook.service", str(exe))
        assert _run(_config()).status == "pass"


class TestItIsDocumented:
    def test_doctor_md_covers_the_check(self):
        from pathlib import Path

        doc = (
            Path(__file__).resolve().parent.parent / "docs" / "doctor.md"
        ).read_text()
        assert "unit_entrypoints" in doc


class TestTheHelperItself:
    """The parser is the part that can silently match nothing."""

    def test_it_finds_the_exec_start_binary(self):
        from fraisier.doctor import _exec_start_binary

        assert _exec_start_binary("ExecStart=/a/b/fraisier-webhook --x") == (
            "/a/b/fraisier-webhook"
        )

    def test_it_ignores_other_directives(self):
        from fraisier.doctor import _exec_start_binary

        assert _exec_start_binary("ExecStop=/a/b/fraisier-webhook") is None
        assert _exec_start_binary("Environment=X=1") is None

    def test_it_tolerates_whitespace(self):
        from fraisier.doctor import _exec_start_binary

        assert _exec_start_binary("  ExecStart =  /a/fraisier ") == "/a/fraisier"

    def test_an_empty_exec_start_is_not_a_binary(self):
        from fraisier.doctor import _exec_start_binary

        assert _exec_start_binary("ExecStart=") is None


class TestOsAccessIsNotTheOnlyGate:
    def test_running_as_root_does_not_mask_a_missing_file(self, unit_dir, bindir):
        """`os.access(X_OK)` is permissive for root; existence is checked first."""
        _unit(
            unit_dir, "fraisier-api-webhook.service", str(bindir / "fraisier-webhook")
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "access", lambda *_a, **_k: True)
            assert _run(_config()).status == "fail"
