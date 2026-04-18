"""Low-level PostgreSQL helpers.

Every function shells out to psql/createdb/dropdb using a caller-supplied
``connection_url``.  Host, port, user and password are extracted from the
URL; no sudo wrapper and no peer-auth fallback.

The requirement that strategies provide an ``admin_url`` (and therefore a
``connection_url`` at this layer) is enforced at config-load time by
``fraisier.validation``.
"""

import os
import subprocess
from urllib.parse import urlparse

from fraisier.dbops._validation import validate_pg_identifier


def _parse_connection_flags(connection_url: str) -> tuple[list[str], dict[str, str]]:
    """Extract CLI flags and env vars from a PostgreSQL connection URL.

    Returns (flags, env) where *flags* are ``-h host -p port -U user``
    and *env* contains ``PGPASSWORD`` when the URL has a password.
    """
    parsed = urlparse(connection_url)
    flags: list[str] = []
    if parsed.hostname:
        flags.extend(["-h", parsed.hostname])
    if parsed.port:
        flags.extend(["-p", str(parsed.port)])
    if parsed.username:
        flags.extend(["-U", parsed.username])
    env: dict[str, str] = {}
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    return flags, env


def _pg_cmd(
    cmd: list[str],
    *,
    connection_url: str,
) -> tuple[int, str, str]:
    """Run a PostgreSQL CLI command against *connection_url*.

    Host, port, user and password are taken from the URL and injected
    into the command (``-h -p -U``) and environment (``PGPASSWORD``).

    Returns (exit_code, stdout, stderr).
    """
    conn_flags, extra_env = _parse_connection_flags(connection_url)
    full_cmd = [cmd[0], *conn_flags, *cmd[1:]]
    run_env = {**os.environ, **extra_env} if extra_env else None

    result = subprocess.run(
        full_cmd,
        capture_output=True,
        text=True,
        check=False,
        env=run_env,
    )
    return result.returncode, result.stdout, result.stderr


def run_psql(
    sql_or_file: str,
    *,
    db_name: str,
    connection_url: str,
) -> tuple[int, str, str]:
    """Execute a psql command against *db_name*.

    *sql_or_file* is passed via ``-c`` (inline SQL).
    """
    cmd = ["psql", "-d", db_name, "-c", sql_or_file]
    return _pg_cmd(cmd, connection_url=connection_url)


def run_sql(
    sql: str,
    *,
    db_name: str,
    connection_url: str,
) -> tuple[int, str, str]:
    """Execute inline SQL with tuples-only output (``-t -A``)."""
    cmd = ["psql", "-d", db_name, "-t", "-A", "-c", sql]
    return _pg_cmd(cmd, connection_url=connection_url)


def check_db_exists(
    db_name: str,
    *,
    connection_url: str,
) -> bool:
    """Return True if the database *db_name* exists."""
    validate_pg_identifier(db_name, "database name")
    # db_name is validated as a safe identifier — embed directly in SQL.
    # psql >=18 no longer substitutes :'var' in -c mode.
    code, stdout, _ = _pg_cmd(
        [
            "psql",
            "-t",
            "-A",
            "-c",
            f"SELECT count(*) FROM pg_database WHERE datname='{db_name}'",
        ],
        connection_url=connection_url,
    )
    if code != 0:
        return False
    return stdout.strip() == "1"


def terminate_backends(
    db_name: str,
    *,
    connection_url: str,
) -> tuple[int, str, str]:
    """Terminate all connections to *db_name*."""
    validate_pg_identifier(db_name, "database name")
    # db_name is validated as a safe identifier — embed directly in SQL.
    # psql >=18 no longer substitutes :'var' in -c mode.
    return _pg_cmd(
        [
            "psql",
            "-c",
            "SELECT pg_terminate_backend(pid) "
            "FROM pg_stat_activity "
            f"WHERE datname='{db_name}' AND pid <> pg_backend_pid()",
        ],
        connection_url=connection_url,
    )


def drop_db(
    db_name: str,
    *,
    force_disconnect: bool = False,
    force: bool = False,
    connection_url: str,
) -> tuple[int, str, str]:
    """Drop database *db_name*.

    If *force_disconnect* is True, terminate all backends first.
    If *force* is True, use DROP DATABASE ... WITH (FORCE) via psql.
    """
    validate_pg_identifier(db_name, "database name")
    if force_disconnect:
        terminate_backends(db_name, connection_url=connection_url)
    if force:
        sql = f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)"
        return _pg_cmd(["psql", "-c", sql], connection_url=connection_url)
    return _pg_cmd(["dropdb", "--if-exists", db_name], connection_url=connection_url)


def create_db(
    db_name: str,
    *,
    template: str | None = None,
    owner: str | None = None,
    connection_url: str,
) -> tuple[int, str, str]:
    """Create database *db_name*, optionally from *template*."""
    validate_pg_identifier(db_name, "database name")
    if template:
        validate_pg_identifier(template, "template name")
    if owner:
        validate_pg_identifier(owner, "owner name")
    cmd = ["createdb"]
    if template:
        cmd.extend(["-T", template])
    if owner:
        cmd.extend(["-O", owner])
    cmd.append(db_name)
    return _pg_cmd(cmd, connection_url=connection_url)


def stop_service(service_name: str) -> tuple[int, str, str]:
    """Stop a systemd service using sudo systemctl stop.

    Returns (exit_code, stdout, stderr).
    """
    result = subprocess.run(
        ["sudo", "systemctl", "stop", service_name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout, result.stderr


def start_service(service_name: str) -> tuple[int, str, str]:
    """Start a systemd service using sudo systemctl start.

    Returns (exit_code, stdout, stderr).
    """
    result = subprocess.run(
        ["sudo", "systemctl", "start", service_name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout, result.stderr
