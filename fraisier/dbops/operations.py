"""Low-level PostgreSQL helpers.

All functions shell out via subprocess to psql/createdb/dropdb as the
postgres system user (sudo -u postgres).  This matches the production
deployment model where fraisier runs as a deploy user with sudo rights
to the postgres account.
"""

import subprocess

from fraisier.dbops._validation import validate_pg_identifier


def _pg_cmd(
    cmd: list[str],
    *,
    sudo_user: str = "postgres",
) -> tuple[int, str, str]:
    """Run a PostgreSQL CLI command as *sudo_user*.

    Returns (exit_code, stdout, stderr).
    """
    full_cmd = ["sudo", "-u", sudo_user, *cmd]
    result = subprocess.run(
        full_cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout, result.stderr


def run_psql(
    sql_or_file: str,
    *,
    db_name: str,
    sudo_user: str = "postgres",
) -> tuple[int, str, str]:
    """Execute a psql command against *db_name*.

    *sql_or_file* is passed via ``-c`` (inline SQL).
    """
    cmd = ["psql", "-d", db_name, "-c", sql_or_file]
    return _pg_cmd(cmd, sudo_user=sudo_user)


def run_sql(
    sql: str,
    *,
    db_name: str,
    sudo_user: str = "postgres",
) -> tuple[int, str, str]:
    """Execute inline SQL with tuples-only output (``-t -A``)."""
    cmd = ["psql", "-d", db_name, "-t", "-A", "-c", sql]
    return _pg_cmd(cmd, sudo_user=sudo_user)


def check_db_exists(
    db_name: str,
    *,
    sudo_user: str = "postgres",
) -> bool:
    """Return True if the database *db_name* exists."""
    validate_pg_identifier(db_name, "database name")
    code, stdout, _ = _pg_cmd(
        [
            "psql",
            "-t",
            "-A",
            "-v",
            f"dbname={db_name}",
            "-c",
            "SELECT count(*) FROM pg_database WHERE datname=:'dbname'",
        ],
        sudo_user=sudo_user,
    )
    if code != 0:
        return False
    return stdout.strip() == "1"


def terminate_backends(
    db_name: str,
    *,
    sudo_user: str = "postgres",
) -> tuple[int, str, str]:
    """Terminate all connections to *db_name*."""
    validate_pg_identifier(db_name, "database name")
    return _pg_cmd(
        [
            "psql",
            "-v",
            f"dbname={db_name}",
            "-c",
            "SELECT pg_terminate_backend(pid) "
            "FROM pg_stat_activity "
            "WHERE datname=:'dbname' AND pid <> pg_backend_pid()",
        ],
        sudo_user=sudo_user,
    )


def drop_db(
    db_name: str,
    *,
    force_disconnect: bool = False,
    sudo_user: str = "postgres",
) -> tuple[int, str, str]:
    """Drop database *db_name*.

    If *force_disconnect* is True, terminate all backends first.
    """
    validate_pg_identifier(db_name, "database name")
    if force_disconnect:
        terminate_backends(db_name, sudo_user=sudo_user)
    return _pg_cmd(["dropdb", db_name], sudo_user=sudo_user)


def create_db(
    db_name: str,
    *,
    template: str | None = None,
    owner: str | None = None,
    sudo_user: str = "postgres",
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
    return _pg_cmd(cmd, sudo_user=sudo_user)
