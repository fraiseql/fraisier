"""Main CLI group definition.

The actual command implementations live in sibling modules
(``_info``, ``_deploy``, ``_validate``, ``_diagnose``, ``_rollback``,
``health``, ``db``, ``logs``, etc.) which import this module's ``main``
group and attach themselves with ``@main.command(...)``.  We import
those siblings at the bottom of this file so that ``fraisier.cli.main``
exposes a fully-populated ``main`` group on import.
"""

from __future__ import annotations

import os

import click

from fraisier.config import get_config

from ._envmap_help import CommandWithEnvvarEpilog


@click.group()
@click.version_option(package_name="fraisier", prog_name="fraisier")
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True),
    help="Path to fraises.yaml configuration file",
)
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose/debug output")
@click.option(
    "--no-color",
    is_flag=True,
    envvar="NO_COLOR",
    help="Disable colored output (also honoured via NO_COLOR env var).",
)
@click.pass_context
def main(ctx: click.Context, config: str | None, verbose: bool, no_color: bool) -> None:
    """Fraisier - Deployment orchestrator for the FraiseQL ecosystem.

    Manage deployments for all your fraises (services) across multiple providers
    (Bare Metal, Docker Compose).

    \b
    Machine-readable output:
        Most commands accept --json for structured JSON output on stdout.
        Errors are written to stderr so they don't corrupt JSON pipelines.
        Set NO_COLOR=1 (or pass --no-color) to strip ANSI from all output.

    \b
    Examples:
        fraisier list
        fraisier trigger-deploy my_api production
        fraisier deployment-status my_api
        fraisier history --json | jq '.[] | select(.status=="failed")'
        NO_COLOR=1 fraisier validate --json
    """
    if no_color:
        os.environ["NO_COLOR"] = "1"

    if verbose:
        import logging

        logging.basicConfig(format="%(name)s %(levelname)s %(message)s")
        logging.getLogger().setLevel(logging.DEBUG)

    ctx.ensure_object(dict)
    try:
        ctx.obj["config"] = get_config(config)
    except FileNotFoundError:
        ctx.obj["config"] = None
    ctx.obj["skip_health"] = False


# Default every @main.command(...) to the envvar-epilog-aware Command
# subclass. Per-command overrides (cls=...) still win.
main.command_class = CommandWithEnvvarEpilog


# Import submodules to register their commands with `main`.
# Each sibling does ``from .main import main`` and attaches its commands via
# ``@main.command(...)``, so importing the module is enough to register them.
from . import _deploy as _deploy_mod  # noqa: E402, F401
from . import _diagnose as _diagnose_mod  # noqa: E402, F401
from . import _info as _info_mod  # noqa: E402, F401
from . import _rollback as _rollback_mod  # noqa: E402, F401
from . import _validate as _validate_mod  # noqa: E402, F401
from . import bootstrap as _bootstrap_mod  # noqa: E402, F401
from . import db as _db_mod  # noqa: E402, F401
from . import health as _health_mod  # noqa: E402, F401
from . import logs as _logs_mod  # noqa: E402, F401
from . import ops as _ops_mod  # noqa: E402, F401
from . import providers as _providers_mod  # noqa: E402, F401
from . import repair_remote as _repair_remote_mod  # noqa: E402, F401
from . import scaffold as _scaffold_mod  # noqa: E402, F401
from . import setup as _setup_mod  # noqa: E402, F401
from . import sync as _sync_mod  # noqa: E402, F401
from . import test_components as _test_components_mod  # noqa: E402, F401
from . import test_db as _test_db_mod  # noqa: E402, F401
from . import validate_remote as _validate_remote_mod  # noqa: E402, F401
from . import version as _version_mod  # noqa: E402, F401
