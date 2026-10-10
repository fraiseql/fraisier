"""Tests for __pycache__ prevention in fraisier/__init__.py (#196)."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

_CORE_TEMPLATES = Path(__file__).parent.parent / "fraisier/scaffold/templates/core"

_EXEC_START = re.compile(r"^ExecStart=(\S+)", re.MULTILINE)

# Jinja expressions contain spaces, so they must collapse to a single token
# before the executable can be split off the front of an ExecStart= line.
_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)

# Concrete shell interpreters. Anything else — including a Jinja variable, whose
# value is not knowable here — is treated as Python: this guard fails closed, so
# a unit whose ExecStart cannot be classified must still disable bytecode.
_SHELL_EXEC = re.compile(r"(^|/)(sh|bash|dash|env)$|\.sh$")


def _invokes_python(content: str) -> bool:
    executables = _EXEC_START.findall(_JINJA.sub("VAR", content))
    return any(not _SHELL_EXEC.search(exe) for exe in executables)


def _unit_templates() -> list[Path]:
    units = [
        *sorted(_CORE_TEMPLATES.glob("*.service.j2")),
        _CORE_TEMPLATES / "service.j2",
        _CORE_TEMPLATES / "deploy-service.j2",
    ]
    return [p for p in units if p.is_file()]


def _python_unit_templates() -> list[Path]:
    return [p for p in _unit_templates() if _invokes_python(p.read_text())]


class TestBytecodeDisabled:
    def test_sys_dont_write_bytecode_is_true(self):
        """Importing fraisier sets sys.dont_write_bytecode = True."""
        import fraisier

        assert sys.dont_write_bytecode is True

    def test_env_var_is_set(self):
        """Importing fraisier sets PYTHONDONTWRITEBYTECODE=1 in os.environ."""
        import fraisier

        assert os.environ.get("PYTHONDONTWRITEBYTECODE") == "1"


class TestUnitTemplatesDisableBytecode:
    """Every Python-invoking unit template disables bytecode writing (#292).

    #292 was one unit template that missed the directive every other one had.
    Deriving the list from the templates directory rather than hardcoding it
    means a unit added later is covered on the day it is added.
    """

    def test_the_audit_finds_units(self):
        """Guard the guard: an empty parametrize list would pass vacuously."""
        assert len(_python_unit_templates()) >= 8

    @pytest.mark.parametrize("template", _python_unit_templates(), ids=lambda p: p.name)
    def test_python_units_set_dont_write_bytecode(self, template):
        assert "Environment=PYTHONDONTWRITEBYTECODE=1" in template.read_text()

    def test_only_the_shell_units_are_exempt(self):
        """Pin what the classifier excludes, so a wrong exclusion is visible."""
        covered = {p.name for p in _python_unit_templates()}
        exempt = {p.name for p in _unit_templates()} - covered
        assert exempt == {"backup.service.j2", "backup-alert@.service.j2"}


_PROVIDER_TEMPLATES = (
    Path(__file__).parent.parent / "fraisier/scaffold/templates/provider"
)
_REMOTE_DEBUG = "Environment=PYTHON_DISABLE_REMOTE_DEBUG=1"


class TestUnitTemplatesDisableRemoteDebug:
    """Every Python-invoking unit opts out of PEP 768 remote attach (#436).

    Python 3.14 lets anything that may ptrace a process inject code into it
    without restarting it. Nothing on a deploy host uses that, and the units
    hold database credentials. Same enumeration as the bytecode sweep, so a
    unit added later is covered on the day it is added.
    """

    def test_the_audit_finds_units(self):
        assert len(_python_unit_templates()) >= 8

    @pytest.mark.parametrize("template", _python_unit_templates(), ids=lambda p: p.name)
    def test_python_units_disable_remote_debug(self, template):
        assert _REMOTE_DEBUG in template.read_text()

    def test_the_app_unit_sets_it_before_the_user_environment(self):
        """systemd keeps the last ``Environment=``, so a user's value must come after."""
        text = (_CORE_TEMPLATES / "service.j2").read_text()
        assert text.index(_REMOTE_DEBUG) < text.index("service.environment.items()")

    def test_the_rc_script_exports_it_before_the_user_environment(self):
        text = (_PROVIDER_TEMPLATES / "rc.d.j2").read_text()
        default = 'export PYTHON_DISABLE_REMOTE_DEBUG="1"'
        assert default in text
        assert text.index(default) < text.index("env.items()")


class TestProcessEnvironmentDisablesRemoteDebug:
    """Importing fraisier sets the opt-out for every child it starts (#436).

    A unit's ``Environment=`` covers a unit; a manual ``fraisier deploy`` from a
    shell has none, and its ``confiture`` and ``uv`` children inherit only what
    the process carries.
    """

    def _child_value(self, env: dict[str, str]) -> str:
        import subprocess

        code = (
            "import os, fraisier; "
            "print(repr(os.environ.get('PYTHON_DISABLE_REMOTE_DEBUG')))"
        )
        return subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def test_it_is_set_when_absent(self):
        env = {
            k: v for k, v in os.environ.items() if k != "PYTHON_DISABLE_REMOTE_DEBUG"
        }
        assert self._child_value(env) == "'1'"

    def test_an_operators_value_is_kept(self):
        env = {**os.environ, "PYTHON_DISABLE_REMOTE_DEBUG": "operator"}
        assert self._child_value(env) == "'operator'"
