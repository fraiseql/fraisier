"""Template-based database reset for fast development cycles.

Uses PostgreSQL ``CREATE DATABASE ... WITH TEMPLATE`` for sub-second
database resets.  The workflow is:

1. Build the database (confiture build / alembic upgrade).
2. ``create_template`` to snapshot it as ``template_<db_name>``.
3. ``reset_from_template`` to drop + recreate from template in ~100 ms.
"""

from dataclasses import dataclass

from fraisier.dbops._validation import validate_pg_identifier
from fraisier.dbops.operations import (
    _pg_cmd,
    create_db,
    drop_db,
    terminate_backends,
)


@dataclass
class TemplateResult:
    """Result of a template operation."""

    success: bool
    template_name: str = ""
    error: str = ""


def create_template(
    db_name: str,
    *,
    prefix: str = "template_",
    sudo_user: str = "postgres",
    connection_url: str | None = None,
) -> TemplateResult:
    """Create a template database from *db_name*.

    Drops any existing template, then creates a new one via
    ``CREATE DATABASE template_<db_name> WITH TEMPLATE <db_name>``.
    """
    template_name = f"{prefix}{db_name}"

    # Drop existing template (ignore errors — may not exist)
    terminate_backends(
        template_name, sudo_user=sudo_user, connection_url=connection_url
    )
    drop_db(template_name, sudo_user=sudo_user, connection_url=connection_url)

    # Disconnect from source before templating
    terminate_backends(db_name, sudo_user=sudo_user, connection_url=connection_url)

    # Create new template
    code, _, stderr = create_db(
        template_name,
        template=db_name,
        sudo_user=sudo_user,
        connection_url=connection_url,
    )
    if code != 0:
        return TemplateResult(
            success=False,
            template_name=template_name,
            error=stderr.strip(),
        )

    return TemplateResult(success=True, template_name=template_name)


def reset_from_template(
    db_name: str,
    *,
    prefix: str = "template_",
    sudo_user: str = "postgres",
    connection_url: str | None = None,
) -> TemplateResult:
    """Reset *db_name* by dropping and recreating from its template."""
    template_name = f"{prefix}{db_name}"

    # Force-disconnect and drop the live database
    code, _, stderr = drop_db(
        db_name,
        force_disconnect=True,
        sudo_user=sudo_user,
        connection_url=connection_url,
    )
    if code != 0:
        return TemplateResult(
            success=False,
            template_name=template_name,
            error=stderr.strip(),
        )

    # Disconnect from template before using it
    terminate_backends(
        template_name, sudo_user=sudo_user, connection_url=connection_url
    )

    # Recreate from template
    code, _, stderr = create_db(
        db_name,
        template=template_name,
        sudo_user=sudo_user,
        connection_url=connection_url,
    )
    if code != 0:
        return TemplateResult(
            success=False,
            template_name=template_name,
            error=stderr.strip(),
        )

    return TemplateResult(success=True, template_name=template_name)


def cleanup_templates(
    db_name: str,
    *,
    prefix: str = "template_",
    max_templates: int = 3,
    sudo_user: str = "postgres",
    connection_url: str | None = None,
) -> int:
    """Remove old template databases, keeping at most *max_templates*.

    Template databases are named ``<prefix><db_name>_<N>`` or just
    ``<prefix><db_name>``.  This queries ``pg_database`` for matching
    names, orders by ``oid`` descending (newest first), and drops
    anything beyond *max_templates*.

    Returns the number of templates dropped.
    """
    validate_pg_identifier(db_name, "database name")
    validate_pg_identifier(prefix.rstrip("_") or "t", "template prefix")
    pattern = f"{prefix}{db_name}%"
    code, stdout, _ = _pg_cmd(
        [
            "psql",
            "-t",
            "-A",
            "-v",
            f"pattern={pattern}",
            "-c",
            "SELECT datname FROM pg_database "
            "WHERE datname LIKE :'pattern' "
            "ORDER BY oid DESC",
        ],
        sudo_user=sudo_user,
        connection_url=connection_url,
    )
    if code != 0:
        return 0

    templates = [line.strip() for line in stdout.strip().splitlines() if line.strip()]
    to_drop = templates[max_templates:]
    for tmpl in to_drop:
        terminate_backends(tmpl, sudo_user=sudo_user, connection_url=connection_url)
        drop_db(tmpl, sudo_user=sudo_user, connection_url=connection_url)

    return len(to_drop)
