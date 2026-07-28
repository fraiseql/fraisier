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
