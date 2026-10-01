"""``database.restore.source: pgbackrest`` — the one place its shape is defined (#424).

Read by the config validator, the scaffold (which bakes it into a root helper's
unit) and the restore source itself, so a pattern is written once.  A physical
restore replaces **every** database in the staging cluster, so what it is told —
which stanza, which cluster, which instant — is the part worth being strict about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: What ``restore.source`` may say.  ``dump`` is the default.
RESTORE_SOURCES: tuple[str, ...] = ("dump", "pgbackrest")

#: The keys ``restore.pgbackrest`` accepts.  Closed on purpose: a misspelt
#: ``targt:`` would silently restore ``latest``.
PGBACKREST_KEYS: frozenset[str] = frozenset(
    {"stanza", "repo", "cluster", "target", "timeout_seconds"}
)

#: Becomes an argv element in a root helper's unit file, so: a plain name.
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

#: ``<major>/<name>`` — a Debian ``postgresql-common`` cluster, which is what
#: ``pg_lsclusters`` and ``pg_ctlcluster`` speak.
CLUSTER_RE = re.compile(r"[0-9]+/[A-Za-z0-9][A-Za-z0-9_.-]*")

#: An instant **with** an offset.  Without one pgBackRest reads it in the
#: server's own zone, which is a different instant on every host.  ASCII digits
#: only: Python's ``\d`` matches every Unicode decimal digit.
TARGET_INSTANT_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}(?::?[0-9]{2})?)"
)

LATEST = "latest"

#: Seconds a restore or a cluster start may take, when nothing says otherwise.
#: Generous: the motivating refresh restores ~160 GB.
DEFAULT_TIMEOUT_SECONDS = 6 * 3600
MIN_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class PgBackRestSpec:
    """``restore.pgbackrest``, parsed."""

    stanza: str
    repo: int
    cluster: str
    target: str = LATEST
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    @property
    def is_latest(self) -> bool:
        return self.target == LATEST

    @property
    def major(self) -> str:
        return self.cluster.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.cluster.split("/", 1)[1]


def parse_pgbackrest(restore: dict[str, Any]) -> PgBackRestSpec | None:
    """The spec from a *validated* ``restore`` block; ``None`` for the dump source."""
    if restore.get("source", "dump") != "pgbackrest":
        return None
    block = restore["pgbackrest"]
    return PgBackRestSpec(
        stanza=str(block["stanza"]),
        repo=int(block["repo"]),
        cluster=str(block["cluster"]),
        target=str(block.get("target", LATEST)),
        timeout_seconds=int(block.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
    )
