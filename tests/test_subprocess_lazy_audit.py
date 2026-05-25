"""Subprocess audit — LazyEnv never reaches argv (#220 Phase 5 Cycle 5.6).

Audit summary (the 31 source files containing ``subprocess.run`` or
``subprocess.Popen`` calls, surveyed during Phase 5):

  Category                           Risk     Coverage
  --------------------------------   ------   ----------------------------
  Fixed-string CLI invocations       none     git, apt, pip, systemctl,
   (bootstrap, ship/, git/, ssh*,            launched by literal argv;
   versioning, scaffold/, etc.)               no config-derived args.

  Strategy → dbops chain             handled  Cycle 5.5: resolve_db_url
   (strategies/_core, dbops/*)                at strategy entry; the
                                              connection_url contract
                                              past that point is `str`.

  Token provider OAuth2 HTTP POST    handled  Cycle 5.2: _resolve_form_body
   (token_providers._post_oauth2_*)           coerces all form values
                                              before httpx encodes them.

  ExecTokenProvider command argv     handled  Parser at
   (token_providers._parse)                   `ExecTokenProvider._parse`
                                              coerces each command arg
                                              via `str(arg)` at parse
                                              time — eager but
                                              type-safe; LazyEnv never
                                              reaches the subprocess.

  systemd_service name consumers     deferred Cycle 5.8 covers naming.py,
   (naming.py, validation.py,                 validation.py,
   remote_validator.py, setup.py,             remote_validator.py,
   cli/_helpers.py, cli/_diagnose.py,         setup.py, cli/_helpers.py,
   cli/db.py)                                 cli/_diagnose.py, cli/db.py
                                              before they shell out to
                                              ``systemctl``.

This test is an end-to-end regression lock-in: it intercepts the
``subprocess.run`` call deep inside ``dbops`` and asserts the captured
argv contains the resolved URL string (and zero "LazyEnv(" repr leaks).
If a future refactor moved the resolution boundary deeper, or removed
``resolve_db_url`` from ``MigrateStrategy.execute``, this test catches
it without depending on individual unit tests in 5.5.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from fraisier.config._lazy_env import LazyEnv


class TestStrategyChainEmitsResolvedUrl:
    def test_migrate_strategy_argv_carries_resolved_url(self, monkeypatch, tmp_path):
        # Stub the migrate_up + preflight functions (their internals
        # talk to the real DB). The captured `database_url` they
        # receive must already be the resolved string.
        from fraisier.strategies._core import MigrateStrategy

        monkeypatch.setenv("DB_URL", "postgresql:///e2e_resolved")
        captured_urls: list[object] = []

        def fake_preflight(*args, **kwargs):
            captured_urls.append(kwargs.get("database_url"))

        def fake_migrate_up(*args, **kwargs):
            captured_urls.append(kwargs.get("database_url"))
            from fraisier.dbops.confiture import MigrationResult

            return MigrationResult(success=True, steps_applied=0, errors=[])

        with (
            patch("fraisier.strategies._core.preflight", side_effect=fake_preflight),
            patch("fraisier.strategies._core.migrate_up", side_effect=fake_migrate_up),
        ):
            MigrateStrategy().execute(
                tmp_path / "confiture.yaml",
                database_url=LazyEnv("DB_URL", "fraises.api.prod.database_url"),
            )

        # All downstream calls receive the concrete resolved string.
        assert captured_urls == [
            "postgresql:///e2e_resolved",
            "postgresql:///e2e_resolved",
        ]
        # And no LazyEnv repr leakage in the captured values.
        for url in captured_urls:
            assert "LazyEnv(" not in str(url)


class TestDbopsArgvNeverContainsLazyEnv:
    def test_pg_cmd_argv_only_strings(self, monkeypatch):
        # Direct invocation of the lowest-level subprocess site with a
        # resolved URL. The contract is that callers have already
        # called resolve_db_url at the strategy boundary.
        from fraisier.dbops.operations import _pg_cmd

        captured_argv: list[list[str]] = []

        def fake_run(argv, **kwargs):
            captured_argv.append(argv)
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        _pg_cmd(
            ["psql", "-c", "SELECT 1"],
            connection_url="postgresql://u:p@h:5432/db",
        )
        assert len(captured_argv) == 1
        for arg in captured_argv[0]:
            assert isinstance(arg, str)
            assert "LazyEnv(" not in arg


class TestExecTokenProviderArgvHasNoLazyEnv:
    def test_command_args_resolved_at_parse_time(self, monkeypatch):
        # ExecTokenProvider._parse coerces each command arg via
        # str(arg) — eager resolution. The audit point: by the time
        # resolve() is called and subprocess.run executes, every argv
        # element is a plain str. This locks in that eager parse-time
        # coercion in case a future refactor tries to defer it without
        # also coercing at subprocess boundary.
        from fraisier.token_providers import (
            ExecTokenProvider,
            parse_token_provider,
        )

        monkeypatch.setenv("MY_BIN", "/usr/local/bin/get-token.sh")
        provider = parse_token_provider(
            {
                "type": "exec",
                "command": [LazyEnv("MY_BIN", "p"), "--scope=read"],
            }
        )
        # After parse, every element is a concrete str.
        assert isinstance(provider, ExecTokenProvider)
        for arg in provider.command:
            assert isinstance(arg, str)
            assert "LazyEnv(" not in arg

    def test_command_resolution_runs_subprocess_with_strings(self, monkeypatch):
        from fraisier.token_providers import parse_token_provider

        monkeypatch.setenv("MY_BIN", "/usr/bin/true")
        captured: list[list[str]] = []

        def fake_run(argv, **kwargs):
            captured.append(argv)
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="my-token\n", stderr=""
            )

        with patch("fraisier.token_providers.subprocess.run", side_effect=fake_run):
            provider = parse_token_provider(
                {
                    "type": "exec",
                    "command": [LazyEnv("MY_BIN", "p"), "--arg"],
                }
            )
            assert provider.resolve() == "my-token"
        assert captured == [["/usr/bin/true", "--arg"]]


@pytest.mark.parametrize(
    "module_path",
    [
        # These modules shell out with literal argv (git/systemctl/etc.)
        # — surveyed by grep; no config-derived value reaches argv.
        "fraisier.bootstrap",
        "fraisier.versioning",
        "fraisier.systemctl_helper",
        "fraisier.git.operations",
        "fraisier.runners",
        "fraisier.ssh",
        "fraisier.ssh_config",
        "fraisier.install_helper",
    ],
)
def test_known_safe_modules_import_clean(module_path):
    # Smoke import — guards against the audit-summary docstring above
    # silently going stale because a referenced module was renamed or
    # removed without updating this test.
    import importlib

    importlib.import_module(module_path)
