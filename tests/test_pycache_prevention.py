"""Tests for __pycache__ prevention in fraisier/__init__.py (#196)."""

from __future__ import annotations

import os
import sys


class TestBytecodeDisabled:
    def test_sys_dont_write_bytecode_is_true(self):
        """Importing fraisier sets sys.dont_write_bytecode = True."""
        import fraisier

        assert sys.dont_write_bytecode is True

    def test_env_var_is_set(self):
        """Importing fraisier sets PYTHONDONTWRITEBYTECODE=1 in os.environ."""
        import fraisier

        assert os.environ.get("PYTHONDONTWRITEBYTECODE") == "1"
