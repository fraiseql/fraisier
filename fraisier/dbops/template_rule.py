"""Does this restore need a rollback template? (#481)

Step 8 of ``restore_migrate`` copies the whole restored database so that a
failure in steps 9 to 11 can put it back. On a large database that copy is most
of the run, and it is only needed when ``confiture migrate down`` cannot undo
what ``migrate up`` is about to apply.

``restore.template`` says when to take it:

- ``always`` — every run, as ``create_template: true`` did before 0.91.0.
- ``never``  — no run; rollback is ``migrate down``.
- ``auto``   — only when a pending migration needs it.

The decision is a function of one payload: confiture's static
``migrate preflight --format json --scope pending``, read against the restored
database, so the pending set is exactly what ``migrate up`` will apply. Nothing
else is consulted, so the same rule ports to any consumer of that seam.

Since confiture 1.40 that payload answers the question itself, in
``reversible`` (#484). The rule takes the template unless the verdict is
``true`` and no pending migration is a known blocker; the per-migration walk
also names, in the reason, which migration forced it.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from fraisier.dbops._url import child_env, move_password_to_env
from fraisier.dbops.confiture_executable import confiture_executable

if TYPE_CHECKING:
    from pathlib import Path


TemplateMode = Literal["auto", "always", "never"]

#: The values ``restore.template`` accepts.
TEMPLATE_MODES: tuple[TemplateMode, ...] = ("auto", "always", "never")


#: Issues that mean ``migrate down`` cannot undo the migration they name: the
#: set confiture 1.40 builds ``reversible`` from.
#:
#: A missing or empty down has nothing to run, and since 1.40 ``migrate down``
#: refuses one outright; a ``.py`` down confiture cannot read may do anything;
#: a non-transactional migration can stop half-applied, where its own down does
#: not fit; a destructive gate or an irreversible marker loses data that no down
#: brings back; an unparseable file cannot be judged at all.
_BLOCKING_ISSUES = {
    "PFLIGHT_MISSING_DOWN": "has no down",
    "PFLIGHT_DOWN_EMPTY": "has an empty down",
    "PFLIGHT_DOWN_UNVERIFIED": "has a down confiture cannot verify",
    "PFLIGHT_NON_TRANSACTIONAL": "is not transactional",
    "PFLIGHT_DESTRUCTIVE_GATED": "is gated as destructive",
    "PFLIGHT_IRREVERSIBLE": "is marked irreversible",
    "PFLIGHT_UNPARSEABLE": "could not be parsed",
}

#: ``change_set`` tiers a down can undo. Anything else — ``destructive``,
#: ``irreversible``, an absent tier (a ``.py`` migration, an unparseable file,
#: a statement outside the taxonomy) or a tier this release does not know —
#: takes the template: the schema says to deny on what it cannot classify.
_UNDOABLE_TIERS = frozenset({"additive", "reversible", "lock_risky"})


@dataclass(frozen=True)
class TemplateDecision:
    """Whether step 8 copies the database, and why."""

    take: bool
    reason: str
    #: The pending migrations that made ``auto`` take it, by version.
    forced_by: tuple[str, ...] = ()


def decide_template(
    mode: TemplateMode, payload: dict[str, Any] | None
) -> TemplateDecision:
    """Decide step 8 from *mode* and the pending-scoped preflight *payload*.

    *payload* is ``None`` when the preflight could not run or be read. Under
    ``auto`` every doubt takes the template: an unread payload, one not scoped
    to the pending set, and any finding the rule does not recognise as undoable.
    """
    if mode == "always":
        return TemplateDecision(take=True, reason="restore.template is always")
    if mode == "never":
        return TemplateDecision(take=False, reason="restore.template is never")
    if payload is None:
        return TemplateDecision(take=True, reason="no preflight result to decide on")
    try:
        return _decide_auto(payload)
    except KeyError, TypeError, AttributeError:
        return TemplateDecision(
            take=True, reason="the preflight payload could not be read"
        )


def _decide_auto(payload: dict[str, Any]) -> TemplateDecision:
    if payload["scope"] != "pending":
        return TemplateDecision(
            take=True,
            reason=f"the preflight judged scope {payload['scope']!r}, not 'pending'",
        )
    pending = payload["summary"]["migrations_checked"]
    if pending == 0:
        return TemplateDecision(take=False, reason="no pending migration")

    why: dict[str, list[str]] = {}
    for issue in payload["issues"]:
        verdict = _BLOCKING_ISSUES.get(issue["code"])
        if verdict is not None:
            why.setdefault(str(issue["migration"]), []).append(verdict)
    for change in payload["change_set"]["changes"]:
        tier = change.get("tier")
        if tier not in _UNDOABLE_TIERS:
            label = "unclassified" if tier is None else tier
            why.setdefault(str(change["migration"]), []).append(
                f"is {label} ({change['kind']})"
            )

    verdict = payload.get("reversible")
    if verdict is not True and not why:
        return TemplateDecision(
            take=True,
            reason=(
                "the preflight gives no `reversible` verdict (confiture < 1.40)"
                if verdict is None
                else "the preflight says `migrate down` cannot undo the pending set"
            ),
        )
    if not why:
        return TemplateDecision(
            take=False,
            reason=(
                f"all {pending} pending migration(s) have a down, are "
                "transactional and are undoable, so `migrate down` is the rollback"
            ),
        )
    return TemplateDecision(
        take=True,
        reason="; ".join(
            f"{version} {', '.join(dict.fromkeys(verdicts))}"
            for version, verdicts in why.items()
        ),
        forced_by=tuple(why),
    )


def read_pending_preflight(
    confiture_config: Path,
    migrations_dir: Path,
    *,
    database_url: str | None = None,
    project_dir: Path | None = None,
    timeout_seconds: int = 120,
) -> tuple[dict[str, Any] | None, str | None]:
    """Run confiture's static, pending-scoped preflight; return ``(payload, error)``.

    Exactly one of the two is ``None``.
    """
    cmd = [
        confiture_executable(),
        "migrate",
        "preflight",
        "--migrations-dir",
        str(migrations_dir),
        "--format",
        "json",
        "--config",
        str(confiture_config),
        "--scope",
        "pending",
    ]
    password_env: dict[str, str] = {}
    if database_url:
        url, password_env = move_password_to_env(database_url)
        cmd += ["--database-url", url]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=project_dir,
            env=child_env(password_env),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"confiture preflight did not run: {exc}"
    try:
        return json.loads(proc.stdout), None
    except ValueError:
        return None, f"confiture preflight exited {proc.returncode} without JSON"
