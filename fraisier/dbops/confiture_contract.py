"""Canonical ``(exit_code, error_code)`` → semantic class for confiture failures.

Confiture is the single source of truth. It owns the ``(exit_int → semantic
class)`` table — ``confiture.core.error_codes.EXIT_CODE_SEMANTIC_CLASS``, emitted
by ``confiture --exit-codes-json`` — frozen as a stability contract (a class
rename is a breaking change). This module *derives* from it: at runtime it prefers
the installed confiture's table (zero drift), falling back to a vendored copy when
the installed confiture predates it (fraisier still pins ``fraiseql-confiture <
0.36``, which has no table yet). ``tests/test_confiture_contract.py`` asserts the
vendored copy still matches confiture's live table whenever confiture exposes it,
so a drift fails CI.

The Rust adapter
(``fraisier-core/crates/fraisier-adapter-confiture/src/exit_codes.rs``) vendors
the same ``confiture --exit-codes-json`` output and verifies it the same way —
both consumers project one confiture-owned table. Every consumer in
:mod:`fraisier.dbops` is a *thin projection* of
:func:`classify_confiture_failure` rather than re-encoding the mapping ad hoc.
"""

from __future__ import annotations

import json
from enum import StrEnum

# Confiture's error code for a reachable-but-uninitialised database — no
# migration ledger (``tb_confiture`` absent). It exits 2, and is the one code
# that identifies "no ledger" when only the structured envelope is in hand.
NO_LEDGER_ERROR_CODE = "PRECON_1001"


class ConfitureFailureClass(StrEnum):
    """The semantic class of one confiture process exit — the canonical taxonomy
    shared with the Rust adapter. There is exactly one class per documented exit
    integer ``0..8``; each member's *value* is the cross-repo wire string.
    """

    #: Exit 0 — success. Present so the table is total; never an error.
    OK = "ok"
    #: Exit 1 — generic / unclassified failure: SQL or hook execution, an
    #: ambiguous-change advisory, ``status: pending``, or the ``INTERNAL_ERROR``
    #: envelope confiture emits for an unexpected exception.
    INTERNAL_ERROR = "internal_error"
    #: Exit 2 — reachable-but-uninitialised database (``PRECON_1001``, no ledger).
    PRECONDITION_FAILED = "precondition_failed"
    #: Exit 3 — database connection failed (host / auth / network unreachable).
    DB_UNREACHABLE = "db_unreachable"
    #: Exit 4 — schema / DDL / build error.
    SCHEMA_ERROR = "schema_error"
    #: Exit 5 — configuration invalid, or a validation / sync / lint failure.
    INVALID_CONFIG = "invalid_config"
    #: Exit 6 — lock or connection-pool contention (**retriable**).
    LOCK_CONTENTION = "lock_contention"
    #: Exit 7 — git / pgGit / grant-accompaniment error.
    GIT_ERROR = "git_error"
    #: Exit 8 — irreversible rollback, or inconsistent state after rollback.
    IRREVERSIBLE_ROLLBACK = "irreversible_rollback"

    @property
    def is_retriable(self) -> bool:
        """Whether a failure of this class is worth retrying unchanged. Only lock
        or connection-pool contention is — another writer holds the lock.
        """
        return self is ConfitureFailureClass.LOCK_CONTENTION


# Vendored copy of confiture's EXIT_CODE_SEMANTIC_CLASS — the runtime fallback for
# an installed confiture too old to export the table (< the version that added it).
# The contract test keeps it in lockstep with confiture's live table.
_VENDORED_EXIT_CLASS: dict[int, ConfitureFailureClass] = {
    0: ConfitureFailureClass.OK,
    1: ConfitureFailureClass.INTERNAL_ERROR,
    2: ConfitureFailureClass.PRECONDITION_FAILED,
    3: ConfitureFailureClass.DB_UNREACHABLE,
    4: ConfitureFailureClass.SCHEMA_ERROR,
    5: ConfitureFailureClass.INVALID_CONFIG,
    6: ConfitureFailureClass.LOCK_CONTENTION,
    7: ConfitureFailureClass.GIT_ERROR,
    8: ConfitureFailureClass.IRREVERSIBLE_ROLLBACK,
}


def _exit_class_table() -> dict[int, ConfitureFailureClass]:
    """The confiture-owned ``exit_int → class`` table.

    Prefers the installed confiture's ``EXIT_CODE_SEMANTIC_CLASS`` (zero drift);
    falls back to the vendored copy when confiture is too old to export it, or
    when it exports a class this fraisier does not model yet (a confiture newer
    than us — the contract test surfaces the gap rather than crashing import).
    """
    from confiture.core import error_codes  # confiture is a hard dependency

    # `EXIT_CODE_SEMANTIC_CLASS` is new in a future confiture and absent from the
    # currently pinned line — read it dynamically so this resolves both ways.
    table = getattr(error_codes, "EXIT_CODE_SEMANTIC_CLASS", None)
    if table is None:
        return dict(_VENDORED_EXIT_CLASS)
    try:
        return {int(code): ConfitureFailureClass(name) for code, name in table.items()}
    except ValueError:
        return dict(_VENDORED_EXIT_CLASS)


_EXIT_CLASS = _exit_class_table()


def classify_confiture_failure(
    exit_code: int | None, error_code: str | None = None
) -> ConfitureFailureClass:
    """Classify a confiture process exit into its semantic class.

    Keyed on the integer exit code (confiture's frozen ``exit-codes.md`` table).
    The error code is consulted for one refinement only: a ``PRECON_1001``
    envelope identifies "no ledger" when the process left **no** exit code
    (killed by a signal) — so a consumer holding only the structured envelope
    still classifies it. A present exit code is authoritative and is **never**
    laundered by the error code: an exit 5 (config invalid) stays
    :attr:`~ConfitureFailureClass.INVALID_CONFIG` even if a stray ``PRECON_1001``
    rides along, so a severe failure is never downgraded to a benign
    precondition. (For a conformant confiture the two always agree —
    ``PRECON_1001`` only ever exits 2 — so this matters only for a malformed or
    skewed producer.)
    """
    known = _EXIT_CLASS.get(exit_code) if exit_code is not None else None
    if known is not None:
        return known
    if exit_code is None and error_code == NO_LEDGER_ERROR_CODE:
        return ConfitureFailureClass.PRECONDITION_FAILED
    return ConfitureFailureClass.INTERNAL_ERROR


def envelope_error_code(output: str) -> str | None:
    """The ``error.code`` from a confiture ``--format json`` error envelope.

    Confiture writes ``{"ok": false, "error": {"code": ..., ...}}`` on every
    failure path under ``--format json``. Returns ``None`` when *output* is not
    that envelope — not JSON, a success payload, or an envelope carrying no code.
    """
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        return code if isinstance(code, str) else None
    return None
