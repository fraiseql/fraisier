"""The rebuild's ``admin_url`` must be able to create what it drops.

``DROP DATABASE`` needs only ownership; ``CREATE DATABASE`` needs
``CREATEDB``. The live behaviour is pinned in
``tests/integration/test_rebuild_admin_url.py``; these pin the three answers.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from fraisier.dbops.operations import refuse_without_createdb
from fraisier.errors import DatabaseError

_URL = "postgresql://acme:s3cret@/postgres?host=/nonexistent/acme"


def _psql_says(code: int, stdout: str = "", stderr: str = ""):
    return patch(
        "fraisier.dbops.operations._pg_cmd", return_value=(code, stdout, stderr)
    )


def test_a_role_that_may_create_databases_passes():
    with _psql_says(0, "acme|t\n"):
        refuse_without_createdb(connection_url=_URL, db_name="acme_db")


def test_a_role_that_may_not_is_refused_by_name():
    with _psql_says(0, "acme|f\n"), pytest.raises(DatabaseError) as info:
        refuse_without_createdb(connection_url=_URL, db_name="acme_db")

    message = str(info.value)
    assert "role 'acme'" in message
    assert "ALTER ROLE acme CREATEDB" in message
    assert "Nothing was dropped" in message
    assert "s3cret" not in message


def test_a_check_that_cannot_connect_is_refused():
    with (
        _psql_says(2, stderr="psql: error: connection refused\n"),
        pytest.raises(DatabaseError, match="cannot check admin_url before dropping"),
    ):
        refuse_without_createdb(connection_url=_URL, db_name="acme_db")
