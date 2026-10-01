"""Wire format and parsers for the pgBackRest root helper (#424).

A physical restore stops a PostgreSQL cluster and rewrites its data directory as
the cluster's owner.  The deploy units that need that are ``NoNewPrivileges``, so
the work is done by a root helper behind a Unix socket (``fraisier.pgbackrest_helper``)
and this module is the contract between the two halves.

The helper is told **which operation**, and nothing it would have to trust::

    {"action": "info"}
    {"action": "restore", "set": "20261001-141744F_20261001-141820I"}

There is no field for a path, an argv, a cluster, a stanza or a target.  Those are
baked into the helper's root-owned unit file at scaffold time — a request that
could name them would be the escalation this design exists to avoid — and an
unexpected key is refused rather than ignored.

The parsers read pgBackRest's and ``pg_lsclusters``'s own output and are pinned
against captures from the real tools.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

#: The operations, in the order a refresh uses them.
ACTIONS: tuple[str, ...] = ("info", "status", "stop", "restore", "start")

#: pgBackRest's own label format: a full (``F``), or a differential/incremental
#: (``D``/``I``) naming the full it hangs off.
LABEL_RE = re.compile(r"\d{8}-\d{6}F(?:_\d{8}-\d{6}[DI])?")

#: Upper bound on a request line.  Nothing legitimate is near it.
MAX_REQUEST_BYTES = 4096


class RequestRejected(ValueError):
    """The request is not one this helper will act on."""


@dataclass(frozen=True)
class Request:
    """A validated request."""

    action: str
    set: str | None = None


def parse_request(raw: str | bytes) -> Request:
    """Validate one request line.

    Raises:
        RequestRejected: malformed, an unknown action, a ``set`` that is not a
            backup label (or on an action that takes none), a restore with no
            ``set``, or **any** key beyond ``action`` and ``set``.
    """
    try:
        text = raw.decode() if isinstance(raw, bytes) else raw
        payload: Any = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestRejected(f"malformed request: {exc}") from exc
    if not isinstance(payload, dict):
        raise RequestRejected("malformed request: expected a JSON object")

    unexpected = sorted(set(payload) - {"action", "set"})
    if unexpected:
        raise RequestRejected(
            f"unexpected field(s) {unexpected}: this helper takes an action and "
            f"nothing else — its stanza, repository, cluster and target are baked "
            f"into its unit"
        )

    action = payload.get("action")
    if action not in ACTIONS:
        raise RequestRejected(
            f"action not allowed: {action!r}; allowed: {list(ACTIONS)}"
        )

    label = payload.get("set")
    if action != "restore":
        if label is not None:
            raise RequestRejected(f"'set' is only for restore, not {action!r}")
        return Request(action)
    if label is None:
        raise RequestRejected(
            "restore requires a 'set': the backup label the client validated"
        )
    if not isinstance(label, str) or not LABEL_RE.fullmatch(label):
        raise RequestRejected(f"'set' is not a pgBackRest backup label: {label!r}")
    return Request(action, label)


def render_response(**fields: Any) -> bytes:
    """One JSON line carrying *fields*."""
    return json.dumps(fields).encode() + b"\n"


# ---------------------------------------------------------------------------
# pg_lsclusters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClusterRow:
    """One line of ``pg_lsclusters --no-header``."""

    version: str
    name: str
    port: int
    status: str
    owner: str
    datadir: str

    @property
    def is_online(self) -> bool:
        """Running.  ``down,recovery`` (restored, never started) is not."""
        return self.status.split(",", 1)[0] == "online"


def parse_lsclusters(text: str) -> list[ClusterRow]:
    """The clusters ``pg_lsclusters --no-header`` listed.

    Lines that are not a cluster row are skipped rather than misread — a bad
    parse here would hand a root helper the wrong data directory.
    """
    rows: list[ClusterRow] = []
    for line in text.splitlines():
        fields = line.split()
        # The columns are: version, cluster, port, status, owner, datadir, logfile.
        if len(fields) < 6 or not fields[2].isdigit() or not fields[5].startswith("/"):
            continue
        rows.append(
            ClusterRow(
                version=fields[0],
                name=fields[1],
                port=int(fields[2]),
                status=fields[3],
                owner=fields[4],
                datadir=fields[5],
            )
        )
    return rows


# ---------------------------------------------------------------------------
# pgbackrest restore
# ---------------------------------------------------------------------------

_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
_FILE_LINE = re.compile(
    r"restore file \S+ (?P<rest>.*)\((?P<size>[\d.]+)(?P<unit>[KMGT]?B), "
)
_SET_LINE = re.compile(r"restore backup set (?P<label>\S+?),")
_SIZE_LINE = re.compile(r"restore size = (?P<size>\S+), file total = (?P<total>\d+)")


@dataclass(frozen=True)
class RestoreSummary:
    """What a ``pgbackrest restore --log-level-console=detail`` said it did."""

    label: str | None = None
    restore_size: str | None = None
    files_total: int | None = None
    files_rewritten: int = 0
    bytes_rewritten: int = 0

    #: pgBackRest prints sizes rounded ("824KB"), so the byte count is a sum of
    #: rounded figures.  Said so in the type rather than left to a comment.
    bytes_rewritten_is_estimate: bool = True


def parse_restore_log(text: str) -> RestoreSummary:
    """Read the label, totals and rewritten-file count off a restore log.

    A ``--delta`` restore lists **every** file, marking the ones it left alone
    (``exists and matches backup``, ``exists and is zero size``); the files it
    actually wrote are the ones without that marker.  pgBackRest reports only the
    whole backup's ``restore size`` — never the delta — so the rewritten byte
    count is the sum of the (rounded) sizes the log gives for those files.
    """
    label: str | None = None
    restore_size: str | None = None
    files_total: int | None = None
    rewritten = 0
    written_bytes = 0
    for line in text.splitlines():
        if (match := _SET_LINE.search(line)) and label is None:
            label = match.group("label")
        elif match := _SIZE_LINE.search(line):
            restore_size = match.group("size")
            files_total = int(match.group("total"))
        elif (match := _FILE_LINE.search(line)) and "exists and" not in match.group(
            "rest"
        ):
            rewritten += 1
            written_bytes += int(
                float(match.group("size")) * _SIZE_UNITS[match.group("unit")]
            )
    return RestoreSummary(
        label=label,
        restore_size=restore_size,
        files_total=files_total,
        files_rewritten=rewritten,
        bytes_rewritten=written_bytes,
    )
