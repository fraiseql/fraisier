"""The confiture floor is a capability floor, not a formality.

The post-migration drift gate (#395) reads confiture's ``--check-live-drift``
verdict as authoritative.  Before confiture **1.0.0** that verdict was wrong on
any project whose DDL is schema-qualified: ``core/drift.py`` took the table name
with ``(\\w+)`` — so ``core.tb_meter`` parsed as a table called ``core`` — and
``core/schema_analyzer.py`` read the live side with a hardcoded
``table_schema = 'public'``.  Measured on 0.46 against a database applied
verbatim from its own DDL, the check reported ``CRITICAL MISSING_TABLE core``
and exit 1.

So a fraisier that resolves an older confiture would run the gate and fail
*every* deploy of a multi-schema project.  That is the #262 shape: a version
range wide enough to admit output fraisier misreads, and no signal while it
does.  The floor below is what keeps the gate's verdict meaningful, and this
test is why it cannot be lowered by accident.
"""

from __future__ import annotations

import importlib
import tomllib
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

if TYPE_CHECKING:
    from packaging.specifiers import SpecifierSet

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

#: The confiture release that made ``--check-live-drift`` see schema-qualified
#: DDL: tables keyed ``schema.table``, live side read with
#: ``table_schema = ANY(%s)``.
SCHEMA_QUALIFIED_DRIFT_FLOOR = Version("1.0.0")

#: Every confiture attribute fraisier imports, by module.  A confiture bump that
#: moves or renames one of these breaks a deploy path, and the failure would
#: otherwise surface as an ``ImportError`` inside a migration rather than here.
CONFITURE_IMPORT_SURFACE: dict[str, tuple[str, ...]] = {
    "confiture": ("MigrateUpResult", "MigrateDownResult"),
    "confiture.config.environment": ("Environment",),
    "confiture.core.builder": ("SchemaBuilder",),
    # Moved here from ``confiture.core.error_codes`` in confiture 1.0.0 — a
    # move its changelog does not list. ``dbops.confiture_contract`` tries
    # both homes; this row pins the one the current floor actually ships.
    "confiture.error_codes": ("EXIT_CODE_SEMANTIC_CLASS", "NO_LEDGER_ERROR_CODE"),
    "confiture.core.hooks": ("HookPhase",),
    # ``from confiture.core.hooks import builtin`` is the submodule below:
    # importing it is exactly what that form needs to resolve.
    "confiture.core.hooks.builtin": ("BackupHook",),
    "confiture.core.locking": ("LockAcquisitionError",),
    "confiture.core.migrator": ("Migrator",),
    "confiture.core.restorer": ("DatabaseRestorer", "RestoreOptions"),
    "confiture.core.view_manager": ("ViewManager",),
    "confiture.exceptions": ("MigrationError", "RestoreError"),
    "confiture.models.migration": ("Migration",),
}


def _declared_specifier() -> SpecifierSet:
    """The version range fraisier ships for confiture, read from pyproject."""
    data = tomllib.loads(_PYPROJECT.read_text())
    for raw in data["project"]["dependencies"]:
        requirement = Requirement(raw)
        if requirement.name == "fraiseql-confiture":
            return requirement.specifier
    msg = "pyproject declares no fraiseql-confiture dependency"
    raise AssertionError(msg)


def test_declared_floor_admits_only_schema_qualified_drift() -> None:
    """The declared range must reject every confiture whose drift check is blind.

    Asserted as "the floor rejects 0.99" rather than as a literal string, so
    the test still holds when the cap moves.
    """
    specifier = _declared_specifier()
    assert not specifier.contains("0.46.0"), (
        "confiture 0.46 reports CRITICAL MISSING_TABLE on schema-qualified DDL; "
        "the post-migration drift gate cannot trust it"
    )
    assert not specifier.contains("0.99.0")
    assert specifier.contains(str(SCHEMA_QUALIFIED_DRIFT_FLOOR))


def test_installed_confiture_satisfies_the_declared_range() -> None:
    """The environment the suite runs in is inside the range fraisier ships."""
    resolved = installed_version("fraiseql-confiture")
    assert _declared_specifier().contains(resolved), (
        f"installed fraiseql-confiture {resolved} is outside {_declared_specifier()}"
    )
    assert Version(resolved) >= SCHEMA_QUALIFIED_DRIFT_FLOOR


@pytest.mark.parametrize(
    ("module_name", "attribute"),
    [
        (module_name, attribute)
        for module_name, attributes in CONFITURE_IMPORT_SURFACE.items()
        for attribute in attributes
    ],
)
def test_confiture_import_surface_resolves(module_name: str, attribute: str) -> None:
    """Every confiture symbol fraisier reaches for still exists, by name."""
    module = importlib.import_module(module_name)
    assert hasattr(module, attribute), (
        f"{module_name}.{attribute} is gone — a fraisier deploy path imports it"
    )
