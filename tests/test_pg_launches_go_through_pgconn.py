"""No PostgreSQL client or confiture is launched outside ``PgConn`` (D2).

A launch that builds its own URL or environment is how a password reached
argv (#491) and how an ambient ``PGSERVICE`` redirected a child (#493). This
walks every function under ``fraisier/`` and fails on one that launches a
libpq client or confiture without going through one of the doors below.

A door is where the URL is parsed by ``PgConn`` and the child's environment
is built from it: ``_pg_cmd`` (env only), ``url_child`` / ``url_runner_child``
(a password-free URL on argv), ``PreflightDatabase._conn_flags``, or
``PgConn.child_env()`` itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "fraisier"

#: The libpq clients and confiture, by the first word of their argv.
PG_TOOLS = frozenset(
    {"psql", "pg_dump", "pg_dumpall", "pg_restore", "createdb", "dropdb"}
)

#: Calls that return such an argv.
ARGV_BUILDERS = frozenset({"confiture_executable", "build_psql_argv"})

#: Calls that launch a child. Functions of fraisier's own that launch an argv
#: they were given (drift's ``_run``) are added by :func:`_launchers`.
LAUNCHERS = frozenset(
    {"run", "Popen", "check_call", "check_output", "short_cmd", "cmd_with_input"}
)

DOORS = frozenset(
    {"_pg_cmd", "url_child", "url_runner_child", "_conn_flags", "child_env"}
)

#: ``module:function`` → why it launches without a door.
EXEMPT: dict[str, str] = {
    "dbops/preflight.py:extract_schema_only": (
        "`pg_restore --schema-only --file OUT ARCHIVE` reads an archive and "
        "never connects"
    ),
    "dbops/confiture.py:confiture_migrate": (
        "confiture reads its connection from its own config; fraisier names no target"
    ),
    "dbops/confiture.py:confiture_rebuild": "as confiture_migrate",
    "dbops/confiture.py:confiture_status": "as confiture_migrate",
    "dbops/drift.py:build_expected_schema": (
        "`confiture build --schema-only` writes a file and never connects"
    ),
    "dbops/drift.py:check_schema_drift": (
        "confiture reads its connection from its own config; database_url is "
        "only compared, never passed"
    ),
    "doctor.py:_check_confiture_version": "`confiture --version` never connects",
    "doctor.py:_confiture_cli_version": "`confiture --version` never connects",
}


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _starts_a_pg_argv(node: ast.AST) -> bool:
    if isinstance(node, ast.List) and node.elts:
        first = node.elts[0]
        if isinstance(first, ast.Constant) and first.value in PG_TOOLS:
            return True
        if isinstance(first, ast.Call) and _name(first.func) in ARGV_BUILDERS:
            return True
    return isinstance(node, ast.Call) and _name(node.func) in ARGV_BUILDERS


def _functions() -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = path.relative_to(ROOT).as_posix()
        found.extend(
            (f"{rel}:{node.name}", node)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
    return found


def _own_nodes(func: ast.AST) -> list[ast.AST]:
    """*func*'s nodes, without those of functions nested in it."""
    out: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _launchers(
    functions: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]],
) -> frozenset[str]:
    """:data:`LAUNCHERS`, plus every function that launches a parameter."""
    found = set(LAUNCHERS)
    grew = True
    while grew:
        grew = False
        for _, func in functions:
            params = {a.arg for a in (*func.args.args, *func.args.kwonlyargs)}
            launches_a_param = any(
                isinstance(n, ast.Call)
                and _name(n.func) in found
                and n.args
                and isinstance(n.args[0], ast.Name)
                and n.args[0].id in params
                for n in _own_nodes(func)
            )
            if launches_a_param and func.name not in found:
                found.add(func.name)
                grew = True
    return frozenset(found)


def _violation(func: ast.AST, launchers: frozenset[str] = LAUNCHERS) -> str | None:
    nodes = _own_nodes(func)
    called = {_name(n.func) for n in nodes if isinstance(n, ast.Call)}
    if not any(_starts_a_pg_argv(n) for n in nodes):
        return None
    if not called & launchers and not called & DOORS:
        return None  # builds an argv another function launches
    if any(
        isinstance(n, ast.Attribute) and n.attr == "environ" and _name(n.value) == "os"
        for n in nodes
    ):
        return "reads os.environ"
    if "move_password_to_env" in called:
        return "strips the password itself instead of parsing with PgConn"
    if not called & DOORS:
        return "launches without a PgConn door"
    return None


def test_every_pg_launch_goes_through_pgconn():
    functions = _functions()
    launchers = _launchers(functions)
    offenders = {
        where: why
        for where, func in functions
        if where not in EXEMPT and (why := _violation(func, launchers)) is not None
    }

    assert offenders == {}


def test_a_wrapper_that_launches_its_argument_is_a_launcher():
    """drift launches through ``_run(cmd)``; a wrapper must not hide a launch."""
    assert "_run" in _launchers(_functions())


def test_every_exemption_is_still_needed():
    """One that no longer matches would exempt whatever that function grows."""
    functions = _functions()
    launchers = _launchers(functions)
    flagged = {where for where, func in functions if _violation(func, launchers)}

    assert sorted(set(EXEMPT) - flagged) == []


def test_every_exemption_still_names_a_function():
    """An exemption outliving its function would exempt its successor."""
    names = {where for where, _ in _functions()}

    assert sorted(set(EXEMPT) - names) == []


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("def f(u):\n    subprocess.run(['psql', u])\n", True),
        ("def f(u):\n    subprocess.run([confiture_executable(), 'x', u])\n", True),
        ("def f(u):\n    runner.run(['pg_dump', u])\n", True),
        (
            "def f(u):\n    x, e = move_password_to_env(u)\n"
            "    subprocess.run(['psql', x], env=e)\n",
            True,
        ),
        (
            "def f(u):\n    c = PgConn.parse(u)\n"
            "    subprocess.run(['psql'], env={**os.environ, **c.pg_env()})\n",
            True,
        ),
        (
            "def f(u):\n    return _pg_cmd(['psql', '-c', 'x'], connection_url=u)\n",
            False,
        ),
        (
            "def f(u):\n    x, e = url_child(u)\n"
            "    subprocess.run(['psql', x], env=e)\n",
            False,
        ),
        ("def f(p):\n    return ['pg_restore', '--list', p]\n", False),
        ("def f():\n    subprocess.run(['git', 'status'])\n", False),
    ],
)
def test_the_check_tells_a_door_from_a_bypass(source, flagged):
    """The check itself, on code that does and does not bypass PgConn."""
    func = ast.parse(source).body[0]

    assert (_violation(func) is not None) is flagged
