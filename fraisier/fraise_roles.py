"""What a fraise does on its host: whether it migrates, whether it serves.

The type alone does not say. A fraise's ``type`` picks its deployer, but only
an ``api`` with a ``database:`` block runs migrations, and a fraise of any type
can declare a long-running service. Every loop that renders, installs or checks
something per fraise asks one of these two predicates, so the doctor, the
scaffold and the deployer cannot disagree.

Both take the raw fraise dict and the raw environment dict, because
``exec_command`` is read at fraise level and never copied into the merged
environment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


def fraise_migrates(fraise: Mapping[str, Any], env: Mapping[str, Any]) -> bool:
    """Whether a deploy of *fraise* in *env* runs database migrations.

    Mirrors ``APIDeployer``, the only deployer that migrates, and only when
    its ``database`` config is set. ``ETLDeployer`` reads ``database`` too but
    never migrates.
    """
    return fraise.get("type") == "api" and bool(env.get("database"))


def fraise_serves(fraise: Mapping[str, Any], env: Mapping[str, Any]) -> bool:
    """Whether *fraise* in *env* runs a long-lived app unit (``core/service.j2``).

    An ``api`` always does. Any other type does only when it declares what to
    run: a ``service:`` block, or the legacy ``exec_command`` at fraise or
    environment level. Without one, the template falls back to uvicorn on port
    8000, which is never right for a scheduled, backup or etl fraise.
    """
    return (
        fraise.get("type") == "api"
        or bool(env.get("service"))
        or bool(fraise.get("exec_command"))
        or bool(env.get("exec_command"))
    )
