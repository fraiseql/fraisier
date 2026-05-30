"""LLM-native output layer for the fraisier CLI.

fraisier defaults to **compact output** — one-line successes, focused
failures, and an on-disk tee of the full Rich story when something
breaks. Operators reading CI logs interactively opt into the
human-friendly Rich output with ``--verbose``/``-v``; tooling that
wants structured payloads passes ``--json``.

Inspired by `rtk-ai/rtk <https://github.com/rtk-ai/rtk>`_; fraisier
inverts the polarity so compact is default.

Module surface:

- :class:`OutputMode` — ``COMPACT``, ``VERBOSE``, ``JSON``.
- :class:`OutputContext` — frozen state carried via ``ContextVar``.
- :func:`output_context` — context manager that swaps the active
  ``OutputContext`` for nested CLI calls.
- :func:`get_context` — read the active ``OutputContext``.
- :func:`compact`, :func:`verbose` — line printers gated on mode.
- :func:`success`, :func:`failure` — three-mode dispatch.
- :func:`tee` — context manager mirroring stdout/stderr to a log
  file under ``XDG_DATA_HOME/fraisier/logs/``. Clean exits remove the
  log; failures (recorded via :func:`failure` or an exception in the
  body) keep it.
- :func:`emit_json` — write the final JSON payload to stdout in JSON
  mode; no-op otherwise.
- :func:`install_cli_flags` — decorator that wires
  ``--verbose``/``-v``, ``--json``, ``--no-tee`` onto a Click group.
"""

from __future__ import annotations

import functools
import io
import json
import os
import sys
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from rich.console import Console
from rich.markup import render as _render_markup

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "OutputContext",
    "OutputMode",
    "compact",
    "emit_json",
    "failure",
    "get_context",
    "install_cli_flags",
    "output_context",
    "success",
    "tee",
    "verbose",
]


class OutputMode(Enum):
    """Three mutually exclusive output personalities."""

    COMPACT = "compact"
    VERBOSE = "verbose"
    JSON = "json"


@dataclass(frozen=True)
class OutputContext:
    """Immutable per-invocation output state.

    Stored in a :class:`ContextVar` so nested subcommand calls and
    spawned threads see the parent's configuration without
    parameter drilling.

    Internal mutable bookkeeping (``_failure_recorded``) lives in a
    one-element list so the dataclass stays ``frozen=True`` while
    helpers can flip the flag.
    """

    mode: OutputMode = OutputMode.COMPACT
    verbosity: int = 0
    tee_path: Path | None = None
    json_buffer: dict[str, Any] | None = None
    tee_disabled: bool = False
    _failure_recorded: list[bool] = field(default_factory=lambda: [False])


_DEFAULT_CTX = OutputContext()
_ctx: ContextVar[OutputContext] = ContextVar(
    "fraisier_output_ctx", default=_DEFAULT_CTX
)


def _rich_stdout() -> Console:
    """Construct a fresh Console pointing at the current ``sys.stdout``.

    Recreated per call so the helper plays nice with pytest's ``capsys``
    (which swaps ``sys.stdout`` per test) and with tee-mode replacement.
    """
    return Console(file=sys.stdout, soft_wrap=True)


def _rich_stderr() -> Console:
    return Console(file=sys.stderr, soft_wrap=True)


def get_context() -> OutputContext:
    """Return the active :class:`OutputContext` (default-compact)."""
    return _ctx.get()


@contextmanager
def output_context(
    *,
    mode: OutputMode = OutputMode.COMPACT,
    verbosity: int = 0,
    json_buffer: dict[str, Any] | None = None,
    tee_disabled: bool = False,
) -> Iterator[OutputContext]:
    """Swap the active :class:`OutputContext` for the body, then restore.

    JSON mode auto-initialises a ``{"events": []}`` buffer if one is
    not supplied.
    """
    if mode is OutputMode.JSON and json_buffer is None:
        json_buffer = {"events": []}
    new_ctx = OutputContext(
        mode=mode,
        verbosity=verbosity,
        json_buffer=json_buffer,
        tee_disabled=tee_disabled,
    )
    token = _ctx.set(new_ctx)
    try:
        yield new_ctx
    finally:
        _ctx.reset(token)


def _strip_markup(line: str) -> str:
    """Strip Rich BBCode-style markup tags, leaving plain text."""
    return _render_markup(line).plain


def compact(line: str, *, markup: bool = False) -> None:
    """Print a compact line.

    Compact mode strips Rich markup. Verbose mode renders with markup.
    JSON mode suppresses the line entirely (the eventual JSON payload
    is the channel).
    """
    ctx = get_context()
    if ctx.mode is OutputMode.JSON:
        return
    if ctx.mode is OutputMode.VERBOSE:
        if markup:
            _rich_stdout().print(line)
        else:
            sys.stdout.write(_strip_markup(line) + "\n")
        return
    sys.stdout.write(_strip_markup(line) + "\n")


def verbose(line: str, *, level: int = 1) -> None:
    """Print only when ``ctx.verbosity >= level`` AND mode is verbose."""
    ctx = get_context()
    if ctx.mode is not OutputMode.VERBOSE:
        return
    if ctx.verbosity < level:
        return
    _rich_stdout().print(line)


def _format_fields(fields: dict[str, Any]) -> str:
    """Render keyword fields as ``k=v`` pairs separated by spaces."""
    return " ".join(f"{k}={v}" for k, v in fields.items())


def success(label: str, **fields: Any) -> None:
    """Emit a success event.

    - Compact: ``ok <label> [k=v ...]`` on stdout.
    - Verbose: Rich-rendered ``[green]ok[/green] <label>`` on stdout.
    - JSON: append ``{"status": "ok", "label": ..., **fields}`` to the
      active buffer.
    """
    ctx = get_context()
    if ctx.mode is OutputMode.JSON:
        if ctx.json_buffer is not None:
            event: dict[str, Any] = {"status": "ok", "label": label}
            event.update(fields)
            ctx.json_buffer.setdefault("events", []).append(event)
        return
    suffix = f" {_format_fields(fields)}" if fields else ""
    line = f"ok {label}{suffix}"
    if ctx.mode is OutputMode.VERBOSE:
        _rich_stdout().print(f"[green]ok[/green] {label}{suffix}")
    else:
        sys.stdout.write(_strip_markup(line) + "\n")


def failure(
    label: str,
    *,
    detail: str = "",
    log_path: Path | None = None,
) -> None:
    """Emit a failure event.

    - Compact: ``FAILED: <label>\\n  <detail>\\n  full log: <path>``
      on stderr. Lines without content are omitted.
    - Verbose: Rich-rendered red ``FAILED:`` line plus the same detail
      and log lines.
    - JSON: append
      ``{"status": "error", "label": ..., "detail": ..., "log_path": ...}``
      to the active buffer.

    Also flips the context's ``_failure_recorded`` flag so :func:`tee`
    knows to preserve the log on exit.
    """
    ctx = get_context()
    if ctx.tee_path is not None or log_path is not None:
        # Mark the context so tee() preserves the file on exit.
        ctx._failure_recorded[0] = True
    if ctx.mode is OutputMode.JSON:
        if ctx.json_buffer is not None:
            event: dict[str, Any] = {
                "status": "error",
                "label": label,
                "detail": detail,
            }
            if log_path is not None:
                event["log_path"] = str(log_path)
            ctx.json_buffer.setdefault("events", []).append(event)
        return
    lines = [f"FAILED: {label}"]
    if detail:
        lines.append(f"  {detail}")
    if log_path is not None:
        lines.append(f"  full log: {log_path}")
    text = "\n".join(lines) + "\n"
    if ctx.mode is OutputMode.VERBOSE:
        _rich_stderr().print(f"[red]FAILED:[/red] {label}")
        if detail:
            _rich_stderr().print(f"  {detail}")
        if log_path is not None:
            _rich_stderr().print(f"  full log: {log_path}")
    else:
        sys.stderr.write(text)


class _TeeStream(io.TextIOBase):
    """Mirror writes to ``primary`` (the real stream) and ``log`` (a file)."""

    def __init__(self, primary: Any, log: Any) -> None:
        self._primary = primary
        self._log = log

    def write(self, data: str) -> int:  # type: ignore[override]
        with suppress(OSError):
            self._log.write(data)
        return self._primary.write(data)

    def flush(self) -> None:  # type: ignore[override]
        with suppress(OSError):
            self._log.flush()
        self._primary.flush()

    def isatty(self) -> bool:  # type: ignore[override]
        return self._primary.isatty()


def _log_dir() -> Path:
    """Resolve the XDG-compliant log directory."""
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "fraisier" / "logs"
    return Path.home() / ".local" / "share" / "fraisier" / "logs"


def _now_stamp() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")


@contextmanager
def tee(command: str) -> Iterator[Path | None]:
    """Mirror stdout/stderr to a per-command log file.

    Yields the log path (or ``None`` when teeing is disabled via
    ``--no-tee``). Clean exits delete the log so successful runs do
    not fill the disk; a recorded :func:`failure` call or an
    exception propagating through the body preserves the file for
    later inspection.

    The currently-active :class:`OutputContext`'s ``tee_path`` is
    refreshed to the log file path for the duration of the body so
    helpers can pass it to :func:`failure`.
    """
    ctx = get_context()
    if ctx.tee_disabled:
        yield None
        return

    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{command}-{_now_stamp()}.log"
    # 0o600 so deploy tokens / secrets leaking via verbose passes don't
    # become world-readable on shared hosts.
    fd = os.open(
        str(log_path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    log_file = os.fdopen(fd, "w", buffering=1)

    real_stdout, real_stderr = sys.stdout, sys.stderr
    sys.stdout = _TeeStream(real_stdout, log_file)  # type: ignore[assignment]
    sys.stderr = _TeeStream(real_stderr, log_file)  # type: ignore[assignment]

    # Refresh the active context so failure() can pick up the path.
    new_ctx = replace(ctx, tee_path=log_path)
    token = _ctx.set(new_ctx)

    raised = False
    try:
        yield log_path
    except BaseException:
        raised = True
        raise
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr
        with suppress(OSError):
            log_file.flush()
            log_file.close()
        keep = raised or new_ctx._failure_recorded[0]
        if not keep:
            with suppress(OSError):
                log_path.unlink(missing_ok=True)
        _ctx.reset(token)


def emit_json(payload: dict[str, Any]) -> None:
    """Flush the buffered JSON payload to stdout.

    Merges ``payload`` with the active buffer (caller keys win for
    top-level overrides; ``events`` is preserved). No-op outside JSON
    mode so callers can sprinkle ``emit_json`` calls without guarding.
    """
    ctx = get_context()
    if ctx.mode is not OutputMode.JSON or ctx.json_buffer is None:
        return
    merged = {**ctx.json_buffer, **payload}
    sys.stdout.write(json.dumps(merged) + "\n")


def install_cli_flags(group: click.Group) -> click.Group:
    """Wire ``--verbose``/``-v``, ``--json``, ``--no-tee`` onto a Click group.

    Call **after** ``@click.group()`` has built the ``Group`` object.
    Appends three params and wraps the existing callback so the active
    :class:`OutputMode` is set in the ``ContextVar`` before subcommand
    dispatch.

    ``--verbose`` and ``--json`` are mutually exclusive; passing both
    fails with a usage error.

    Usage::

        @click.group()
        def main(ctx): ...

        install_cli_flags(main)
    """
    group.params.append(
        click.Option(
            ["--verbose", "-v", "verbose_count"],
            count=True,
            help=(
                "Restore human-friendly Rich output (compact is default). "
                "Repeat for more detail: -v rich story, -vv per-phase "
                "timings, -vvv full subprocess output."
            ),
        )
    )
    group.params.append(
        click.Option(
            ["--json", "json_"],
            is_flag=True,
            default=False,
            help="Emit a structured JSON payload on stdout (machine-readable).",
        )
    )
    group.params.append(
        click.Option(
            ["--no-tee", "no_tee"],
            is_flag=True,
            default=False,
            help="Skip writing a failure log under ~/.local/share/fraisier/logs/.",
        )
    )

    original_callback = group.callback

    def wrapper(
        *args: Any,
        verbose_count: int = 0,
        json_: bool = False,
        no_tee: bool = False,
        **kwargs: Any,
    ) -> Any:
        if verbose_count > 0 and json_:
            raise click.UsageError("--verbose and --json are mutually exclusive")
        if json_:
            mode = OutputMode.JSON
        elif verbose_count > 0:
            mode = OutputMode.VERBOSE
        else:
            mode = OutputMode.COMPACT
        new_ctx = OutputContext(
            mode=mode,
            verbosity=verbose_count,
            json_buffer={"events": []} if mode is OutputMode.JSON else None,
            tee_disabled=no_tee,
        )
        _ctx.set(new_ctx)
        if original_callback is None:
            return None
        return original_callback(*args, **kwargs)

    if original_callback is not None:
        functools.update_wrapper(wrapper, original_callback)
    group.callback = wrapper
    return group
