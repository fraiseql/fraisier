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
import os
import subprocess
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
    "confiture.exceptions": ("ConfigurationError", "MigrationError", "RestoreError"),
    "confiture.models.migration": ("Migration",),
    # ``doctor``'s ``pg_tviews_contract`` asks confiture's own check (1.34.0).
    "confiture.platform": ("MINIMUM_PG_TVIEWS", "require_supported_pg_tviews_on"),
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


def _declared_floor() -> Version:
    """The lowest confiture the declared range admits."""
    for clause in _declared_specifier():
        if clause.operator in (">=", "=="):
            return Version(clause.version)
    msg = "the fraiseql-confiture requirement declares no lower bound"
    raise AssertionError(msg)


def test_declared_floor_admits_only_schema_qualified_drift() -> None:
    """The declared range must reject every confiture whose drift check is blind.

    Stated as the floor's *purpose* — that nothing below the capability floor
    is admitted — rather than as "the floor is exactly 1.0.0".  Adopting a
    newer confiture then satisfies this more strictly instead of breaking it,
    which is what happened when the cap moved to one minor at a time.
    """
    specifier = _declared_specifier()
    assert not specifier.contains("0.46.0"), (
        "confiture 0.46 reports CRITICAL MISSING_TABLE on schema-qualified DDL; "
        "the post-migration drift gate cannot trust it"
    )
    assert not specifier.contains("0.99.0")
    assert _declared_floor() >= SCHEMA_QUALIFIED_DRIFT_FLOOR, (
        f"the declared floor {_declared_floor()} is below "
        f"{SCHEMA_QUALIFIED_DRIFT_FLOOR}, the release that taught "
        "--check-live-drift to see schema-qualified DDL"
    )


def test_the_cap_admits_exactly_one_confiture_minor() -> None:
    """Adopting a confiture minor is a fraisier release, not a resolver outcome.

    While the cap was ``<2`` a user resolved the newest 1.x whatever fraisier
    locked, so every confiture audit here was retrospective by construction --
    1.26.0 published and changed a deploy's verdict with no fraisier release
    involved.  The cap is one minor so that adopting the next one is an edit
    someone makes on purpose.
    """
    specifier = _declared_specifier()
    floor = _declared_floor()
    next_minor = Version(f"{floor.major}.{floor.minor + 1}.0")
    assert specifier.contains(str(floor))
    assert not specifier.contains(str(next_minor)), (
        f"the declared range admits {next_minor}: confiture would be adopted "
        "by the user's resolver rather than by a fraisier release"
    )


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


#: The confiture release whose ``migrate preflight`` takes ``--scope pending``,
#: which fraisier's deploy and ``db preflight`` both pass. Older ones refuse
#: the flag, so every preflight would fail before it judged anything.
PREFLIGHT_SCOPE_PENDING_FLOOR = Version("1.35.0")


def test_the_floor_ships_preflight_scope_pending() -> None:
    assert _declared_floor() >= PREFLIGHT_SCOPE_PENDING_FLOOR


def test_installed_preflight_reads_scope_pending(tmp_path: Path) -> None:
    """The installed confiture takes the flag and reads a ledger for it.

    With no tracking database to read, ``--scope pending`` is refused with
    ``CONFIG_010`` rather than judging every file under a ``pending`` label.
    fraisier always passes ``--config``, so a ``CONFIG_010`` from a deploy
    means fraisier dropped it.
    """
    from fraisier.dbops.confiture_contract import envelope_error_code
    from fraisier.dbops.confiture_executable import confiture_executable

    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    result = subprocess.run(
        [
            confiture_executable(),
            "migrate",
            "preflight",
            "--migrations-dir",
            str(migrations_dir),
            "--format",
            "json",
            "--no-config",
            "--scope",
            "pending",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={k: v for k, v in os.environ.items() if "DATABASE_URL" not in k},
        timeout=60,
    )
    assert envelope_error_code(result.stdout) == "CONFIG_010", (
        result.stdout + result.stderr
    )
    assert result.returncode == 5


def test_restore_options_carry_the_password_off_argv() -> None:
    """``restore_backup`` hands the password to ``RestoreOptions`` (D3).

    That is safe only while confiture keeps it off ``pg_restore``'s argv and
    takes ``host``/``port`` as the ``-h``/``-p`` it does put there.
    """
    import dataclasses

    from confiture.core.restorer import DatabaseRestorer, RestoreOptions

    assert "password" in {f.name for f in dataclasses.fields(RestoreOptions)}
    options = RestoreOptions(
        backup_path=Path("/b.dump"),
        target_db="db",
        host="h",
        port=6543,
        username="u",
        password="sentinel-pw",
    )
    cmd = DatabaseRestorer()._build_command("data", options, parallel=False)
    assert "sentinel-pw" not in " ".join(cmd)
    assert cmd[cmd.index("-h") + 1 : cmd.index("-h") + 4] == ["h", "-p", "6543"]
