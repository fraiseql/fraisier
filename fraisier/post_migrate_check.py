"""``database.post_migrate_check`` — the schema-drift gate's config (#395).

The gate asks confiture whether the live database matches the schema this
checkout builds, and it runs **after** ``migrate up``, not before.
``--check-live-drift`` grades expected (the DDL files) against actual (live) and
rates "in the DDL, not in live" — ``MISSING_TABLE`` / ``MISSING_COLUMN`` —
CRITICAL.  A pending migration that adds a table or a column is exactly that, so
the ``pre_migrate_check`` the issue proposed fails closed on every deploy
carrying one.  Hence the name.

```yaml
database:
  post_migrate_check:
    enabled: true
    checks: [live-drift]      # live-drift | signatures
    on_critical: fail         # fail | warn
    escalate: []              # warning kinds that must fail the gate
```

``on_critical`` also governs a gate that could not reach a verdict at all — a
failed schema build, an unreachable database, a report fraisier cannot parse.
``fail`` means "stop me", and a check that did not run has cleared nothing;
``warn`` means "tell me, do not stop me", and that intent holds however the
check failed.

``escalate`` is the answer to #412.  confiture grades a dropped index, a
changed default, a widened type and a lost ``NOT NULL`` ``warning``, so
``has_critical_drift`` stays false and even ``on_critical: fail`` deploys over
them.  That grade is confiture's to set and fraisier does not argue with it;
naming a kind here says only that *this* deploy will not accept losing it.
Empty by default: a gate that ran yesterday returns the same verdict today.

A lost or re-pointed constraint is **not** on that list and needs no
escalation: confiture 1.26.0 grades it ``critical``, so the gate already stops
on it (confiture#506/#518).  Naming one is a configuration error, which says
the true thing — the deploy stops either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from fraisier.dbops.drift import CHECK_FLAGS, ESCALATABLE_KINDS

#: What ``on_critical`` may say.
ON_CRITICAL: tuple[str, ...] = ("fail", "warn")

#: Drift kinds ``escalate`` may name — confiture's warning-graded ones, measured
#: rather than read off a changelog.  Re-exported from :mod:`fraisier.dbops.drift`
#: so the config surface and the gate that enforces it cannot drift apart.
VALID_ESCALATIONS: tuple[str, ...] = ESCALATABLE_KINDS

#: Checks a deploy may ask for.  ``--check-body-replay`` is deliberately not
#: offered: it replays every function body and belongs on a timer.
VALID_CHECKS: tuple[str, ...] = tuple(CHECK_FLAGS)

_DEFAULT_CHECKS: tuple[str, ...] = ("live-drift",)


@dataclass(frozen=True)
class PostMigrateCheck:
    """The gate's resolved configuration."""

    enabled: bool = True
    checks: tuple[str, ...] = _DEFAULT_CHECKS
    on_critical: Literal["fail", "warn"] = "fail"
    escalate: tuple[str, ...] = ()
    #: Did a human write this gate, or is it the default arriving on its own?
    #:
    #: Only one thing turns on it, and it is not what the gate *checks* — a
    #: declared gate and a defaulted one grade identical drift identically.
    #: It decides what an **unrunnable** gate costs: see
    #: :meth:`fraisier.deployers.api.APIDeployer._run_post_migrate_check`.
    declared: bool = False


def load_post_migrate_check(database_config: dict[str, Any]) -> PostMigrateCheck:
    """Parse ``database.post_migrate_check``; on unless the project declines.

    Shape validation happens at config-load time in
    :mod:`fraisier.config._validation`, so this reads a block already known
    to be well-formed.  A block that is absent entirely — the shape every
    project that predates the default has — resolves to the default gate.
    """
    block = database_config.get("post_migrate_check")
    if isinstance(block, dict):
        raw, declared = block, True
    else:
        raw, declared = {}, False
    if not raw.get("enabled", True):
        return PostMigrateCheck(enabled=False, declared=True)
    checks = raw.get("checks")
    return PostMigrateCheck(
        enabled=True,
        checks=tuple(checks) if checks else _DEFAULT_CHECKS,
        on_critical=raw.get("on_critical", "fail"),
        escalate=tuple(raw.get("escalate") or ()),
        declared=declared,
    )
