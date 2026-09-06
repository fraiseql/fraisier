"""A failing child process reaches the journal with its own explanation (#381).

`LocalRunner.run` defaults to ``check=True``, so a non-zero `fraisier scaffold`
raised `CalledProcessError` *before* the branch that assembles stderr + stdout.
What the deploy journal got, under the line "fix the underlying error below",
was::

    Command '['/usr/bin/fraisier', 'scaffold', ...]' returned non-zero exit
    status 1.

and then nothing. The child's own reason — "environment production resolves to
no host", a YAML error, a permission denial — was discarded.

The tests that covered those branches passed because their `MagicMock` runner
returned ``returncode=1`` **without raising**, a shape `LocalRunner` cannot
produce. These use a real `LocalRunner` and a real failing executable: a stub
may stand in for a process, but it may not return a shape the real boundary
cannot.
"""

from __future__ import annotations

import stat
import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from fraisier.deployers.api import APIDeployer
from fraisier.errors import DeploymentError
from fraisier.runners import LocalRunner

if TYPE_CHECKING:
    from pathlib import Path

STDERR_LINE = "Error: environment production resolves to no host"
STDOUT_LINE = "Generated 3 files"


def _refusing_executable(tmp_path: Path) -> Path:
    """A real program that reports to both streams and exits non-zero."""
    exe = tmp_path / "fraisier-stub"
    exe.write_text(
        f"#!/bin/sh\necho '{STDOUT_LINE}'\necho '{STDERR_LINE}' >&2\nexit 1\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def _deployer(tmp_path: Path) -> APIDeployer:
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    return APIDeployer(
        {"fraise_name": "api", "environment": "production", "app_path": str(app)},
        runner=LocalRunner(),
    )


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "fraises.yaml"
    path.write_text("project_name: p\n")
    return path


class TestTheRealRunnerRaises:
    """The contract the MagicMock stubs contradicted."""

    def test_check_true_raises_instead_of_returning_a_non_zero_result(self, tmp_path):
        exe = _refusing_executable(tmp_path)
        with pytest.raises(subprocess.CalledProcessError):
            LocalRunner().run([str(exe)])

    def test_check_false_returns_the_non_zero_result_with_both_streams(self, tmp_path):
        exe = _refusing_executable(tmp_path)
        result = LocalRunner().run([str(exe)], check=False)
        assert result.returncode == 1
        assert STDOUT_LINE in result.stdout
        assert STDERR_LINE in result.stderr


class TestScaffoldRegeneration:
    def test_a_refusing_render_surfaces_its_stderr(self, tmp_path, config_path):
        deployer = _deployer(tmp_path)
        exe = _refusing_executable(tmp_path)

        with (
            patch.object(deployer, "_get_fraisier_executable", return_value=str(exe)),
            patch.object(
                deployer, "_scaffold_state_dir", return_value=tmp_path / "state"
            ),
            pytest.raises(DeploymentError) as exc,
        ):
            deployer._regenerate_scaffold(config_path=config_path)

        assert STDERR_LINE in str(exc.value)
        assert STDOUT_LINE in str(exc.value)


class TestScaffoldInstall:
    def test_the_subprocess_fallback_surfaces_its_stderr(self, tmp_path, config_path):
        deployer = _deployer(tmp_path)
        exe = _refusing_executable(tmp_path)

        with (
            patch.object(deployer, "_get_fraisier_executable", return_value=str(exe)),
            patch.object(
                deployer, "_scaffold_state_dir", return_value=tmp_path / "state"
            ),
            patch.object(
                deployer, "_try_scaffold_install_via_socket", return_value=None
            ),
            pytest.raises(DeploymentError) as exc,
        ):
            deployer._install_scaffold(config_path=config_path)

        assert STDERR_LINE in str(exc.value)

    def test_the_socket_helper_failure_quotes_its_stderr(self, tmp_path, config_path):
        """The helper's stderr was captured and then dropped from the message."""
        deployer = _deployer(tmp_path)
        helper_reply = SimpleNamespace(
            returncode=1,
            stdout="installing 4 units",
            stderr="install.sh: command not allowed: uv sync --frozen",
        )

        with (
            patch.object(
                deployer, "_try_scaffold_install_via_socket", return_value=helper_reply
            ),
            pytest.raises(DeploymentError) as exc,
        ):
            deployer._install_scaffold(config_path=config_path)

        assert "command not allowed" in str(exc.value)


class TestAnUnsetWebhookSecret:
    """`!envvar` resolution raises `ConfigurationError`, not `ValueError`.

    `_resolve_provider_config` → `to_str` → `LazyEnv.resolve` raises a
    `FrameworkError`, which the `except ValueError` beside it does not catch,
    so FastAPI answered a bare 500 with an empty body on every delivery. Fail
    closed, correctly — but the name of the missing variable was only in a
    traceback (#381).
    """

    def test_it_answers_a_structured_error_and_names_the_variable_in_the_log(
        self, caplog
    ):
        import logging

        from fastapi import HTTPException

        from fraisier import webhook
        from fraisier.config._lazy_env import LazyEnv

        unset = LazyEnv("FRAISIER_WEBHOOK_SECRET", "git.github.webhook_secret")

        def _raise(_raw):
            unset.resolve()

        with (
            patch.object(webhook, "_collect_webhook_secrets", return_value=[None]),
            patch.object(webhook, "get_config", side_effect=FileNotFoundError),
            patch.object(webhook, "_resolve_provider_config", side_effect=_raise),
            caplog.at_level(logging.ERROR),
            pytest.raises(HTTPException) as exc,
        ):
            webhook._verify_signature("github", b"{}", {})

        assert exc.value.status_code == 500
        detail = exc.value.detail
        assert isinstance(detail, dict)
        assert detail["error_type"] == "configuration_error"
        assert detail["recovery_hint"]
        assert "FRAISIER_WEBHOOK_SECRET" not in str(detail)
        assert "FRAISIER_WEBHOOK_SECRET" in caplog.text
