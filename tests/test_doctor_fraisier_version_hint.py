"""`fraisier_version` repairs fraisier the way hosts install it (#457).

Hosts run fraisier from a ``uv tool`` venv (``bootstrap.py``), which has no
``pip``; a ``pip`` found on PATH installs into some other environment and
leaves the broken one in place.  The hint reinstalls the tool, in the same
form as bootstrap and the ``python_version`` hint.
"""

from __future__ import annotations

from pathlib import Path

from fraisier import doctor

ROOT = Path(__file__).resolve().parent.parent

REINSTALL = "uv tool install --force --python 3.14 fraisier==<version>"


def _unresolvable_hint(monkeypatch) -> str:
    def _raise(_name: str) -> str:
        raise ValueError("no metadata")

    monkeypatch.setattr("importlib.metadata.version", _raise)
    result = doctor.DOCTOR_CHECKS["fraisier_version"].fn(None)
    assert result.status == "fail"
    return result.fix_hint or ""


def test_the_hint_reinstalls_the_uv_tool(monkeypatch) -> None:
    assert REINSTALL in _unresolvable_hint(monkeypatch)


def test_the_hint_never_says_pip(monkeypatch) -> None:
    assert "pip" not in _unresolvable_hint(monkeypatch)


def test_the_docs_row_gives_the_same_fix() -> None:
    rows = [
        line
        for line in (ROOT / "docs" / "doctor.md").read_text().splitlines()
        if line.startswith("| `fraisier_version` |")
    ]
    assert len(rows) == 1, rows
    assert REINSTALL in rows[0]
    assert "pip" not in rows[0]
