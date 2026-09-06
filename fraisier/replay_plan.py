"""Which refused dispatches to replay, and in what order (#367).

Pure: it reads a ledger and a configuration and returns a list. No IO, no
dispatch — so the three decisions it encodes can be argued with in a test
rather than inferred from a deploy log.

**Which ref.** The plan carries the *branch*, never the recorded sha. Without
the bug the refused push would have deployed, and any later push would have
deployed after it: the end state is *branch head deployed*. Replaying the
recorded sha is a regression whenever newer commits exist, and is never more
correct — so there is no mode that does it.

**What order.** Production last, otherwise alphabetical by ``(environment,
fraise)``. One host serving two environments had two entries and no defined
order; production-first is wrong, and "whatever the ledger happens to hold" is
not a policy. Sorting the riskiest target last means a replay mechanism that is
itself broken breaks on a lower-stakes target first. The entries are separate
debts, so a staging failure does not hold production back.

**Whether at all.** A target whose ``(fraise, environment)`` no longer resolves
in the configuration is dropped: the fraise was renamed or removed, and
deploying a guess is worse than leaving the entry for ``doctor``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from fraisier.refused_dispatch_record import RefusedDispatch

log = logging.getLogger(__name__)

#: Sorted last. Not a list of "safe" environments — a list of the one whose
#: failure costs the most, so it is attempted once the others have shown the
#: mechanism works.
_LAST_ENVIRONMENTS = ("production",)


@dataclass(frozen=True)
class ReplayTarget:
    """One deploy the upgrade owes, ready to dispatch.

    Deliberately carries no commit sha: see the module docstring.
    """

    fraise: str
    environment: str
    branch: str
    fraise_config: dict[str, Any]

    @property
    def target(self) -> tuple[str, str]:
        """The ledger key this discharges when its deploy succeeds."""
        return (self.fraise, self.environment)


def _rank(environment: str) -> int:
    return 1 if environment in _LAST_ENVIRONMENTS else 0


def plan_replays(
    entries: list[RefusedDispatch],
    config: Any,
) -> list[ReplayTarget]:
    """The deploys to re-fire, in the order to fire them.

    *config* is a ``FraisierConfig``; only ``get_fraise_environment`` is used,
    so a plan can be argued with without a real one.
    """
    planned: list[ReplayTarget] = []
    for entry in entries:
        if not entry.branch:
            log.warning(
                "not replaying %s/%s: the refusal recorded no branch, so there "
                "is no head to resolve. The entry stands for `fraisier doctor`.",
                entry.fraise,
                entry.environment,
            )
            continue
        try:
            env_config = config.get_fraise_environment(entry.fraise, entry.environment)
        except Exception:
            log.warning(
                "not replaying %s/%s: its configuration could not be read",
                entry.fraise,
                entry.environment,
                exc_info=True,
            )
            continue
        if not env_config:
            log.warning(
                "not replaying %s/%s: it is no longer in this host's "
                "fraises.yaml. The entry stands for `fraisier doctor`.",
                entry.fraise,
                entry.environment,
            )
            continue
        planned.append(
            ReplayTarget(
                fraise=entry.fraise,
                environment=entry.environment,
                branch=entry.branch,
                fraise_config=env_config,
            )
        )

    planned.sort(key=lambda t: (_rank(t.environment), t.environment, t.fraise))
    return planned
