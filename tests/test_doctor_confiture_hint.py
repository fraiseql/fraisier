"""`confiture_version` names the package fraisier depends on (#454).

On PyPI, ``confiture`` is an unrelated configuration parser that ships a
top-level ``confiture`` module of its own, so ``pip install confiture``
installs a foreign package over the import fraisier uses. fraisier depends on
``fraiseql-confiture``, and the binary the check looks for on PATH is the one
in fraisier's own tool venv: ``--with-executables-from`` exposes it, at the
version fraisier pins.
"""

from __future__ import annotations

import re
from pathlib import Path

from fraisier import doctor

ROOT = Path(__file__).resolve().parent.parent

#: ``install confiture``, with any flags in between, but never
#: ``fraiseql-confiture``: the lookbehind refuses a name that ends in it.
WRONG_PACKAGE = re.compile(r"install(?:\s+-{1,2}\S+)*\s+(?<![-\w])confiture\b(?!-)")


def _missing_binary_hint(monkeypatch) -> str:
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    result = doctor.DOCTOR_CHECKS["confiture_version"].fn(None)
    assert result.status == "fail"
    return result.fix_hint or ""


def test_the_hint_exposes_confiture_from_fraisiers_own_venv(monkeypatch) -> None:
    hint = _missing_binary_hint(monkeypatch)
    assert "--with-executables-from fraiseql-confiture" in hint


def test_the_hint_never_names_the_unrelated_package(monkeypatch) -> None:
    hint = _missing_binary_hint(monkeypatch)
    assert not WRONG_PACKAGE.search(hint), hint


def test_the_docs_row_gives_the_same_fix() -> None:
    row = next(
        line
        for line in (ROOT / "docs" / "doctor.md").read_text().splitlines()
        if line.startswith("| `confiture_version` |")
    )
    assert "--with-executables-from fraiseql-confiture" in row
    assert not WRONG_PACKAGE.search(row), row


def test_nothing_shipped_tells_anyone_to_install_the_unrelated_package() -> None:
    shipped = [
        ROOT / "README.md",
        *sorted((ROOT / "fraisier").rglob("*.py")),
        *sorted((ROOT / "docs").rglob("*.md")),
    ]
    offenders = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in shipped
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if WRONG_PACKAGE.search(line)
    ]
    assert not offenders, offenders


def test_the_pattern_tells_the_two_packages_apart() -> None:
    assert WRONG_PACKAGE.search("pip install confiture")
    assert WRONG_PACKAGE.search("uv tool install --force confiture")
    assert not WRONG_PACKAGE.search("pip install fraiseql-confiture")
    assert not WRONG_PACKAGE.search(
        "uv tool install fraisier --with-executables-from fraiseql-confiture"
    )
    assert not WRONG_PACKAGE.search("install confiture-tools")
