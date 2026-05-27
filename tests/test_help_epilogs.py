"""Snapshot tests for `--help` epilog conventions (#221 bundle A).

Each subcommand under ``fraisier ...`` (including subgroups like ``db``)
must include at least two worked-example lines in its ``--help`` output,
so an agent or operator can grok flag interactions without source-reading.

The convention matches existing docstrings: indented lines under an
``Examples:`` header beginning with ``fraisier <subcommand>`` (a leading
``$ `` shell prompt is also accepted).

``ship`` additionally must spell out the ``--pr`` / ``--auto-merge`` /
``--wait-deploy`` interaction matrix that #221 flags as opaque.
"""

from __future__ import annotations

import re

import click
import pytest
from click.testing import CliRunner

from fraisier.cli.main import main as main_group

# Commands whose --help legitimately has no worked-example block:
# internal/socket-only commands that operators never invoke directly.
_NO_EXAMPLE_REQUIRED: frozenset[str] = frozenset(
    {
        "deploy-daemon",  # stdin-driven socket-activated entrypoint
    }
)

_EXAMPLE_LINE = re.compile(r"^\s+(?:\$\s+)?fraisier\b")


def _iter_leaf_commands(
    group: click.Group, prefix: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], click.Command]]:
    """Flatten Click group tree into (path, leaf_command) tuples."""
    out: list[tuple[tuple[str, ...], click.Command]] = []
    for name, cmd in group.commands.items():
        path = (*prefix, name)
        if isinstance(cmd, click.Group):
            out.extend(_iter_leaf_commands(cmd, path))
        else:
            out.append((path, cmd))
    return out


def _render_help(path: tuple[str, ...]) -> str:
    runner = CliRunner()
    result = runner.invoke(main_group, [*path, "--help"], catch_exceptions=False)
    assert result.exit_code == 0, (
        f"`fraisier {' '.join(path)} --help` exited {result.exit_code}: {result.output}"
    )
    return result.output


_LEAVES = _iter_leaf_commands(main_group)


@pytest.mark.parametrize(
    "path",
    [pytest.param(p, id=" ".join(p)) for p, _ in _LEAVES],
)
def test_every_subcommand_has_examples(path: tuple[str, ...]) -> None:
    """Each subcommand --help must contain >=2 `$ fraisier` lines."""
    full_name = " ".join(path)
    if full_name in _NO_EXAMPLE_REQUIRED:
        pytest.skip(f"{full_name} is explicitly exempt from the example contract")  # ty: ignore[too-many-positional-arguments]

    output = _render_help(path)
    example_count = sum(1 for line in output.splitlines() if _EXAMPLE_LINE.match(line))
    assert example_count >= 2, (
        f"`fraisier {full_name} --help` has {example_count} "
        f"`fraisier ...` example line(s); expected at least 2. "
        f"Add worked examples to the docstring under a \\b block:\n"
        f"  Examples:\n"
        f"      fraisier {full_name} ...\n"
        f"      fraisier {full_name} ... --flag"
    )


def test_ship_help_describes_flag_interactions() -> None:
    """The ``ship`` --help body must explain --pr / --auto-merge / --wait-deploy."""
    output = _render_help(("ship",))
    for substring in (
        "--pr",
        "--auto-merge",
        "--wait-deploy",
        "Flag interactions:",
    ):
        assert substring in output, (
            f"`fraisier ship --help` missing required marker {substring!r}. "
            f"Output:\n{output}"
        )
