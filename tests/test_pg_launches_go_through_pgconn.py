"""No PostgreSQL client or confiture is launched outside ``PgConn``.

A launch that builds its own URL or environment is how a password reached
argv (#491) and how an ambient ``PGSERVICE`` redirected a child (#493). This
walks every function under ``fraisier/`` and fails on one that launches a
libpq client or confiture without going through one of the doors below.

A door is where the URL is parsed by ``PgConn`` and the child's environment
is built from it: ``_pg_cmd`` (env only), ``url_child`` / ``url_runner_child``
(a password-free URL on argv), ``PreflightDatabase._conn_flags``,
``PgConn.child_env()`` itself, or ``config_child_env`` for a confiture child
that reads its URLs from its config.

An in-process connection has no child environment to build: libpq reads
``os.environ`` for every keyword its conninfo leaves out (#496). Its one door
is ``PgConn.conninfo()``, which pins them, passed as the conninfo itself with
no libpq keyword beside it. confiture's own session must be opened with
``connection_factory=migration_connection``, which is that door.

A syntactic check, so a heuristic. It follows names imported or aliased from
psycopg, a door name reassigned before the connect, the module body, and a
launch whose ``env=`` some path leaves ``None``. It cannot follow data: a
subclass's ``connect``, a ``functools.partial``, a door built for another URL,
or a connection opened inside a dependency (confiture's backup hook) pass it.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
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
    {
        "_pg_cmd",
        "url_child",
        "url_runner_child",
        "_conn_flags",
        "child_env",
        "config_child_env",
    }
)

#: ``module.connect`` calls that open a libpq connection in process.
CONNECT_MODULES = frozenset({"psycopg", "psycopg2", "Connection", "AsyncConnection"})

#: Pools that open them.
POOLS = frozenset(
    {
        "ConnectionPool",
        "AsyncConnectionPool",
        "NullConnectionPool",
        "AsyncNullConnectionPool",
    }
)

#: ``module:function`` → why it launches without a door.
EXEMPT: dict[str, str] = {
    "dbops/preflight.py:extract_schema_only": (
        "`pg_restore --schema-only --file OUT ARCHIVE` reads an archive and "
        "never connects"
    ),
    "dbops/drift.py:build_expected_schema": (
        "`confiture build --schema-only` writes a file and never connects"
    ),
    "doctor.py:_check_confiture_version": "`confiture --version` never connects",
    "doctor.py:_confiture_cli_version": "`confiture --version` never connects",
}


#: ``module:function`` → why a path of it launches with ``env=None`` on purpose.
#: Every other rule still applies to it.
INHERITS_ON_PURPOSE: dict[str, str] = {
    "cli/db.py:db_exec": (
        "with no admin_url the operator's own command connects by database "
        "name, as their psql would, through their shell's PG*"
    ),
}

#: :func:`_violation`'s answer for that rule.
INHERITS = "a path launches with env=None, inheriting every PG*"


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
    if _may_launch_without_env(nodes, launchers):
        return INHERITS
    return None


def _is_none(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _may_launch_without_env(nodes: list[ast.AST], launchers: frozenset[str]) -> bool:
    """Whether a launch's ``env=`` is ``None``, or a name some path sets to it."""
    set_to_none = {
        target.id
        for n in nodes
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and _is_none(n.value)
        for target in (n.targets if isinstance(n, ast.Assign) else [n.target])
        if isinstance(target, ast.Name)
    }
    return any(
        isinstance(n, ast.Call)
        and _name(n.func) in launchers
        and k.arg == "env"
        and (
            _is_none(k.value)
            or (isinstance(k.value, ast.Name) and k.value.id in set_to_none)
        )
        for n in nodes
        if isinstance(n, ast.Call)
        for k in n.keywords
    )


#: The packages whose ``connect`` and pools open a libpq connection.
CONNECT_PACKAGES = frozenset({"psycopg", "psycopg2", "psycopg_pool"})

#: Keywords that may sit beside the door: none of them is a libpq keyword.
DOOR_COMPANIONS = frozenset(
    {
        "autocommit",
        "connect_timeout",
        "row_factory",
        "cursor_factory",
        "prepare_threshold",
        "context",
        "kwargs",
        "min_size",
        "max_size",
        "open",
        "name",
        "timeout",
    }
)


@dataclass(frozen=True)
class _Imported:
    """What a module calls a connect, a connection class and a pool."""

    modules: frozenset[str]
    connects: frozenset[str]
    pools: frozenset[str]
    migrators: frozenset[str]


#: confiture's ways into a migration session, which must be given the door.
CONFITURE_SESSIONS = frozenset({"Migrator", "MigratorSession"})


def _imported(tree: ast.Module) -> _Imported:
    modules, connects, pools = set(CONNECT_MODULES), set(), set(POOLS)
    migrators = set(CONFITURE_SESSIONS)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.split(".")[0] == "confiture"
        ):
            migrators.update(
                alias.asname
                for alias in node.names
                if alias.name in CONFITURE_SESSIONS and alias.asname
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                package = alias.name.split(".")[0]
                if package in CONNECT_PACKAGES:
                    modules.add(alias.asname or package)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] not in CONNECT_PACKAGES:
                continue
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name == "connect":
                    connects.add(local)
                elif alias.name in CONNECT_MODULES:
                    modules.add(local)
                elif alias.name in POOLS:
                    pools.add(local)
    return _Imported(
        frozenset(modules), frozenset(connects), frozenset(pools), frozenset(migrators)
    )


def _opens_a_connection(node: ast.AST, names: _Imported) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "connect":
        return _name(func.value) in names.modules
    if isinstance(func, ast.Name) and func.id in names.connects:
        return True
    return _name(func) in names.pools


def _is_door(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and _name(node.func) == "conninfo"


def _holds_the_door(name: str, before: int, nodes: list[ast.AST]) -> bool:
    """Whether *name*'s last assignment before line *before* is the door."""
    assigned = [
        n
        for n in nodes
        if isinstance(n, ast.Assign)
        and n.lineno < before
        and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)
    ]
    if not assigned:
        return False
    return _is_door(max(assigned, key=lambda n: n.lineno).value)


def _overrides(call: ast.Call) -> bool:
    """Whether a keyword beside the door could override one of its keywords."""
    for keyword in call.keywords:
        if keyword.arg == "conninfo":
            continue
        if keyword.arg not in DOOR_COMPANIONS:
            return True
        if keyword.arg == "kwargs" and isinstance(keyword.value, ast.Dict):
            keys = keyword.value.keys
            if any(
                not isinstance(k, ast.Constant) or k.value not in DOOR_COMPANIONS
                for k in keys
            ):
                return True
    return False


def _confiture_session_violation(call: ast.Call, names: _Imported) -> str | None:
    """A confiture session must open its connection through the door.

    ``Migrator.from_config(...)`` and ``MigratorSession(...)`` take
    ``connection_factory=``; confiture's ``create_connection`` is the bare
    URL's connect itself.
    """
    func = call.func
    if _name(func) == "create_connection":
        return "connects through confiture's create_connection, on the bare URL"
    opens_a_session = (
        isinstance(func, ast.Attribute)
        and func.attr == "from_config"
        and _name(func.value) in names.migrators
    ) or (isinstance(func, ast.Name) and func.id in names.migrators - {"Migrator"})
    if not opens_a_session:
        return None
    factory = next(
        (k.value for k in call.keywords if k.arg == "connection_factory"), None
    )
    if factory is None or _name(factory) != "migration_connection":
        return (
            "opens a confiture session without connection_factory=migration_connection"
        )
    return None


def _scope_violation(scope: ast.AST, names: _Imported) -> str | None:
    nodes = _own_nodes(scope)
    for call in nodes:
        if not isinstance(call, ast.Call):
            continue
        if why := _confiture_session_violation(call, names):
            return why
        if not _opens_a_connection(call, names):
            continue
        given = [
            *call.args[:1],
            *(k.value for k in call.keywords if k.arg == "conninfo"),
        ]
        if not any(
            _is_door(arg)
            or (
                isinstance(arg, ast.Name)
                and _holds_the_door(arg.id, call.lineno, nodes)
            )
            for arg in given
        ):
            return "connects in process without PgConn.conninfo()"
        if _overrides(call):
            return "overrides PgConn.conninfo() with a keyword beside it"
    return None


def _in_process_violations(tree: ast.Module, rel: str) -> dict[str, str]:
    """``where`` → why, for the module body and every function in *tree*."""
    names = _imported(tree)
    scopes: list[tuple[str, ast.AST]] = [(f"{rel}:<module>", tree)]
    scopes.extend(
        (f"{rel}:{node.name}", node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    return {
        where: why
        for where, scope in scopes
        if (why := _scope_violation(scope, names)) is not None
    }


def test_every_in_process_connect_goes_through_conninfo():
    offenders = {}
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        offenders.update(
            _in_process_violations(tree, path.relative_to(ROOT).as_posix())
        )

    assert offenders == {}


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("def f(u):\n    psycopg.connect(u, autocommit=True)\n", True),
        ("def f(u):\n    psycopg2.connect(u)\n", True),
        ("async def f(u):\n    await psycopg.AsyncConnection.connect(u)\n", True),
        ("def f(u):\n    AsyncConnectionPool(u, kwargs={})\n", True),
        ("def f(u):\n    ConnectionPool(conninfo=u)\n", True),
        ("def f(u):\n    psycopg.connect(**PgConn.parse(u).connect_kwargs())\n", True),
        (
            "def f(u):\n    c = PgConn.parse(u).conninfo()\n"
            "    psycopg.connect(u, application_name=c)\n",
            True,
        ),
        ("def f(u):\n    psycopg.connect(PgConn.parse(u).conninfo())\n", False),
        (
            "def f(u):\n    c = PgConn.parse(u).conninfo()\n"
            "    AsyncConnectionPool(c, kwargs={})\n",
            False,
        ),
        (
            "def f(u):\n    c = PgConn.parse(u).conninfo()\n"
            "    ConnectionPool(conninfo=c)\n",
            False,
        ),
        ("def f(p):\n    sock.connect(p)\n", False),
        ("async def f(self):\n    await self.adapter.connect()\n", False),
        # A name imported or aliased from psycopg.
        ("from psycopg import connect\ndef f(u):\n    connect(u)\n", True),
        ("from psycopg import connect as c\ndef f(u):\n    c(u)\n", True),
        ("import psycopg as pg\ndef f(u):\n    pg.connect(u)\n", True),
        ("from psycopg import Connection as C\ndef f(u):\n    C.connect(u)\n", True),
        ("from psycopg_pool import ConnectionPool as P\ndef f(u):\n    P(u)\n", True),
        (
            "from psycopg import connect\n"
            "def f(u):\n    connect(PgConn.parse(u).conninfo())\n",
            False,
        ),
        # A keyword beside the door overrides it.
        (
            "def f(u):\n    psycopg.connect(PgConn.parse(u).conninfo(), user='x')\n",
            True,
        ),
        ("def f(u, k):\n    psycopg.connect(PgConn.parse(u).conninfo(), **k)\n", True),
        (
            "def f(u):\n    c = PgConn.parse(u).conninfo()\n"
            "    AsyncConnectionPool(c, kwargs={'options': '-c x=1'})\n",
            True,
        ),
        (
            "def f(u):\n    psycopg.connect(PgConn.parse(u).conninfo(), "
            "autocommit=True, connect_timeout=5)\n",
            False,
        ),
        (
            "def f(u):\n    c = PgConn.parse(u).conninfo()\n"
            "    AsyncConnectionPool(c, min_size=1, kwargs={'row_factory': r})\n",
            False,
        ),
        # A door name that no longer holds the door when it connects.
        (
            "def f(u):\n    c = PgConn.parse(u).conninfo()\n    c = u\n"
            "    psycopg.connect(c)\n",
            True,
        ),
        (
            "def f(u):\n    c = u\n    c = PgConn.parse(u).conninfo()\n"
            "    psycopg.connect(c)\n",
            False,
        ),
        # Outside any function.
        ("psycopg.connect(URL)\n", True),
        # confiture's own connection.
        ("def f(e):\n    Migrator.from_config(e, migrations_dir=d)\n", True),
        (
            "def f(e):\n    Migrator.from_config(e, connection_factory=psycopg.connect)\n",
            True,
        ),
        (
            "def f(e):\n    Migrator.from_config(e, "
            "connection_factory=migration_connection)\n",
            False,
        ),
        (
            "from confiture.core.migrator import Migrator as M\n"
            "def f(e):\n    M.from_config(e)\n",
            True,
        ),
        ("def f(e, d):\n    MigratorSession(e, d)\n", True),
        (
            "def f(e, d):\n    MigratorSession(e, d, "
            "connection_factory=migration_connection)\n",
            False,
        ),
        (
            "from confiture.core.connection import create_connection\n"
            "def f(u):\n    create_connection(u)\n",
            True,
        ),
    ],
)
def test_the_check_tells_an_in_process_door_from_a_bypass(source, flagged):
    assert bool(_in_process_violations(ast.parse(source), "m.py")) is flagged


def test_every_pg_launch_goes_through_pgconn():
    functions = _functions()
    launchers = _launchers(functions)
    offenders = {
        where: why
        for where, func in functions
        if where not in EXEMPT
        and (why := _violation(func, launchers)) is not None
        and not (why == INHERITS and where in INHERITS_ON_PURPOSE)
    }

    assert offenders == {}


def test_every_inheritance_on_purpose_still_inherits():
    functions = _functions()
    launchers = _launchers(functions)
    inherits = {
        where for where, func in functions if _violation(func, launchers) == INHERITS
    }

    assert sorted(set(INHERITS_ON_PURPOSE) - inherits) == []


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
        # A door in one branch, the whole environment in the other.
        (
            "def f(u):\n    env = None\n    if u:\n        x, env = url_child(u)\n"
            "    subprocess.run([confiture_executable(), 'x'], env=env)\n",
            True,
        ),
        (
            "def f(u):\n    env: dict | None = None\n    if u:\n"
            "        x, env = url_child(u)\n"
            "    subprocess.run([confiture_executable(), 'x'], env=env)\n",
            True,
        ),
        (
            "def f(u):\n    x, e = url_child(u)\n"
            "    subprocess.run(['psql', x], env=None)\n",
            True,
        ),
        (
            "def f(u, c):\n    if u:\n        x, env = url_child(u)\n"
            "    else:\n        env = config_child_env(c)\n"
            "    subprocess.run([confiture_executable(), 'x'], env=env)\n",
            False,
        ),
    ],
)
def test_the_check_tells_a_door_from_a_bypass(source, flagged):
    """The check itself, on code that does and does not bypass PgConn."""
    func = ast.parse(source).body[0]

    assert (_violation(func) is not None) is flagged
