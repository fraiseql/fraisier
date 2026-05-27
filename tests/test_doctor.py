"""`fraisier doctor` CLI + check registry (#221 bundle B phase 04)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fraisier import doctor
from fraisier.cli.main import main as main_group


def _invoke(args: list[str]):
    runner = CliRunner()
    return runner.invoke(main_group, args, catch_exceptions=False)


class TestCheckRegistry:
    def test_required_checks_registered(self):
        for name in (
            "python_version",
            "fraisier_version",
            "fraises_yaml_loadable",
            "fraises_yaml_resolves",
            "secrets_env_readable",
            "confiture_version",
            "helper_sudoers",
        ):
            assert name in doctor.DOCTOR_CHECKS, f"{name} not in DOCTOR_CHECKS"

    def test_each_check_returns_a_check_result(self):
        # Smoke: every registered check executes against a None config
        # without raising, and returns a CheckResult.
        for entry in doctor.DOCTOR_CHECKS.values():
            result = entry.fn(None)
            assert isinstance(result, doctor.CheckResult)
            assert result.status in {"pass", "warn", "fail", "skip"}


class TestRunAll:
    def test_one_failing_check_does_not_abort_others(self, monkeypatch):
        called: list[str] = []

        def _boom(_config):
            called.append("boom")
            raise RuntimeError("nope")

        def _ok(_config):
            called.append("ok")
            return doctor.CheckResult("ok", "pass", "all good")

        monkeypatch.setattr(
            doctor,
            "DOCTOR_CHECKS",
            {
                "boom": doctor._CheckEntry(fn=_boom, network=False),
                "ok": doctor._CheckEntry(fn=_ok, network=False),
            },
        )

        results = doctor.run_all(None)
        assert called == ["boom", "ok"]
        statuses = {r.name: r.status for r in results}
        assert statuses["boom"] == "fail"
        assert statuses["ok"] == "pass"

    def test_skip_network_marks_network_checks_as_skip(self, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "DOCTOR_CHECKS",
            {
                "net": doctor._CheckEntry(
                    fn=lambda _c: doctor.CheckResult("net", "pass", "ran"),
                    network=True,
                ),
                "local": doctor._CheckEntry(
                    fn=lambda _c: doctor.CheckResult("local", "pass", "ran"),
                    network=False,
                ),
            },
        )
        results = doctor.run_all(None, skip_network=True)
        statuses = {r.name: r.status for r in results}
        assert statuses["net"] == "skip"
        assert statuses["local"] == "pass"

    def test_only_filter(self, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "DOCTOR_CHECKS",
            {
                "a": doctor._CheckEntry(
                    fn=lambda _c: doctor.CheckResult("a", "pass", "a"),
                    network=False,
                ),
                "b": doctor._CheckEntry(
                    fn=lambda _c: doctor.CheckResult("b", "pass", "b"),
                    network=False,
                ),
            },
        )
        results = doctor.run_all(None, only=["a"])
        assert {r.name for r in results} == {"a"}


class TestSecretsCheck:
    def test_skip_when_secrets_env_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        result = doctor._check_secrets_env_readable(None)
        assert result.status == "skip"

    def test_fail_when_mode_too_open(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        secrets = tmp_path / ".config" / "fraisier" / "secrets.env"
        secrets.parent.mkdir(parents=True)
        secrets.write_text("X=y\n")
        secrets.chmod(0o644)
        result = doctor._check_secrets_env_readable(None)
        assert result.status == "fail"
        assert "0o644" in result.detail
        assert result.fix_hint is not None

    def test_pass_when_mode_is_0600(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        secrets = tmp_path / ".config" / "fraisier" / "secrets.env"
        secrets.parent.mkdir(parents=True)
        secrets.write_text("X=y\n")
        secrets.chmod(0o600)
        result = doctor._check_secrets_env_readable(None)
        assert result.status == "pass"


class TestCli:
    def test_doctor_runs_all_checks(self):
        r = _invoke(["doctor", "--skip-network"])
        # Even if some checks fail (e.g., no fraises.yaml), the command
        # itself runs successfully and prints a summary line.
        assert "pass" in r.output  # the summary words always appear
        for name in (
            "python_version",
            "fraisier_version",
            "fraises_yaml_loadable",
        ):
            assert name in r.output

    def test_doctor_check_filter(self):
        r = _invoke(["doctor", "--check", "python_version"])
        assert "python_version" in r.output
        assert "fraisier_version" not in r.output

    def test_doctor_json_format(self):
        r = _invoke(["doctor", "--format", "json", "--skip-network"])
        payload = json.loads(r.output)
        assert "checks" in payload
        assert "summary" in payload
        assert "fraisier_version" in payload
        names = {c["name"] for c in payload["checks"]}
        assert "python_version" in names

    def test_exit_code_zero_when_all_pass(self):
        r = _invoke(["doctor", "--check", "python_version"])
        # python_version always passes on this CI matrix.
        assert r.exit_code == 0
