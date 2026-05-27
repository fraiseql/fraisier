"""LazyEnv-aware JSON serialization for CLI diagnostic output (#220).

CLI commands serialize state with ``json.dumps`` for ``--json`` flags
and structured logging. Diagnostic output should NEVER resolve a
``LazyEnv`` placeholder — secrets must not leak into stdout, log files,
or pipelines. This helper substitutes ``"<envvar:NAME>"`` placeholders
for reachable ``LazyEnv`` instances at serialization time, without
calling ``resolve()``.

Use :func:`dumps` as a drop-in for ``json.dumps`` at every CLI JSON
output site. Non-LazyEnv non-serializable values still ``TypeError``
so silent data loss can't happen.
"""

from __future__ import annotations

import json
from typing import Any

from fraisier.config._lazy_env import LazyEnv


def _default(obj: Any) -> Any:
    if isinstance(obj, LazyEnv):
        return f"<envvar:{obj.name}>"
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON-serializable")


def dumps(obj: Any, *, indent: int | None = None) -> str:
    """Serialize *obj* to JSON, rendering ``LazyEnv`` as a placeholder.

    The placeholder is ``"<envvar:NAME>"`` and is generated without
    calling ``LazyEnv.resolve()`` — diagnostic JSON cannot leak the
    secret even when the env var is set.
    """
    return json.dumps(obj, indent=indent, default=_default)


# ---------------------------------------------------------------------------
# Shared --format option (#221 bundle B phase 05)
# ---------------------------------------------------------------------------

import click  # noqa: E402

_DEPRECATION_WARNING_EMITTED: set[str] = set()


def format_option(extra_choices: tuple[str, ...] = ()):
    """Shared ``--format text|json`` decorator for commands that emit
    structured output.

    The decorator is composable with existing ``--json`` flags via
    :func:`resolve_format` — pass both the new ``--format`` value and the
    legacy ``--json`` boolean and ``resolve_format`` picks the right
    one, emitting a one-time deprecation warning on stderr when
    ``--json`` was used.

    Args:
        extra_choices: Additional format names beyond ``text`` and
            ``json`` (e.g. ``"yaml"`` for commands that grow more
            formats).
    """
    choices = ("text", "json", *extra_choices)

    def decorator(fn):
        return click.option(
            "--format",
            "fmt",
            type=click.Choice(choices),
            default="text",
            help="Output format (default: text)",
        )(fn)

    return decorator


def resolve_format(fmt: str, legacy_json: bool, *, command_name: str) -> str:
    """Pick the effective format from the new --format and legacy --json.

    When the legacy ``--json`` flag is set, returns ``"json"`` and emits
    a one-time deprecation warning on stderr per command-name. When
    both ``--format json`` and ``--json`` are set, the new flag wins
    silently (consumers in transition).
    """
    if fmt == "json":
        return "json"
    if legacy_json:
        from fraisier.cli._helpers import err_console

        if command_name not in _DEPRECATION_WARNING_EMITTED:
            _DEPRECATION_WARNING_EMITTED.add(command_name)
            err_console.print(
                "[yellow]warning:[/yellow] `--json` is deprecated; "
                "use `--format json` instead. "
                "(Will be removed in a future release.)"
            )
        return "json"
    return fmt
