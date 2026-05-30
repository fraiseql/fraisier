"""Tests for the LLM-native output layer (``fraisier._output``).

The module owns the three output modes (compact/verbose/json), the
``OutputContext`` carried through nested CLI calls, the
``compact``/``verbose``/``success``/``failure`` helpers, the
``tee()`` failure-log infrastructure, and the ``emit_json()`` exit
flush.

Inspired by `rtk-ai/rtk`'s compression strategies; fraisier inverts the
flag polarity so compact is the default.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from fraisier._output import (
    OutputContext,
    OutputMode,
    compact,
    emit_json,
    failure,
    get_context,
    output_context,
    success,
    tee,
    verbose,
)


class TestOutputContext:
    """Cycle 1: ``OutputMode``, ``OutputContext``, ``get_context()``."""

    def test_default_mode_is_compact(self):
        """No active context → compact mode by default."""
        assert get_context().mode is OutputMode.COMPACT

    def test_default_verbosity_is_zero(self):
        assert get_context().verbosity == 0

    def test_default_tee_path_is_none(self):
        assert get_context().tee_path is None

    def test_default_json_buffer_is_none(self):
        assert get_context().json_buffer is None

    def test_output_context_is_frozen(self):
        """OutputContext is a frozen dataclass — immutable by design."""
        ctx = OutputContext(
            mode=OutputMode.COMPACT, verbosity=0, tee_path=None, json_buffer=None
        )
        # Use setattr to bypass ty's static frozen-detection; the runtime
        # FrozenInstanceError is what the test actually asserts on.
        with pytest.raises((AttributeError, Exception)):
            setattr(ctx, "mode", OutputMode.VERBOSE)  # noqa: B010

    def test_output_context_overrides_propagate(self):
        """The ``output_context`` context manager swaps the ContextVar."""
        with output_context(mode=OutputMode.VERBOSE, verbosity=2):
            inner = get_context()
            assert inner.mode is OutputMode.VERBOSE
            assert inner.verbosity == 2
        # Reverts after exit.
        assert get_context().mode is OutputMode.COMPACT


class TestCompactAndVerbose:
    """Cycle 2: ``compact()`` strips markup; ``verbose()`` requires verbose mode."""

    def test_compact_strips_rich_markup(self, capsys):
        with output_context(mode=OutputMode.COMPACT):
            compact("[green]hello[/green]")
        captured = capsys.readouterr()
        assert captured.out == "hello\n"

    def test_compact_passes_plain_text_through(self, capsys):
        with output_context(mode=OutputMode.COMPACT):
            compact("plain text")
        assert capsys.readouterr().out == "plain text\n"

    def test_verbose_silent_under_compact_mode(self, capsys):
        with output_context(mode=OutputMode.COMPACT):
            verbose("[bold]debug only[/bold]")
        assert capsys.readouterr().out == ""

    def test_verbose_prints_under_verbose_mode(self, capsys):
        with output_context(mode=OutputMode.VERBOSE, verbosity=1):
            verbose("hi")
        # Rich renders the line (may add color codes when capsys sees a tty;
        # under pytest's capsys, NO_COLOR is typically respected). Just
        # assert the visible content survives.
        assert "hi" in capsys.readouterr().out

    def test_verbose_respects_level(self, capsys):
        with output_context(mode=OutputMode.VERBOSE, verbosity=1):
            verbose("level1", level=1)
            verbose("level2", level=2)
            verbose("level3", level=3)
        out = capsys.readouterr().out
        assert "level1" in out
        assert "level2" not in out
        assert "level3" not in out

    def test_compact_silent_under_json_mode(self, capsys):
        """JSON mode suppresses text output; final payload comes via emit_json."""
        with output_context(mode=OutputMode.JSON, json_buffer={"events": []}):
            compact("ignored line")
        assert capsys.readouterr().out == ""


class TestSuccessAndFailure:
    """Cycle 3: ``success()`` / ``failure()`` three-mode behaviour."""

    def test_success_compact_prefixes_ok(self, capsys):
        with output_context(mode=OutputMode.COMPACT):
            success("Shipped v1.0.1")
        assert capsys.readouterr().out == "ok Shipped v1.0.1\n"

    def test_success_compact_with_fields(self, capsys):
        with output_context(mode=OutputMode.COMPACT):
            success("deploy", elapsed_s=8.2, version="v1.0.1")
        out = capsys.readouterr().out
        assert out.startswith("ok deploy ")
        assert "elapsed_s=8.2" in out
        assert "version=v1.0.1" in out

    def test_success_verbose_renders_markup(self, capsys):
        with output_context(mode=OutputMode.VERBOSE, verbosity=1):
            success("Shipped v1.0.1")
        assert "Shipped v1.0.1" in capsys.readouterr().out

    def test_success_json_buffers_event(self, capsys):
        buf: dict = {"events": []}
        with output_context(mode=OutputMode.JSON, json_buffer=buf):
            success("Shipped v1.0.1")
        assert capsys.readouterr().out == ""
        assert buf["events"] == [{"status": "ok", "label": "Shipped v1.0.1"}]

    def test_failure_compact_three_line_shape(self, capsys):
        with output_context(mode=OutputMode.COMPACT):
            failure(
                "ship aborted at verify",
                detail="pytest: 2 failed",
                log_path=Path("/tmp/x.log"),
            )
        err = capsys.readouterr().err
        assert "FAILED: ship aborted at verify" in err
        assert "  pytest: 2 failed" in err
        assert "  full log: /tmp/x.log" in err

    def test_failure_writes_to_stderr_not_stdout(self, capsys):
        with output_context(mode=OutputMode.COMPACT):
            failure("boom", detail="x", log_path=None)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "FAILED: boom" in captured.err

    def test_failure_omits_log_line_when_none(self, capsys):
        with output_context(mode=OutputMode.COMPACT):
            failure("boom", detail="x", log_path=None)
        assert "full log:" not in capsys.readouterr().err

    def test_failure_json_buffers_event(self, capsys):
        buf: dict = {"events": []}
        with output_context(mode=OutputMode.JSON, json_buffer=buf):
            failure("boom", detail="x", log_path=Path("/tmp/x.log"))
        assert capsys.readouterr().out == ""
        assert buf["events"] == [
            {
                "status": "error",
                "label": "boom",
                "detail": "x",
                "log_path": "/tmp/x.log",
            }
        ]


class TestTee:
    """Cycle 4 + 5: ``tee()`` context manager writes log + cleans up."""

    def test_tee_writes_under_xdg_data_home(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        with output_context(mode=OutputMode.COMPACT), tee("ship") as log_path:
            assert log_path is not None
            print("hello stdout")
            # Force a failure so the log is preserved.
            failure("ship aborted", detail="x", log_path=log_path)
        assert log_path.exists()
        content = log_path.read_text()
        assert "hello stdout" in content
        assert log_path.parent == tmp_path / "fraisier" / "logs"

    def test_tee_removes_log_on_clean_exit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        with output_context(mode=OutputMode.COMPACT), tee("ship") as log_path:
            assert log_path is not None
            print("transient")
            saved = log_path
        # No failure() called → clean exit → log removed.
        assert not saved.exists()

    def test_tee_keeps_log_when_failure_called(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        with output_context(mode=OutputMode.COMPACT), tee("ship") as log_path:
            assert log_path is not None
            print("interesting")
            failure("ship aborted", detail="x", log_path=log_path)
            saved = log_path
        assert saved.exists()
        assert "interesting" in saved.read_text()

    def test_tee_keeps_log_when_exception_raised(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        captured_path: Path | None = None
        with (
            output_context(mode=OutputMode.COMPACT),
            pytest.raises(RuntimeError, match="boom"),
            tee("ship") as log_path,
        ):
            captured_path = log_path
            print("before raise")
            raise RuntimeError("boom")
        assert captured_path is not None
        assert captured_path.exists()
        assert "before raise" in captured_path.read_text()

    def test_tee_path_filename_includes_command_and_timestamp(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        with output_context(mode=OutputMode.COMPACT), tee("sync") as log_path:
            assert log_path is not None
            failure("x", detail="y", log_path=log_path)
            saved = log_path
        assert saved.name.startswith("sync-")
        assert saved.name.endswith(".log")


class TestNoTeeFlag:
    """Cycle 6: ``--no-tee`` yields ``None`` and creates no file."""

    def test_no_tee_yields_none_and_skips_file_creation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        with (
            output_context(mode=OutputMode.COMPACT, tee_disabled=True),
            tee("ship") as log_path,
        ):
            assert log_path is None
            print("not captured anywhere")
        log_dir = tmp_path / "fraisier" / "logs"
        # The directory may or may not exist; what matters is no file landed.
        if log_dir.exists():
            assert list(log_dir.iterdir()) == []


class TestCliFlags:
    """Cycle 7: top-level ``--verbose``/``--json``/``--no-tee`` flags."""

    def _make_cli(self):
        """Build a stub CLI that exposes the active OutputContext for testing."""
        from fraisier._output import install_cli_flags

        @click.group()
        def cli() -> None: ...

        @cli.command()
        def show_mode() -> None:
            ctx = get_context()
            click.echo(f"mode={ctx.mode.value} verbosity={ctx.verbosity}")

        install_cli_flags(cli)
        return cli

    def test_no_flags_defaults_to_compact(self):
        cli = self._make_cli()
        result = CliRunner().invoke(cli, ["show-mode"])
        assert result.exit_code == 0
        assert "mode=compact" in result.output
        assert "verbosity=0" in result.output

    def test_single_v_enables_verbose_mode(self):
        cli = self._make_cli()
        result = CliRunner().invoke(cli, ["-v", "show-mode"])
        assert result.exit_code == 0
        assert "mode=verbose" in result.output
        assert "verbosity=1" in result.output

    def test_vv_sets_verbosity_2(self):
        cli = self._make_cli()
        result = CliRunner().invoke(cli, ["-vv", "show-mode"])
        assert result.exit_code == 0
        assert "verbosity=2" in result.output

    def test_vvv_sets_verbosity_3(self):
        cli = self._make_cli()
        result = CliRunner().invoke(cli, ["-vvv", "show-mode"])
        assert result.exit_code == 0
        assert "verbosity=3" in result.output

    def test_json_flag_sets_json_mode(self):
        cli = self._make_cli()
        result = CliRunner().invoke(cli, ["--json", "show-mode"])
        assert result.exit_code == 0
        assert "mode=json" in result.output

    def test_verbose_and_json_mutually_exclusive(self):
        cli = self._make_cli()
        result = CliRunner().invoke(cli, ["-v", "--json", "show-mode"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()


class TestAutoDetect:
    """Cycle 8: auto-detect *confirms* compact (never upgrades to verbose)."""

    def _make_cli(self):
        from fraisier._output import install_cli_flags

        @click.group()
        def cli() -> None: ...

        @cli.command()
        def show_mode() -> None:
            click.echo(f"mode={get_context().mode.value}")

        install_cli_flags(cli)
        return cli

    def test_claudecode_env_does_not_upgrade_to_verbose(self, monkeypatch):
        monkeypatch.setenv("CLAUDECODE", "1")
        cli = self._make_cli()
        result = CliRunner().invoke(cli, ["show-mode"])
        # Auto-detect never upgrades — default stays compact.
        assert "mode=compact" in result.output

    def test_ci_env_does_not_upgrade_to_verbose(self, monkeypatch):
        monkeypatch.setenv("CI", "1")
        cli = self._make_cli()
        result = CliRunner().invoke(cli, ["show-mode"])
        assert "mode=compact" in result.output

    def test_explicit_verbose_wins_over_claudecode(self, monkeypatch):
        monkeypatch.setenv("CLAUDECODE", "1")
        cli = self._make_cli()
        result = CliRunner().invoke(cli, ["-v", "show-mode"])
        assert "mode=verbose" in result.output


class TestEmitJson:
    """Cycle 9: ``emit_json()`` writes the final payload in JSON mode."""

    def test_emit_json_writes_final_payload(self, capsys):
        with output_context(
            mode=OutputMode.JSON,
            json_buffer={"events": []},
        ):
            success("Shipped v1.0.1")
            emit_json({"command": "ship"})
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["command"] == "ship"
        assert payload["events"] == [{"status": "ok", "label": "Shipped v1.0.1"}]

    def test_emit_json_noop_outside_json_mode(self, capsys):
        with output_context(mode=OutputMode.COMPACT):
            emit_json({"command": "ship"})
        assert capsys.readouterr().out == ""

    def test_emit_json_merges_overrides_top_level_keys(self, capsys):
        with output_context(
            mode=OutputMode.JSON,
            json_buffer={"events": [{"status": "ok", "label": "x"}]},
        ):
            emit_json({"command": "deploy", "fraise": "api"})
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "deploy"
        assert payload["fraise"] == "api"
        assert payload["events"][0]["label"] == "x"
