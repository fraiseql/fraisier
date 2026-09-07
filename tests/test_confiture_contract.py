"""Contract test for the canonical confiture exit-code / error-code table.

Confiture owns the ``(exit_int → semantic class)`` table
(``confiture.core.error_codes.EXIT_CODE_SEMANTIC_CLASS``, emitted by
``confiture --exit-codes-json``), frozen since confiture #146.
``fraisier.dbops.confiture_contract`` derives from it — live when the installed
confiture exposes it, else a vendored copy. This test enumerates the matrix
against the frozen wire strings AND, when the installed confiture is new enough,
asserts the vendored copy still matches confiture's live table — the cross-repo
drift check (the Rust adapter vendors and verifies the same output).
"""

from __future__ import annotations

import types
from unittest import mock

import pytest

from fraisier.dbops.confiture_contract import (
    _VENDORED_EXIT_CLASS as VENDORED_EXIT_CLASS,
)
from fraisier.dbops.confiture_contract import (
    NO_LEDGER_ERROR_CODE,
    ConfitureFailureClass,
    classify_confiture_failure,
    confiture_error_codes,
    envelope_error_code,
)

# The canonical (exit_code, error_code) -> class matrix, mirrored from confiture
# docs/reference/exit-codes.md and identical to the Rust adapter's MATRIX.
# Symbolic codes are drawn from that doc's per-exit-code lists.
MATRIX: list[tuple[int | None, str | None, ConfitureFailureClass]] = [
    (0, None, ConfitureFailureClass.OK),
    (0, "MIGR_105", ConfitureFailureClass.OK),
    (1, None, ConfitureFailureClass.INTERNAL_ERROR),
    (1, "INTERNAL_ERROR", ConfitureFailureClass.INTERNAL_ERROR),
    (1, "SQL_001", ConfitureFailureClass.INTERNAL_ERROR),
    (2, None, ConfitureFailureClass.PRECONDITION_FAILED),
    (2, "PRECON_1001", ConfitureFailureClass.PRECONDITION_FAILED),
    (3, None, ConfitureFailureClass.DB_UNREACHABLE),
    (3, "CONFIG_006", ConfitureFailureClass.DB_UNREACHABLE),
    (4, None, ConfitureFailureClass.SCHEMA_ERROR),
    (4, "SCHEMA_001", ConfitureFailureClass.SCHEMA_ERROR),
    (5, None, ConfitureFailureClass.INVALID_CONFIG),
    (5, "CONFIG_010", ConfitureFailureClass.INVALID_CONFIG),
    (5, "VALID_001", ConfitureFailureClass.INVALID_CONFIG),
    (6, None, ConfitureFailureClass.LOCK_CONTENTION),
    (6, "LOCK_1300", ConfitureFailureClass.LOCK_CONTENTION),
    (7, None, ConfitureFailureClass.GIT_ERROR),
    (7, "GIT_001", ConfitureFailureClass.GIT_ERROR),
    (8, None, ConfitureFailureClass.IRREVERSIBLE_ROLLBACK),
    (8, "ROLLBACK_600", ConfitureFailureClass.IRREVERSIBLE_ROLLBACK),
    # A present exit code is authoritative and is never laundered by the error
    # code: exit 5 stays invalid_config even under a stray PRECON_1001, so a real
    # config error is never downgraded to a benign precondition.
    (5, "PRECON_1001", ConfitureFailureClass.INVALID_CONFIG),
    # ...but with no exit code at all (killed by signal), a PRECON_1001 envelope
    # still identifies "no ledger"; anything else is internal.
    (None, None, ConfitureFailureClass.INTERNAL_ERROR),
    (None, "PRECON_1001", ConfitureFailureClass.PRECONDITION_FAILED),
    (None, "LOCK_1300", ConfitureFailureClass.INTERNAL_ERROR),
    # An exit code outside the documented 0..8 universe is internal.
    (9, None, ConfitureFailureClass.INTERNAL_ERROR),
]


@pytest.mark.parametrize(("exit_code", "error_code", "expected"), MATRIX)
def test_classify_covers_the_confiture_exit_code_matrix(
    exit_code: int | None, error_code: str | None, expected: ConfitureFailureClass
) -> None:
    assert classify_confiture_failure(exit_code, error_code) is expected


def test_wire_strings_are_the_cross_repo_contract() -> None:
    """The exact class strings the Rust twin's `ExitClass::as_str` pins."""
    expected = {
        ConfitureFailureClass.OK: "ok",
        ConfitureFailureClass.INTERNAL_ERROR: "internal_error",
        ConfitureFailureClass.PRECONDITION_FAILED: "precondition_failed",
        ConfitureFailureClass.DB_UNREACHABLE: "db_unreachable",
        ConfitureFailureClass.SCHEMA_ERROR: "schema_error",
        ConfitureFailureClass.INVALID_CONFIG: "invalid_config",
        ConfitureFailureClass.LOCK_CONTENTION: "lock_contention",
        ConfitureFailureClass.GIT_ERROR: "git_error",
        ConfitureFailureClass.IRREVERSIBLE_ROLLBACK: "irreversible_rollback",
    }
    for member, wire in expected.items():
        assert str(member) == wire
        assert member == wire  # StrEnum: the member IS its wire string
    # Exactly one class per documented exit integer 0..8 — no more, no fewer.
    assert len(ConfitureFailureClass) == 9


def test_only_lock_contention_is_retriable() -> None:
    assert ConfitureFailureClass.LOCK_CONTENTION.is_retriable
    for member in ConfitureFailureClass:
        if member is not ConfitureFailureClass.LOCK_CONTENTION:
            assert not member.is_retriable


def test_no_ledger_error_code_is_precon_1001() -> None:
    # Pinned so a rename in confiture (a breaking change on its side) is caught
    # here rather than silently misclassifying "no ledger".
    assert NO_LEDGER_ERROR_CODE == "PRECON_1001"


def test_confiture_error_codes_module_resolves() -> None:
    """The live table has a home, and fraisier finds it wherever it moved to.

    confiture 1.0.0 moved the module from ``confiture.core.error_codes`` to
    ``confiture.error_codes`` without listing the move among its documented
    breaking changes.  ``_exit_class_table`` imported the old path directly, so
    the bump turned it into an ``ImportError`` raised at *fraisier* import time
    — every module that reaches ``dbops.confiture`` failed to collect.
    """
    assert confiture_error_codes() is not None, (
        "no confiture error-codes module resolved; the exit-code contract would "
        "silently fall back to the vendored copy"
    )


def test_vendored_table_matches_live_confiture() -> None:
    """The vendored copy must equal confiture's live table — the cross-repo guard.

    Asserted unconditionally: the floor is ``fraiseql-confiture>=1.0.0`` and
    1.0.0 *freezes* the exit codes, so "the installed confiture is too old to
    export the table" is no longer a state this suite can be in.  It used to
    skip, and a skip is not a pass.  The Rust adapter runs the equivalent diff
    against ``confiture --exit-codes-json``.
    """
    error_codes = confiture_error_codes()
    assert error_codes is not None
    live = error_codes.EXIT_CODE_SEMANTIC_CLASS

    vendored = {code: str(cls) for code, cls in VENDORED_EXIT_CLASS.items()}
    assert vendored == {int(c): n for c, n in live.items()}, (
        "vendored _VENDORED_EXIT_CLASS is stale vs the installed confiture; "
        "update it to match confiture's EXIT_CODE_SEMANTIC_CLASS"
    )


def test_exit_class_table_reads_the_live_module_not_the_vendored_copy() -> None:
    """The live table is what is *used*, not merely what is compared against.

    fraisier's vendored copy and confiture's live table are equal by
    construction (the test above), so reading the result proves nothing about
    which one produced it. Feed the resolver a module whose table differs from
    the vendored one and assert the difference reaches the output: a
    ``_exit_class_table`` that quietly always returned the vendored copy would
    pass every other test in this module.
    """
    from fraisier.dbops import confiture_contract

    stub = types.SimpleNamespace(
        EXIT_CODE_SEMANTIC_CLASS={0: "lock_contention", 6: "ok"}
    )
    with mock.patch.object(confiture_contract, "confiture_error_codes", lambda: stub):
        table = confiture_contract._exit_class_table()
    assert table == {
        0: ConfitureFailureClass.LOCK_CONTENTION,
        6: ConfitureFailureClass.OK,
    }


def test_exit_class_table_falls_back_when_confiture_exports_no_table() -> None:
    """No table anywhere degrades to the vendored copy rather than raising."""
    from fraisier.dbops import confiture_contract

    with mock.patch.object(confiture_contract, "confiture_error_codes", lambda: None):
        assert confiture_contract._exit_class_table() == dict(VENDORED_EXIT_CLASS)


def test_no_ledger_error_code_matches_confiture() -> None:
    """fraisier's no-ledger code stays confiture's, verified live."""
    error_codes = confiture_error_codes()
    assert error_codes is not None
    assert error_codes.NO_LEDGER_ERROR_CODE == NO_LEDGER_ERROR_CODE


def test_envelope_error_code_reads_the_confiture_failure_shape() -> None:
    envelope = '{"ok": false, "error": {"code": "PRECON_1001", "message": "no ledger"}}'
    assert envelope_error_code(envelope) == "PRECON_1001"
    # A success payload, non-JSON, or an envelope without a code → None.
    assert envelope_error_code('{"ok": true, "revision": "003"}') is None
    assert envelope_error_code("confiture: fatal\nnot json") is None
    assert envelope_error_code('{"ok": false, "error": {"message": "boom"}}') is None
