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


class TestInstallCompileBytecodeCheck:
    """`uv sync` without --compile-bytecode leaves the venv uncompiled (#298).

    Since v0.50.1 every app unit sets PYTHONDONTWRITEBYTECODE=1, so nothing
    ever writes the cache at runtime either. Measured on a 49 MB site-packages
    app: ~434 ms of avoidable recompilation on every single start. The two
    settings compose — PYTHONDONTWRITEBYTECODE only blocks *writes*, so a cache
    laid down at install time is still read.
    """

    class _Cfg:
        """Minimal stand-in for FraisierConfig's fraise iteration."""

        def __init__(self, command, *, install_user="appuser"):
            install = {"command": command}
            if install_user:
                install["user"] = install_user
            self._f = {
                "api": {
                    "install": install,
                    "environments": {"production": {"app_path": "/var/www/api"}},
                }
            }

        @property
        def fraises(self):
            return self._f

    def _run(self, cfg):
        return doctor.DOCTOR_CHECKS["install_compile_bytecode"].fn(cfg)

    def test_registered(self):
        assert "install_compile_bytecode" in doctor.DOCTOR_CHECKS

    def test_warns_for_uv_sync_without_the_flag(self):
        result = self._run(self._Cfg(["uv", "sync", "--frozen"]))

        assert result.status == "warn"
        assert "api" in result.detail
        assert result.fix_hint is not None
        assert "--compile-bytecode" in result.fix_hint

    def test_passes_when_the_flag_is_present(self):
        result = self._run(self._Cfg(["uv", "sync", "--frozen", "--compile-bytecode"]))

        assert result.status == "pass"

    def test_skips_non_uv_install_commands(self):
        """poetry/npm/pip say nothing about uv's bytecode behaviour.

        `skip`, not `pass` — nothing was checked, and the doctor framework
        already uses skip for inapplicable checks (see helper_sudoers). Skip
        counts as pass for the exit code.
        """
        for cmd in (
            ["npm", "ci"],
            ["poetry", "install"],
            ["pip", "install", "-e", "."],
        ):
            result = self._run(self._Cfg(cmd))
            assert result.status == "skip", f"{cmd} should not warn: {result.detail}"

    def test_matches_an_absolute_uv_path(self):
        """install.command[0] is often resolved to an absolute path."""
        result = self._run(self._Cfg(["/usr/local/bin/uv", "sync", "--frozen"]))

        assert result.status == "warn"

    def test_skips_uv_subcommands_other_than_sync(self):
        """`uv run` does not create the venv, so it says nothing about .pyc."""
        result = self._run(self._Cfg(["uv", "run", "something"]))

        assert result.status == "skip"

    def test_skips_without_a_config(self):
        assert self._run(None).status == "skip"

    def test_skips_when_no_install_command_configured(self):
        class _Empty:
            def __init__(self):
                self.fraises = {"api": {"environments": {"production": {}}}}

        assert self._run(_Empty()).status == "skip"


class TestInstallCompileBytecodeEnvLevel:
    """`install:` may be set per-environment, overriding the fraise level.

    `renderer.py:372` resolves it as `env_config.get("install") or fraise_install`,
    so a check that only reads the fraise level misses these entirely.
    """

    def _run(self, cfg):
        return doctor.DOCTOR_CHECKS["install_compile_bytecode"].fn(cfg)

    def _cfg(self, fraise_install, env_install):
        class _Cfg:
            def __init__(self):
                self.fraises = {
                    "api": {
                        **({"install": fraise_install} if fraise_install else {}),
                        "environments": {
                            "production": {
                                "app_path": "/var/www/api",
                                **({"install": env_install} if env_install else {}),
                            }
                        },
                    }
                }

        return _Cfg()

    def test_warns_on_env_level_install_command(self):
        result = self._run(
            self._cfg(None, {"command": ["uv", "sync", "--frozen"], "user": "appuser"})
        )

        assert result.status == "warn"
        assert "api" in result.detail

    def test_env_level_overrides_a_compliant_fraise_level(self):
        """Env-level wins, so a compliant fraise default must not mask it."""
        result = self._run(
            self._cfg(
                {"command": ["uv", "sync", "--frozen", "--compile-bytecode"]},
                {"command": ["uv", "sync", "--frozen"]},
            )
        )

        assert result.status == "warn"

    def test_env_level_compliant_overrides_a_warning_fraise_level(self):
        result = self._run(
            self._cfg(
                {"command": ["uv", "sync", "--frozen"]},
                {"command": ["uv", "sync", "--frozen", "--compile-bytecode"]},
            )
        )

        assert result.status == "pass"
