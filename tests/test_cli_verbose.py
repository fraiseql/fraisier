"""Tests for the --verbose / -v global CLI flag (#37)."""

from __future__ import annotations

import logging
from unittest.mock import patch

from click.testing import CliRunner

from fraisier.cli.main import main


def test_verbose_flag_accepted():
    """The --verbose flag should be accepted without error."""
    runner = CliRunner()
    with patch("fraisier.cli.main.get_config", side_effect=FileNotFoundError):
        result = runner.invoke(main, ["--verbose", "--help"])
    assert result.exit_code == 0


def test_short_verbose_flag_accepted():
    """The -v shorthand should be accepted without error."""
    runner = CliRunner()
    with patch("fraisier.cli.main.get_config", side_effect=FileNotFoundError):
        result = runner.invoke(main, ["-v", "--help"])
    assert result.exit_code == 0


def test_verbose_sets_debug_logging():
    """When --verbose is passed, root logger should be set to DEBUG."""
    runner = CliRunner()
    root_logger = logging.getLogger()
    original_level = root_logger.level
    try:
        with patch("fraisier.cli.main.get_config", side_effect=FileNotFoundError):
            result = runner.invoke(main, ["--verbose", "init", "--help"])
        assert result.exit_code == 0
        assert root_logger.level == logging.DEBUG
    finally:
        root_logger.setLevel(original_level)


def test_help_mentions_verbose():
    """The --help output should document the --verbose flag."""
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert "--verbose" in result.output
