"""The one table mapping a fraise type to the deployer that runs it.

There used to be three — in ``cli/_helpers.py``, in ``daemon.py`` and inline in
``webhook.py`` — and they disagreed. The webhook's knew nothing about
``scheduled`` or ``backup``, so a push to a branch mapped to one of those was
answered ``deployment_triggered`` and then dropped by a background task that
logged "Unknown fraise type" and returned: no status write, no deployments row,
no notification, no retry (#379).

The runner is not part of the table. Each entry point decides it — the CLI from
the fraise's ``ssh:`` block, the daemon and the webhook always locally, because
they already run on the target host.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fraisier.deployers.base import BaseDeployer
    from fraisier.runners import CommandRunner

#: Every fraise type that can be deployed. A type outside this set has no
#: deployer, and every entry point refuses it the same way.
FRAISE_TYPES = frozenset({"api", "etl", "scheduled", "backup", "docker_compose"})


class UnknownFraiseTypeError(ValueError):
    """A fraise type no deployer handles.

    A ``ValueError`` so the entry points that already treat a bad request as a
    ``ValueError`` keep working; the type and the known set are in the message
    because both are what an operator needs to fix the config.
    """

    def __init__(self, fraise_type: str | None) -> None:
        self.fraise_type = fraise_type
        super().__init__(
            f"Unknown fraise type {fraise_type!r} — known types: "
            f"{', '.join(sorted(FRAISE_TYPES))}"
        )


def build_deployer(
    fraise_type: str | None,
    config: dict[str, Any],
    *,
    runner: CommandRunner,
    job: str | None = None,
) -> BaseDeployer:
    """Build the deployer for *fraise_type*.

    Args:
        fraise_type: the fraise's ``type:``.
        config: the fraise environment config the deployer is built from.
        runner: how commands reach the target host.
        job: for ``scheduled``/``backup`` fraises with a ``jobs:`` block, the
            job to deploy. Its config is merged over the fraise's.

    Raises:
        UnknownFraiseTypeError: naming the type and the known set.
    """
    if fraise_type == "api":
        from fraisier.deployers.api import APIDeployer

        return APIDeployer(config, runner=runner)

    if fraise_type == "etl":
        from fraisier.deployers.etl import ETLDeployer

        return ETLDeployer(config, runner=runner)

    if fraise_type == "docker_compose":
        from fraisier.deployers.docker_compose import DockerComposeDeployer

        return DockerComposeDeployer(config, runner=runner)

    if fraise_type in ("scheduled", "backup"):
        from fraisier.deployers.scheduled import ScheduledDeployer

        job_config = (config.get("jobs") or {}).get(job) if job else None
        if job_config:
            return ScheduledDeployer(
                {**config, **job_config, "job_name": job},
                runner=runner,
            )
        return ScheduledDeployer(config, runner=runner)

    raise UnknownFraiseTypeError(fraise_type)
