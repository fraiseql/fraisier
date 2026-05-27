"""``--help`` epilog hook that lists reachable ``!envvar`` references.

Each subcommand's ``--help`` output gains a ``Reads envvars:`` section
listing the env-var names the command would touch if invoked, derived
from the introspection map. Marks unset variables with ``[unset]`` so
CI gates and operators can spot misconfiguration.

Implementation note (verified via the bundle-B phase-02 spike): Click's
``format_epilog`` is called with a fully-established context for both
``fraisier <cmd> --help`` and ``fraisier --config X <cmd> --help``
invocation orders. ``ctx.parent.params`` carries the group-level
``--config`` value in both, so the simpler ``format_epilog`` override
works — no ``get_help`` fallback needed.
"""

from __future__ import annotations

from typing import Any

import click

from fraisier.introspection import (
    COMMANDS_WITHOUT_CONFIG_ACCESS,
    SUBCOMMAND_CONFIG_SECTIONS,
    EnvVarRef,
    reachable_envvars,
)


def _format_ref(ref: EnvVarRef) -> str:
    marker = " [unset]" if not ref.is_set else ""
    return f"  {ref.name}{marker}  ({ref.yaml_path})"


def _load_config_for_help(ctx: click.Context) -> dict[str, Any] | None:
    """Best-effort config load for --help epilog rendering.

    Never raises. Returns ``None`` when no config can be found, when
    parsing fails, or when the resolved file isn't readable. The
    epilog renders a placeholder line in that case rather than aborting
    the help display.
    """
    config_path: str | None = None
    parent = ctx.parent
    while parent is not None:
        if "config" in parent.params:
            config_path = parent.params.get("config")
            break
        parent = parent.parent

    try:
        from fraisier.config import get_config

        config_obj = get_config(config_path)
    except Exception:
        return None
    if config_obj is None:
        return None
    raw = getattr(config_obj, "_config", None)
    return raw if isinstance(raw, dict) else None


def _command_name_from_ctx(ctx: click.Context) -> str:
    """Reconstruct the dotted subcommand path that the introspection
    map keys by (e.g. ``db preflight``, ``version show``)."""
    parts: list[str] = []
    cur: click.Context | None = ctx
    while cur is not None and cur.parent is not None:
        parts.append(cur.info_name or "")
        cur = cur.parent
    return " ".join(reversed(parts)).strip()


def _epilog_lines_for(ctx: click.Context) -> list[str]:
    cmd_name = _command_name_from_ctx(ctx)
    if not cmd_name:
        return []
    if cmd_name in COMMANDS_WITHOUT_CONFIG_ACCESS:
        return []
    sections = SUBCOMMAND_CONFIG_SECTIONS.get(cmd_name)
    if not sections:
        return []

    config = _load_config_for_help(ctx)
    if config is None:
        return [
            "Reads envvars:",
            "  (no fraises.yaml found — load a config to see env-var requirements)",
        ]

    refs = reachable_envvars(config, cmd_name)
    if not refs:
        return []

    # Dedupe by (name, yaml_path); preserve traversal order.
    seen: set[tuple[str, str]] = set()
    unique: list[EnvVarRef] = []
    for r in refs:
        key = (r.name, r.yaml_path)
        if key not in seen:
            seen.add(key)
            unique.append(r)

    return ["Reads envvars:", *(_format_ref(r) for r in unique)]


class CommandWithEnvvarEpilog(click.Command):
    """``Command`` subclass that appends a ``Reads envvars:`` block to
    ``--help`` output. Composes with whatever ``epilog`` the command
    already declares; the env-var block goes last so it's adjacent to
    the help footer."""

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        super().format_epilog(ctx, formatter)
        lines = _epilog_lines_for(ctx)
        if not lines:
            return
        formatter.write_paragraph()
        for line in lines:
            formatter.write_text(line)
